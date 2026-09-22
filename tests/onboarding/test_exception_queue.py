"""Tests for M5: Exception Queue & Reprocessing."""

from datetime import date, datetime, timezone
from pathlib import Path
import polars as pl
import pytest

from scd2_copilot.onboarding.adapters.csv_adapter import CSVSourceAdapter
from scd2_copilot.onboarding.exceptions import (
    FatalConfigurationFailureError,
    InvalidCorrectionError,
    InvalidExceptionStateTransitionError,
    NonDismissibleExceptionError,
)
from scd2_copilot.onboarding.exception_queue.repository import ExceptionQueueRepository
from scd2_copilot.onboarding.exception_queue.service import ExceptionQueueService
from scd2_copilot.onboarding.models.approval import (
    ApprovedMappingDefinition,
    ApprovedMappingVersion,
    ReviewDecisionType,
)
from scd2_copilot.onboarding.models.exception import (
    CorrectionType,
    CustomerRecordException,
    ExceptionBatch,
    ExceptionCategory,
    ExceptionStatus,
    RecordCorrection,
    ReprocessingResult,
)
from scd2_copilot.onboarding.models.mapping import MappingType, TransformationOpType, TransformationStep
from scd2_copilot.onboarding.models.source import SourceDefinition, SourceType
from scd2_copilot.onboarding.models.transformation import (
    CanonicalCustomerRecord,
    RecordValidationError,
    TransformationResult,
    TransformedRecord,
    ValidationCategory,
)
from scd2_copilot.onboarding.transformation.service import TransformationPipeline


def _build_test_approved_version(mappings: list[ApprovedMappingDefinition]) -> ApprovedMappingVersion:
    """Helper to build a valid ApprovedMappingVersion."""
    return ApprovedMappingVersion(
        mapping_version_id="map_ver_test_m5",
        source_id="test_m5_source",
        source_fingerprint="fingerprint_test_m5",
        canonical_schema_name="customer",
        canonical_schema_version=1,
        version_number=1,
        mappings=mappings,
        is_complete=True,
        approved_by="steward@example.com",
    )


def _build_test_transformation_result_with_invalid_record() -> TransformationResult:
    """Helper to assemble an M4 TransformationResult with one invalid record."""
    invalid_rec = TransformedRecord(
        row_index=0,
        source_record_id="CUST-101",
        raw_values={
            "id": "CUST-101",
            "name": "Alice Smith",
            "email": "alice@@bad-email..com",
            "status": "ACTIVE",
            "created_at": "2024-01-01T00:00:00Z",
        },
        canonical_values={
            "customer_id": "CUST-101",
            "first_name": "Alice",
            "last_name": "Smith",
            "email": "alice@@bad-email..com",
            "status": "ACTIVE",
            "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
        },
        is_valid=False,
        errors=[
            RecordValidationError(
                row_index=0,
                record_id="CUST-101",
                rule_id="INVALID_EMAIL_FORMAT",
                category=ValidationCategory.FORMAT,
                field="email",
                observed_value="alice@@bad-email..com",
                reason="Observed value 'alice@@bad-email..com' does not conform to RFC email regex.",
            )
        ],
    )
    return TransformationResult(
        source_id="test_crm",
        mapping_version_id="map_ver_test_m5",
        canonical_schema_name="customer",
        canonical_schema_version=1,
        total_records=1,
        valid_record_count=0,
        invalid_record_count=1,
        valid_records=[],
        invalid_records=[invalid_rec],
        all_errors=invalid_rec.errors,
    )


# ---------------------------------------------------------------------------
# 1. EXCEPTION CREATION & PROVENANCE
# ---------------------------------------------------------------------------


def test_m4_invalid_record_converts_to_exception() -> None:
    # 1. M4 invalid record converts into exception
    result = _build_test_transformation_result_with_invalid_record()
    service = ExceptionQueueService()
    exceptions = service.create_exceptions_from_transformation_result(result, run_id="run_001")

    assert len(exceptions) == 1
    exc = exceptions[0]
    assert exc.run_id == "run_001"
    assert exc.source_id == "test_crm"
    assert exc.status == ExceptionStatus.OPEN


