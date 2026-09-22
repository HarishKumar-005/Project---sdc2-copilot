"""Domain service coordinating customer canonical data ingestion into the deterministic SCD2 engine."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
import logging
from pathlib import Path
from typing import Any, Optional, Union

import polars as pl
import psycopg

from ...db.models import MonitoredEntityHistoryRow, _ensure_utc
from ...db.repositories.monitored_entity_history_repository import MonitoredEntityHistoryRepository
from ..exceptions import (
    NoValidRecordsError,
    SCD2InvariantValidationError,
    SchemaCompatibilityError,
)
from ..models.drift import MappingCompatibilityState, SchemaDriftReport
from ..models.transformation import CanonicalCustomerRecord
from .adapter import CustomerSCD2Adapter
from .models import CustomerPointInTimeState, CustomerSCD2Config, CustomerSCD2ExecutionResult

logger = logging.getLogger("scd2_copilot.onboarding.scd2.service")


class CustomerSCD2Service:
    """Coordinates canonical customer data ingestion into the parent SCD2 engine and persistence."""

    def __init__(
        self,
        repository: Optional[MonitoredEntityHistoryRepository] = None,
        adapter: Optional[CustomerSCD2Adapter] = None,
        config: Optional[CustomerSCD2Config] = None,
    ) -> None:
        self.config = config or CustomerSCD2Config()
        self.repository = repository or MonitoredEntityHistoryRepository(in_memory=True)
        self.adapter = adapter or CustomerSCD2Adapter(config=self.config)

    def prepare_candidate_scd2(
        self,
        records: Union[list[CanonicalCustomerRecord], pl.DataFrame],
        *,
        processing_date: Optional[date] = None,
        source_id: Optional[str] = None,
        drift_report: Optional[SchemaDriftReport] = None,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> tuple[pl.DataFrame, Any, Any, pl.DataFrame]:
        """Validate drift gate, adapt inputs, and calculate candidate SCD2 transformation without committing history."""
        if drift_report is not None and drift_report.overall_compatibility == MappingCompatibilityState.BROKEN:
            breaking_impacts = [
                i for i in drift_report.impacted_mappings if i.compatibility == MappingCompatibilityState.BROKEN
            ]
            raise SchemaCompatibilityError(
                f"SCD2 processing blocked: Schema drift status is BROKEN for source '{drift_report.source_id}'. "
                f"Approved mapping version '{drift_report.mapping_version_id}' is incompatible.",
                details={
                    "source_id": drift_report.source_id,
                    "mapping_version_id": drift_report.mapping_version_id,
                    "overall_compatibility": drift_report.overall_compatibility.value,
                    "breaking_impact_count": len(breaking_impacts),
                },
            )

        proc_date = processing_date or date.today()
        source_df = self.adapter.canonical_records_to_source_df(records, config=self.config)
        total_records = len(records) if isinstance(records, list) else source_df.height

        if total_records == 0:
            target_df = self.adapter.current_history_to_target_df([], config=self.config)
            scd2_df, change_report, validation_report = self.adapter.execute_scd2(
                source_df=source_df,
                target_df=target_df,
                processing_date=proc_date,
                config=self.config,
            )
            return scd2_df, change_report, validation_report, source_df

        incoming_keys = [{"customer_id": str(val)} for val in source_df["customer_id"].to_list()]
        current_history = self.repository.fetch_current_history_for_keys(
            source_name=self.config.entity_source_name,
            entity_keys=incoming_keys,
            conn=conn,
        )
        target_df = self.adapter.current_history_to_target_df(
            current_history, config=self.config
        )

        scd2_df, change_report, validation_report = self.adapter.execute_scd2(
            source_df=source_df,
            target_df=target_df,
            processing_date=proc_date,
            config=self.config,
        )

        if not validation_report.passed:
            failure_msgs = [
                r.message for r in validation_report.rules if r.status.value == "fail"
            ]
            logger.error("SCD2 validation invariants failed: %s", failure_msgs)
            raise SCD2InvariantValidationError(
                f"SCD2 invariant validation failed: {'; '.join(failure_msgs)}",
                details={
                    "failures": failure_msgs,
                    "rules": [r.__dict__ for r in validation_report.rules],
                },
            )

        return scd2_df, change_report, validation_report, source_df

    def commit_candidate_scd2(
        self,
        scd2_df: pl.DataFrame,
        change_report: Any,
        processing_date: date,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> tuple[int, int]:
        """Commit validated candidate SCD2 historical mutations to the entity history repository."""
        keys_to_close = self.adapter.extract_keys_to_close(change_report, config=self.config)
        closed_count = 0
        if keys_to_close:
            closed_dt = datetime.combine(processing_date, time.min, tzinfo=timezone.utc)
            closed_count = self.repository.close_current_versions(
                source_name=self.config.entity_source_name,
                entity_keys=keys_to_close,
                closed_at=closed_dt,
                conn=conn,
            )

        rows_to_insert = self.adapter.build_rows_to_insert(
            scd2_df=scd2_df,
            change_report=change_report,
            processing_date=processing_date,
            config=self.config,
        )
        inserted_count = 0
        if rows_to_insert:
            inserted_count = self.repository.insert_history_rows(rows_to_insert, conn=conn)

        return closed_count, inserted_count

    def process_canonical_batch(
        self,
        records: Union[list[CanonicalCustomerRecord], pl.DataFrame],
        *,
        processing_date: Optional[date] = None,
        run_id: Optional[str] = None,
        batch_id: Optional[str] = None,
        source_id: Optional[str] = None,
        drift_report: Optional[SchemaDriftReport] = None,
        artifact_path: Optional[Union[str, Path]] = None,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> CustomerSCD2ExecutionResult:
        """Process a batch of validated canonical customer records through the deterministic SCD2 engine.

        Guarantees:
        1. Pre-execution Drift Gate: If drift_report is provided and compatibility is BROKEN,
           execution is strictly blocked with SchemaCompatibilityError.
        2. Input Gate: Invalid records never enter SCD2.
        3. Deterministic SCD2 Invariants: Validated before committing.
        4. History persistence: Atomic closure of old versions and creation of new versions.
        """
        proc_date = processing_date or date.today()
        src_id = source_id or self.config.entity_source_name
        total_records = len(records) if isinstance(records, list) else (records.height if hasattr(records, "height") else 0)

        if total_records == 0:
            logger.info("Empty canonical record batch provided; zero SCD2 operations performed.")
            res = CustomerSCD2ExecutionResult(
                run_id=run_id,
                batch_id=batch_id or "empty_batch",
                source_id=src_id,
                processing_date=proc_date,
                total_records_seen=0,
                validation_passed=True,
            )
            if artifact_path:
                res.save_artifact(artifact_path)
            return res

        # 1 & 2. Prepare candidate SCD2 transformation
        scd2_df, change_report, validation_report, source_df = self.prepare_candidate_scd2(
            records=records,
            processing_date=proc_date,
            source_id=src_id,
            drift_report=drift_report,
            conn=conn,
        )

        # 3. Commit candidate changes to historical storage
        closed_count, inserted_count = self.commit_candidate_scd2(
            scd2_df=scd2_df,
            change_report=change_report,
            processing_date=proc_date,
            conn=conn,
        )

        # 8. Compile structured execution result
        result = CustomerSCD2ExecutionResult(
            run_id=run_id,
            batch_id=batch_id or f"batch_{proc_date.isoformat()}",
            source_id=src_id,
            processing_date=proc_date,
            total_records_seen=total_records,
            new_count=len(change_report.new),
            changed_count=len(change_report.changed),
            unchanged_count=len(change_report.unchanged),
            deleted_count=len(change_report.deleted),
            closed_versions_count=closed_count,
            new_versions_count=inserted_count,
            scd2_rows_total=scd2_df.height,
            validation_passed=validation_report.passed,
            validation_report={
                "passed": validation_report.passed,
                "rules": [
                    {"name": r.name, "status": r.status.value, "message": r.message}
                    for r in validation_report.rules
                ],
            },
            change_report_summary={
                "new_keys": [r.business_key_values.get("customer_id") for r in change_report.new],
                "changed_keys": [
                    r.business_key_values.get("customer_id") for r in change_report.changed
                ],
                "unchanged_keys": [
                    r.business_key_values.get("customer_id") for r in change_report.unchanged
                ],
                "deleted_keys": [
                    r.business_key_values.get("customer_id") for r in change_report.deleted
                ],
            },
            persisted_history_rows_count=inserted_count,
        )

        if artifact_path:
            result.save_artifact(artifact_path)

        logger.info(
            "Customer SCD2 batch processed successfully: new=%d, changed=%d, unchanged=%d, deleted=%d, persisted=%d",
            result.new_count,
            result.changed_count,
            result.unchanged_count,
            result.deleted_count,
            result.persisted_history_rows_count,
        )
        return result

    def process_batch(
        self,
        records: Union[list[CanonicalCustomerRecord], pl.DataFrame],
        *,
        processing_date: Optional[date] = None,
        run_id: Optional[str] = None,
        batch_id: Optional[str] = None,
        source_id: Optional[str] = None,
        drift_report: Optional[SchemaDriftReport] = None,
        artifact_path: Optional[Union[str, Path]] = None,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> CustomerSCD2ExecutionResult:
        """Alias for process_canonical_batch."""
        return self.process_canonical_batch(
            records=records,
            processing_date=processing_date,
            run_id=run_id,
            batch_id=batch_id,
            source_id=source_id,
            drift_report=drift_report,
            artifact_path=artifact_path,
            conn=conn,
        )

    # ── Historical & Point-in-Time Inspection APIs ───────────────


    def get_customer_history(
        self,
        customer_id: str,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> list[MonitoredEntityHistoryRow]:
        """Fetch complete chronological SCD2 history for a customer."""
        return self.repository.fetch_history_for_key(
            source_name=self.config.entity_source_name,
            entity_key={"customer_id": str(customer_id)},
            conn=conn,
        )

    def get_customer_point_in_time(
        self,
        customer_id: str,
        as_of: datetime,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> CustomerPointInTimeState:
        """Inspect the active customer state at timestamp as_of under [effective_from, effective_to) half-open semantics."""
        row = self.repository.fetch_history_at_timestamp(
            source_name=self.config.entity_source_name,
            entity_key={"customer_id": str(customer_id)},
            as_of=as_of,
            conn=conn,
        )
        if row is None:
            return CustomerPointInTimeState(
                customer_id=str(customer_id),
                as_of=as_of,
                found=False,
            )

        return CustomerPointInTimeState(
            customer_id=str(customer_id),
            as_of=as_of,
            found=True,
            history_id=row.history_id,
            effective_from=_ensure_utc(row.effective_from),
            effective_to=_ensure_utc(row.effective_to),
            is_current=row.is_current,
            attributes=dict(row.attributes),
        )

    def get_current_customer(
        self,
        customer_id: str,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> Optional[MonitoredEntityHistoryRow]:
        """Fetch the currently active (is_current=True) historical version for a customer."""
        history = self.repository.fetch_current_history_for_keys(
            source_name=self.config.entity_source_name,
            entity_keys=[{"customer_id": str(customer_id)}],
            conn=conn,
        )
        return history[0] if history else None
