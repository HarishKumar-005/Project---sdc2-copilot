"""Tests for M4: Deterministic transformation engine, operations, canonical output, and immutability."""

from datetime import date, datetime, timezone
from pathlib import Path
import polars as pl
import pytest
from pydantic import ValidationError

from scd2_copilot.onboarding.adapters.csv_adapter import CSVSourceAdapter
from scd2_copilot.onboarding.canonical import CanonicalField, CanonicalSchema, get_canonical_customer_v1
from scd2_copilot.onboarding.approval.service import MappingReviewService
from scd2_copilot.onboarding.exceptions import (
    CanonicalSchemaMismatchError,
    MappingNotApprovedError,
    MissingSourceColumnError,
    SourceSchemaMismatchError,
    TransformationConfigurationError,
)
from scd2_copilot.onboarding.models.approval import (
    ApprovedMappingDefinition,
    ApprovedMappingVersion,
    FieldReviewDecision,
    ReviewDecisionType,
)
from scd2_copilot.onboarding.models.mapping import (
    MappingProposalBatch,
    MappingType,
    TransformationOpType,
    TransformationStep,
)
from scd2_copilot.onboarding.models.schema_snapshot import SchemaFingerprint, SourceSchemaSnapshot
from scd2_copilot.onboarding.models.source import SourceDefinition, SourceType
from scd2_copilot.onboarding.models.transformation import (
    CanonicalCustomerRecord,
    RecordValidationError,
    TransformationResult,
    TransformedRecord,
    ValidationCategory,
)
from scd2_copilot.onboarding.profiler.engine import DataProfiler
from scd2_copilot.onboarding.transformation.engine import DeterministicTransformationEngine
from scd2_copilot.onboarding.transformation.service import TransformationPipeline


def _build_test_approved_version(
    mappings: list[ApprovedMappingDefinition],
    source_id: str = "test_src",
    source_fingerprint: str = "hash_123",
) -> ApprovedMappingVersion:
    """Helper to construct an approved mapping version for tests."""
    return ApprovedMappingVersion(
        mapping_version_id=f"map_ver_{source_id}_v1",
        source_id=source_id,
        source_fingerprint=source_fingerprint,
        canonical_schema_name="customer",
        canonical_schema_version=1,
        version_number=1,
        mappings=mappings,
        is_complete=True,
        approved_by="test_reviewer",
    )