def test_rule_id_and_record_id_preserved() -> None:
    # 2 & 3. Rule ID and Record ID preserved
    result = _build_test_transformation_result_with_invalid_record()
    service = ExceptionQueueService()
    exceptions = service.create_exceptions_from_transformation_result(result)
    exc = exceptions[0]

    assert exc.rule_id == "INVALID_EMAIL_FORMAT"
    assert exc.source_record_id == "CUST-101"
    assert exc.row_index == 0


def test_field_category_reason_preserved() -> None:
    # 4. Field, category, reason preserved
    result = _build_test_transformation_result_with_invalid_record()
    service = ExceptionQueueService()
    exceptions = service.create_exceptions_from_transformation_result(result)
    exc = exceptions[0]

    assert exc.field == "email"
    assert exc.category == ExceptionCategory.VALIDATION_ERROR
    assert "RFC email regex" in exc.reason
    assert exc.suggested_fix is not None


def test_provenance_preserved() -> None:
    # 5. Provenance (mapping_version_id, canonical_schema_version) preserved
    result = _build_test_transformation_result_with_invalid_record()
    service = ExceptionQueueService()
    exceptions = service.create_exceptions_from_transformation_result(result)
    exc = exceptions[0]

    assert exc.mapping_version_id == "map_ver_test_m5"
    assert exc.canonical_schema_version == 1
    assert exc.raw_record["id"] == "CUST-101"


def test_fatal_configuration_failures_raise_error() -> None:
    # 7. Fatal mapping/configuration failures are not misclassified as record exceptions
    result = TransformationResult(
        source_id="test_crm",
        mapping_version_id="",  # missing mapping version ID
        total_records=1,
        valid_record_count=0,
        invalid_record_count=1,
        invalid_records=[],
    )
    service = ExceptionQueueService()
    with pytest.raises(FatalConfigurationFailureError):
        service.create_exceptions_from_transformation_result(result)


# ---------------------------------------------------------------------------
# 2. LIFECYCLE & STATE TRANSITIONS
# ---------------------------------------------------------------------------


def test_initial_state_is_open() -> None:
    # 8. OPEN state on initial creation
    result = _build_test_transformation_result_with_invalid_record()
    service = ExceptionQueueService()
    exc = service.create_exceptions_from_transformation_result(result)[0]
    assert exc.status == ExceptionStatus.OPEN
    assert exc.resolved_at is None
    assert exc.dismissed_at is None


def test_open_to_corrected_transition() -> None:
    # 9. Valid OPEN -> CORRECTED transition
    result = _build_test_transformation_result_with_invalid_record()
    service = ExceptionQueueService()
    exc = service.create_exceptions_from_transformation_result(result)[0]

    corrected_exc = service.apply_correction(
        exception=exc,
        field="email",
        correction_type=CorrectionType.VALUE_OVERRIDE,
        applied_by="steward@example.com",
        reason="Fixed malformed email address",
        corrected_value="alice@example.com",
    )

    assert corrected_exc.status == ExceptionStatus.CORRECTED
    assert len(corrected_exc.corrections) == 1
    assert corrected_exc.corrections[0].corrected_value == "alice@example.com"


def test_invalid_state_transitions_rejected() -> None:
    # 10. Invalid state transitions rejected
    result = _build_test_transformation_result_with_invalid_record()
    service = ExceptionQueueService()
    exc = service.create_exceptions_from_transformation_result(result)[0]

    # Directly setting RESOLVED on an OPEN exception with a dummy ReprocessingResult without reprocessing
    resolved_exc = exc.model_copy(update={"status": ExceptionStatus.RESOLVED})

    # Cannot apply correction to already resolved exception
    with pytest.raises(InvalidExceptionStateTransitionError):
        service.apply_correction(
            exception=resolved_exc,
            field="email",
            correction_type=CorrectionType.VALUE_OVERRIDE,
            applied_by="steward",
            reason="late fix",
            corrected_value="late@example.com",
        )

    # Cannot dismiss already resolved exception
    with pytest.raises(InvalidExceptionStateTransitionError):
        service.dismiss_exception(resolved_exc, dismissed_by="steward", reason="test")


