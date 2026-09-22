"""Comprehensive test suite for M8 — SCD2 Integration with deterministic Polars engine."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
import json
from pathlib import Path
import tempfile
import pytest

import polars as pl

from src.scd2_copilot.db.models import MonitoredEntityHistoryRow
from src.scd2_copilot.db.repositories.monitored_entity_history_repository import (
    MonitoredEntityHistoryRepository,
)
from src.scd2_copilot.onboarding.canonical import get_canonical_customer_v1
from src.scd2_copilot.onboarding.exceptions import (
    SCD2InvariantValidationError,
    SchemaCompatibilityError,
)
from src.scd2_copilot.onboarding.models.approval import (
    ApprovedMappingDefinition,
    ApprovedMappingVersion,
    ReviewDecisionType,
)
from src.scd2_copilot.onboarding.models.drift import (
    DriftType,
    FieldMappingImpact,
    MappingCompatibilityState,
    SchemaDiffResult,
    SchemaDriftEvent,
    SchemaDriftReport,
)
from src.scd2_copilot.onboarding.models.mapping import (
    CandidateMapping,
    MappingType,
    TransformationOpType,
    TransformationStep,
)
from src.scd2_copilot.onboarding.models.run import RunCreateRequest, RunStatus
from src.scd2_copilot.onboarding.models.transformation import CanonicalCustomerRecord
from src.scd2_copilot.onboarding.runs.service import OnboardingRunService
from src.scd2_copilot.onboarding.scd2.adapter import CustomerSCD2Adapter
from src.scd2_copilot.onboarding.scd2.models import CustomerSCD2Config, CustomerSCD2ExecutionResult
from src.scd2_copilot.onboarding.scd2.service import CustomerSCD2Service


def _create_canonical_record(
    customer_id: str,
    first_name: str = "Jane",
    last_name: str = "Doe",
    email: str = "jane.doe@example.com",
    date_of_birth: date = date(1990, 5, 15),
    status: str = "ACTIVE",
    created_at: datetime = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
) -> CanonicalCustomerRecord:
    return CanonicalCustomerRecord(
        customer_id=customer_id,
        first_name=first_name,
        last_name=last_name,
        email=email,
        date_of_birth=date_of_birth,
        status=status,
        created_at=created_at,
    )


# ── Core SCD2 Ingestion Tests ────────────────────────────────


def test_initial_customer_load_creates_active_scd2_version() -> None:
    repo = MonitoredEntityHistoryRepository(in_memory=True)
    service = CustomerSCD2Service(repository=repo)

    records = [
        _create_canonical_record("CUST-1001", first_name="Alice", status="ACTIVE"),
        _create_canonical_record("CUST-1002", first_name="Bob", status="ACTIVE"),
        _create_canonical_record("CUST-1003", first_name="Charlie", status="INACTIVE"),
    ]

    proc_date = date(2026, 3, 1)
    result = service.process_canonical_batch(records=records, processing_date=proc_date)

    assert result.total_records_seen == 3
    assert result.new_count == 3
    assert result.changed_count == 0
    assert result.unchanged_count == 0
    assert result.deleted_count == 0
    assert result.persisted_history_rows_count == 3
    assert result.validation_passed is True

    # Inspect active customer state
    alice = service.get_current_customer("CUST-1001")
    assert alice is not None
    assert alice.is_current is True
    assert alice.effective_to is None
    assert alice.effective_from == datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert alice.attributes["first_name"] == "Alice"
    assert alice.attributes["status"] == "ACTIVE"


def test_business_attribute_change_closes_old_and_opens_new_version() -> None:
    repo = MonitoredEntityHistoryRepository(in_memory=True)
    service = CustomerSCD2Service(repository=repo)

    # Day 1: Initial load
    day1_records = [
        _create_canonical_record("CUST-1001", status="ACTIVE", email="alice@old.com"),
    ]
    service.process_canonical_batch(records=day1_records, processing_date=date(2026, 3, 1))

    # Day 2: Business change (ACTIVE -> INACTIVE, updated email)
    day2_records = [
        _create_canonical_record("CUST-1001", status="INACTIVE", email="alice@new.com"),
    ]
    result2 = service.process_canonical_batch(records=day2_records, processing_date=date(2026, 3, 15))

    assert result2.changed_count == 1
    assert result2.new_count == 0
    assert result2.closed_versions_count == 1
    assert result2.new_versions_count == 1

    # Full history for customer
    history = service.get_customer_history("CUST-1001")
    assert len(history) == 2

    v1 = history[0]
    assert v1.is_current is False
    assert v1.effective_from == datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert v1.effective_to == datetime(2026, 3, 15, tzinfo=timezone.utc)
    assert v1.attributes["status"] == "ACTIVE"
    assert v1.attributes["email"] == "alice@old.com"

    v2 = history[1]
    assert v2.is_current is True
    assert v2.effective_from == datetime(2026, 3, 15, tzinfo=timezone.utc)
    assert v2.effective_to is None
    assert v2.attributes["status"] == "INACTIVE"
    assert v2.attributes["email"] == "alice@new.com"


def test_unchanged_customer_produces_no_new_version() -> None:
    repo = MonitoredEntityHistoryRepository(in_memory=True)
    service = CustomerSCD2Service(repository=repo)

    day1 = [_create_canonical_record("CUST-1001", first_name="Alice", status="ACTIVE")]
    service.process_canonical_batch(records=day1, processing_date=date(2026, 3, 1))

    # Day 2: Identical business values
    day2 = [_create_canonical_record("CUST-1001", first_name="Alice", status="ACTIVE")]
    result2 = service.process_canonical_batch(records=day2, processing_date=date(2026, 3, 5))

    assert result2.unchanged_count == 1
    assert result2.changed_count == 0
    assert result2.new_count == 0
    assert result2.persisted_history_rows_count == 0

    history = service.get_customer_history("CUST-1001")
    assert len(history) == 1
    assert history[0].is_current is True
    assert history[0].effective_from == datetime(2026, 3, 1, tzinfo=timezone.utc)


def test_idempotent_batch_replay() -> None:
    repo = MonitoredEntityHistoryRepository(in_memory=True)
    service = CustomerSCD2Service(repository=repo)

    batch = [
        _create_canonical_record("CUST-001", first_name="Alice"),
        _create_canonical_record("CUST-002", first_name="Bob"),
        _create_canonical_record("CUST-003", first_name="Charlie"),
    ]

    r1 = service.process_canonical_batch(records=batch, processing_date=date(2026, 3, 1))
    assert r1.new_count == 3
    assert r1.persisted_history_rows_count == 3

    # Exact replay
    r2 = service.process_canonical_batch(records=batch, processing_date=date(2026, 3, 1))
    assert r2.unchanged_count == 3
    assert r2.new_count == 0
    assert r2.changed_count == 0
    assert r2.closed_versions_count == 0
    assert r2.persisted_history_rows_count == 0

    # Total rows in repo remain 3
    assert len(repo.fetch_history_for_key("customer", {"customer_id": "CUST-001"})) == 1
    assert len(repo.fetch_history_for_key("customer", {"customer_id": "CUST-002"})) == 1
    assert len(repo.fetch_history_for_key("customer", {"customer_id": "CUST-003"})) == 1


# ── Metadata Isolation Tests ─────────────────────────────────


def test_metadata_changes_do_not_trigger_scd2_history() -> None:
    """Operational metadata (run_id, mapping_version_id, created_at, schema fingerprint)

    MUST NEVER cause artificial SCD2 version creation.
    """
    repo = MonitoredEntityHistoryRepository(in_memory=True)
    service = CustomerSCD2Service(repository=repo)

    # Run 1: Source system CRM, mapping v1, timestamp Jan 1
    rec1 = _create_canonical_record(
        "CUST-1001",
        first_name="Alice",
        status="ACTIVE",
        created_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
    )
    r1 = service.process_canonical_batch(
        records=[rec1],
        processing_date=date(2026, 3, 1),
        run_id="onb_run_001",
        batch_id="batch_crm_v1",
        source_id="crm_system",
    )
    assert r1.new_count == 1

    # Run 2: Source system Billing, mapping v2, completely different created_at
    rec2 = _create_canonical_record(
        "CUST-1001",
        first_name="Alice",  # Identical business attributes
        status="ACTIVE",
        created_at=datetime(2026, 3, 10, 8, 30, 0, tzinfo=timezone.utc),  # Metadata difference
    )
    r2 = service.process_canonical_batch(
        records=[rec2],
        processing_date=date(2026, 3, 10),
        run_id="onb_run_999",  # Metadata difference
        batch_id="batch_billing_v2",  # Metadata difference
        source_id="billing_system",  # Metadata difference
    )

    # Crucial assertion: Must be UNCHANGED
    assert r2.unchanged_count == 1
    assert r2.changed_count == 0
    assert r2.new_count == 0
    assert r2.persisted_history_rows_count == 0

    history = service.get_customer_history("CUST-1001")
    assert len(history) == 1
    assert history[0].effective_from == datetime(2026, 3, 1, tzinfo=timezone.utc)


# ── Schema Drift Gating Tests ────────────────────────────────


def test_broken_schema_drift_blocks_scd2_integration() -> None:
    repo = MonitoredEntityHistoryRepository(in_memory=True)
    service = CustomerSCD2Service(repository=repo)

    records = [_create_canonical_record("CUST-1001")]

    # Create a BROKEN drift report
    drift_event = SchemaDriftEvent(
        drift_type=DriftType.REMOVED_COLUMN,
        field_name="email_addr",
        details="Crucial mapped column removed",
    )
    broken_report = SchemaDriftReport(
        report_id="drift_rep_01",
        source_id="crm_source",
        prior_schema_version=1,
        current_schema_version=2,
        prior_fingerprint="fp1",
        current_fingerprint="fp2",
        mapping_version_id="map_v1",
        overall_compatibility=MappingCompatibilityState.BROKEN,
        summary_reason="Crucial mapped column removed",
        drift_events=[drift_event],
        impacted_mappings=[
            FieldMappingImpact(
                source_field="email_addr",
                target_field="email",
                mapping_version_id="map_v1",
                impact_category=DriftType.REMOVED_COLUMN,
                compatibility=MappingCompatibilityState.BROKEN,
                reason="Source column was removed",
            )
        ],
    )

    with pytest.raises(SchemaCompatibilityError) as exc_info:
        service.process_canonical_batch(
            records=records,
            processing_date=date(2026, 3, 1),
            drift_report=broken_report,
        )

    assert "SCD2 processing blocked: Schema drift status is BROKEN" in str(exc_info.value)
    # Ensure zero side effects in repository
    assert service.get_customer_history("CUST-1001") == []


def test_compatible_schema_drift_allows_scd2_integration() -> None:
    repo = MonitoredEntityHistoryRepository(in_memory=True)
    service = CustomerSCD2Service(repository=repo)

    records = [_create_canonical_record("CUST-1001")]

    compatible_report = SchemaDriftReport(
        report_id="drift_rep_02",
        source_id="crm_source",
        prior_schema_version=1,
        current_schema_version=2,
        prior_fingerprint="fp1",
        current_fingerprint="fp2",
        mapping_version_id="map_v1",
        overall_compatibility=MappingCompatibilityState.COMPATIBLE,
        summary_reason="All fields compatible",
        drift_events=[],
        impacted_mappings=[],
    )

    result = service.process_canonical_batch(
        records=records,
        processing_date=date(2026, 3, 1),
        drift_report=compatible_report,
    )
    assert result.new_count == 1
    assert result.persisted_history_rows_count == 1



# ── Half-Open Temporal Semantics & Point-in-Time Tests ────────


def test_half_open_temporal_interval_and_point_in_time_queries() -> None:
    """Validate half-open interval semantics [effective_from, effective_to).

    V1: [2026-01-01, 2026-01-10) -> status = 'ACTIVE'
    V2: [2026-01-10, 2026-01-20) -> status = 'SUSPENDED'
    V3: [2026-01-20, None)       -> status = 'INACTIVE'
    """
    repo = MonitoredEntityHistoryRepository(in_memory=True)
    service = CustomerSCD2Service(repository=repo)

    # 1. Day 2026-01-01: V1
    service.process_canonical_batch(
        records=[_create_canonical_record("CUST-777", status="ACTIVE")],
        processing_date=date(2026, 1, 1),
    )

    # 2. Day 2026-01-10: V2
    service.process_canonical_batch(
        records=[_create_canonical_record("CUST-777", status="SUSPENDED")],
        processing_date=date(2026, 1, 10),
    )

    # 3. Day 2026-01-20: V3
    service.process_canonical_batch(
        records=[_create_canonical_record("CUST-777", status="INACTIVE")],
        processing_date=date(2026, 1, 20),
    )

    # Query before start of history
    t_pre = datetime(2025, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    pit_pre = service.get_customer_point_in_time("CUST-777", as_of=t_pre)
    assert pit_pre.found is False

    # Query middle of V1
    t_v1 = datetime(2026, 1, 5, 12, 0, 0, tzinfo=timezone.utc)
    pit_v1 = service.get_customer_point_in_time("CUST-777", as_of=t_v1)
    assert pit_v1.found is True
    assert pit_v1.attributes["status"] == "ACTIVE"
    assert pit_v1.effective_from == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert pit_v1.effective_to == datetime(2026, 1, 10, tzinfo=timezone.utc)

    # Query EXACT boundary instant 2026-01-10 00:00:00 UTC
    # Under [from, to), boundary instant belongs STRICTLY to V2!
    t_boundary_1 = datetime(2026, 1, 10, 0, 0, 0, tzinfo=timezone.utc)
    pit_b1 = service.get_customer_point_in_time("CUST-777", as_of=t_boundary_1)
    assert pit_b1.found is True
    assert pit_b1.attributes["status"] == "SUSPENDED"
    assert pit_b1.effective_from == datetime(2026, 1, 10, tzinfo=timezone.utc)
    assert pit_b1.effective_to == datetime(2026, 1, 20, tzinfo=timezone.utc)

    # Query middle of V2
    t_v2 = datetime(2026, 1, 15, 0, 0, 0, tzinfo=timezone.utc)
    pit_v2 = service.get_customer_point_in_time("CUST-777", as_of=t_v2)
    assert pit_v2.found is True
    assert pit_v2.attributes["status"] == "SUSPENDED"

    # Query EXACT boundary instant 2026-01-20 00:00:00 UTC -> belongs to V3!
    t_boundary_2 = datetime(2026, 1, 20, 0, 0, 0, tzinfo=timezone.utc)
    pit_b2 = service.get_customer_point_in_time("CUST-777", as_of=t_boundary_2)
    assert pit_b2.found is True
    assert pit_b2.attributes["status"] == "INACTIVE"
    assert pit_b2.effective_from == datetime(2026, 1, 20, tzinfo=timezone.utc)
    assert pit_b2.effective_to is None
    assert pit_b2.is_current is True

    # Query far future
    t_future = datetime(2027, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    pit_future = service.get_customer_point_in_time("CUST-777", as_of=t_future)
    assert pit_future.found is True
    assert pit_future.attributes["status"] == "INACTIVE"


# ── Multi-Source Standardisation Tests ───────────────────────


def test_multi_source_feeds_update_same_canonical_customer() -> None:
    """Verify that heterogeneous source systems (CRM, Billing, Support)

    mapping into the single canonical customer_id track unified customer history.
    """
    repo = MonitoredEntityHistoryRepository(in_memory=True)
    service = CustomerSCD2Service(repository=repo)

    # Feed 1: CRM creates customer
    crm_record = _create_canonical_record(
        customer_id="CUST-8888",
        first_name="David",
        last_name="Miller",
        email="david@crm.org",
        status="ACTIVE",
    )
    service.process_canonical_batch(
        records=[crm_record],
        processing_date=date(2026, 2, 1),
        source_id="crm",
    )

    # Feed 2: Billing system updates status
    billing_record = _create_canonical_record(
        customer_id="CUST-8888",
        first_name="David",
        last_name="Miller",
        email="david@crm.org",
        status="INACTIVE",
    )
    service.process_canonical_batch(
        records=[billing_record],
        processing_date=date(2026, 2, 15),
        source_id="billing",
    )

    # Feed 3: Support system updates contact email
    support_record = _create_canonical_record(
        customer_id="CUST-8888",
        first_name="David",
        last_name="Miller",
        email="david.miller@support.org",
        status="INACTIVE",
    )
    service.process_canonical_batch(
        records=[support_record],
        processing_date=date(2026, 3, 1),
        source_id="support",
    )

    history = service.get_customer_history("CUST-8888")
    assert len(history) == 3
    assert [h.attributes["status"] for h in history] == ["ACTIVE", "INACTIVE", "INACTIVE"]
    assert [h.attributes["email"] for h in history] == [
        "david@crm.org",
        "david@crm.org",
        "david.miller@support.org",
    ]


# ── Onboarding Run Integration Tests ─────────────────────────


def test_onboarding_run_service_integrate_run_to_scd2() -> None:
    """Verify end-to-end integration: OnboardingRun -> Canonical Validation -> SCD2 Engine."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        repo = MonitoredEntityHistoryRepository(in_memory=True)
        scd2_service = CustomerSCD2Service(repository=repo)
        run_service = OnboardingRunService(
            artifacts_root=tmp_path / "runs",
            scd2_service=scd2_service,
        )

        # 1. Register an approved mapping version
        mappings = [
            ApprovedMappingDefinition(
                source_field="cust_id",
                target_field="customer_id",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="admin@example.com",
                confidence=1.0,
                provenance_reason="ID direct",
            ),
            ApprovedMappingDefinition(
                source_field="first",
                target_field="first_name",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="admin@example.com",
                confidence=1.0,
                provenance_reason="First name direct",
            ),
            ApprovedMappingDefinition(
                source_field="last",
                target_field="last_name",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="admin@example.com",
                confidence=1.0,
                provenance_reason="Last name direct",
            ),
            ApprovedMappingDefinition(
                source_field="email_address",
                target_field="email",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="admin@example.com",
                confidence=1.0,
                provenance_reason="Email direct",
            ),
            ApprovedMappingDefinition(
                source_field="state",
                target_field="status",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="admin@example.com",
                confidence=1.0,
                provenance_reason="Status direct",
            ),
            ApprovedMappingDefinition(
                source_field="signup_date",
                target_field="created_at",
                mapping_type=MappingType.TRANSFORMED,
                transformations=[TransformationStep(op=TransformationOpType.PARSE_DATE)],
                decision=ReviewDecisionType.APPROVE,
                reviewer="admin@example.com",
                confidence=1.0,
                provenance_reason="Parse created_at date",
            ),
        ]
        approved_version = ApprovedMappingVersion(
            mapping_version_id="map_v1_crm_test",
            source_id="crm_csv",
            source_fingerprint="fp_crm_test",
            canonical_schema_name="customer",
            canonical_schema_version=1,
            version_number=1,
            mappings=mappings,
            is_complete=True,
            approved_by="admin@example.com",
        )
        run_service.register_approved_mapping(approved_version)

        # 2. Prepare sample CSV payload and execute run
        csv_file = tmp_path / "crm_data.csv"
        csv_file.write_text(
            "cust_id,first,last,email_address,state,signup_date\n"
            "CUST-501,Diana,Prince,diana@themyscira.com,ACTIVE,2026-01-01\n"
            "CUST-502,Bruce,Wayne,bruce@wayne.com,ACTIVE,2026-01-01\n",
            encoding="utf-8",
        )

        req = RunCreateRequest(
            idempotency_key="RUN-IDEMP-SCD2-001",
            source_id="crm_csv",
            file_path=str(csv_file),
            mapping_version_id=approved_version.mapping_version_id,
        )

        completed_run, _ = run_service.submit_run(req)
        assert completed_run.status == RunStatus.COMPLETED

        # 3. Integrate with SCD2
        scd2_result = run_service.integrate_run_to_scd2(
            run_id=completed_run.run_id,
            processing_date=date(2026, 4, 1),
        )

        assert scd2_result.new_count == 2
        assert scd2_result.persisted_history_rows_count == 2
        assert completed_run.artifact_paths.extra.get("scd2_execution_path") is not None

        # Verify historical record in repo
        diana = scd2_service.get_current_customer("CUST-501")
        assert diana is not None
        assert diana.attributes["first_name"] == "Diana"
        assert diana.attributes["status"] == "ACTIVE"


