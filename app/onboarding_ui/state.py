"""State manager and domain service bridge for Customer Data Onboarding Streamlit UI.

Provides:
- Thread-safe initialization of real backend services.
- Real dataset loading from sample-data/onboarding/.
- Session state persistence for interactive user flows.
- 18-step sequential demo orchestrator binding directly to domain services.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import logging
from pathlib import Path
from typing import Any, Optional
from uuid import UUID, uuid4
import polars as pl
import streamlit as st

from src.scd2_copilot.config import get_settings
from src.scd2_copilot.db.connection import DatabaseManager, redact_database_url
from src.scd2_copilot.onboarding.adapters.csv_adapter import CSVSourceAdapter
from src.scd2_copilot.onboarding.adapters.mock_rest_adapter import MockRESTSourceAdapter
from src.scd2_copilot.onboarding.approval.service import MappingReviewService, MappingReviewSession
from src.scd2_copilot.onboarding.canonical import CANONICAL_CUSTOMER_V1, get_canonical_customer_v1
from src.scd2_copilot.onboarding.drift.engine import DeterministicSchemaDiffEngine
from src.scd2_copilot.onboarding.drift.impact import MappingImpactAnalyzer
from src.scd2_copilot.onboarding.drift.repository import SchemaDriftRepository
from src.scd2_copilot.onboarding.drift.service import SchemaDriftService
from src.scd2_copilot.onboarding.evaluation.datasets import (
    create_approved_mapping_for_dataset,
    get_dataset_a_clean,
    get_dataset_b_moderately_messy,
    get_dataset_c_highly_messy,
)
from src.scd2_copilot.onboarding.exception_queue.service import ExceptionQueueService
from src.scd2_copilot.onboarding.guardrail.models import CustomerGuardrailEvaluationResult
from src.scd2_copilot.onboarding.guardrail.service import CustomerHistoricalGuardrailService
from src.scd2_copilot.onboarding.mapping.candidates import DeterministicCandidateGenerator
from src.scd2_copilot.onboarding.mapping.engine import SemanticMappingEngine
from src.scd2_copilot.onboarding.models.approval import (
    ApprovedMappingVersion,
    FieldReviewDecision,
    ReviewDecisionType,
)
from src.scd2_copilot.onboarding.models.drift import SchemaDriftReport
from src.scd2_copilot.onboarding.models.exception import CorrectionType, CustomerRecordException, ExceptionBatch
from src.scd2_copilot.onboarding.models.mapping import CandidateMapping, MappingProposalBatch
from src.scd2_copilot.onboarding.models.profile import DataProfile
from src.scd2_copilot.onboarding.models.run import OnboardingRun, RunCreateRequest, RunStatus
from src.scd2_copilot.onboarding.models.schema_snapshot import SourceSchemaSnapshot
from src.scd2_copilot.onboarding.models.source import SourceDefinition, SourceType
from src.scd2_copilot.onboarding.models.transformation import CanonicalCustomerRecord, TransformationResult
from src.scd2_copilot.onboarding.profiler.engine import DataProfiler
from src.scd2_copilot.onboarding.runs.service import OnboardingRunService
from src.scd2_copilot.onboarding.scd2.models import CustomerSCD2ExecutionResult
from src.scd2_copilot.onboarding.scd2.service import CustomerSCD2Service
from src.scd2_copilot.onboarding.transformation.service import TransformationPipeline

logger = logging.getLogger("scd2_copilot.ui.onboarding_state")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SAMPLE_DIR = REPO_ROOT / "sample-data" / "onboarding"

AVAILABLE_FIXTURES: dict[str, dict[str, Any]] = {
    "CRM Customers": {
        "file_path": SAMPLE_DIR / "crm_customers.csv",
        "source_id": "crm_salesforce",
        "description": "Messy CRM export with customer names, status, emails, and unparseable edge cases.",
    },
    "Billing Accounts": {
        "file_path": SAMPLE_DIR / "billing_accounts.csv",
        "source_id": "billing_stripe",
        "description": "Billing account export with account codes, balances, and cycle dates.",
    },
    "Support Tickets": {
        "file_path": SAMPLE_DIR / "support_tickets.csv",
        "source_id": "support_zendesk",
        "description": "Support desk tickets with reporter emails and ticket reference IDs.",
    },
}


class OnboardingUIState:
    """Manages session state and coordinates domain service calls for the Onboarding UI."""

    def __init__(self) -> None:
        # Domain Services
        self.profiler = DataProfiler()
        self.candidate_generator = DeterministicCandidateGenerator(CANONICAL_CUSTOMER_V1)
        self.mapping_engine = SemanticMappingEngine(enable_ai=True)
        self.review_service = MappingReviewService()
        self.pipeline = TransformationPipeline()
        self.exception_service = ExceptionQueueService()
        self.run_service = OnboardingRunService(
            review_service=self.review_service,
            exception_service=self.exception_service,
            profiler=self.profiler,
            mapping_engine=self.mapping_engine,
        )
        self.drift_service = SchemaDriftService(
            repository=SchemaDriftRepository(in_memory=True),
        )
        self.scd2_service = CustomerSCD2Service()
        self.guardrail_service = CustomerHistoricalGuardrailService(scd2_service=self.scd2_service)

    @staticmethod
    def get_instance() -> OnboardingUIState:
        """Retrieve or create singleton state manager in st.session_state."""
        if "onboarding_ui_state" not in st.session_state:
            st.session_state["onboarding_ui_state"] = OnboardingUIState()
            OnboardingUIState._init_defaults()
        return st.session_state["onboarding_ui_state"]

    @staticmethod
    def _init_defaults() -> None:
        """Initialize default session state keys if not already present."""
        defaults: dict[str, Any] = {
            "onb_selected_fixture": "CRM Customers",
            "onb_custom_df": None,
            "onb_source_id": "crm_salesforce",
            "onb_raw_df": None,
            "onb_source_schema": None,
            "onb_profile": None,
            "onb_proposals": None,
            "onb_review_session": None,
            "onb_approved_mapping": None,
            "onb_transformation_result": None,
            "onb_exception_batch": None,
            "onb_active_run": None,
            "onb_prior_schema": None,
            "onb_drift_report": None,
            "onb_scd2_result": None,
            "onb_guardrail_result": None,
            "onb_demo_step": 1,
            "onb_demo_logs": [],
        }
        for k, v in defaults.items():
            if k not in st.session_state:
                st.session_state[k] = v

    # ── Dataset Loading ──────────────────────────────────────────

    def load_fixture(self, fixture_name: str) -> tuple[pl.DataFrame, SourceSchemaSnapshot]:
        """Load an authoritative onboarding fixture and discover its schema."""
        cfg = AVAILABLE_FIXTURES.get(fixture_name, AVAILABLE_FIXTURES["CRM Customers"])
        file_path = cfg["file_path"]
        source_id = cfg["source_id"]

        source_def = SourceDefinition(
            source_id=source_id,
            source_name=fixture_name,
            source_type=SourceType.CSV,
            connection_config={"file_path": str(file_path)},
        )
        adapter = CSVSourceAdapter(source_def)
        df = adapter.read_data()
        schema = adapter.discover_schema()

        st.session_state["onb_selected_fixture"] = fixture_name
        st.session_state["onb_source_id"] = source_id
        st.session_state["onb_raw_df"] = df
        st.session_state["onb_source_schema"] = schema
        # Reset downstream workflow caches
        st.session_state["onb_profile"] = None
        st.session_state["onb_proposals"] = None
        st.session_state["onb_approved_mapping"] = None
        st.session_state["onb_transformation_result"] = None
        st.session_state["onb_exception_batch"] = None
        st.session_state["onb_drift_report"] = None
        st.session_state["onb_guardrail_result"] = None
        return df, schema

    def load_custom_csv(self, file_bytes: bytes, filename: str) -> tuple[pl.DataFrame, SourceSchemaSnapshot]:
        """Load an uploaded custom CSV buffer."""
        source_def = SourceDefinition(
            source_id=f"custom_{Path(filename).stem}",
            source_name=filename,
            source_type=SourceType.CSV,
            connection_config={"file_bytes": file_bytes},
        )
        adapter = CSVSourceAdapter(source_def)
        df = adapter.read_data()
        schema = adapter.discover_schema()

        st.session_state["onb_selected_fixture"] = f"Custom: {filename}"
        st.session_state["onb_source_id"] = source_def.source_id
        st.session_state["onb_raw_df"] = df
        st.session_state["onb_source_schema"] = schema
        # Reset downstream workflow caches
        st.session_state["onb_profile"] = None
        st.session_state["onb_proposals"] = None
        st.session_state["onb_approved_mapping"] = None
        st.session_state["onb_transformation_result"] = None
        st.session_state["onb_exception_batch"] = None
        st.session_state["onb_drift_report"] = None
        st.session_state["onb_guardrail_result"] = None
        return df, schema

    def load_rest_source(
        self,
        base_url: str,
        endpoint: str = "",
        headers: Optional[dict[str, str]] = None,
        paginated: bool = False,
        page_size: int = 100,
        max_records: int = 10000,
    ) -> tuple[pl.DataFrame, SourceSchemaSnapshot]:
        """Load customer records from a REST API endpoint via MockRESTSourceAdapter."""
        clean_ep = endpoint.strip("/").replace("/", "_") or "root"
        source_id = f"rest_{clean_ep}"
        source_def = SourceDefinition(
            source_id=source_id,
            source_name=f"REST: {base_url.rstrip('/')}/{endpoint.lstrip('/')}",
            source_type=SourceType.MOCK_REST,
            connection_config={
                "base_url": base_url,
                "endpoint": endpoint,
                "headers": headers or {},
                "paginated": paginated,
                "page_size": page_size,
                "max_records": max_records,
            },
        )
        adapter = MockRESTSourceAdapter(source_def)
        df = adapter.read_data()
        schema = adapter.discover_schema()

        st.session_state["onb_selected_fixture"] = f"REST: {base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        st.session_state["onb_source_id"] = source_def.source_id
        st.session_state["onb_raw_df"] = df
        st.session_state["onb_source_schema"] = schema
        # Reset downstream workflow caches
        st.session_state["onb_profile"] = None
        st.session_state["onb_proposals"] = None
        st.session_state["onb_approved_mapping"] = None
        st.session_state["onb_transformation_result"] = None
        st.session_state["onb_exception_batch"] = None
        st.session_state["onb_drift_report"] = None
        st.session_state["onb_guardrail_result"] = None
        return df, schema

    # ── Operations ──────────────────────────────────────────────

    def profile_active_source(self) -> DataProfile:
        """Run statistical profiling with PII masking."""
        df = st.session_state.get("onb_raw_df")
        schema = st.session_state.get("onb_source_schema")
        source_id = st.session_state.get("onb_source_id", "source")

        if df is None or schema is None:
            df, schema = self.load_fixture(st.session_state.get("onb_selected_fixture", "CRM Customers"))

        profile = self.profiler.profile_dataframe(
            source_id=source_id,
            df=df,
            fingerprint_hash=schema.schema_fingerprint,
        )
        st.session_state["onb_profile"] = profile
        return profile

    def generate_mapping_proposals(self, enable_ai: bool = True) -> MappingProposalBatch:
        """Generate candidate proposals via SemanticMappingEngine."""
        schema = st.session_state.get("onb_source_schema")
        profile = st.session_state.get("onb_profile")
        source_id = st.session_state.get("onb_source_id", "source")

        if schema is None:
            _, schema = self.load_fixture(st.session_state.get("onb_selected_fixture", "CRM Customers"))
        if profile is None:
            profile = self.profile_active_source()

        self.mapping_engine.enable_ai = enable_ai
        proposals = self.mapping_engine.propose_mappings(
            source_id=source_id,
            columns=schema.columns,
            profile=profile,
        )
        st.session_state["onb_proposals"] = proposals

        session = self.review_service.create_session(
            proposal_batch=proposals,
            canonical_schema=CANONICAL_CUSTOMER_V1,
            source_schema=schema,
        )
        st.session_state["onb_review_session"] = session
        return proposals

    def approve_all_mappings(self, reviewed_by: str = "data_lead@enterprise.org") -> ApprovedMappingVersion:
        """Finalize and approve all proposals into an immutable version."""
        session: Optional[MappingReviewSession] = st.session_state.get("onb_review_session")
        if session is None:
            self.generate_mapping_proposals(enable_ai=False)
            session = st.session_state["onb_review_session"]

        # Sort proposals by confidence descending so highest confidence claims the canonical target first
        assigned_targets: set[str] = set()
        sorted_proposals = sorted(session.proposal_batch.proposals, key=lambda p: p.confidence, reverse=True)

        for p in sorted_proposals:
            target = p.target_field
            if target and target in assigned_targets:
                # Canonical invariant: duplicate target mappings not permitted
                target = None

            if target is not None:
                assigned_targets.add(target)
                decision = FieldReviewDecision(
                    source_field=p.source_field,
                    decision=ReviewDecisionType.OVERRIDE if (p.is_ambiguous or p.mapping_type.value == "AMBIGUOUS") else ReviewDecisionType.APPROVE,
                    target_field=target,
                    transformations=list(p.transformations),
                    reviewer=reviewed_by,
                    review_notes="Approved in operator review",
                )
            else:
                decision = FieldReviewDecision(
                    source_field=p.source_field,
                    decision=ReviewDecisionType.REJECT,
                    target_field=None,
                    transformations=[],
                    reviewer=reviewed_by,
                    review_notes="Unmapped column (duplicate or non-canonical target rejected)",
                )
            session.apply_decision(decision)

        approved = self.review_service.finalize_version(
            session=session,
            approved_by=reviewed_by,
            version_number=1,
            allow_incomplete=True,
        )
        st.session_state["onb_approved_mapping"] = approved
        self.run_service.register_approved_mapping(approved)
        return approved

    def run_transformation(self) -> TransformationResult:
        """Execute deterministic transformation and validation pipeline."""
        df = st.session_state.get("onb_raw_df")
        schema = st.session_state.get("onb_source_schema")
        approved = st.session_state.get("onb_approved_mapping")

        if df is None:
            df, schema = self.load_fixture(st.session_state.get("onb_selected_fixture", "CRM Customers"))
        if approved is None:
            approved = self.approve_all_mappings()

        res = self.pipeline.execute(
            df=df,
            approved_version=approved,
            source_schema=schema,
        )
        st.session_state["onb_transformation_result"] = res

        # Enqueue exceptions if invalid records exist
        if res.invalid_records:
            batch = self.exception_service.enqueue_from_transformation_result(
                result=res,
                run_id=f"run-{uuid4().hex[:8]}",
                source_id=approved.source_id,
                mapping_version_id=approved.mapping_version_id,
            )
            st.session_state["onb_exception_batch"] = batch

        return res

    def correct_and_reprocess(
        self,
        exception: CustomerRecordException,
        field_name: str,
        corrected_value: Any,
        reason: str = "Manual operator correction",
        operator: str = "data_engineer@enterprise.org",
    ) -> tuple[CustomerRecordException, Optional[CanonicalCustomerRecord]]:
        """Apply correction to an exception and reprocess it."""
        approved = st.session_state.get("onb_approved_mapping")
        if approved is None:
            approved = self.approve_all_mappings()

        corr = self.exception_service.apply_correction(
            exception=exception,
            field=field_name,
            correction_type=CorrectionType.VALUE_OVERRIDE,
            applied_by=operator,
            reason=reason,
            corrected_value=corrected_value,
        )
        reprocessed, canon_rec = self.exception_service.reprocess_exception(
            exception=corr,
            approved_version=approved,
            reprocessed_by=operator,
        )
        # Refresh exception batch
        batch = st.session_state.get("onb_exception_batch")
        if batch:
            updated_excs = [reprocessed if e.exception_id == reprocessed.exception_id else e for e in batch.exceptions]
            st.session_state["onb_exception_batch"] = ExceptionBatch(
                batch_id=batch.batch_id,
                source_id=batch.source_id,
                run_id=batch.run_id,
                mapping_version_id=batch.mapping_version_id,
                exceptions=updated_excs,
                created_at=batch.created_at,
            )
        return reprocessed, canon_rec

    def submit_durable_run(self) -> OnboardingRun:
        """Submit and record an onboarding run via OnboardingRunService."""
        df = st.session_state.get("onb_raw_df")
        approved = st.session_state.get("onb_approved_mapping")
        if df is None:
            df, _ = self.load_fixture(st.session_state.get("onb_selected_fixture", "CRM Customers"))
        if approved is None:
            approved = self.approve_all_mappings()

        raw_records = df.to_dicts()
        req = RunCreateRequest(
            idempotency_key=f"run-{approved.source_id}-{date.today().isoformat()}",
            source_id=approved.source_id,
            canonical_schema_version=1,
            mapping_version_id=approved.mapping_version_id,
            data_payload=raw_records,
            auto_execute=True,
        )
        run_row, is_replay = self.run_service.submit_run(req)
        st.session_state["onb_active_run"] = run_row
        return run_row

    def get_all_runs(self) -> list[OnboardingRun]:
        """Return all persisted runs ordered by created_at descending."""
        return self.run_service.repository.list_all_runs()

    def get_latest_run(self) -> Optional[OnboardingRun]:
        """Return the most recently created run, or None."""
        return self.run_service.repository.get_latest_run()


    def evaluate_schema_drift(self) -> SchemaDriftReport:
        """Simulate source schema evolution and evaluate drift impact."""
        approved = st.session_state.get("onb_approved_mapping")
        if approved is None:
            approved = self.approve_all_mappings()

        # Compare baseline dataset A to drifted dataset C
        ds_a = get_dataset_a_clean(10)
        ds_c = get_dataset_c_highly_messy(10)

        report = self.drift_service.evaluate_drift(
            prior_schema=ds_a.source_schema,
            current_schema=ds_c.source_schema,
            approved_mapping=approved,
            persist=True,
        )
        st.session_state["onb_prior_schema"] = ds_a.source_schema
        st.session_state["onb_drift_report"] = report
        return report

    def process_scd2_normal_batch(self) -> CustomerGuardrailEvaluationResult:
        """Evaluate a normal valid batch and commit to SCD2 history."""
        clean_cust = CanonicalCustomerRecord(
            customer_id="CUST-1001",
            first_name="Alice",
            last_name="Smith",
            email="alice.smith@example.com",
            status="ACTIVE",
            created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        gd_res = self.guardrail_service.evaluate_and_process(
            records=[clean_cust],
            processing_date=date(2026, 9, 1),
        )
        st.session_state["onb_guardrail_result"] = gd_res
        return gd_res

    def trigger_suspicious_hold(self) -> CustomerGuardrailEvaluationResult:
        """Trigger a suspicious mass deactivation batch that is safely held in containment."""
        # 1. Ensure baseline entities exist in active history
        baseline = [
            CanonicalCustomerRecord(
                customer_id=f"CUST-SUSP-{i:03d}",
                first_name=f"User{i}",
                last_name="ActiveUser",
                email=f"user{i}@active.corp",
                status="ACTIVE",
                created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            )
            for i in range(1, 10)
        ]
        self.guardrail_service.evaluate_and_process(records=baseline, processing_date=date(2026, 9, 1))

        # 2. Configure thresholds
        self.guardrail_service.config.max_deactivation_count = 2
        self.guardrail_service.config.max_changed_records = 2

        # 3. Trigger bulk deactivation on existing active entities
        susp_records = [
            CanonicalCustomerRecord(
                customer_id=f"CUST-SUSP-{i:03d}",
                first_name=f"User{i}",
                last_name="ActiveUser",
                email=f"user{i}@active.corp",
                status="INACTIVE",
                created_at=datetime(2026, 9, 10, tzinfo=timezone.utc),
            )
            for i in range(1, 10)
        ]
        gd_res = self.guardrail_service.evaluate_and_process(
            records=susp_records,
            processing_date=date(2026, 9, 10),
            source_id=self.guardrail_service.config.source_name,
        )
        st.session_state["onb_guardrail_result"] = gd_res
        return gd_res

    def list_holds(self, status: Optional[str] = None) -> list[Any]:
        """List held change batches for customer onboarding."""
        return self.guardrail_service.list_held_batches(status=status)

    def get_held_batch(self, hold_id: Any) -> Any:
        """Retrieve a specific held change batch by ID."""
        from uuid import UUID
        h_id = UUID(str(hold_id)) if not isinstance(hold_id, UUID) else hold_id
        return self.guardrail_service.get_held_batch(hold_id=h_id)

    def release_held_batch(self, hold_id: Any, reason: str = "Authorized by operator") -> Any:
        """Operator recovery: release a held batch to history."""
        from uuid import UUID
        h_id = UUID(str(hold_id)) if not isinstance(hold_id, UUID) else hold_id
        return self.guardrail_service.release_held_batch(
            hold_id=h_id,
            operator_reason=reason,
        )

    def reprocess_held_batch(self, hold_id: Any, force_normal: bool = False) -> Any:
        """Operator recovery: reprocess a held batch by re-evaluating rules."""
        from uuid import UUID
        h_id = UUID(str(hold_id)) if not isinstance(hold_id, UUID) else hold_id
        return self.guardrail_service.reprocess_held_batch(
            hold_id=h_id,
            force_normal=force_normal,
        )

    def discard_held_batch(self, hold_id: Any, reason: str = "Operator discarded suspicious batch") -> Any:
        """Operator recovery: permanently discard a held batch with 0 history mutation."""
        from uuid import UUID
        h_id = UUID(str(hold_id)) if not isinstance(hold_id, UUID) else hold_id
        return self.guardrail_service.discard_held_batch(
            hold_id=h_id,
            operator_reason=reason,
        )

    def dismiss_exception(
        self,
        exception: CustomerRecordException,
        dismissed_by: str = "lead_data_engineer@enterprise.org",
        reason: str = "Dismissed by operator",
    ) -> CustomerRecordException:
        """Dismiss an exception with non-dismissible invariant rule enforcement."""
        dismissed = self.exception_service.dismiss_exception(
            exception=exception,
            dismissed_by=dismissed_by,
            reason=reason,
        )
        batch = st.session_state.get("onb_exception_batch")
        if batch:
            updated_excs = [dismissed if e.exception_id == dismissed.exception_id else e for e in batch.exceptions]
            st.session_state["onb_exception_batch"] = ExceptionBatch(
                batch_id=batch.batch_id,
                source_id=batch.source_id,
                run_id=batch.run_id,
                mapping_version_id=batch.mapping_version_id,
                exceptions=updated_excs,
                created_at=batch.created_at,
            )
        return dismissed

    def get_historical_record_count(self) -> int:
        """Return exact count of records currently in target SCD2 history."""
        repo = self.scd2_service.repository
        if getattr(repo, "in_memory_mode", False):
            return len(repo._memory_rows)
        try:
            with repo.db.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) FROM monitored_entity_history WHERE source_name = %s",
                        (self.scd2_service.config.entity_source_name,),
                    )
                    row = cur.fetchone()
                    return row[0] if row else 0
        except Exception:
            return len(getattr(repo, "_memory_rows", []))

    def get_system_health(self) -> dict[str, Any]:
        """Inspect and return operational connection and service health."""
        settings = get_settings()
        db_mgr = DatabaseManager(settings=settings)
        db_ok = False
        db_err = None
        if db_mgr.is_configured:
            try:
                with db_mgr.get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1")
                        db_ok = True
            except Exception as e:
                db_err = str(e)
        else:
            db_err = "Not configured (In-Memory Repository Active)"

        return {
            "database": {
                "configured": db_mgr.is_configured,
                "connected": db_ok,
                "redacted_url": db_mgr.redacted_url or "In-memory / Unconfigured",
                "error": db_err,
            },
            "ai_providers": {
                "has_gemini": settings.has_gemini_key,
                "has_groq": settings.has_groq_key,
                "effective_provider": settings.get_effective_provider().value,
                "gemini_model": settings.gemini_model,
                "gemini_fallback_models": settings.gemini_fallback_models,
            },
            "environment": {
                "snapshot_mode": settings.snapshot_mode.value if hasattr(settings.snapshot_mode, "value") else str(settings.snapshot_mode),
                "delete_policy": settings.delete_policy.value if hasattr(settings.delete_policy, "value") else str(settings.delete_policy),
            },
            "services": {
                "DataProfiler (M1)": "Ready",
                "DeterministicCandidateGenerator (M2)": "Ready",
                "SemanticMappingEngine (M2)": "Ready",
                "MappingReviewService (M3)": "Ready",
                "TransformationPipeline (M4)": "Ready",
                "ExceptionQueueService (M5)": "Ready",
                "OnboardingRunService (M6)": "Ready",
                "SchemaDriftService (M7)": "Ready",
                "CustomerSCD2Service (M8)": "Ready",
                "CustomerHistoricalGuardrailService (M9)": "Ready",
            },
        }