def test_dismissal_behavior_and_policy() -> None:
    # 14. Dismissed behavior with policy enforcement
    service = ExceptionQueueService()

    # Non-dismissible rule: DUPLICATE_CUSTOMER_ID
    dup_exc = CustomerRecordException(
        exception_id="exc_dup_1",
        source_id="src1",
        row_index=0,
        rule_id="DUPLICATE_CUSTOMER_ID",
        category=ExceptionCategory.DUPLICATE_ERROR,
        field="customer_id",
        reason="Duplicate id",
        mapping_version_id="map_v1",
    )
    with pytest.raises(NonDismissibleExceptionError):
        service.dismiss_exception(dup_exc, dismissed_by="admin", reason="ignore duplicate")

    # Dismissible rule (e.g. optional warning or non-primary field)
    dismissible_exc = CustomerRecordException(
        exception_id="exc_opt_1",
        source_id="src1",
        row_index=0,
        rule_id="OPTIONAL_FIELD_WARNING",
        category=ExceptionCategory.VALIDATION_ERROR,
        field="notes",
        reason="Optional notes missing",
        mapping_version_id="map_v1",
    )
    dismissed = service.dismiss_exception(dismissible_exc, dismissed_by="admin", reason="Optional field not provided by source")
    assert dismissed.status == ExceptionStatus.DISMISSED
    assert dismissed.dismissed_by == "admin"
    assert dismissed.dismissal_reason == "Optional field not provided by source"
    assert dismissed.dismissed_at is not None


# ---------------------------------------------------------------------------
# 3. CORRECTION MODEL & BOUNDARIES
# ---------------------------------------------------------------------------


def test_structured_correction_accepted() -> None:
    # 15. Structured correction accepted
    result = _build_test_transformation_result_with_invalid_record()
    service = ExceptionQueueService()
    exc = service.create_exceptions_from_transformation_result(result)[0]

    corrected = service.apply_correction(
        exception=exc,
        field="email",
        correction_type=CorrectionType.VALUE_OVERRIDE,
        applied_by="operator1",
        reason="Operator typo fix",
        corrected_value="alice.smith@example.com",
    )
    assert corrected.status == ExceptionStatus.CORRECTED
    assert len(corrected.corrections) == 1
    corr = corrected.corrections[0]
    assert corr.applied_by == "operator1"
    assert corr.corrected_value == "alice.smith@example.com"


def test_invalid_correction_rejected() -> None:
    # 16. Invalid correction rejected (e.g. target field does not exist in canonical or source)
    result = _build_test_transformation_result_with_invalid_record()
    service = ExceptionQueueService()
    exc = service.create_exceptions_from_transformation_result(result)[0]

    with pytest.raises(InvalidCorrectionError):
        service.apply_correction(
            exception=exc,
            field="completely_bogus_field",
            correction_type=CorrectionType.VALUE_OVERRIDE,
            applied_by="operator",
            reason="test",
            corrected_value="val",
        )

    # Missing transformation steps when type is APPLY_TRANSFORMATION
    with pytest.raises(InvalidCorrectionError):
        service.apply_correction(
            exception=exc,
            field="email",
            correction_type=CorrectionType.APPLY_TRANSFORMATION,
            applied_by="operator",
            reason="test",
            transformations=[],
        )


def test_correction_cannot_execute_arbitrary_code() -> None:
    # 17. Correction cannot execute arbitrary code
    result = _build_test_transformation_result_with_invalid_record()
    service = ExceptionQueueService()
    exc = service.create_exceptions_from_transformation_result(result)[0]

    # Attempting to pass an unapproved transformation operation
    class FakeStep:
        op = "RUN_ARBITRARY_PYTHON_CODE"

    with pytest.raises(InvalidCorrectionError):
        service.apply_correction(
            exception=exc,
            field="email",
            correction_type=CorrectionType.APPLY_TRANSFORMATION,
            applied_by="hacker",
            reason="exploit attempt",
            transformations=[FakeStep()],  # type: ignore
        )


def test_correction_history_preserved() -> None:
    # 18. Multiple corrections append to history rather than overwriting
    result = _build_test_transformation_result_with_invalid_record()
    service = ExceptionQueueService()
    exc = service.create_exceptions_from_transformation_result(result)[0]

    c1 = service.apply_correction(
        exception=exc,
        field="email",
        correction_type=CorrectionType.VALUE_OVERRIDE,
        applied_by="op1",
        reason="attempt 1",
        corrected_value="bad@domain",
    )
    c2 = service.apply_correction(
        exception=c1,
        field="email",
        correction_type=CorrectionType.VALUE_OVERRIDE,
        applied_by="op2",
        reason="attempt 2",
        corrected_value="good@example.com",
    )
    assert len(c2.corrections) == 2
    assert c2.corrections[0].corrected_value == "bad@domain"
    assert c2.corrections[1].corrected_value == "good@example.com"


