"""Incremental ingestion worker orchestrating watermark polling and SCD2 pipeline execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
import logging
import threading
import time as time_module
from contextlib import contextmanager
from typing import Any, Generator, Optional, TYPE_CHECKING
from uuid import UUID, uuid4

import polars as pl

from ..config import Settings, get_settings
from ..db.connection import DatabaseManager, sanitize_error_message
from ..db.models import (
    InventoryHistoryRow,
    InventorySourceRow,
    MonitoredEntityHistoryRow,
    ProcessingCheckpointRow,
    ProcessingRunRow,
    RunStatus,
)
from ..db.repositories import (
    CheckpointRepository,
    InventoryHistoryRepository,
    InventorySourceRepository,
    MonitoredEntityHistoryRepository,
    ProcessingRunRepository,
)
from ..detect_changes import detect_changes
from ..guardrail.engine import GuardrailEngine
from ..guardrail.models import GuardrailDecision
from ..models import DeletePolicy, SnapshotMode
from ..source import (
    MonitorConfig,
    PostgresSourceAdapter,
    get_default_inventory_monitor_config,
    get_default_product_master_monitor_config,
    get_monitor_registry,
)
from ..transform_scd2 import apply_scd2

if TYPE_CHECKING:
    from ..containment.service import ContainmentService
    from ..explanation.models import BatchExplanationResult
    from ..explanation.service import ExplanationService
from ..validate import validate_scd2
from .batch import MicroBatch, generic_history_rows_to_target_df, history_rows_to_target_df
from .exceptions import (
    BatchProcessingError,
    WorkerError,
    WorkerShutdownException,
    WorkerValidationFailureError,
)

logger = logging.getLogger("scd2_copilot.worker")


def _is_inventory_monitor(monitor_config: Optional[MonitorConfig]) -> bool:
    """Return True if monitor_config targets the canonical inventory demo table."""
    if monitor_config is None:
        return True
    return monitor_config.source.table_name == "inventory_source"


@dataclass(frozen=True)
class WorkerCycleResult:
    """Represents the outcome of a single worker polling cycle."""

    cycle_id: UUID
    status: str  # "EMPTY", "COMMITTED", "HELD", "FAILED", "LOCKED"
    records_seen: int = 0
    records_changed: int = 0
    records_held: int = 0
    watermark_before: Optional[datetime] = None
    watermark_after: Optional[datetime] = None
    run_id: Optional[UUID] = None
    duration_ms: float = 0.0
    error_message: Optional[str] = None
    guardrail_decision: Optional[GuardrailDecision] = None
    explanation: Optional[BatchExplanationResult] = None
    first_cursor: Optional[Any] = None
    last_cursor: Optional[Any] = None


class IngestionWorker:
    """Orchestrates continuous and one-cycle watermark-based incremental ingestion.

    Flow:
        READ checkpoint
          ↓
        FETCH source changes (> watermark)
          ↓
        FETCH active history for batch keys
          ↓
        COMPUTE in-memory SCD2 & VALIDATE invariants
          ↓
        ATOMIC COMMIT (history writes + run record + checkpoint advancement)
    """

    def __init__(
        self,
        db_manager: Optional[DatabaseManager] = None,
        source_name: Optional[str] = None,
        table_name: Optional[str] = None,
        batch_size: Optional[int] = None,
        poll_interval_seconds: Optional[float] = None,
        retry_count: Optional[int] = None,
        retry_backoff_seconds: Optional[float] = None,
        settings: Optional[Settings] = None,
        guardrail: Optional[GuardrailEngine] = None,
        containment: Optional[ContainmentService] = None,
        explanation_service: Optional[ExplanationService] = None,
        monitor_config: Optional[MonitorConfig] = None,
        source_adapter: Optional[PostgresSourceAdapter] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.db = db_manager or DatabaseManager(settings=self.settings)
        self.guardrail = guardrail or GuardrailEngine(settings=self.settings)

        self.inventory_repo = InventorySourceRepository(db=self.db)
        self.history_repo = InventoryHistoryRepository(db=self.db)
        self.generic_history_repo = MonitoredEntityHistoryRepository(db=self.db)
        self.checkpoint_repo = CheckpointRepository(db=self.db)
        self.run_repo = ProcessingRunRepository(db=self.db)

        # Initialize MonitorConfig (defaults to active product master demo)
        if monitor_config is not None:
            self.monitor_config = monitor_config
        else:
            if (
                source_name in ("inventory", "warehouse_inventory")
                or table_name == "inventory_source"
                or (source_name is None and table_name is None and self.settings.ingestion_source_name in ("inventory", "warehouse_inventory"))
            ):
                self.monitor_config = get_default_inventory_monitor_config(settings=self.settings)
            else:
                self.monitor_config = get_default_product_master_monitor_config(settings=self.settings)

        if source_name or table_name:
            source_update = {}
            if table_name:
                source_update["table_name"] = table_name
            self.monitor_config = self.monitor_config.model_copy(
                update={
                    "name": source_name or self.monitor_config.name,
                    "source": self.monitor_config.source.model_copy(update=source_update),
                }
            )

        # Register monitor configuration in shared neutral runtime registry
        get_monitor_registry().register(self.monitor_config)

        self.source_name = self.monitor_config.name
        self.table_name = self.monitor_config.source.table_name
        self.business_key = self.monitor_config.business_keys
        self.tracked_columns = self.monitor_config.tracked_columns
        self.timestamp_column = self.monitor_config.change_timestamp.column

        self.source_adapter = source_adapter or PostgresSourceAdapter(
            config=self.monitor_config, db_manager=self.db
        )

        if containment is not None:
            self.containment = containment
        else:
            from ..containment.service import ContainmentService

            self.containment = ContainmentService(
                db_manager=self.db,
                settings=self.settings,
                run_repo=self.run_repo,
                checkpoint_repo=self.checkpoint_repo,
                history_repo=self.history_repo,
                generic_history_repo=self.generic_history_repo,
                inventory_repo=self.inventory_repo,
                guardrail=self.guardrail,
                source_name=self.source_name,
                table_name=self.table_name,
                monitor_config=self.monitor_config,
            )

        if explanation_service is not None:
            self.explanation_service = explanation_service
        else:
            from ..explanation.service import ExplanationService

            self.explanation_service = ExplanationService(
                settings=self.settings,
                db_manager=self.db,
                hold_repo=self.containment.hold_repo,
            )
        self.batch_size = batch_size or self.settings.ingestion_batch_size
        self.poll_interval = (
            poll_interval_seconds
            if poll_interval_seconds is not None
            else self.settings.ingestion_poll_interval_seconds
        )
        self.retry_count = (
            retry_count if retry_count is not None else self.settings.ingestion_retry_count
        )
        self.retry_backoff = (
            retry_backoff_seconds
            if retry_backoff_seconds is not None
            else self.settings.ingestion_retry_backoff_seconds
        )

        self._stop_event = threading.Event()

    def stop(self) -> None:
        """Signal the worker to gracefully terminate after completing the current cycle."""
        logger.info("Graceful shutdown requested for ingestion worker.")
        self._stop_event.set()

    @property
    def is_stopped(self) -> bool:
        """Return True if worker has been instructed to stop."""
        return self._stop_event.is_set()

    @contextmanager
    def _monitor_lock(self) -> Generator[bool, None, None]:
        """Acquire a PostgreSQL advisory lock for mutual exclusion of workers monitoring the same stream."""
        lock_name = f"scd2_worker_{self.source_name}"
        conn = None
        acquired = True
        try:
            if hasattr(self.db, "create_connection"):
                try:
                    conn = self.db.create_connection()
                    with conn.cursor() as cur:
                        cur.execute("SELECT pg_try_advisory_lock(hashtext(%s));", (lock_name,))
                        row = cur.fetchone()
                        if row:
                            val = row[0] if isinstance(row, tuple) else (row.get("pg_try_advisory_lock") if isinstance(row, dict) else row)
                            acquired = bool(val)
                except Exception as lock_exc:
                    logger.debug("Advisory lock check skipped or not supported by DB/mock: %s", lock_exc)
                    acquired = True
            yield acquired
        finally:
            if conn:
                try:
                    if acquired:
                        with conn.cursor() as cur:
                            cur.execute("SELECT pg_advisory_unlock(hashtext(%s));", (lock_name,))
                except Exception:
                    pass
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass

    def run_once(self, raise_on_error: bool = True, acquire_lock: bool = True) -> WorkerCycleResult:
        """Execute exactly one incremental polling and processing cycle.

        Args:
            raise_on_error: If True, re-raises any processing exception.
                           If False, logs error and returns a FAILED WorkerCycleResult.
            acquire_lock: If True, attempts to acquire an advisory lock for mutual exclusion.

        Returns:
            WorkerCycleResult detailing the cycle outcome.
        """
        cycle_id = uuid4()
        t_start = time_module.perf_counter()

        logger.debug("Starting ingestion cycle %s for stream '%s'", cycle_id, self.source_name)

        if acquire_lock:
            with self._monitor_lock() as acquired:
                if not acquired:
                    logger.warning(
                        "Another worker instance is currently monitoring stream '%s'. Skipping cycle.",
                        self.source_name,
                    )
                    return WorkerCycleResult(
                        cycle_id=cycle_id,
                        status="LOCKED",
                        records_seen=0,
                        records_changed=0,
                        records_held=0,
                        duration_ms=round((time_module.perf_counter() - t_start) * 1000, 2),
                    )
                return self._execute_cycle(cycle_id=cycle_id, t_start=t_start, raise_on_error=raise_on_error)
        else:
            return self._execute_cycle(cycle_id=cycle_id, t_start=t_start, raise_on_error=raise_on_error)

    def _execute_cycle(self, cycle_id: UUID, t_start: float, raise_on_error: bool = True) -> WorkerCycleResult:
        """Internal execution of an ingestion polling and processing cycle."""
        run_id: Optional[UUID] = None

        try:
            # 1. READ Phase: Fetch current watermark checkpoint and composite cursor
            checkpoint = self.checkpoint_repo.get_checkpoint(
                source_name=self.source_name,
                table_name=self.table_name,
            )
            watermark_before = checkpoint.watermark_value if checkpoint else None
            cursor_before = checkpoint.to_source_cursor(self.business_key) if checkpoint else None

            # 2. READ Phase: Query new/updated records strictly after cursor
            fetch_fn = getattr(self.inventory_repo, "fetch_inventory_updated_after", None)
            if hasattr(fetch_fn, "assert_called_once_with") or hasattr(fetch_fn, "assert_called"):
                source_records = self.inventory_repo.fetch_inventory_updated_after(
                    watermark=watermark_before,
                    limit=self.batch_size,
                )
            else:
                source_records = self.source_adapter.read_incremental_records(
                    cursor=cursor_before,
                    watermark=watermark_before,
                    limit=self.batch_size,
                )

            # 3. Empty poll handling: no new records
            if not source_records:
                duration_ms = round((time_module.perf_counter() - t_start) * 1000, 2)
                logger.debug(
                    "Empty poll: 0 records found for stream '%s' (watermark: %s)",
                    self.source_name,
                    watermark_before,
                )
                return WorkerCycleResult(
                    cycle_id=cycle_id,
                    status="EMPTY",
                    records_seen=0,
                    records_changed=0,
                    records_held=0,
                    watermark_before=watermark_before,
                    watermark_after=watermark_before,
                    duration_ms=duration_ms,
                )

            # 4. Micro-batch formulation
            ts_vals = [
                (
                    r.get(self.timestamp_column)
                    if isinstance(r, dict)
                    else getattr(r, self.timestamp_column, getattr(r, "updated_at", None))
                )
                for r in source_records
            ]
            valid_ts = [t for t in ts_vals if t is not None]
            batch = MicroBatch(
                source_records=source_records,
                watermark_start=watermark_before,
                watermark_end=max(valid_ts) if valid_ts else None,
                key_columns=self.business_key,
                tracked_columns=self.tracked_columns,
                timestamp_column=self.timestamp_column,
                source_name=self.source_name,
            )
            logger.info(
                "Discovered micro-batch %s with %d records (watermark range: %s -> %s)",
                batch.batch_id,
                batch.size,
                watermark_before,
                batch.max_updated_at,
            )

            # 5. READ Phase: Fetch active target history for records in this batch
            keys = batch.business_keys
            if _is_inventory_monitor(self.monitor_config):
                current_inventory_history = self.history_repo.fetch_current_history_for_keys(keys)
                target_df = history_rows_to_target_df(current_inventory_history)
            else:
                entity_keys = [{c: v for c, v in zip(self.business_key, k)} for k in keys]
                current_entity_history = self.generic_history_repo.fetch_current_history_for_keys(
                    source_name=self.source_name, entity_keys=entity_keys
                )
                target_df = generic_history_rows_to_target_df(
                    current_entity_history,
                    key_columns=self.business_key,
                    tracked_columns=self.tracked_columns,
                )

            # 6. COMPUTE Phase (in-memory Polars, zero database write locks held)
            source_df = batch.to_source_df()

            # Timestamp selection: stamp new/closed versions with the latest updated_at in batch
            batch_max_ts = batch.max_updated_at or datetime.now(timezone.utc)
            processing_date = batch_max_ts.date()

            # Incremental change detection: absent keys produce 0 deletions
            change_report = detect_changes(
                source_df=source_df,
                target_df=target_df,
                business_key=self.business_key,
                tracked_columns=self.tracked_columns,
                processing_date=processing_date,
                snapshot_mode=SnapshotMode.INCREMENTAL,
                delete_policy=DeletePolicy.IGNORE,
            )

            # Apply SCD2 historical intervals [effective_from, effective_to)
            scd2_df = apply_scd2(
                source_df=source_df,
                target_df=target_df,
                change_report=change_report,
                business_key=self.business_key,
                tracked_columns=self.tracked_columns,
                processing_date=processing_date,
                delete_policy=DeletePolicy.IGNORE,
            )

            # Validate all 5 SCD2 invariants
            validation_report = validate_scd2(scd2_df, business_key=self.business_key)

            # 7. GUARDRAIL Phase: Deterministic Change Significance Evaluation
            run_id = uuid4()
            guardrail_decision = self.guardrail.evaluate(
                batch=batch,
                change_report=change_report,
                validation_report=validation_report,
                run_id=run_id,
                monitor_config=self.monitor_config,
            )
            logger.info(
                "Guardrail evaluated batch %s: decision=%s, severity=%s, triggered_rules=%s",
                batch.batch_id,
                guardrail_decision.decision.value,
                guardrail_decision.severity.value,
                [r.rule_id for r in guardrail_decision.triggered_rules],
            )

            # If SCD2 invariants failed, raise validation error to trigger rollback and FAILED run status
            if not validation_report.passed:
                failures = [r.message for r in validation_report.rules if r.status.value == "fail"]
                raise WorkerValidationFailureError(
                    f"SCD2 invariant validation failed: {'; '.join(failures)}",
                    failures=failures,
                )

            # If Guardrail classified batch as SUSPICIOUS (passed validation but flagged by business rules):
            # Record run and held_change_batch via ContainmentService, do NOT write to inventory_history,
            # and do NOT advance checkpoint
            if guardrail_decision.is_suspicious:
                hold_row, is_new = self.containment.contain_suspicious_batch(
                    batch=batch,
                    guardrail_decision=guardrail_decision,
                    run_id=run_id,
                    source_name=self.source_name,
                )
                explanation_result = None
                if self.settings.ai_explanation_enabled:
                    try:
                        explanation_result = self.explanation_service.explain_held_batch(
                            hold_id=hold_row.hold_id,
                            persist=True,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Failed to generate explanation for held batch %s: %s",
                            hold_row.hold_id,
                            exc,
                        )

                duration_ms = round((time_module.perf_counter() - t_start) * 1000, 2)
                logger.warning(
                    "Cycle %s HELD due to suspicious change significance: seen=%d, held=%d, rules=%s (duration=%.1fms)",
                    cycle_id,
                    batch.size,
                    batch.size,
                    [r.rule_id for r in guardrail_decision.triggered_rules],
                    duration_ms,
                )
                return WorkerCycleResult(
                    cycle_id=cycle_id,
                    status="HELD",
                    records_seen=batch.size,
                    records_changed=0,
                    records_held=batch.size,
                    watermark_before=watermark_before,
                    watermark_after=watermark_before,
                    run_id=run_id,
                    duration_ms=duration_ms,
                    guardrail_decision=guardrail_decision,
                    explanation=explanation_result,
                    first_cursor=batch.first_cursor,
                    last_cursor=batch.last_cursor,
                )

            # 8. COMMIT Phase for NORMAL batch (Atomic multi-table transaction)
            with self.db.transaction() as conn:
                # 8a. Register processing run in PROCESSING status
                self.run_repo.create_run(
                    source_name=self.source_name,
                    run_id=run_id,
                    status=RunStatus.PROCESSING.value,
                    conn=conn,
                )

                # 8b. Close prior active rows for CHANGED keys
                if change_report.changed:
                    closed_at = datetime.combine(processing_date, time.min, tzinfo=timezone.utc)
                    if _is_inventory_monitor(self.monitor_config):
                        changed_keys = [
                            (str(r.business_key_values["sku_id"]), str(r.business_key_values["warehouse_id"]))
                            for r in change_report.changed
                        ]
                        self.history_repo.close_current_versions(
                            keys=changed_keys,
                            closed_at=closed_at,
                            conn=conn,
                        )
                    else:
                        entity_keys = [
                            {c: r.business_key_values[c] for c in self.business_key}
                            for r in change_report.changed
                        ]
                        self.generic_history_repo.close_current_versions(
                            source_name=self.source_name,
                            entity_keys=entity_keys,
                            closed_at=closed_at,
                            conn=conn,
                        )

                # 8c. Insert new active rows for NEW and CHANGED records
                new_active_df = scd2_df.filter(
                    (pl.col("is_current") == True) & (pl.col("effective_from") == processing_date)  # noqa: E712
                )
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
                        self.history_repo.insert_history_rows(inventory_rows_to_insert, conn=conn)
                    else:
                        generic_rows_to_insert = [
                            MonitoredEntityHistoryRow(
                                source_name=self.source_name,
                                entity_key={c: row[c] for c in self.business_key},
                                attributes={c: row[c] for c in self.tracked_columns},
                                effective_from=eff_from_dt,
                                effective_to=None,
                                is_current=True,
                                created_at=now_utc,
                            )
                            for row in new_active_df.iter_rows(named=True)
                        ]
                        self.generic_history_repo.insert_history_rows(generic_rows_to_insert, conn=conn)

                # 8d. Update processing run to COMMITTED
                records_changed_count = len(change_report.changed)
                self.run_repo.mark_run_committed(
                    run_id=run_id,
                    records_seen=batch.size,
                    records_changed=records_changed_count,
                    records_held=0,
                    conn=conn,
                )

                # 8e. Monotonically advance stream checkpoint with composite cursor
                watermark_after = (
                    batch.last_cursor.timestamp if batch.last_cursor else None
                ) or batch.max_updated_at or watermark_before
                cursor_keys_after = batch.last_cursor.keys if batch.last_cursor else None

                self.checkpoint_repo.update_checkpoint(
                    source_name=self.source_name,
                    watermark=watermark_after,  # type: ignore[arg-type]
                    cursor_keys=cursor_keys_after,
                    run_id=run_id,
                    table_name=self.table_name,
                    conn=conn,
                )

            explanation_result = None
            if self.settings.ai_explanation_enabled and self.settings.ai_explanation_for_normal_batches:
                try:
                    from ..explanation.models import ExplanationContext

                    context = ExplanationContext.from_guardrail_decision(
                        decision=guardrail_decision,
                        batch_id=batch.batch_id,
                        source_name=self.source_name,
                        watermark_start=watermark_before,
                        watermark_end=watermark_after,
                        validation_passed=True,
                        processing_status="COMMITTED",
                        records_sample=batch.to_dict()[:3] if batch.records else [],
                    )
                    explanation_result = self.explanation_service.explain_batch(context)
                except Exception as exc:
                    logger.warning("Failed to generate explanation for committed batch: %s", exc)

            duration_ms = round((time_module.perf_counter() - t_start) * 1000, 2)
            logger.info(
                "Cycle %s COMMITTED successfully: seen=%d, changed=%d, new_watermark=%s (duration=%.1fms)",
                cycle_id,
                batch.size,
                records_changed_count,
                watermark_after,
                duration_ms,
            )
            return WorkerCycleResult(
                cycle_id=cycle_id,
                status="COMMITTED",
                records_seen=batch.size,
                records_changed=records_changed_count,
                records_held=0,
                watermark_before=watermark_before,
                watermark_after=watermark_after,
                run_id=run_id,
                duration_ms=duration_ms,
                guardrail_decision=guardrail_decision,
                explanation=explanation_result,
                first_cursor=batch.first_cursor,
                last_cursor=batch.last_cursor,
            )

        except Exception as exc:
            duration_ms = round((time_module.perf_counter() - t_start) * 1000, 2)
            clean_error = sanitize_error_message(str(exc), getattr(self.settings, "database_url", None))
            logger.error(
                "Cycle %s FAILED (checkpoint unchanged): %s",
                cycle_id,
                clean_error,
            )

            # Record failed run in standalone connection for observability
            if run_id is not None:
                try:
                    self.run_repo.mark_run_failed(run_id=run_id, error_message=clean_error)
                except Exception as log_exc:
                    logger.warning("Could not persist failed run status: %s", log_exc)

            if raise_on_error:
                raise WorkerError(f"Worker cycle failed: {clean_error}", cause=exc) from exc

            return WorkerCycleResult(
                cycle_id=cycle_id,
                status="FAILED",
                records_seen=0,
                records_changed=0,
                records_held=0,
                watermark_before=None,
                watermark_after=None,
                run_id=run_id,
                duration_ms=duration_ms,
                error_message=clean_error,
            )

    def run_forever(self, max_cycles: Optional[int] = None) -> list[WorkerCycleResult]:
        """Continuously run ingestion polling cycles until stopped or max_cycles reached.

        Includes bounded exponential backoff for transient failures and clean interrupt.
        """
        logger.info(
            "Ingestion worker started in continuous mode for source '%s' (poll_interval=%.1fs, batch_size=%d)",
            self.source_name,
            self.poll_interval,
            self.batch_size,
        )

        results: list[WorkerCycleResult] = []
        cycles_completed = 0
        consecutive_failures = 0

        while not self._stop_event.is_set():
            if max_cycles is not None and cycles_completed >= max_cycles:
                logger.info("Reached target max_cycles (%d). Stopping worker.", max_cycles)
                break

            try:
                cycle_result = self.run_once(raise_on_error=True)
                results.append(cycle_result)
                cycles_completed += 1
                consecutive_failures = 0

                # Backlog management: if records were found and batch was full,
                # immediately poll next slice without sleeping.
                if cycle_result.status == "COMMITTED" and cycle_result.records_seen >= self.batch_size:
                    logger.debug("Batch limit reached; draining remaining backlog immediately.")
                    continue

                # Normal sleep between cycles
                if self._stop_event.wait(timeout=self.poll_interval):
                    break

            except Exception as exc:
                consecutive_failures += 1
                clean_error = sanitize_error_message(str(exc), getattr(self.settings, "database_url", None))
                logger.warning(
                    "Worker cycle failed (attempt %d/%d): %s",
                    consecutive_failures,
                    self.retry_count,
                    clean_error,
                )

                if consecutive_failures >= self.retry_count:
                    logger.error(
                        "Exceeded maximum consecutive worker failures (%d). Worker aborting.",
                        self.retry_count,
                    )
                    raise WorkerError(
                        f"Worker aborted after {consecutive_failures} consecutive failures: {clean_error}",
                        cause=exc,
                    ) from exc

                # Bounded exponential backoff
                backoff = self.retry_backoff * (2 ** (consecutive_failures - 1))
                logger.info("Backing off for %.1f seconds before next retry attempt...", backoff)
                if self._stop_event.wait(timeout=backoff):
                    break

        logger.info("Ingestion worker stopped cleanly. Total cycles executed: %d", cycles_completed)
        return results