def test_approved_mapping_executes_successfully() -> None:
    # 1. Approved mapping executes successfully
    mappings = [
        ApprovedMappingDefinition(
            source_field="cust_id",
            target_field="customer_id",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="ID",
        ),
        ApprovedMappingDefinition(
            source_field="first_name",
            target_field="first_name",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="FN",
        ),
        ApprovedMappingDefinition(
            source_field="last_name",
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
    df = pl.DataFrame(
        {
            "cust_id": ["C-101"],
            "first_name": ["Alice"],
            "last_name": ["Smith"],
            "email": ["alice@example.com"],
            "status": ["ACTIVE"],
            "created_at": ["2026-01-01T00:00:00Z"],
        }
    )

    engine = DeterministicTransformationEngine(version)
    recs = engine.transform_dataframe(df)
    assert len(recs) == 1
    assert recs[0].is_valid is True
    assert recs[0].canonical_values["customer_id"] == "C-101"
    assert recs[0].canonical_values["first_name"] == "Alice"


def test_trim_operation() -> None:
    # 3. TRIM removes leading/trailing whitespace
    mappings = [
        ApprovedMappingDefinition(
            source_field="raw_name",
            target_field="first_name",
            mapping_type=MappingType.TRANSFORMED,
            transformations=[TransformationStep(op=TransformationOpType.TRIM)],
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="Trim name",
        )
    ]
    version = _build_test_approved_version(mappings)
    df = pl.DataFrame({"raw_name": ["  Bob Dylan  ", "\tCharlie\n", "NoSpace"]})

    engine = DeterministicTransformationEngine(version)
    recs = engine.transform_dataframe(df)
    assert recs[0].canonical_values["first_name"] == "Bob Dylan"
    assert recs[1].canonical_values["first_name"] == "Charlie"
    assert recs[2].canonical_values["first_name"] == "NoSpace"


def test_lowercase_and_uppercase_operations() -> None:
    # 4 & 5. LOWERCASE and UPPERCASE
    mappings = [
        ApprovedMappingDefinition(
            source_field="upper_col",
            target_field="first_name",
            mapping_type=MappingType.TRANSFORMED,
            transformations=[TransformationStep(op=TransformationOpType.LOWERCASE)],
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="Lowercase",
        ),
        ApprovedMappingDefinition(
            source_field="lower_col",
            target_field="last_name",
            mapping_type=MappingType.TRANSFORMED,
            transformations=[TransformationStep(op=TransformationOpType.UPPERCASE)],
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="Uppercase",
        ),
    ]
    version = _build_test_approved_version(mappings)
    df = pl.DataFrame({"upper_col": ["ALICE"], "lower_col": ["smith"]})

    engine = DeterministicTransformationEngine(version)
    recs = engine.transform_dataframe(df)
    assert recs[0].canonical_values["first_name"] == "alice"
    assert recs[0].canonical_values["last_name"] == "SMITH"


def test_cast_success_and_failure() -> None:
    # 6 & 7. CAST success and failure
    mappings = [
        ApprovedMappingDefinition(
            source_field="str_id",
            target_field="customer_id",
            mapping_type=MappingType.TRANSFORMED,
            transformations=[TransformationStep(op=TransformationOpType.CAST, params={"target_type": "string"})],
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="Cast string",
        ),
        ApprovedMappingDefinition(
            source_field="int_val",
            target_field="status",
            mapping_type=MappingType.TRANSFORMED,
            transformations=[TransformationStep(op=TransformationOpType.CAST, params={"target_type": "boolean"})],
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="Cast bool",
        ),
    ]
    version = _build_test_approved_version(mappings)
    df = pl.DataFrame({"str_id": [12345, 67890], "int_val": ["true", "not_a_bool"]})

    engine = DeterministicTransformationEngine(version)
    recs = engine.transform_dataframe(df)

    # First row succeeds
    assert recs[0].canonical_values["customer_id"] == "12345"
    assert recs[0].canonical_values["status"] is True

    # Second row triggers cast error for boolean
    assert recs[1].canonical_values["status"] is None
    assert recs[1].is_valid is False
    assert any(e.rule_id == "STATUS_CAST_ERROR" for e in recs[1].errors)


def test_parse_date_success_and_invalid_date() -> None:
    # 8 & 9. PARSE_DATE success and invalid date handling
    mappings = [
        ApprovedMappingDefinition(
            source_field="slash_date",
            target_field="date_of_birth",
            mapping_type=MappingType.TRANSFORMED,
            transformations=[
                TransformationStep(op=TransformationOpType.PARSE_DATE, params={"format": "%d/%m/%Y"})
            ],
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="Parse date",
        )
    ]
    version = _build_test_approved_version(mappings)
    df = pl.DataFrame({"slash_date": ["31/12/1990", "99/99/9999", "not_a_date"]})

    engine = DeterministicTransformationEngine(version)
    recs = engine.transform_dataframe(df)

    # First row parses correctly
    assert recs[0].canonical_values["date_of_birth"] == date(1990, 12, 31)

    # Second and third rows record INVALID_DATE error
    assert recs[1].canonical_values["date_of_birth"] is None
    assert any(e.rule_id == "INVALID_DATE" for e in recs[1].errors)

    assert recs[2].canonical_values["date_of_birth"] is None
    assert any(e.rule_id == "INVALID_DATE" for e in recs[2].errors)


def test_normalize_email_operation() -> None:
    # 10 & 11. NORMALIZE_EMAIL: trims and lowercases, preserves string for validation
    mappings = [
        ApprovedMappingDefinition(
            source_field="raw_email",
            target_field="email",
            mapping_type=MappingType.TRANSFORMED,
            transformations=[TransformationStep(op=TransformationOpType.NORMALIZE_EMAIL)],
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="Normalize email",
        )
    ]
    version = _build_test_approved_version(mappings)
    df = pl.DataFrame({"raw_email": ["  Alice.Smith@Example.COM  ", "  broken@@domain  "]})

    engine = DeterministicTransformationEngine(version)
    recs = engine.transform_dataframe(df)

    assert recs[0].canonical_values["email"] == "alice.smith@example.com"
    # Invalid email string is normalized without crashing or fabricating
    assert recs[1].canonical_values["email"] == "broken@@domain"


def test_map_enum_known_and_unknown_values() -> None:
    # 12 & 13. MAP_ENUM known value and unknown value
    mappings = [
        ApprovedMappingDefinition(
            source_field="source_status",
            target_field="status",
            mapping_type=MappingType.TRANSFORMED,
            transformations=[
                TransformationStep(
                    op=TransformationOpType.MAP_ENUM,
                    params={
                        "enum_map": {
                            "ENABLED": "ACTIVE",
                            "DISABLED": "INACTIVE",
                            "CLOSED": "INACTIVE",
                        }
                    },
                )
            ],
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="Map status enum",
        )
    ]
    version = _build_test_approved_version(mappings)
    df = pl.DataFrame({"source_status": ["enabled", "DISABLED", "UNKNOWN_TOKEN"]})

    engine = DeterministicTransformationEngine(version)
    recs = engine.transform_dataframe(df)

    assert recs[0].canonical_values["status"] == "ACTIVE"
    assert recs[1].canonical_values["status"] == "INACTIVE"
    # Unknown token is preserved as-is so downstream validator flags exact invalid value
    assert recs[2].canonical_values["status"] == "UNKNOWN_TOKEN"


def test_concat_operation() -> None:
    # 14. CONCAT combining multiple source fields
    mappings = [
        ApprovedMappingDefinition(
            source_field="fn",
            target_field="first_name",
            mapping_type=MappingType.TRANSFORMED,
            transformations=[
                TransformationStep(
                    op=TransformationOpType.CONCAT,
                    params={
                        "fields": ["title", "fn"],
                        "separator": " ",
                        "null_handling": "skip",
                    },
                )
            ],
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="Concat name",
        )
    ]
    version = _build_test_approved_version(mappings)
    df = pl.DataFrame({"title": ["Dr.", None], "fn": ["Jane", "John"]})

    engine = DeterministicTransformationEngine(version)
    recs = engine.transform_dataframe(df)

    assert recs[0].canonical_values["first_name"] == "Dr. Jane"
    assert recs[1].canonical_values["first_name"] == "John"


def test_null_handling() -> None:
    # 15. Null handling across transformations
    mappings = [
        ApprovedMappingDefinition(
            source_field="nullable_col",
            target_field="first_name",
            mapping_type=MappingType.TRANSFORMED,
            transformations=[
                TransformationStep(op=TransformationOpType.TRIM),
                TransformationStep(op=TransformationOpType.LOWERCASE),
            ],
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="Null check",
        )
    ]
    version = _build_test_approved_version(mappings)
    df = pl.DataFrame({"nullable_col": [None, "  Data  "]})

    engine = DeterministicTransformationEngine(version)
    recs = engine.transform_dataframe(df)

    assert recs[0].canonical_values["first_name"] is None
    assert recs[1].canonical_values["first_name"] == "data"


def test_missing_source_column_raises_error() -> None:
    # 16. Missing source column raises MissingSourceColumnError
    mappings = [
        ApprovedMappingDefinition(
            source_field="non_existent_column",
            target_field="customer_id",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="ID",
        )
    ]
    version = _build_test_approved_version(mappings)
    df = pl.DataFrame({"other_col": [1, 2, 3]})

    engine = DeterministicTransformationEngine(version)
    with pytest.raises(MissingSourceColumnError) as exc_info:
        engine.transform_dataframe(df)
    assert "Mapped source column 'non_existent_column' is missing" in str(exc_info.value)


def test_unapproved_or_draft_mapping_rejected() -> None:
    # 19. Unapproved / draft mapping rejected
    draft_batch = MappingProposalBatch(
        source_id="draft_src",
        source_fingerprint="hash",
        canonical_schema_name="customer",
        canonical_schema_version=1,
        provider_used="heuristic",
    )
    with pytest.raises(MappingNotApprovedError):
        DeterministicTransformationEngine(draft_batch)  # type: ignore


def test_canonical_schema_mismatch_rejected() -> None:
    # Incompatible canonical schema version rejected
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
    version = ApprovedMappingVersion(
        mapping_version_id="map_ver_v2",
        source_id="test_src",
        source_fingerprint="hash",
        canonical_schema_name="customer",
        canonical_schema_version=2,  # mapping targets customer.v2
        version_number=1,
        mappings=mappings,
        is_complete=True,
        approved_by="steward",
    )
    # Default schema is customer.v1
    with pytest.raises(CanonicalSchemaMismatchError) as exc_info:
        DeterministicTransformationEngine(version)
    assert "canonical schema version '2' does not match expected '1'" in str(exc_info.value)


def test_source_schema_fingerprint_mismatch_rejected() -> None:
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
    version = _build_test_approved_version(mappings, source_fingerprint="expected_hash")
    df = pl.DataFrame({"id": ["C-1"]})
    bad_snapshot = SourceSchemaSnapshot(
        source_id="test_src",
        schema_version=1,
        fingerprint=SchemaFingerprint(
            fingerprint_hash="mismatched_drift_hash",
            column_count=1,
            normalized_signature="id",
        ),
        columns=[],
    )

    engine = DeterministicTransformationEngine(version)
    with pytest.raises(SourceSchemaMismatchError) as exc_info:
        engine.transform_dataframe(df, source_schema=bad_snapshot)
    assert "does not match approved mapping fingerprint" in str(exc_info.value)


def test_immutability_and_boundaries(crm_csv_path: Path) -> None:
    # 39. M3 approved mapping is not mutated during execution
    defn = SourceDefinition(
        source_id="crm_immutability",
        source_name="CRM",
        source_type=SourceType.CSV,
        connection_config={"file_path": str(crm_csv_path)},
    )
    adapter = CSVSourceAdapter(defn)
    df = adapter.read_data()

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
            mapping_type=MappingType.TRANSFORMED,
            transformations=[TransformationStep(op=TransformationOpType.TRIM)],
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="FN",
        ),
        ApprovedMappingDefinition(
            source_field="Last Name",
            target_field="last_name",
            mapping_type=MappingType.TRANSFORMED,
            transformations=[TransformationStep(op=TransformationOpType.TRIM)],
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
            source_field="Birth_Date",
            target_field="date_of_birth",
            mapping_type=MappingType.TRANSFORMED,
            transformations=[TransformationStep(op=TransformationOpType.PARSE_DATE, params={"format": "%Y-%m-%d"})],
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="DOB",
        ),
        ApprovedMappingDefinition(
            source_field="Cust_Status",
            target_field="status",
            mapping_type=MappingType.TRANSFORMED,
            transformations=[
                TransformationStep(
                    op=TransformationOpType.MAP_ENUM,
                    params={"enum_map": {"ACTIVE": "ACTIVE", "INACTIVE": "INACTIVE", "PENDING": "INACTIVE", "SUSPENDED": "INACTIVE", "CLOSED": "INACTIVE"}},
                )
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
    version = _build_test_approved_version(mappings, source_id="crm_immutability")

    pipeline = TransformationPipeline()
    result1 = pipeline.execute(df, version)
    result2 = pipeline.execute(df, version)

    # 35. Deterministic: Identical inputs produce identical outputs
    assert result1.total_records == result2.total_records
    assert result1.valid_record_count == result2.valid_record_count
    assert result1.invalid_record_count == result2.invalid_record_count
    assert len(result1.all_errors) == len(result2.all_errors)

    # Version was not mutated
    assert version.source_id == "crm_immutability"
    assert version.version_number == 1
    with pytest.raises((ValidationError, TypeError)):
        version.version_number = 99  # type: ignore