# ---------------------------------------------------------------------------
# 4. DETERMINISTIC REPROCESSING
# ---------------------------------------------------------------------------


def test_reprocessing_uses_m4_pipeline_and_resolves() -> None:
    # 19, 20, 21. Reprocessing uses actual M4 engine & validator, resolving corrected record
    mappings = [
        ApprovedMappingDefinition(
            source_field="id",
            target_field="customer_id",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="ID",
        ),
        ApprovedMappingDefinition(
            source_field="name",
            target_field="first_name",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="FN",
        ),
        ApprovedMappingDefinition(
            source_field="name",
            target_field="last_name",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="LN",
        ),
        ApprovedMappingDefinition(
            source_field="email",
            target_field="email",
            mapping_type=MappingType.TRANSFORMED,
            transformations=[TransformationStep(op=TransformationOpType.NORMALIZE_EMAIL)],
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="EM",
        ),
        ApprovedMappingDefinition(
            source_field="status",
            target_field="status",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="ST",
        ),
        ApprovedMappingDefinition(
            source_field="created_at",
            target_field="created_at",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="CA",
        ),
    ]
    version = _build_test_approved_version(mappings)

    result = _build_test_transformation_result_with_invalid_record()
    service = ExceptionQueueService()
    exc = service.create_exceptions_from_transformation_result(result)[0]

    # Apply valid email correction
    corrected_exc = service.apply_correction(
        exception=exc,
        field="email",
        correction_type=CorrectionType.VALUE_OVERRIDE,
        applied_by="steward",
        reason="Fixed typo in domain",
        corrected_value="alice@good-email.com",
    )

    # Reprocess
    reprocessed_exc, canonical_rec = service.reprocess_exception(corrected_exc, version)

    assert reprocessed_exc.status == ExceptionStatus.RESOLVED
    assert reprocessed_exc.resolved_at is not None
    assert canonical_rec is not None
    assert canonical_rec.email == "alice@good-email.com"
    assert canonical_rec.customer_id == "CUST-101"
    assert len(reprocessed_exc.reprocessing_history) == 1
    assert reprocessed_exc.reprocessing_history[0].success is True


def test_incorrect_correction_remains_invalid() -> None:
    # 22. Incorrect correction remains invalid, moving to REPROCESSED with preserved history
    mappings = [
        ApprovedMappingDefinition(
            source_field="id",
            target_field="customer_id",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="ID",
        ),
        ApprovedMappingDefinition(
            source_field="name",
            target_field="first_name",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="FN",
        ),
        ApprovedMappingDefinition(
            source_field="name",
            target_field="last_name",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="LN",
        ),
        ApprovedMappingDefinition(
            source_field="email",
            target_field="email",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="EM",
        ),
        ApprovedMappingDefinition(
            source_field="status",
            target_field="status",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="ST",
        ),
        ApprovedMappingDefinition(
            source_field="created_at",
            target_field="created_at",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="CA",
        ),
    ]
    version = _build_test_approved_version(mappings)

    result = _build_test_transformation_result_with_invalid_record()
    service = ExceptionQueueService()
    exc = service.create_exceptions_from_transformation_result(result)[0]

    # Apply still-invalid email
    corrected_exc = service.apply_correction(
        exception=exc,
        field="email",
        correction_type=CorrectionType.VALUE_OVERRIDE,
        applied_by="steward",
        reason="Entered another invalid email",
        corrected_value="still_not_an_email",
    )

    reprocessed_exc, canonical_rec = service.reprocess_exception(corrected_exc, version)

    assert reprocessed_exc.status == ExceptionStatus.REPROCESSED
    assert reprocessed_exc.resolved_at is None
    assert canonical_rec is None
    assert len(reprocessed_exc.reprocessing_history) == 1
    assert reprocessed_exc.reprocessing_history[0].success is False
    assert len(reprocessed_exc.reprocessing_history[0].errors) > 0