def test_empty_record_batch_returns_empty_result() -> None:
    repo = MonitoredEntityHistoryRepository(in_memory=True)
    service = CustomerSCD2Service(repository=repo)

    result = service.process_canonical_batch(records=[], processing_date=date(2026, 3, 1))
    assert result.total_records_seen == 0
    assert result.new_count == 0
    assert result.persisted_history_rows_count == 0


def test_mixed_batch_new_changed_unchanged() -> None:
    """Verify a single batch containing a mixture of NEW, CHANGED, and UNCHANGED customers."""
    repo = MonitoredEntityHistoryRepository(in_memory=True)
    service = CustomerSCD2Service(repository=repo)

    # Day 1: CUST-1, CUST-2
    service.process_canonical_batch(
        records=[
            _create_canonical_record("CUST-1", status="ACTIVE"),
            _create_canonical_record("CUST-2", status="ACTIVE"),
        ],
        processing_date=date(2026, 3, 1),
    )

    # Day 2: Mixed batch
    # CUST-1: CHANGED (ACTIVE -> INACTIVE)
    # CUST-2: UNCHANGED (ACTIVE -> ACTIVE)
    # CUST-3: NEW
    mixed_batch = [
        _create_canonical_record("CUST-1", status="INACTIVE"),
        _create_canonical_record("CUST-2", status="ACTIVE"),
        _create_canonical_record("CUST-3", status="ACTIVE"),
    ]
    result = service.process_canonical_batch(
        records=mixed_batch,
        processing_date=date(2026, 3, 15),
    )

    assert result.total_records_seen == 3
    assert result.new_count == 1
    assert result.changed_count == 1
    assert result.unchanged_count == 1
    assert result.closed_versions_count == 1
    assert result.new_versions_count == 2
    assert result.persisted_history_rows_count == 2

    # Verify CUST-1 history has 2 versions
    c1_hist = service.get_customer_history("CUST-1")
    assert len(c1_hist) == 2
    assert c1_hist[0].is_current is False
    assert c1_hist[1].is_current is True

    # Verify CUST-2 history has 1 version
    c2_hist = service.get_customer_history("CUST-2")
    assert len(c2_hist) == 1
    assert c2_hist[0].is_current is True

    # Verify CUST-3 history has 1 version
    c3_hist = service.get_customer_history("CUST-3")
    assert len(c3_hist) == 1
    assert c3_hist[0].is_current is True


