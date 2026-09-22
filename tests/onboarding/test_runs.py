"""Unit and integration tests for M6 Onboarding Runs, state machine, fingerprinting, and idempotency."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import pytest

import polars as pl

from src.scd2_copilot.onboarding.canonical import get_canonical_customer_v1
from src.scd2_copilot.onboarding.exceptions import (
    IdempotencyConflictError,
    InvalidRunStateTransitionError,
    RunExecutionError,
    RunNotFoundError,
)
from src.scd2_copilot.onboarding.models.approval import (
    ApprovedMappingDefinition,
    ApprovedMappingVersion,
    ReviewDecisionType,
)
from src.scd2_copilot.onboarding.models.mapping import (
    CandidateMapping,
    MappingType,
    TransformationOpType,
    TransformationStep,
)
from src.scd2_copilot.onboarding.models.run import (
    OnboardingRun,
    RunArtifactPaths,
    RunCreateRequest,
    RunMetrics,
    RunStatus,
    VALID_RUN_TRANSITIONS,
    validate_run_transition,
)
from src.scd2_copilot.onboarding.runs.fingerprint import (
    compute_input_fingerprint,
    compute_request_fingerprint,
)
from src.scd2_copilot.onboarding.runs.repository import OnboardingRunRepository
from src.scd2_copilot.onboarding.runs.service import OnboardingRunService


# ── Domain Model & State Machine Tests ─────────────────────


def test_run_model_defaults_and_serialization() -> None:
    now = datetime.now(timezone.utc)
    run = OnboardingRun(
        idempotency_key="TEST-KEY-001",
        request_fingerprint="fp_abc123",
        source_id="crm_system",
    )
    assert run.run_id.startswith("onb_run_")
    assert run.status == RunStatus.CREATED
    assert run.canonical_schema_version == 1
    assert run.metrics.total_records == 0
    assert run.started_at is None
    assert run.completed_at is None

    # Test serialization round-trip
    run_dict = run.to_dict()
    assert run_dict["idempotency_key"] == "TEST-KEY-001"
    assert run_dict["request_fingerprint"] == "fp_abc123"

    loaded = OnboardingRun.from_dict(run_dict)
    assert loaded.run_id == run.run_id
    assert loaded.idempotency_key == run.idempotency_key
    assert loaded.status == RunStatus.CREATED


def test_valid_state_machine_transitions() -> None:
    run = OnboardingRun(
        idempotency_key="TEST-KEY-002",
        request_fingerprint="fp_123",
        source_id="crm_system",
    )

    # Standard forward progression
    run.transition_to(RunStatus.PROFILING)
    assert run.status == RunStatus.PROFILING
    assert run.started_at is not None

    run.transition_to(RunStatus.MAPPING_PENDING)
    assert run.status == RunStatus.MAPPING_PENDING

    run.transition_to(RunStatus.APPROVAL_PENDING)
    assert run.status == RunStatus.APPROVAL_PENDING

    run.transition_to(RunStatus.TRANSFORMING)
    assert run.status == RunStatus.TRANSFORMING

    run.transition_to(RunStatus.VALIDATING)
    assert run.status == RunStatus.VALIDATING

    run.transition_to(RunStatus.COMPLETED)
    assert run.status == RunStatus.COMPLETED
    assert run.completed_at is not None


def test_fast_path_and_partial_transitions() -> None:
    # Fast path: CREATED directly to TRANSFORMING when pre-approved mapping exists
    run = OnboardingRun(
        idempotency_key="FAST-PATH",
        request_fingerprint="fp_fast",
        source_id="crm_system",
    )
    run.transition_to(RunStatus.TRANSFORMING)
    assert run.status == RunStatus.TRANSFORMING

    run.transition_to(RunStatus.VALIDATING)
    run.transition_to(RunStatus.PARTIAL)
    assert run.status == RunStatus.PARTIAL
    assert run.completed_at is not None


def test_illegal_state_transitions_raise_error() -> None:
    run = OnboardingRun(
        idempotency_key="ILLEGAL-TEST",
        request_fingerprint="fp_ill",
        source_id="crm_system",
    )
    run.transition_to(RunStatus.PROFILING)
    run.transition_to(RunStatus.MAPPING_PENDING)
    run.transition_to(RunStatus.APPROVAL_PENDING)
    run.transition_to(RunStatus.TRANSFORMING)
    run.transition_to(RunStatus.VALIDATING)
    run.transition_to(RunStatus.COMPLETED)

    # Terminal state cannot transition anywhere
    with pytest.raises(InvalidRunStateTransitionError) as exc:
        run.transition_to(RunStatus.PROFILING)
    assert "Cannot transition onboarding run from 'COMPLETED' to 'PROFILING'" in str(exc.value)

    # Direct jump from CREATED to VALIDATING is forbidden
    fresh_run = OnboardingRun(
        idempotency_key="JUMP-TEST",
        request_fingerprint="fp_jump",
        source_id="crm_system",
    )
    with pytest.raises(InvalidRunStateTransitionError):
        fresh_run.transition_to(RunStatus.VALIDATING)


def test_retry_transition_from_failed_only() -> None:
    run = OnboardingRun(
        idempotency_key="RETRY-TEST",
        request_fingerprint="fp_ret",
        source_id="crm_system",
    )
    run.transition_to(RunStatus.FAILED, error_message="Network glitch")
    assert run.status == RunStatus.FAILED

    # FAILED can transition back to CREATED for retry
    run.transition_to(RunStatus.CREATED)
    assert run.status == RunStatus.CREATED


# ── Fingerprint Tests ──────────────────────────────────────


def test_input_fingerprint_determinism() -> None:
    data_payload = [
        {"customer_id": "C-101", "email": "alice@example.com", "name": "Alice"},
        {"customer_id": "C-102", "email": "bob@example.com", "name": "Bob"},
    ]
    # Inverted key order should produce exact same hash because keys are sorted
    inverted_payload = [
        {"name": "Alice", "customer_id": "C-101", "email": "alice@example.com"},
        {"email": "bob@example.com", "name": "Bob", "customer_id": "C-102"},
    ]

    fp1 = compute_input_fingerprint(data_payload)
    fp2 = compute_input_fingerprint(inverted_payload)
    assert fp1 == fp2
    assert len(fp1) == 64

    # File fingerprint matching bytes
    with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".csv") as f:
        f.write("col_a,col_b\n1,2\n3,4\n")
        tmp_path = Path(f.name)

    try:
        file_fp = compute_input_fingerprint(tmp_path)
        with open(tmp_path, "rb") as bf:
            bytes_fp = compute_input_fingerprint(bf.read())
        assert file_fp == bytes_fp
    finally:
        tmp_path.unlink(missing_ok=True)


def test_request_fingerprint_determinism_and_sensitivity() -> None:
    base_fp = compute_request_fingerprint(
        source_id="crm_north",
        input_fingerprint="input_abc",
        canonical_schema_version=1,
        mapping_version_id="map_v1",
        options={"allow_partial": True},
    )

    # Identical parameters yield same hash
    identical_fp = compute_request_fingerprint(
        source_id="crm_north",
        input_fingerprint="input_abc",
        canonical_schema_version=1,
        mapping_version_id="map_v1",
        options={"allow_partial": True},
    )
    assert base_fp == identical_fp

    # Sensitivity tests: changing any parameter changes the fingerprint
    assert base_fp != compute_request_fingerprint(
        source_id="crm_south",  # different source
        input_fingerprint="input_abc",
        canonical_schema_version=1,
        mapping_version_id="map_v1",
        options={"allow_partial": True},
    )
    assert base_fp != compute_request_fingerprint(
        source_id="crm_north",
        input_fingerprint="input_diff",  # different content
        canonical_schema_version=1,
        mapping_version_id="map_v1",
        options={"allow_partial": True},
    )
    assert base_fp != compute_request_fingerprint(
        source_id="crm_north",
        input_fingerprint="input_abc",
        canonical_schema_version=2,  # different canonical schema version
        mapping_version_id="map_v1",
        options={"allow_partial": True},
    )
    assert base_fp != compute_request_fingerprint(
        source_id="crm_north",
        input_fingerprint="input_abc",
        canonical_schema_version=1,
        mapping_version_id="map_v2",  # different mapping version
        options={"allow_partial": True},
    )


# ── Repository & Concurrency Tests ─────────────────────────


def test_repository_in_memory_crud() -> None:
    repo = OnboardingRunRepository(in_memory=True)
    repo.clear()

    run = OnboardingRun(
        run_id="onb_run_test1",
        idempotency_key="KEY-001",
        request_fingerprint="fp_test1",
        source_id="crm",
    )
    repo.create(run)

    fetched = repo.get_by_id("onb_run_test1")
    assert fetched is not None
    assert fetched.run_id == "onb_run_test1"

    by_key = repo.get_by_idempotency_key("KEY-001")
    assert by_key is not None
    assert by_key.run_id == "onb_run_test1"

    # Update
    run.transition_to(RunStatus.PROFILING)
    run.metrics.total_records = 100
    repo.update(run)

    updated = repo.get_by_id("onb_run_test1")
    assert updated is not None
    assert updated.status == RunStatus.PROFILING
    assert updated.metrics.total_records == 100

    # List & Filter
    runs, total = repo.list_runs(source_id="crm", status=RunStatus.PROFILING)
    assert total == 1
    assert len(runs) == 1

    runs_empty, total_empty = repo.list_runs(source_id="other_source")
    assert total_empty == 0
    assert len(runs_empty) == 0


def test_repository_idempotent_replay_and_conflict() -> None:
    repo = OnboardingRunRepository(in_memory=True)
    repo.clear()

    run1 = OnboardingRun(
        run_id="onb_run_first",
        idempotency_key="IDEMP-UNIQUE-1",
        request_fingerprint="fp_exact_match",
        source_id="crm",
    )
    created1 = repo.create(run1)

    # 1. Exact match replayed: returns existing without error
    duplicate = OnboardingRun(
        run_id="onb_run_second",
        idempotency_key="IDEMP-UNIQUE-1",
        request_fingerprint="fp_exact_match",
        source_id="crm",
    )
    result = repo.create(duplicate)
    assert result.run_id == created1.run_id  # Reused original run

    # 2. Different fingerprint with same idempotency key: explicit conflict
    conflicting = OnboardingRun(
        run_id="onb_run_third",
        idempotency_key="IDEMP-UNIQUE-1",
        request_fingerprint="fp_different_payload",
        source_id="crm",
    )
    with pytest.raises(IdempotencyConflictError) as exc:
        repo.create(conflicting)
    assert "previously used with a different request fingerprint" in str(exc.value)


def test_repository_multithreaded_concurrency_safety() -> None:
    repo = OnboardingRunRepository(in_memory=True)
    repo.clear()

    key = "CONCURRENT-KEY-999"
    fp = "fp_concurrent_shared"

    def _submit(idx: int) -> str:
        run = OnboardingRun(
            run_id=f"onb_run_t_{idx}",
            idempotency_key=key,
            request_fingerprint=fp,
            source_id="billing",
        )
        saved = repo.create(run)
        return saved.run_id

    # 10 concurrent threads submitting with the exact same idempotency key
    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(_submit, range(10)))

    # Invariant: All threads receive the EXACT SAME run_id
    assert len(set(results)) == 1
    # Exactly 1 run persisted in repository
    all_runs, total = repo.list_runs()
    assert total == 1


# ── OnboardingRunService Pipeline Execution Tests ───────────


def _create_crm_mapping(source_schema_fp: str) -> ApprovedMappingVersion:
    """Helper to construct an approved mapping version for CRM sample data."""
    mappings = [
        ApprovedMappingDefinition(
            source_field="cust_id",
            target_field="customer_id",
            mapping_type=MappingType.DIRECT,
            transformations=[TransformationStep(op=TransformationOpType.TRIM)],
            decision=ReviewDecisionType.APPROVE,
            reviewer="lead.auditor@company.com",
            confidence=1.0,
            provenance_reason="Exact match",
        ),
        ApprovedMappingDefinition(
            source_field="first_name",
            target_field="first_name",
            mapping_type=MappingType.DIRECT,
            transformations=[TransformationStep(op=TransformationOpType.TRIM)],
            decision=ReviewDecisionType.APPROVE,
            reviewer="lead.auditor@company.com",
            confidence=1.0,
            provenance_reason="Exact match",
        ),
        ApprovedMappingDefinition(
            source_field="last_name",
            target_field="last_name",
            mapping_type=MappingType.DIRECT,
            transformations=[TransformationStep(op=TransformationOpType.TRIM)],
            decision=ReviewDecisionType.APPROVE,
            reviewer="lead.auditor@company.com",
            confidence=1.0,
            provenance_reason="Exact match",
        ),
        ApprovedMappingDefinition(
            source_field="email_address",
            target_field="email",
            mapping_type=MappingType.TRANSFORMED,
            transformations=[TransformationStep(op=TransformationOpType.NORMALIZE_EMAIL)],
            decision=ReviewDecisionType.APPROVE,
            reviewer="lead.auditor@company.com",
            confidence=1.0,
            provenance_reason="Normalize email",
        ),
        ApprovedMappingDefinition(
            source_field="signup_date",
            target_field="created_at",
            mapping_type=MappingType.TRANSFORMED,
            transformations=[TransformationStep(op=TransformationOpType.PARSE_DATE)],
            decision=ReviewDecisionType.APPROVE,
            reviewer="lead.auditor@company.com",
            confidence=1.0,
            provenance_reason="Parse date",
        ),
        ApprovedMappingDefinition(
            source_field="acc_status",
            target_field="status",
            mapping_type=MappingType.TRANSFORMED,
            transformations=[TransformationStep(op=TransformationOpType.UPPERCASE)],
            decision=ReviewDecisionType.APPROVE,
            reviewer="lead.auditor@company.com",
            confidence=1.0,
            provenance_reason="Uppercase status",
        ),
    ]
    return ApprovedMappingVersion(
        mapping_version_id=f"map_v1_test_{source_schema_fp[:8]}",
        source_id="crm_fixture",
        source_fingerprint=source_schema_fp,
        canonical_schema_name="customer",
        canonical_schema_version=1,
        version_number=1,
        mappings=mappings,
        is_complete=True,
        approved_by="lead.auditor@company.com",
    )


def test_service_halts_at_approval_pending_when_mapping_missing() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = OnboardingRunService(artifacts_root=tmp_dir)

        payload = [
            {"cust_id": "C-1", "first_name": "Alice", "last_name": "Smith", "email_address": "alice@test.com", "signup_date": "2026-01-01", "acc_status": "ACTIVE"},
            {"cust_id": "C-2", "first_name": "Bob", "last_name": "Jones", "email_address": "bob@test.com", "signup_date": "2026-01-02", "acc_status": "ACTIVE"},
        ]

        req = RunCreateRequest(
            source_id="crm_fresh",
            idempotency_key="RUN-HALT-001",
            data_payload=payload,
            auto_execute=True,
        )

        run, is_replay = service.submit_run(req)
        assert is_replay is False
        # Invariant: Unapproved source MUST halt at APPROVAL_PENDING per M3 gate
        assert run.status == RunStatus.APPROVAL_PENDING
        assert run.source_schema_fingerprint is not None
        assert run.artifact_paths.profile_path is not None
        assert run.artifact_paths.proposal_path is not None
        assert Path(run.artifact_paths.profile_path).is_file()
        assert Path(run.artifact_paths.proposal_path).is_file()


def test_service_executes_to_completed_with_clean_data() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = OnboardingRunService(artifacts_root=tmp_dir)

        payload = [
            {"cust_id": "C-101", "first_name": "Alice", "last_name": "Green", "email_address": "alice@company.com", "signup_date": "2026-01-01", "acc_status": "ACTIVE"},
            {"cust_id": "C-102", "first_name": "Bob", "last_name": "Brown", "email_address": "bob@company.com", "signup_date": "2026-01-02", "acc_status": "ACTIVE"},
        ]

        # First profile to get schema fingerprint
        df = pl.DataFrame(payload)
        profile = service.profiler.profile_dataframe(source_id="crm_clean", df=df)
        schema_fp = profile.fingerprint_hash

        # Pre-approve mapping
        approved = _create_crm_mapping(schema_fp)
        service.register_approved_mapping(approved)

        req = RunCreateRequest(
            source_id="crm_clean",
            idempotency_key="CLEAN-RUN-001",
            data_payload=payload,
            mapping_version_id=approved.mapping_version_id,
            auto_execute=True,
        )

        run, is_replay = service.submit_run(req)
        assert is_replay is False
        assert run.status == RunStatus.COMPLETED
        assert run.metrics.total_records == 2
        assert run.metrics.valid_records == 2
        assert run.metrics.exception_records == 0
        assert run.artifact_paths.result_path is not None


def test_service_executes_to_partial_and_enqueues_exceptions() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = OnboardingRunService(artifacts_root=tmp_dir)

        # 1 valid record, 1 invalid record (malformed email)
        payload = [
            {"cust_id": "C-201", "first_name": "Charlie", "last_name": "Day", "email_address": "charlie@valid.com", "signup_date": "2026-01-01", "acc_status": "ACTIVE"},
            {"cust_id": "C-202", "first_name": "Dennis", "last_name": "Reynolds", "email_address": "not-an-email", "signup_date": "2026-01-02", "acc_status": "ACTIVE"},
        ]

        df = pl.DataFrame(payload)
        profile = service.profiler.profile_dataframe(source_id="crm_partial", df=df)
        schema_fp = profile.fingerprint_hash

        approved = _create_crm_mapping(schema_fp)
        service.register_approved_mapping(approved)

        req = RunCreateRequest(
            source_id="crm_partial",
            idempotency_key="PARTIAL-RUN-001",
            data_payload=payload,
            mapping_version_id=approved.mapping_version_id,
            auto_execute=True,
            options={"allow_partial": True},
        )

        run, is_replay = service.submit_run(req)
        assert is_replay is False
        assert run.status == RunStatus.PARTIAL
        assert run.metrics.total_records == 2
        assert run.metrics.valid_records == 1
        assert run.metrics.exception_records == 1
        assert run.artifact_paths.exception_batch_path is not None
        assert Path(run.artifact_paths.exception_batch_path).is_file()

        # Check that exception was recorded in ExceptionQueueService
        exceptions = service.exception_service.list_exceptions(run_id=run.run_id)
        assert len(exceptions) == 1
        assert exceptions[0].rule_id == "EMAIL_FORMAT"


def test_service_idempotent_replay_does_not_reexecute() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = OnboardingRunService(artifacts_root=tmp_dir)

        payload = [
            {"cust_id": "C-301", "first_name": "Frank", "last_name": "Reynolds", "email_address": "frank@paddys.com", "signup_date": "2026-01-01", "acc_status": "ACTIVE"},
        ]
        df = pl.DataFrame(payload)
        profile = service.profiler.profile_dataframe(source_id="crm_replay", df=df)
        schema_fp = profile.fingerprint_hash
        approved = _create_crm_mapping(schema_fp)
        service.register_approved_mapping(approved)

        req = RunCreateRequest(
            source_id="crm_replay",
            idempotency_key="REPLAY-KEY-001",
            data_payload=payload,
            mapping_version_id=approved.mapping_version_id,
            auto_execute=True,
        )

        run1, is_replay1 = service.submit_run(req)
        assert is_replay1 is False
        assert run1.status == RunStatus.COMPLETED

        # Submit identical request again
        run2, is_replay2 = service.submit_run(req)
        assert is_replay2 is True
        assert run2.run_id == run1.run_id
        assert run2.created_at == run1.created_at


def test_service_cancel_and_retry() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = OnboardingRunService(artifacts_root=tmp_dir)

        payload = [{"cust_id": "C-401", "first_name": "Dee", "last_name": "Reynolds", "email_address": "dee@bird.com", "signup_date": "2026-01-01", "acc_status": "ACTIVE"}]

        # Create run without auto_execute
        req = RunCreateRequest(
            source_id="crm_ops",
            idempotency_key="CANCEL-TEST-001",
            data_payload=payload,
            auto_execute=False,
        )
        run, _ = service.submit_run(req)
        assert run.status == RunStatus.CREATED

        # Cancel run
        cancelled = service.cancel_run(run.run_id, reason="Operator abort")
        assert cancelled.status == RunStatus.CANCELLED
        assert cancelled.error_message == "Operator abort"


def test_no_raw_pii_stored_in_run_table() -> None:
    run = OnboardingRun(
        idempotency_key="PII-AUDIT-KEY",
        request_fingerprint="fp_audit",
        source_id="crm_confidential",
        input_fingerprint="hash_content_999",
    )
    serialized = json.dumps(run.to_dict())

    # Ensure no customer names, plain emails, or raw record structures exist in serialized run
    assert "john.doe@example.com" not in serialized
    assert "123 Main Street" not in serialized
    assert "John Doe" not in serialized