def test_batch_reprocessing_resolves_duplicates() -> None:
    # 24 & 25. Batch reprocessing evaluates batch-level uniqueness
    mappings = [
        ApprovedMappingDefinition(
            source_field="id",
            target_field="customer_id",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="ID",
        ),
        ApprovedMappingDefinition(
            source_field="name",
            target_field="first_name",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="FN",
        ),
        ApprovedMappingDefinition(
            source_field="name",
            target_field="last_name",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="LN",
        ),
        ApprovedMappingDefinition(
            source_field="email",
            target_field="email",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="EM",
        ),
        ApprovedMappingDefinition(
            source_field="status",
            target_field="status",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="ST",
        ),
        ApprovedMappingDefinition(
            source_field="created_at",
            target_field="created_at",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="CA",
        ),
    ]
    version = _build_test_approved_version(mappings)

    # Initial batch with 2 records having duplicate ID "CUST-DUP"
    df = pl.DataFrame(
        {
            "id": ["CUST-DUP", "CUST-DUP"],
            "name": ["Alice", "Bob"],
            "email": ["alice@example.com", "bob@example.com"],
            "status": ["ACTIVE", "ACTIVE"],
            "created_at": ["2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"],
        }
    )
    pipeline = TransformationPipeline()
    res = pipeline.execute(df, version)
    assert res.invalid_record_count == 2

    service = ExceptionQueueService()
    exceptions = service.create_exceptions_from_transformation_result(res)
    assert len(exceptions) == 2

    # Correct second record's ID to be unique
    exc2 = exceptions[1]
    corrected_exc2 = service.apply_correction(
        exception=exc2,
        field="id",
        correction_type=CorrectionType.VALUE_OVERRIDE,
        applied_by="steward",
        reason="Deduplicated customer_id",
        corrected_value="CUST-NEW-ID",
    )
    reprocessed_batch, canonical_recs = service.reprocess_batch([exceptions[0], corrected_exc2], version)

    assert len(canonical_recs) == 2
    assert reprocessed_batch[0].status == ExceptionStatus.RESOLVED
    assert reprocessed_batch[1].status == ExceptionStatus.RESOLVED
    assert canonical_recs[0].customer_id == "CUST-DUP"
    assert canonical_recs[1].customer_id == "CUST-NEW-ID"


# ---------------------------------------------------------------------------
# 5. REPLAY, ARTIFACT PERSISTENCE & PRIVACY
# ---------------------------------------------------------------------------


def test_exception_artifact_roundtrip(tmp_path: Path) -> None:
    # 30. Serialization to/from JSON artifact roundtrip
    result = _build_test_transformation_result_with_invalid_record()
    service = ExceptionQueueService()
    exceptions = service.create_exceptions_from_transformation_result(result)

    repo = ExceptionQueueRepository()
    repo.add_batch(exceptions)

    art_file = tmp_path / "exceptions.json"
    repo.save_to_artifact(art_file, batch_id="batch_01", source_id="test_crm", mapping_version_id="map_v1")

    # Reload into a fresh repository
    repo2 = ExceptionQueueRepository()
    loaded_excs = repo2.load_from_artifact(art_file)

    assert len(loaded_excs) == 1
    assert loaded_excs[0].exception_id == exceptions[0].exception_id
    assert loaded_excs[0].rule_id == "INVALID_EMAIL_FORMAT"
    assert repo2.count() == 1


def test_privacy_masking_in_observed_values() -> None:
    # 31 & 32. Privacy-preserving masking in observed values
    service = ExceptionQueueService()
    assert service._sanitize_observed_value("email", "john.doe@corporate.org") == "j***@corporate.org"
    assert service._sanitize_observed_value("phone", "+1-555-0199") == "***-0199"
    assert service._sanitize_observed_value("first_name", "Jonathan Doe") == "J*** D***"


def test_approved_mapping_remains_immutable() -> None:
    # 27. Approved mapping remains unchanged during exception processing
    mappings = [
        ApprovedMappingDefinition(
            source_field="id",
            target_field="customer_id",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="ID",
        )
    ]
    version = _build_test_approved_version(mappings)
    # Pydantic frozen model prevents mutation
    with pytest.raises(Exception):
        version.mapping_version_id = "mutated_id"  # type: ignore


# ---------------------------------------------------------------------------
# 6. FIXTURE COVERAGE (CRM, BILLING, SUPPORT)
# ---------------------------------------------------------------------------