def test_scd2_validation_invariant_failure_blocks_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that if SCD2 invariant validation fails, no history changes are committed."""
    from src.scd2_copilot.models import ValidationReport, ValidationRule, ValidationStatus

    repo = MonitoredEntityHistoryRepository(in_memory=True)
    service = CustomerSCD2Service(repository=repo)

    failing_rule = ValidationRule(
        name="date_consistency",
        status=ValidationStatus.FAIL,
        message="Simulated date inconsistency failure",
    )
    mock_report = ValidationReport(rules=[failing_rule])

    # Monkeypatch execute_scd2 to return failing validation report
    orig_execute = service.adapter.execute_scd2

    def fake_execute(*args: Any, **kwargs: Any) -> Any:
        df, change_rep, _ = orig_execute(*args, **kwargs)
        return df, change_rep, mock_report

    monkeypatch.setattr(service.adapter, "execute_scd2", fake_execute)

    records = [_create_canonical_record("CUST-FAIL-01")]
    with pytest.raises(SCD2InvariantValidationError) as exc_info:
        service.process_canonical_batch(records=records, processing_date=date(2026, 3, 1))

    assert "Simulated date inconsistency failure" in str(exc_info.value)
    # Ensure zero side effects in repository
    assert service.get_customer_history("CUST-FAIL-01") == []


def test_save_artifact_persists_valid_json() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        dest = Path(tmp_dir) / "scd2_result.json"
        repo = MonitoredEntityHistoryRepository(in_memory=True)
        service = CustomerSCD2Service(repository=repo)

        records = [_create_canonical_record("CUST-ART-01")]
        result = service.process_canonical_batch(
            records=records,
            processing_date=date(2026, 3, 1),
            artifact_path=dest,
        )

        assert dest.exists()
        with open(dest, "r", encoding="utf-8") as f:
            data = json.load(f)

        assert data["execution_id"] == result.execution_id
        assert data["new_count"] == 1
        assert data["total_records_seen"] == 1
        assert data["validation_passed"] is True
