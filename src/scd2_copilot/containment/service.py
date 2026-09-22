"""Containment service orchestrating suspicious batch quarantine, deduplication, and recovery."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal
import hashlib
import json
import logging
from typing import Any, Optional
from uuid import UUID, uuid4

import polars as pl
import psycopg


def _json_safe(val: Any) -> Any:
    """Recursively convert datetime, UUID, Decimal to JSON-serializable primitives."""
    if isinstance(val, dict):
        return {str(k): _json_safe(v) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return [_json_safe(v) for v in val]
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, UUID):
        return str(val)
    if isinstance(val, Decimal):
        return float(val)
    return val

from ..config import Settings, get_settings
from ..db.connection import DatabaseManager
from ..db.models import (
    HeldChangeBatchRow,
    HoldSeverity,
    HoldStatus,
    InventoryHistoryRow,
    InventorySourceRow,
    MonitoredEntityHistoryRow,
    ProcessingRunRow,
    RunStatus,
    _ensure_utc,
)
from ..db.repositories import (
    CheckpointRepository,
    HeldChangeBatchRepository,
    InventoryHistoryRepository,
    InventorySourceRepository,
    MonitoredEntityHistoryRepository,
    ProcessingRunRepository,
)
from ..detect_changes import detect_changes
from ..guardrail.engine import GuardrailEngine
from ..guardrail.models import GuardrailDecision, GuardrailSeverity
from ..models import DeletePolicy, SnapshotMode
from ..source import MonitorConfig, get_default_inventory_monitor_config
from ..transform_scd2 import apply_scd2
from ..validate import validate_scd2
from ..worker.batch import (
    MicroBatch,
    generic_history_rows_to_target_df,
    history_rows_to_target_df,
)
from .exceptions import (
    ContainmentError,
    DuplicateHoldError,
    HoldNotFoundError,
    HoldReplayError,
    InvalidHoldTransitionError,
)
from .models import HoldResolutionResult

logger = logging.getLogger("scd2_copilot.containment")


def _is_inventory_monitor(monitor_config: Optional[MonitorConfig]) -> bool:
    """Return True if monitor_config targets canonical inventory demo table."""
    if monitor_config is None:
        return True
    return monitor_config.source.table_name == "inventory_source"


def compute_batch_fingerprint(batch: MicroBatch, source_name: str) -> str:
    """Compute a deterministic hash for a batch slice to prevent duplicate holds.

    Fingerprint incorporates:
    - source_name
    - watermark_start (ISO or 'NONE')
    - watermark_end (ISO or 'NONE')
    - batch size
    - sorted list of business keys and timestamps
    """
    hasher = hashlib.sha256()
    hasher.update(source_name.encode("utf-8"))
    start_str = batch.watermark_start.isoformat() if batch.watermark_start else "NONE"
    end_str = batch.max_updated_at.isoformat() if batch.max_updated_at else "NONE"
    hasher.update(start_str.encode("utf-8"))
    hasher.update(end_str.encode("utf-8"))
    hasher.update(str(batch.size).encode("utf-8"))

    def _get(rec: Any, attr: str) -> Any:
        if isinstance(rec, dict):
            return rec.get(attr)
        return getattr(rec, attr, None)

    key_cols = getattr(batch, "key_columns", ["sku_id", "warehouse_id"])
    ts_col = getattr(batch, "timestamp_column", "updated_at")

    sorted_keys = []
    for r in batch.source_records:
        kt = tuple(str(_get(r, c) or "") for c in key_cols)
        ts_val = _get(r, ts_col)
        ts_str = ts_val.isoformat() if isinstance(ts_val, datetime) else (str(ts_val) if ts_val is not None else "")
        sorted_keys.append((kt, ts_str))
    sorted_keys.sort()

    for kt, ts in sorted_keys:
        hasher.update(f"{':'.join(kt)}:{ts}".encode("utf-8"))

    return hasher.hexdigest()


class ContainmentService:
    """Service managing suspicious batch containment, hold deduplication, and recovery.

    Core Invariants:
    1. A suspicious batch is never committed to inventory_history.
    2. Checkpoint watermark is preserved at T0 while batch is HELD.
    3. Frozen source records are preserved in evidence for lossless replay.
    4. Duplicate polling cycles reuse existing active holds instead of spamming.
    5. State transitions: HELD -> RELEASED | REPROCESSED | DISCARDED.
    6. All resolution operations are idempotent.
    7. inventory_source is NEVER mutated or deleted.
    """

    def __init__(
        self,
        db_manager: Optional[DatabaseManager] = None,
        settings: Optional[Settings] = None,
        hold_repo: Optional[HeldChangeBatchRepository] = None,
        run_repo: Optional[ProcessingRunRepository] = None,
        checkpoint_repo: Optional[CheckpointRepository] = None,
        history_repo: Optional[InventoryHistoryRepository] = None,
        generic_history_repo: Optional[MonitoredEntityHistoryRepository] = None,
        inventory_repo: Optional[InventorySourceRepository] = None,
        guardrail: Optional[GuardrailEngine] = None,
        source_name: Optional[str] = None,
        table_name: Optional[str] = None,
        monitor_config: Optional[MonitorConfig] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.db = db_manager or DatabaseManager(settings=self.settings)

        self.hold_repo = hold_repo or HeldChangeBatchRepository(db=self.db)
        self.run_repo = run_repo or ProcessingRunRepository(db=self.db)
        self.checkpoint_repo = checkpoint_repo or CheckpointRepository(db=self.db)
        self.history_repo = history_repo or InventoryHistoryRepository(db=self.db)
        self.generic_history_repo = (
            generic_history_repo or MonitoredEntityHistoryRepository(db=self.db)
        )
        self.inventory_repo = inventory_repo or InventorySourceRepository(db=self.db)
        self.guardrail = guardrail or GuardrailEngine(settings=self.settings)

        self.source_name = source_name or self.settings.ingestion_source_name
        self.table_name = table_name or self.settings.ingestion_table_name
        if monitor_config is not None:
            self.monitor_config = monitor_config
            self.business_key = monitor_config.business_keys
            self.tracked_columns = monitor_config.tracked_columns
            self.timestamp_column = monitor_config.change_timestamp.column
        else:
            self.monitor_config = get_default_inventory_monitor_config(settings=self.settings)
            self.business_key = ["sku_id", "warehouse_id"]
            self.tracked_columns = ["quantity_on_hand", "reorder_level", "status"]
            self.timestamp_column = "updated_at"

    def contain_suspicious_batch(
        self,
        batch: MicroBatch,
        guardrail_decision: GuardrailDecision,
        run_id: Optional[UUID] = None,
        source_name: Optional[str] = None,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> tuple[HeldChangeBatchRow, bool]:
        """Contain a suspicious batch by durably recording it in held_change_batch and processing_run.

        Returns:
            tuple[HeldChangeBatchRow, bool]: (hold_row, is_new)
            is_new is True if a new hold record was created,
            or False if an existing active hold for this batch slice was returned.
        """
        src_name = source_name or self.source_name
        fingerprint = compute_batch_fingerprint(batch, src_name)

        # Check for existing active hold with matching fingerprint to prevent duplicate storms
        active_holds = self.hold_repo.get_recent_holds(
            limit=20,
            status=HoldStatus.HELD.value,
            source_name=src_name,
            conn=conn,
        )
        for existing in active_holds:
            if existing.evidence and existing.evidence.get("batch_fingerprint") == fingerprint:
                logger.info(
                    "Identical active hold %s already exists for fingerprint %s; suppressing duplicate hold creation.",
                    existing.hold_id,
                    fingerprint,
                )
                return existing, False

        active_run_id = run_id or uuid4()

        # Freeze source records with ISO string timestamps for clean JSONB serialization
        frozen_records = []
        for r in batch.source_records:
            if isinstance(r, dict):
                rec_dict = {}
                for k, v in r.items():
                    if isinstance(v, datetime):
                        rec_dict[k] = v.isoformat()
                    elif isinstance(v, UUID):
                        rec_dict[k] = str(v)
                    else:
                        rec_dict[k] = v
                frozen_records.append(rec_dict)
            else:
                frozen_records.append(
                    {
                        "sku_id": r.sku_id,
                        "warehouse_id": r.warehouse_id,
                        "quantity_on_hand": r.quantity_on_hand,
                        "reorder_level": r.reorder_level,
                        "status": r.status,
                        "updated_at": r.updated_at.isoformat() if getattr(r, "updated_at", None) else None,
                    }
                )

        first_c_dict = (
            batch.first_cursor.to_dict()
            if getattr(batch, "first_cursor", None) and hasattr(batch.first_cursor, "to_dict")
            else None
        )
        last_c_dict = (
            batch.last_cursor.to_dict()
            if getattr(batch, "last_cursor", None) and hasattr(batch.last_cursor, "to_dict")
            else None
        )

        structured_evidence: dict[str, Any] = {
            "batch_fingerprint": fingerprint,
            "batch_id": str(batch.batch_id),
            "key_columns": list(getattr(batch, "key_columns", self.business_key)),
            "tracked_columns": list(getattr(batch, "tracked_columns", self.tracked_columns)),
            "timestamp_column": getattr(batch, "timestamp_column", self.timestamp_column),
            "watermark_start": (
                batch.watermark_start.isoformat() if batch.watermark_start else None
            ),
            "watermark_end": (
                batch.max_updated_at.isoformat() if batch.max_updated_at else None
            ),
            "first_cursor": first_c_dict,
            "last_cursor": last_c_dict,
            "guardrail_evidence": guardrail_decision.evidence.to_dict(),
            "triggered_rules": [r.to_dict() for r in guardrail_decision.triggered_rules],
            "reasons": list(guardrail_decision.reasons),
            "severity": guardrail_decision.severity.value,
            "batch_records": frozen_records,
        }
        structured_evidence = _json_safe(structured_evidence)

        sev_value = guardrail_decision.severity.value
        reason_summary = (
            "; ".join(guardrail_decision.reasons)
            or "Suspicious change patterns detected by guardrail engine"
        )

        def _persist(c: psycopg.Connection[Any]) -> HeldChangeBatchRow:
            existing_run = self.run_repo.get_run(active_run_id, conn=c)
            if not existing_run:
                self.run_repo.create_run(
                    source_name=src_name,
                    run_id=active_run_id,
                    status=RunStatus.HELD.value,
                    conn=c,
                )
                self.run_repo.mark_run_held(
                    run_id=active_run_id,
                    records_seen=batch.size,
                    records_held=batch.size,
                    conn=c,
                )
            else:
                self.run_repo.mark_run_held(
                    run_id=active_run_id,
                    records_seen=batch.size,
                    records_held=batch.size,
                    conn=c,
                )

            return self.hold_repo.create_hold(
                run_id=active_run_id,
                source_name=src_name,
                severity=sev_value,
                reason=reason_summary,
                records_affected=batch.size,
                evidence=structured_evidence,
                status=HoldStatus.HELD.value,
                conn=c,
            )

        if conn is not None:
            new_hold = _persist(conn)
        elif getattr(self.hold_repo, "in_memory_mode", False) is True:
            new_hold = _persist(None)
        else:
            with self.db.transaction() as c:
                new_hold = _persist(c)

        logger.warning(
            "Created durable hold %s for batch %s (run_id=%s, severity=%s, records=%d)",
            new_hold.hold_id,
            batch.batch_id,
            active_run_id,
            sev_value,
            batch.size,
        )
        return new_hold, True

    def get_held_batch(
        self,
        hold_id: UUID,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> HeldChangeBatchRow:
        """Fetch a specific held batch by its ID. Raises HoldNotFoundError if missing."""
        hold = self.hold_repo.get_hold(hold_id, conn=conn)
        if hold is None:
            raise HoldNotFoundError(f"Held change batch '{hold_id}' not found.", hold_id=hold_id)
        return hold

    def list_held_batches(
        self,
        status: Optional[str] = None,
        source_name: Optional[str] = None,
        limit: int = 50,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> list[HeldChangeBatchRow]:
        """List held change batches with optional status or source filters."""
        return self.hold_repo.get_recent_holds(
            limit=limit,
            status=status,
            source_name=source_name,
            conn=conn,
        )

    def _reconstruct_batch_from_hold(self, hold: HeldChangeBatchRow) -> MicroBatch:
        """Reconstruct the exact frozen MicroBatch from hold.evidence."""
        evidence = hold.evidence or {}
        records_data = evidence.get("batch_records")
        if not records_data or not isinstance(records_data, list):
            raise HoldReplayError(
                f"Cannot reconstruct batch for hold '{hold.hold_id}': 'batch_records' missing or invalid in evidence.",
                hold_id=hold.hold_id,
            )

        key_cols = evidence.get("key_columns") or self.business_key
        tracked_cols = evidence.get("tracked_columns") or self.tracked_columns
        ts_col = evidence.get("timestamp_column") or self.timestamp_column

        if _is_inventory_monitor(self.monitor_config):
            source_records: list[Any] = []
            for r_dict in records_data:
                try:
                    row = InventorySourceRow.from_dict(r_dict)
                    source_records.append(row)
                except Exception as exc:
                    raise HoldReplayError(
                        f"Failed to deserialize record in hold '{hold.hold_id}': {exc}",
                        hold_id=hold.hold_id,
                    ) from exc
        else:
            source_records = []
            for r in records_data:
                if isinstance(r, dict):
                    rec = dict(r)
                    if ts_col in rec and isinstance(rec[ts_col], str):
                        try:
                            rec[ts_col] = datetime.fromisoformat(rec[ts_col])
                        except (ValueError, TypeError):
                            pass
                    source_records.append(rec)
                else:
                    source_records.append(r)

        w_start_raw = evidence.get("watermark_start")
        w_start = datetime.fromisoformat(w_start_raw) if w_start_raw else None
        w_end_raw = evidence.get("watermark_end")
        w_end = datetime.fromisoformat(w_end_raw) if w_end_raw else None

        from ..source.models import SourceCursor

        first_c = None
        last_c = None
        if "first_cursor" in evidence and evidence["first_cursor"]:
            try:
                first_c = SourceCursor.from_dict(evidence["first_cursor"])
            except Exception:
                first_c = None
        if "last_cursor" in evidence and evidence["last_cursor"]:
            try:
                last_c = SourceCursor.from_dict(evidence["last_cursor"])
            except Exception:
                last_c = None

        return MicroBatch(
            source_records=source_records,
            watermark_start=w_start,
            watermark_end=w_end,
            key_columns=key_cols,
            tracked_columns=tracked_cols,
            timestamp_column=ts_col,
            source_name=hold.source_name,
            first_cursor=first_c,
            last_cursor=last_c,
        )

    def release_held_batch(
        self,
        hold_id: UUID,
        operator_reason: Optional[str] = None,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> HoldResolutionResult:
        """Release a held batch by overriding the guardrail and committing changes downstream.

        Idempotency:
        - If the hold is already RELEASED, returns an idempotent success result immediately.

        State transition rules:
        - Only HELD -> RELEASED is permitted.
        - Any attempt to release from DISCARDED or REPROCESSED raises InvalidHoldTransitionError.
        """
        hold = self.get_held_batch(hold_id, conn=conn)

        if hold.status == HoldStatus.RELEASED.value:
            logger.info("Hold %s is already RELEASED; returning idempotent result.", hold_id)
            return HoldResolutionResult(
                hold_id=hold_id,
                status=HoldStatus.RELEASED.value,
                success=True,
                message="Hold is already released (idempotent call).",
                resolved_at=hold.resolved_at,
                records_affected=hold.records_affected,
                is_idempotent=True,
            )

        if hold.status != HoldStatus.HELD.value:
            raise InvalidHoldTransitionError(
                f"Cannot release hold '{hold_id}' with current status '{hold.status}'. Only HELD batches can be released.",
                hold_id=hold_id,
                current_status=hold.status,
                target_status=HoldStatus.RELEASED.value,
            )

        batch = self._reconstruct_batch_from_hold(hold)

        def _execute_release(c: psycopg.Connection[Any]) -> HoldResolutionResult:
            if _is_inventory_monitor(self.monitor_config):
                current_inventory_history = self.history_repo.fetch_current_history_for_keys(
                    batch.business_keys,
                    conn=c,
                )
                target_df = history_rows_to_target_df(current_inventory_history)
            else:
                entity_keys = [
                    {k: v for k, v in zip(self.business_key, kt)}
                    for kt in batch.business_keys
                ]
                current_entity_history = self.generic_history_repo.fetch_current_history_for_keys(
                    source_name=hold.source_name,
                    entity_keys=entity_keys,
                    conn=c,
                )
                target_df = generic_history_rows_to_target_df(
                    current_entity_history,
                    key_columns=self.business_key,
                    tracked_columns=self.tracked_columns,
                )
            source_df = batch.to_source_df()

            batch_max_ts = batch.max_updated_at or datetime.now(timezone.utc)
            if isinstance(batch_max_ts, str):
                batch_max_ts = datetime.fromisoformat(batch_max_ts)
            processing_date = batch_max_ts.date()

            change_report = detect_changes(
                source_df=source_df,
                target_df=target_df,
                business_key=self.business_key,
                tracked_columns=self.tracked_columns,
                processing_date=processing_date,
                snapshot_mode=SnapshotMode.INCREMENTAL,
                delete_policy=DeletePolicy.IGNORE,
            )

            scd2_df = apply_scd2(
                source_df=source_df,
                target_df=target_df,
                change_report=change_report,
                business_key=self.business_key,
                tracked_columns=self.tracked_columns,
                processing_date=processing_date,
                delete_policy=DeletePolicy.IGNORE,
            )

            # Invariant check: release overrides guardrail, but SCD2 invariants MUST pass
            validation_report = validate_scd2(scd2_df, business_key=self.business_key)
            if not validation_report.passed:
                failures = [r.message for r in validation_report.rules if r.status.value == "fail"]
                raise HoldReplayError(
                    f"Cannot release hold '{hold_id}': SCD2 invariant validation failed: {'; '.join(failures)}",
                    hold_id=hold_id,
                )

            # 1. Close prior active rows for changed keys
            if change_report.changed:
                closed_at = datetime.combine(processing_date, time.min, tzinfo=timezone.utc)
                if _is_inventory_monitor(self.monitor_config):
                    changed_keys = [
                        (
                            str(r.business_key_values["sku_id"]),
                            str(r.business_key_values["warehouse_id"]),
                        )
                        for r in change_report.changed
                    ]
                    self.history_repo.close_current_versions(
                        keys=changed_keys,
                        closed_at=closed_at,
                        conn=c,
                    )
                else:
                    entity_keys = [
                        {k: r.business_key_values[k] for k in self.business_key}
                        for r in change_report.changed
                    ]
                    self.generic_history_repo.close_current_versions(
                        source_name=hold.source_name,
                        entity_keys=entity_keys,
                        closed_at=closed_at,
                        conn=c,
                    )

            # 2. Insert new active rows
            new_active_df = scd2_df.filter(
                (pl.col("is_current") == True) & (pl.col("effective_from") == processing_date)  # noqa: E712
            )
            inserted_count = 0
            if not new_active_df.is_empty():
                now_utc = datetime.now(timezone.utc)
                eff_from_dt = datetime.combine(processing_date, time.min, tzinfo=timezone.utc)
                if _is_inventory_monitor(self.monitor_config):
                    inventory_rows_to_insert = [
                        InventoryHistoryRow(
                            sku_id=str(row["sku_id"]),
                            warehouse_id=str(row["warehouse_id"]),
                            quantity_on_hand=int(row["quantity_on_hand"]),
                            reorder_level=int(row["reorder_level"]),
                            status=str(row["status"]),
                            effective_from=eff_from_dt,
                            effective_to=None,
                            is_current=True,
                            created_at=now_utc,
                        )
                        for row in new_active_df.iter_rows(named=True)
                    ]
                    self.history_repo.insert_history_rows(inventory_rows_to_insert, conn=c)
                    inserted_count = len(inventory_rows_to_insert)
                else:
                    generic_rows_to_insert = [
                        MonitoredEntityHistoryRow(
                            source_name=hold.source_name,
                            entity_key={k: row[k] for k in self.business_key},
                            attributes={k: row[k] for k in self.tracked_columns},
                            effective_from=eff_from_dt,
                            effective_to=None,
                            is_current=True,
                            created_at=now_utc,
                        )
                        for row in new_active_df.iter_rows(named=True)
                    ]
                    self.generic_history_repo.insert_history_rows(generic_rows_to_insert, conn=c)
                    inserted_count = len(generic_rows_to_insert)

            # 3. Update processing run to COMMITTED
            records_changed_count = len(change_report.changed)
            self.run_repo.mark_run_committed(
                run_id=hold.run_id,
                records_seen=batch.size,
                records_changed=records_changed_count,
                records_held=0,
                conn=c,
            )

            # 4. Advance stream watermark to batch max_updated_at and cursor_keys
            watermark_after = batch.max_updated_at
            cursor_keys_after = (
                batch.last_cursor.keys if getattr(batch, "last_cursor", None) else None
            )
            if watermark_after:
                self.checkpoint_repo.update_checkpoint(
                    source_name=hold.source_name,
                    watermark=watermark_after,
                    cursor_keys=cursor_keys_after,
                    run_id=hold.run_id,
                    table_name=self.table_name,
                    conn=c,
                )

            # 5. Update hold status to RELEASED
            now_res = datetime.now(timezone.utc)
            self.hold_repo.release_hold(hold_id=hold_id, resolved_at=now_res, conn=c)

            msg = f"Hold released successfully by operator. {inserted_count} historical rows inserted."
            if operator_reason:
                msg += f" Operator reason: {operator_reason}"

            return HoldResolutionResult(
                hold_id=hold_id,
                status=HoldStatus.RELEASED.value,
                success=True,
                message=msg,
                resolved_at=now_res,
                records_affected=batch.size,
                checkpoint_advanced_to=watermark_after,
                checkpoint_cursor_keys=cursor_keys_after,
                is_idempotent=False,
            )

        if conn is not None:
            return _execute_release(conn)
        elif getattr(self.hold_repo, "in_memory_mode", False) is True:
            return _execute_release(None)
        else:
            with self.db.transaction() as c:
                return _execute_release(c)

    def reprocess_held_batch(
        self,
        hold_id: UUID,
        force_normal: bool = False,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> HoldResolutionResult:
        """Reprocess a held batch by re-evaluating SCD2 transformation, validation, and guardrail rules.

        Idempotency:
        - If the hold is already REPROCESSED, returns an idempotent success result immediately.

        State transition rules:
        - Only HELD -> REPROCESSED is permitted.
        - Any attempt to reprocess from DISCARDED or RELEASED raises InvalidHoldTransitionError.

        Behavior:
        - If re-evaluation is NORMAL (or force_normal is True), commits changes, advances checkpoint,
          and marks hold as REPROCESSED.
        - If re-evaluation is still SUSPICIOUS, retains hold in HELD status and returns success=False.
        """
        hold = self.get_held_batch(hold_id, conn=conn)

        if hold.status == HoldStatus.REPROCESSED.value:
            logger.info("Hold %s is already REPROCESSED; returning idempotent result.", hold_id)
            return HoldResolutionResult(
                hold_id=hold_id,
                status=HoldStatus.REPROCESSED.value,
                success=True,
                message="Hold is already reprocessed (idempotent call).",
                resolved_at=hold.resolved_at,
                records_affected=hold.records_affected,
                is_idempotent=True,
            )

        if hold.status != HoldStatus.HELD.value:
            raise InvalidHoldTransitionError(
                f"Cannot reprocess hold '{hold_id}' with current status '{hold.status}'. Only HELD batches can be reprocessed.",
                hold_id=hold_id,
                current_status=hold.status,
                target_status=HoldStatus.REPROCESSED.value,
            )

        batch = self._reconstruct_batch_from_hold(hold)

        def _execute_reprocess(c: psycopg.Connection[Any]) -> HoldResolutionResult:
            if _is_inventory_monitor(self.monitor_config):
                current_inventory_history = self.history_repo.fetch_current_history_for_keys(
                    batch.business_keys,
                    conn=c,
                )
                target_df = history_rows_to_target_df(current_inventory_history)
            else:
                entity_keys = [
                    {k: v for k, v in zip(self.business_key, kt)}
                    for kt in batch.business_keys
                ]
                current_entity_history = self.generic_history_repo.fetch_current_history_for_keys(
                    source_name=hold.source_name,
                    entity_keys=entity_keys,
                    conn=c,
                )
                target_df = generic_history_rows_to_target_df(
                    current_entity_history,
                    key_columns=self.business_key,
                    tracked_columns=self.tracked_columns,
                )
            source_df = batch.to_source_df()

            batch_max_ts = batch.max_updated_at or datetime.now(timezone.utc)
            if isinstance(batch_max_ts, str):
                batch_max_ts = datetime.fromisoformat(batch_max_ts)
            processing_date = batch_max_ts.date()

            change_report = detect_changes(
                source_df=source_df,
                target_df=target_df,
                business_key=self.business_key,
                tracked_columns=self.tracked_columns,
                processing_date=processing_date,
                snapshot_mode=SnapshotMode.INCREMENTAL,
                delete_policy=DeletePolicy.IGNORE,
            )

            scd2_df = apply_scd2(
                source_df=source_df,
                target_df=target_df,
                change_report=change_report,
                business_key=self.business_key,
                tracked_columns=self.tracked_columns,
                processing_date=processing_date,
                delete_policy=DeletePolicy.IGNORE,
            )

            validation_report = validate_scd2(scd2_df, business_key=self.business_key)
            if not validation_report.passed:
                failures = [r.message for r in validation_report.rules if r.status.value == "fail"]
                raise HoldReplayError(
                    f"Reprocess failed: SCD2 invariant validation failed: {'; '.join(failures)}",
                    hold_id=hold_id,
                )

            guardrail_decision = self.guardrail.evaluate(
                batch=batch,
                change_report=change_report,
                validation_report=validation_report,
                run_id=hold.run_id,
                monitor_config=self.monitor_config,
            )

            # If still suspicious and not forced: do not commit, retain HELD
            if guardrail_decision.is_suspicious and not force_normal:
                logger.warning(
                    "Reprocess for hold %s evaluated as SUSPICIOUS again: %s. Retaining HELD status.",
                    hold_id,
                    guardrail_decision.reasons,
                )
                return HoldResolutionResult(
                    hold_id=hold_id,
                    status=HoldStatus.HELD.value,
                    success=False,
                    message=f"Batch re-evaluated as SUSPICIOUS: {'; '.join(guardrail_decision.reasons)}",
                    records_affected=batch.size,
                    is_idempotent=False,
                )

            # Normal or forced: Commit changes downstream
            if change_report.changed:
                closed_at = datetime.combine(processing_date, time.min, tzinfo=timezone.utc)
                if _is_inventory_monitor(self.monitor_config):
                    changed_keys = [
                        (
                            str(r.business_key_values["sku_id"]),
                            str(r.business_key_values["warehouse_id"]),
                        )
                        for r in change_report.changed
                    ]
                    self.history_repo.close_current_versions(
                        keys=changed_keys,
                        closed_at=closed_at,
                        conn=c,
                    )
                else:
                    entity_keys = [
                        {k: r.business_key_values[k] for k in self.business_key}
                        for r in change_report.changed
                    ]
                    self.generic_history_repo.close_current_versions(
                        source_name=hold.source_name,
                        entity_keys=entity_keys,
                        closed_at=closed_at,
                        conn=c,
                    )

            new_active_df = scd2_df.filter(
                (pl.col("is_current") == True) & (pl.col("effective_from") == processing_date)  # noqa: E712
            )
            inserted_count = 0
            if not new_active_df.is_empty():
                now_utc = datetime.now(timezone.utc)
                eff_from_dt = datetime.combine(processing_date, time.min, tzinfo=timezone.utc)
                if _is_inventory_monitor(self.monitor_config):
                    inventory_rows_to_insert = [
                        InventoryHistoryRow(
                            sku_id=str(row["sku_id"]),
                            warehouse_id=str(row["warehouse_id"]),
                            quantity_on_hand=int(row["quantity_on_hand"]),
                            reorder_level=int(row["reorder_level"]),
                            status=str(row["status"]),
                            effective_from=eff_from_dt,
                            effective_to=None,
                            is_current=True,
                            created_at=now_utc,
                        )
                        for row in new_active_df.iter_rows(named=True)
                    ]
                    self.history_repo.insert_history_rows(inventory_rows_to_insert, conn=c)
                    inserted_count = len(inventory_rows_to_insert)
                else:
                    generic_rows_to_insert = [
                        MonitoredEntityHistoryRow(
                            source_name=hold.source_name,
                            entity_key={k: row[k] for k in self.business_key},
                            attributes={k: row[k] for k in self.tracked_columns},
                            effective_from=eff_from_dt,
                            effective_to=None,
                            is_current=True,
                            created_at=now_utc,
                        )
                        for row in new_active_df.iter_rows(named=True)
                    ]
                    self.generic_history_repo.insert_history_rows(generic_rows_to_insert, conn=c)
                    inserted_count = len(generic_rows_to_insert)

            records_changed_count = len(change_report.changed)
            self.run_repo.mark_run_committed(
                run_id=hold.run_id,
                records_seen=batch.size,
                records_changed=records_changed_count,
                records_held=0,
                conn=c,
            )

            watermark_after = batch.max_updated_at
            cursor_keys_after = (
                batch.last_cursor.keys if getattr(batch, "last_cursor", None) else None
            )
            if watermark_after:
                self.checkpoint_repo.update_checkpoint(
                    source_name=hold.source_name,
                    watermark=watermark_after,
                    cursor_keys=cursor_keys_after,
                    run_id=hold.run_id,
                    table_name=self.table_name,
                    conn=c,
                )

            now_res = datetime.now(timezone.utc)
            self.hold_repo.reprocess_hold(hold_id=hold_id, resolved_at=now_res, conn=c)

            return HoldResolutionResult(
                hold_id=hold_id,
                status=HoldStatus.REPROCESSED.value,
                success=True,
                message=f"Hold reprocessed and committed successfully ({inserted_count} historical rows).",
                resolved_at=now_res,
                records_affected=batch.size,
                checkpoint_advanced_to=watermark_after,
                checkpoint_cursor_keys=cursor_keys_after,
                is_idempotent=False,
            )

        if conn is not None:
            return _execute_reprocess(conn)
        elif getattr(self.hold_repo, "in_memory_mode", False) is True:
            return _execute_reprocess(None)
        else:
            with self.db.transaction() as c:
                return _execute_reprocess(c)

    def discard_held_batch(
        self,
        hold_id: UUID,
        operator_reason: Optional[str] = None,
        advance_checkpoint: bool = True,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> HoldResolutionResult:
        """Discard a held batch (permanent quarantine rejection).

        Idempotency:
        - If the hold is already DISCARDED, returns an idempotent success result immediately.

        State transition rules:
        - Only HELD -> DISCARDED is permitted.
        - Any attempt to discard from RELEASED or REPROCESSED raises InvalidHoldTransitionError.

        Behavior:
        - Updates hold status to DISCARDED.
        - inventory_history is NOT modified (0 rows added, 0 rows closed).
        - If advance_checkpoint=True, advances checkpoint to batch.max_updated_at to unblock
          the stream from re-reading this discarded slice.
        - inventory_source remains completely untouched.
        """
        hold = self.get_held_batch(hold_id, conn=conn)

        if hold.status == HoldStatus.DISCARDED.value:
            logger.info("Hold %s is already DISCARDED; returning idempotent result.", hold_id)
            return HoldResolutionResult(
                hold_id=hold_id,
                status=HoldStatus.DISCARDED.value,
                success=True,
                message="Hold is already discarded (idempotent call).",
                resolved_at=hold.resolved_at,
                records_affected=hold.records_affected,
                is_idempotent=True,
            )

        if hold.status != HoldStatus.HELD.value:
            raise InvalidHoldTransitionError(
                f"Cannot discard hold '{hold_id}' with current status '{hold.status}'. Only HELD batches can be discarded.",
                hold_id=hold_id,
                current_status=hold.status,
                target_status=HoldStatus.DISCARDED.value,
            )

        def _execute_discard(c: psycopg.Connection[Any]) -> HoldResolutionResult:
            now_res = datetime.now(timezone.utc)
            self.hold_repo.discard_hold(hold_id=hold_id, resolved_at=now_res, conn=c)

            checkpoint_after: Optional[datetime] = None
            cursor_keys_after: Optional[dict[str, Any]] = None
            if advance_checkpoint:
                # Retrieve cursor from evidence or reconstruct
                if hold.evidence and "last_cursor" in hold.evidence and hold.evidence["last_cursor"]:
                    last_c_dict = hold.evidence["last_cursor"]
                    if isinstance(last_c_dict, dict):
                        if "timestamp" in last_c_dict and last_c_dict["timestamp"]:
                            try:
                                checkpoint_after = datetime.fromisoformat(last_c_dict["timestamp"])
                            except Exception:
                                checkpoint_after = None
                        cursor_keys_after = last_c_dict.get("keys")

                if checkpoint_after is None:
                    w_end_raw = hold.evidence.get("watermark_end") if hold.evidence else None
                    if w_end_raw:
                        try:
                            checkpoint_after = datetime.fromisoformat(w_end_raw)
                        except Exception:
                            checkpoint_after = None
                    else:
                        try:
                            batch = self._reconstruct_batch_from_hold(hold)
                            checkpoint_after = batch.max_updated_at
                            if getattr(batch, "last_cursor", None):
                                cursor_keys_after = batch.last_cursor.keys
                        except Exception:
                            checkpoint_after = None

                if checkpoint_after:
                    self.checkpoint_repo.update_checkpoint(
                        source_name=hold.source_name,
                        watermark=checkpoint_after,
                        cursor_keys=cursor_keys_after,
                        run_id=hold.run_id,
                        table_name=self.table_name,
                        conn=c,
                    )

            msg = "Hold discarded by operator."
            if operator_reason:
                msg += f" Operator reason: {operator_reason}"

            return HoldResolutionResult(
                hold_id=hold_id,
                status=HoldStatus.DISCARDED.value,
                success=True,
                message=msg,
                resolved_at=now_res,
                records_affected=hold.records_affected,
                checkpoint_advanced_to=checkpoint_after,
                checkpoint_cursor_keys=cursor_keys_after,
                is_idempotent=False,
            )

        if conn is not None:
            return _execute_discard(conn)
        elif getattr(self.hold_repo, "in_memory_mode", False) is True:
            return _execute_discard(None)
        else:
            with self.db.transaction() as c:
                return _execute_discard(c)