def test_crm_fixture_exception_lifecycle(crm_csv_path: Path) -> None:
    # 33. CRM fixture: bad email triggers exception, corrected, reprocessed to valid canonical
    defn = SourceDefinition(
        source_id="crm_e2e_exc",
        source_name="CRM E2E Exceptions",
        source_type=SourceType.CSV,
        connection_config={"file_path": str(crm_csv_path)},
    )
    adapter = CSVSourceAdapter(defn)
    df = adapter.read_data()

    # Modify row 0 email in CRM to be invalid to trigger exception
    df_with_bad_email = df.with_columns(
        pl.when(pl.col("Cust_ID") == pl.col("Cust_ID").first())
        .then(pl.lit("alice@@broken_email.com"))
        .otherwise(pl.col("Contact Email"))
        .alias("Contact Email")
    )

    mappings = [
        ApprovedMappingDefinition(
            source_field="Cust_ID",
            target_field="customer_id",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="ID",
        ),
        ApprovedMappingDefinition(
            source_field="First Name",
            target_field="first_name",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="FN",
        ),
        ApprovedMappingDefinition(
            source_field="Last Name",
            target_field="last_name",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="LN",
        ),
        ApprovedMappingDefinition(
            source_field="Contact Email",
            target_field="email",
            mapping_type=MappingType.TRANSFORMED,
            transformations=[TransformationStep(op=TransformationOpType.NORMALIZE_EMAIL)],
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="EM",
        ),
        ApprovedMappingDefinition(
            source_field="Cust_Status",
            target_field="status",
            mapping_type=MappingType.TRANSFORMED,
            transformations=[
                TransformationStep(
                    op=TransformationOpType.MAP_ENUM,
                    params={
                        "enum_map": {
                            "ACTIVE": "ACTIVE",
                            "INACTIVE": "INACTIVE",
                            "PENDING": "INACTIVE",
                            "SUSPENDED": "INACTIVE",
                            "CLOSED": "INACTIVE",
                        }
                    },
                ),
            ],
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="ST",
        ),
        ApprovedMappingDefinition(
            source_field="Created_Timestamp",
            target_field="created_at",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="CA",
        ),
    ]
    version = _build_test_approved_version(mappings)

    pipeline = TransformationPipeline()
    result = pipeline.execute(df_with_bad_email, version)

    assert result.invalid_record_count == 2

    service = ExceptionQueueService()
    exceptions = service.create_exceptions_from_transformation_result(result)
    assert len(exceptions) == 2
    exc = next(e for e in exceptions if e.row_index == 0)
    assert exc.rule_id == "EMAIL_FORMAT"
    assert exc.status == ExceptionStatus.OPEN

    # Correct the email
    corrected = service.apply_correction(
        exception=exc,
        field="Contact Email",
        correction_type=CorrectionType.VALUE_OVERRIDE,
        applied_by="steward",
        reason="Corrected email domain in CRM",
        corrected_value="alice@fixed-domain.com",
    )
    assert corrected.status == ExceptionStatus.CORRECTED

    # Reprocess
    reprocessed_exc, canonical_rec = service.reprocess_exception(corrected, version)
    assert reprocessed_exc.status == ExceptionStatus.RESOLVED
    assert canonical_rec is not None
    assert canonical_rec.email == "alice@fixed-domain.com"


