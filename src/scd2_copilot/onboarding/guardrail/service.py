"""Domain service coordinating pre-commit guardrail evaluation and suspicious batch containment."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, time, timezone
import logging
from pathlib import Path
from typing import Any, Optional, Union
from uuid import UUID, uuid4

import polars as pl
import psycopg

from ...config import Settings, get_settings
from ...containment.models import HoldResolutionResult
from ...containment.service import ContainmentService
from ...db.connection import DatabaseManager
from ...db.models import HeldChangeBatchRow, HoldStatus
from ...db.repositories import (
    CheckpointRepository,
    HeldChangeBatchRepository,
    MonitoredEntityHistoryRepository,
    ProcessingRunRepository,
)
from ...explanation.service import ExplanationService
from ...guardrail.engine import GuardrailEngine
from ...guardrail.models import (
    GuardrailDecision,
    GuardrailDecisionType,
    GuardrailSeverity,
)
from ..exceptions import (
    NoValidRecordsError,
    SCD2InvariantValidationError,
    SchemaCompatibilityError,
)
from ..models.drift import MappingCompatibilityState, SchemaDriftReport
from ..models.transformation import CanonicalCustomerRecord
from ..scd2.models import CustomerSCD2ExecutionResult
from ..scd2.service import CustomerSCD2Service
from .adapter import CustomerGuardrailAdapter
from .models import CustomerGuardrailConfig, CustomerGuardrailEvaluationResult

logger = logging.getLogger("scd2_copilot.onboarding.guardrail.service")


class CustomerHistoricalGuardrailService:
    """Coordinates deterministic guardrail evaluation BEFORE committing customer historical changes.

    Guarantees:
    1. Pre-Commit Decision Boundary: Guardrail evaluates candidate SCD2 mutations before
       any database writes occur.
    2. NORMAL batches commit cleanly to MonitoredEntityHistoryRepository.
    3. SUSPICIOUS batches are held in held_change_batch and NEVER mutate historical state.
    4. Zero second anomaly detector: Reuses parent GuardrailEngine directly.
    5. Zero second containment system: Reuses parent ContainmentService directly.
    6. Full lineage preserved: run_id, source_id, schema fingerprints, mapping versions.
    7. AI is explanatory only: LLM failure never alters deterministic guardrail decisions.
    """

    def __init__(
        self,
        config: Optional[CustomerGuardrailConfig] = None,
        scd2_service: Optional[CustomerSCD2Service] = None,
        guardrail_engine: Optional[GuardrailEngine] = None,
        containment_service: Optional[ContainmentService] = None,
        explanation_service: Optional[ExplanationService] = None,
        hold_repo: Optional[HeldChangeBatchRepository] = None,
        run_repo: Optional[ProcessingRunRepository] = None,
        db_manager: Optional[DatabaseManager] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.db = db_manager or DatabaseManager(settings=self.settings)
        self.config = config or CustomerGuardrailConfig()
        self.adapter = CustomerGuardrailAdapter(config=self.config)
        self.scd2_service = scd2_service or CustomerSCD2Service()

        # Wire repositories with in-memory fallback support
        is_mem = (
            (hold_repo is not None and getattr(hold_repo, "in_memory_mode", False))
            or getattr(self.scd2_service.repository, "in_memory_mode", False)
            or (not self.db.is_configured)
        )
        self.hold_repo = hold_repo or HeldChangeBatchRepository(db=self.db, in_memory=is_mem)
        self.run_repo = run_repo or ProcessingRunRepository(db=self.db, in_memory=is_mem)
        self.checkpoint_repo = CheckpointRepository(db=self.db, in_memory=is_mem)

        # Wire existing parent GuardrailEngine and configure with customer thresholds
        self.guardrail_engine = guardrail_engine or GuardrailEngine(settings=self.settings)
        self._apply_guardrail_thresholds(self.guardrail_engine, self.config)

        # Wire existing parent ContainmentService with customer monitor configuration
        self.containment_service = containment_service or ContainmentService(
            db_manager=self.db,
            settings=self.settings,
            hold_repo=self.hold_repo,
            run_repo=self.run_repo,
            checkpoint_repo=self.checkpoint_repo,
            generic_history_repo=self.scd2_service.repository,
            guardrail=self.guardrail_engine,
            source_name=self.config.source_name,
            table_name="customer_onboarding",
            monitor_config=self.adapter.build_monitor_config(self.config),
        )

        # Wire optional parent ExplanationService
        self.explanation_service = explanation_service

    @staticmethod
    def _apply_guardrail_thresholds(
        engine: GuardrailEngine,
        config: CustomerGuardrailConfig,
    ) -> None:
        """Apply customer-specific deterministic thresholds to the reused parent GuardrailEngine."""
        engine.enabled = config.enabled
        engine.max_changed_records = config.max_changed_records
        engine.max_affected_population_ratio = config.max_affected_population_ratio
        engine.min_evaluated_records_for_ratio = config.min_evaluated_records_for_ratio
        engine.max_deactivation_count = config.max_deactivation_count
        engine.max_changes_per_second = config.max_changes_per_second
        engine.min_velocity_records = config.min_velocity_records
        engine.max_warehouses_affected = config.max_warehouses_affected

    def evaluate_and_process(
        self,
        records: Union[list[CanonicalCustomerRecord], pl.DataFrame],
        *,
        processing_date: Optional[date] = None,
        run_id: Optional[str] = None,
        batch_id: Optional[str] = None,
        source_id: Optional[str] = None,
        drift_report: Optional[SchemaDriftReport] = None,
        source_schema_fingerprint: Optional[str] = None,
        mapping_version_id: Optional[str] = None,
        canonical_schema_version: str = "customer.v1",
        input_fingerprint: Optional[str] = None,
        artifact_path: Optional[Union[str, Path]] = None,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> CustomerGuardrailEvaluationResult:
        """Evaluate candidate SCD2 changes against deterministic guardrails and commit only if NORMAL."""
        proc_date = processing_date or date.today()
        src_id = source_id or self.config.source_name
        total_records = len(records) if isinstance(records, list) else (records.height if hasattr(records, "height") else 0)
        self._apply_guardrail_thresholds(self.guardrail_engine, self.config)

        # Parse UUID run_id if valid string, else generate deterministic UUID
        parsed_run_id: Optional[UUID] = None
        if run_id:
            try:
                parsed_run_id = UUID(str(run_id))
            except (ValueError, TypeError):
                parsed_run_id = uuid4()
        else:
            parsed_run_id = uuid4()

        # Handle empty records cleanly
        if total_records == 0:
            empty_cand = CustomerSCD2ExecutionResult(
                run_id=run_id,
                batch_id=batch_id or "empty_batch",
                source_id=src_id,
                processing_date=proc_date,
                total_records_seen=0,
                validation_passed=True,
            )
            empty_evidence = self.guardrail_engine.extract_evidence(
                batch=self.adapter.canonical_records_to_micro_batch([], processing_date=proc_date),
                change_report=self.scd2_service.adapter.execute_scd2(
                    source_df=self.scd2_service.adapter.canonical_records_to_source_df([]),
                    target_df=self.scd2_service.adapter.current_history_to_target_df([]),
                    processing_date=proc_date,
                )[1],
            )
            return CustomerGuardrailEvaluationResult(
                decision=GuardrailDecisionType.NORMAL,
                severity=GuardrailSeverity.LOW,
                is_held=False,
                hold_id=None,
                triggered_rules=[],
                reasons=["Empty batch: zero records evaluated."],
                evidence=empty_evidence,
                persisted=False,
                candidate_execution_result=empty_cand,
            )

        # ── Step 1: Calculate Candidate SCD2 Result (Pre-Commit) ──────────
        scd2_df, change_report, validation_report, source_df = self.scd2_service.prepare_candidate_scd2(
            records=records,
            processing_date=proc_date,
            source_id=src_id,
            drift_report=drift_report,
            conn=conn,
        )

        # ── Step 2: Adapt Records into MicroBatch ─────────────────────────
        has_prior_history = bool(self.scd2_service.repository.get_all_rows())
        watermark_start = None
        if has_prior_history:
            raw_ts = [
                r.created_at
                for r in records
                if isinstance(r, CanonicalCustomerRecord) and r.created_at
            ]
            if raw_ts:
                watermark_start = min(raw_ts)

        micro_batch = self.adapter.canonical_records_to_micro_batch(
            records=records,
            config=self.config,
            watermark_start=watermark_start,
            processing_date=proc_date,
        )

        # ── Step 3: Evaluate Guardrail (Pure Deterministic Check) ──────────
        monitor_cfg = self.adapter.build_monitor_config(self.config)
        guardrail_decision = self.guardrail_engine.evaluate(
            batch=micro_batch,
            change_report=change_report,
            validation_report=validation_report,
            run_id=parsed_run_id,
            monitor_config=monitor_cfg,
        )

        # ── Step 4: Decision Boundary (Pre-Commit Enforcement) ───────────
        if guardrail_decision.is_normal:
            # NORMAL PATH: Commit candidate historical changes to persistence
            closed_count, inserted_count = self.scd2_service.commit_candidate_scd2(
                scd2_df=scd2_df,
                change_report=change_report,
                processing_date=proc_date,
                conn=conn,
            )

            # Record run status if using processing_run repo
            try:
                self.run_repo.mark_run_committed(
                    run_id=parsed_run_id,
                    records_seen=total_records,
                    records_changed=len(change_report.changed),
                    records_held=0,
                    conn=conn,
                )
            except Exception:
                pass  # Run tracking is best-effort when not explicitly managed

            candidate_result = CustomerSCD2ExecutionResult(
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

            res = CustomerGuardrailEvaluationResult(
                decision=GuardrailDecisionType.NORMAL,
                severity=guardrail_decision.severity,
                is_held=False,
                hold_id=None,
                triggered_rules=guardrail_decision.triggered_rules,
                reasons=guardrail_decision.reasons,
                evidence=guardrail_decision.evidence,
                persisted=True,
                candidate_execution_result=candidate_result,
            )

            if artifact_path:
                res.save_artifact(artifact_path)
            return res

        else:
            # SUSPICIOUS PATH: Strictly DO NOT commit history!
            # Persist durable hold in held_change_batch preserving evidence and lineage
            hold_row, is_new = self.containment_service.contain_suspicious_batch(
                batch=micro_batch,
                guardrail_decision=guardrail_decision,
                run_id=parsed_run_id,
                source_name=src_id,
                conn=conn,
            )

            # Enrich hold evidence with onboarding lineage metadata
            lineage_metadata = {
                "onboarding_run_id": run_id,
                "source_id": src_id,
                "source_schema_fingerprint": source_schema_fingerprint,
                "mapping_version_id": mapping_version_id,
                "canonical_schema_version": canonical_schema_version,
                "input_fingerprint": input_fingerprint,
                "change_report_summary": {
                    "new_count": len(change_report.new),
                    "changed_count": len(change_report.changed),
                    "unchanged_count": len(change_report.unchanged),
                    "deleted_count": len(change_report.deleted),
                },
            }
            updated_evidence = dict(hold_row.evidence or {})
            updated_evidence["onboarding_lineage"] = lineage_metadata

            explanation_dict: Optional[dict[str, Any]] = None

            # Optional AI explanation generation via parent ExplanationService
            if self.config.ai_explanation_enabled and self.explanation_service is not None:
                try:
                    exp_ctx = self.adapter.build_explanation_context(
                        guardrail_decision=guardrail_decision,
                        source_name=src_id,
                        run_id=parsed_run_id,
                        hold_id=hold_row.hold_id,
                        records_sample=micro_batch.source_records[:5],
                    )
                    exp_result = self.explanation_service.explain_batch(exp_ctx)
                    explanation_dict = exp_result.to_dict() if hasattr(exp_result, "to_dict") else asdict(exp_result)
                    updated_evidence["explanation"] = explanation_dict
                except Exception as exp_err:
                    logger.warning("AI explanation failed; continuing without altering hold: %s", exp_err)
                    updated_evidence["explanation_error"] = str(exp_err)

            self.hold_repo.update_hold_evidence(
                hold_id=hold_row.hold_id,
                evidence=updated_evidence,
                conn=conn,
            )

            # Compile non-persisted candidate result
            candidate_result = CustomerSCD2ExecutionResult(
                run_id=run_id,
                batch_id=batch_id or f"batch_{proc_date.isoformat()}",
                source_id=src_id,
                processing_date=proc_date,
                total_records_seen=total_records,
                new_count=len(change_report.new),
                changed_count=len(change_report.changed),
                unchanged_count=len(change_report.unchanged),
                deleted_count=len(change_report.deleted),
                closed_versions_count=0,
                new_versions_count=0,
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
                persisted_history_rows_count=0,
            )

            res = CustomerGuardrailEvaluationResult(
                decision=GuardrailDecisionType.SUSPICIOUS,
                severity=guardrail_decision.severity,
                is_held=True,
                hold_id=hold_row.hold_id,
                triggered_rules=guardrail_decision.triggered_rules,
                reasons=guardrail_decision.reasons,
                evidence=guardrail_decision.evidence,
                persisted=False,
                candidate_execution_result=candidate_result,
                explanation=explanation_dict,
            )

            if artifact_path:
                res.save_artifact(artifact_path)
            return res

    # ── Operational Recovery Delegates ────────────────────────────

    def release_held_batch(
        self,
        hold_id: UUID,
        operator_reason: Optional[str] = None,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> HoldResolutionResult:
        """Release a held customer batch and commit changes downstream."""
        return self.containment_service.release_held_batch(
            hold_id=hold_id,
            operator_reason=operator_reason,
            conn=conn,
        )

    def reprocess_held_batch(
        self,
        hold_id: UUID,
        force_normal: bool = False,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> HoldResolutionResult:
        """Reprocess a held customer batch by re-evaluating SCD2 and guardrail rules."""
        return self.containment_service.reprocess_held_batch(
            hold_id=hold_id,
            force_normal=force_normal,
            conn=conn,
        )

    def discard_held_batch(
        self,
        hold_id: UUID,
        operator_reason: Optional[str] = None,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> HoldResolutionResult:
        """Permanently discard a held customer batch; history remains untouched."""
        return self.containment_service.discard_held_batch(
            hold_id=hold_id,
            operator_reason=operator_reason,
            advance_checkpoint=False,
            conn=conn,
        )

    def get_held_batch(
        self,
        hold_id: UUID,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> HeldChangeBatchRow:
        """Retrieve a specific held change batch by ID."""
        return self.containment_service.get_held_batch(hold_id=hold_id, conn=conn)

    def list_held_batches(
        self,
        status: Optional[str] = None,
        limit: int = 50,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> list[HeldChangeBatchRow]:
        """List held change batches for the customer onboarding source."""
        return self.containment_service.list_held_batches(
            status=status,
            source_name=self.config.source_name,
            limit=limit,
            conn=conn,
        )