def test_billing_fixture_exception_lifecycle(billing_csv_path: Path) -> None:
    # 34. Billing fixture: invalid account state triggers exception, corrected, reprocessed
    defn = SourceDefinition(
        source_id="billing_e2e_exc",
        source_name="Billing E2E Exceptions",
        source_type=SourceType.CSV,
        connection_config={"file_path": str(billing_csv_path)},
    )
    adapter = CSVSourceAdapter(defn)
    df = adapter.read_data()

    mappings = [
        ApprovedMappingDefinition(
            source_field="acct_code",
            target_field="customer_id",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="ID",
        ),
        ApprovedMappingDefinition(
            source_field="client_full_name",
            target_field="first_name",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="FN",
        ),
        ApprovedMappingDefinition(
            source_field="client_full_name",
            target_field="last_name",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="LN",
        ),
        ApprovedMappingDefinition(
            source_field="billing_email",
            target_field="email",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="EM",
        ),
        ApprovedMappingDefinition(
            source_field="account_state",
            target_field="status",
            mapping_type=MappingType.TRANSFORMED,
            transformations=[
                TransformationStep(
                    op=TransformationOpType.MAP_ENUM,
                    params={"enum_map": {"ACTIVE": "ACTIVE", "DELINQUENT": "INACTIVE", "CLOSED": "INACTIVE"}},
                )
            ],
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="ST",
        ),
        ApprovedMappingDefinition(
            source_field="opened_date",
            target_field="created_at",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="CA",
        ),
    ]
    version = _build_test_approved_version(mappings)

    pipeline = TransformationPipeline()
    result = pipeline.execute(df, version)

    # Billing accounts has 1 missing email and 1 PENDING state (unmapped enum)
    assert result.invalid_record_count > 0

    service = ExceptionQueueService()
    exceptions = service.create_exceptions_from_transformation_result(result)
    assert len(exceptions) >= 2

    # Find the missing email exception
    email_exc = next((e for e in exceptions if e.field == "email"), None)
    assert email_exc is not None

    # Correct missing email
    corrected = service.apply_correction(
        exception=email_exc,
        field="billing_email",
        correction_type=CorrectionType.VALUE_OVERRIDE,
        applied_by="billing_steward",
        reason="Supplied billing contact email",
        corrected_value="finance@deltalogistics.com",
    )
    reprocessed_exc, canonical_rec = service.reprocess_exception(corrected, version)
    assert reprocessed_exc.status == ExceptionStatus.RESOLVED
    assert canonical_rec is not None
    assert canonical_rec.email == "finance@deltalogistics.com"


def test_support_fixture_exception_lifecycle(support_csv_path: Path) -> None:
    # 35. Support fixture: missing email in row 2 triggers exception, corrected, reprocessed
    defn = SourceDefinition(
        source_id="support_e2e_exc",
        source_name="Support E2E Exceptions",
        source_type=SourceType.CSV,
        connection_config={"file_path": str(support_csv_path)},
    )
    adapter = CSVSourceAdapter(defn)
    df = adapter.read_data()

    mappings = [
        ApprovedMappingDefinition(
            source_field="user_identifier",
            target_field="customer_id",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="ID",
        ),
        ApprovedMappingDefinition(
            source_field="ticket_ref",
            target_field="first_name",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="FN",
        ),
        ApprovedMappingDefinition(
            source_field="priority_level",
            target_field="last_name",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="LN",
        ),
        ApprovedMappingDefinition(
            source_field="reporter_email",
            target_field="email",
            mapping_type=MappingType.TRANSFORMED,
            transformations=[TransformationStep(op=TransformationOpType.NORMALIZE_EMAIL)],
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="EM",
        ),
        ApprovedMappingDefinition(
            source_field="resolved_flag",
            target_field="status",
            mapping_type=MappingType.TRANSFORMED,
            transformations=[
                TransformationStep(
                    op=TransformationOpType.MAP_ENUM,
                    params={
                        "enum_map": {
                            "true": "INACTIVE",
                            "false": "ACTIVE",
                            "True": "INACTIVE",
                            "False": "ACTIVE",
                            True: "INACTIVE",
                            False: "ACTIVE",
                        }
                    },
                )
            ],
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="ST",
        ),
        ApprovedMappingDefinition(
            source_field="created_at",
            target_field="created_at",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="CA",
        ),
    ]
    version = _build_test_approved_version(mappings)

    pipeline = TransformationPipeline()
    result = pipeline.execute(df, version)

    # Support tickets has 1 missing email row (TCK-9003)
    assert result.invalid_record_count == 1

    service = ExceptionQueueService()
    exceptions = service.create_exceptions_from_transformation_result(result)
    assert len(exceptions) == 1
    exc = exceptions[0]
    assert exc.rule_id == "EMAIL_REQUIRED"
    assert exc.field == "email"

    # Correct the missing email
    corrected = service.apply_correction(
        exception=exc,
        field="reporter_email",
        correction_type=CorrectionType.VALUE_OVERRIDE,
        applied_by="support_agent",
        reason="Found requester email from ticket history",
        corrected_value="usr103@workplace.com",
    )
    reprocessed_exc, canonical_rec = service.reprocess_exception(corrected, version)

    assert reprocessed_exc.status == ExceptionStatus.RESOLVED
    assert canonical_rec is not None
    assert canonical_rec.email == "usr103@workplace.com"
    assert canonical_rec.customer_id == "USR-103"
