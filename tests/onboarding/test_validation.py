"""Tests for M4: Deterministic validation engine, rule violations, duplicate detection, and fixture coverage."""

from datetime import date, datetime, timezone
from pathlib import Path
import polars as pl
import pytest

from scd2_copilot.onboarding.adapters.csv_adapter import CSVSourceAdapter
from scd2_copilot.onboarding.canonical import get_canonical_customer_v1
from scd2_copilot.onboarding.models.approval import ApprovedMappingDefinition, ApprovedMappingVersion, ReviewDecisionType
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
from scd2_copilot.onboarding.transformation.validator import DeterministicValidator, ValidationConfig


def _build_full_approved_version(source_id: str, id_col: str, fn_col: str, ln_col: str, email_col: str, status_col: str, ca_col: str, dob_col: str = None, enum_map: dict = None) -> ApprovedMappingVersion:
    """Helper to assemble an ApprovedMappingVersion targeting customer.v1."""
    mappings = [
        ApprovedMappingDefinition(
            source_field=id_col,
            target_field="customer_id",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="ID",
        ),
        ApprovedMappingDefinition(
            source_field=fn_col,
            target_field="first_name",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="FN",
        ),
        ApprovedMappingDefinition(
            source_field=ln_col,
            target_field="last_name",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="LN",
        ),
        ApprovedMappingDefinition(
            source_field=email_col,
            target_field="email",
            mapping_type=MappingType.TRANSFORMED,
            transformations=[TransformationStep(op=TransformationOpType.NORMALIZE_EMAIL)],
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="EM",
        ),
        ApprovedMappingDefinition(
            source_field=status_col,
            target_field="status",
            mapping_type=MappingType.TRANSFORMED if enum_map else MappingType.DIRECT,
            transformations=[TransformationStep(op=TransformationOpType.MAP_ENUM, params={"enum_map": enum_map})] if enum_map else [],
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="ST",
        ),
        ApprovedMappingDefinition(
            source_field=ca_col,
            target_field="created_at",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="CA",
        ),
    ]
    if dob_col:
        mappings.append(
            ApprovedMappingDefinition(
                source_field=dob_col,
                target_field="date_of_birth",
                mapping_type=MappingType.TRANSFORMED,
                transformations=[TransformationStep(op=TransformationOpType.PARSE_DATE, params={"format": "%Y-%m-%d"})],
                decision=ReviewDecisionType.APPROVE,
                reviewer="steward",
                confidence=1.0,
                provenance_reason="DOB",
            )
        )

    return ApprovedMappingVersion(
        mapping_version_id=f"map_ver_{source_id}_v1",
        source_id=source_id,
        source_fingerprint="fingerprint_test",
        canonical_schema_name="customer",
        canonical_schema_version=1,
        version_number=1,
        mappings=mappings,
        is_complete=True,
        approved_by="steward",
    )


def test_validation_required_fields() -> None:
    # 25. REQUIRED rule: customer_id, first_name, last_name, email, status, created_at
    version = _build_full_approved_version("test_req", "id", "fn", "ln", "em", "st", "ca")
    df = pl.DataFrame(
        {
            "id": ["C-1", None, "C-3", "C-4"],
            "fn": ["Alice", "Bob", "", "Dave"],
            "ln": ["Smith", "Jones", "Taylor", None],
            "em": ["alice@ex.com", "bob@ex.com", "charlie@ex.com", "dave@ex.com"],
            "st": ["ACTIVE", "ACTIVE", "ACTIVE", "ACTIVE"],
            "ca": ["2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"],
        }
    )

    pipeline = TransformationPipeline()
    result = pipeline.execute(df, version)

    assert result.total_records == 4
    assert result.valid_record_count == 1  # only row 0 is valid
    assert result.invalid_record_count == 3

    # Row 1 has missing customer_id
    assert any(e.row_index == 1 and e.rule_id == "CUSTOMER_ID_REQUIRED" for e in result.all_errors)
    # Row 2 has blank first_name
    assert any(e.row_index == 2 and e.rule_id == "FIRST_NAME_REQUIRED" for e in result.all_errors)
    # Row 3 has missing last_name
    assert any(e.row_index == 3 and e.rule_id == "LAST_NAME_REQUIRED" for e in result.all_errors)


def test_validation_email_format() -> None:
    # 27. FORMAT rule: email formatting
    version = _build_full_approved_version("test_em", "id", "fn", "ln", "em", "st", "ca")
    df = pl.DataFrame(
        {
            "id": ["C-1", "C-2", "C-3"],
            "fn": ["Alice", "Bob", "Charlie"],
            "ln": ["Smith", "Jones", "Taylor"],
            "em": ["valid.email@example.com", "alice@@example.com", "not_an_email"],
            "st": ["ACTIVE", "ACTIVE", "ACTIVE"],
            "ca": ["2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"],
        }
    )

    pipeline = TransformationPipeline()
    result = pipeline.execute(df, version)

    assert result.valid_record_count == 1
    assert result.invalid_record_count == 2
    assert any(e.row_index == 1 and e.rule_id == "EMAIL_FORMAT" for e in result.all_errors)
    assert any(e.row_index == 2 and e.rule_id == "EMAIL_FORMAT" for e in result.all_errors)


def test_validation_status_enum() -> None:
    # 28. ENUM rule: status must be in customer.v1 allowed values (ACTIVE, INACTIVE)
    version = _build_full_approved_version("test_enum", "id", "fn", "ln", "em", "st", "ca")
    df = pl.DataFrame(
        {
            "id": ["C-1", "C-2", "C-3"],
            "fn": ["Alice", "Bob", "Charlie"],
            "ln": ["Smith", "Jones", "Taylor"],
            "em": ["alice@ex.com", "bob@ex.com", "charlie@ex.com"],
            "st": ["ACTIVE", "INACTIVE", "SUSPENDED"],  # SUSPENDED not allowed in canonical enum
            "ca": ["2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"],
        }
    )

    pipeline = TransformationPipeline()
    result = pipeline.execute(df, version)

    assert result.valid_record_count == 2
    assert result.invalid_record_count == 1
    err = next(e for e in result.all_errors if e.row_index == 2)
    assert err.rule_id == "STATUS_ENUM"
    assert err.category == ValidationCategory.ENUM
    assert err.observed_value == "SUSPENDED"


def test_validation_duplicate_customer_id() -> None:
    # 29. UNIQUE / DUPLICATE: customer_id must be unique across the batch
    version = _build_full_approved_version("test_dup", "id", "fn", "ln", "em", "st", "ca")
    df = pl.DataFrame(
        {
            "id": ["C-100", "C-200", "C-100"],  # C-100 appears twice
            "fn": ["Alice", "Bob", "Alice Duplicate"],
            "ln": ["Smith", "Jones", "Smith"],
            "em": ["alice1@ex.com", "bob@ex.com", "alice2@ex.com"],
            "st": ["ACTIVE", "ACTIVE", "ACTIVE"],
            "ca": ["2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"],
        }
    )

    pipeline = TransformationPipeline()
    result = pipeline.execute(df, version)

    # Only Bob (C-200) is valid; both C-100 records are invalid
    assert result.valid_record_count == 1
    assert result.invalid_record_count == 2

    dup_errors = [e for e in result.all_errors if e.rule_id == "CUSTOMER_ID_DUPLICATE"]
    assert len(dup_errors) == 2
    assert {e.row_index for e in dup_errors} == {0, 2}
    assert all(e.category == ValidationCategory.DUPLICATE for e in dup_errors)
    assert all(e.observed_value == "C-100" for e in dup_errors)


def test_multiple_errors_on_one_record() -> None:
    # 32. Multiple errors on a single record
    version = _build_full_approved_version("test_multi", "id", "fn", "ln", "em", "st", "ca")
    df = pl.DataFrame(
        {
            "id": [None],                   # missing customer_id
            "fn": [""],                     # missing first_name
            "ln": ["Smith"],
            "em": ["broken@@domain"],       # invalid email format
            "st": ["INVALID_STATUS"],       # invalid enum value
            "ca": ["2026-01-01T00:00:00Z"],
        }
    )

    pipeline = TransformationPipeline()
    result = pipeline.execute(df, version)

    assert result.invalid_record_count == 1
    inv_rec = result.invalid_records[0]
    error_rule_ids = {e.rule_id for e in inv_rec.errors}

    assert "CUSTOMER_ID_REQUIRED" in error_rule_ids
    assert "FIRST_NAME_REQUIRED" in error_rule_ids
    assert "EMAIL_FORMAT" in error_rule_ids
    assert "STATUS_ENUM" in error_rule_ids
    assert len(inv_rec.errors) == 4


def test_valid_records_separated_and_polars_export() -> None:
    # 33 & 34. Valid records separated from invalid records, and typed Polars export
    version = _build_full_approved_version("test_export", "id", "fn", "ln", "em", "st", "ca", dob_col="dob")
    df = pl.DataFrame(
        {
            "id": ["C-1", "C-2", "C-3"],
            "fn": ["Alice", "", "Charlie"],
            "ln": ["Smith", "Jones", "Taylor"],
            "em": ["alice@ex.com", "bob@ex.com", "charlie@ex.com"],
            "dob": ["1990-05-12", "1985-11-20", "1995-03-30"],
            "st": ["ACTIVE", "ACTIVE", "ACTIVE"],
            "ca": ["2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"],
        }
    )

    pipeline = TransformationPipeline()
    result = pipeline.execute(df, version)

    assert result.valid_record_count == 2
    assert result.invalid_record_count == 1

    valid_df = result.to_valid_polars_df()
    assert isinstance(valid_df, pl.DataFrame)
    assert valid_df.shape == (2, 7)
    assert valid_df.columns == [
        "customer_id",
        "first_name",
        "last_name",
        "email",
        "date_of_birth",
        "status",
        "created_at",
    ]
    # Check exact Polars dtypes
    assert valid_df.schema["customer_id"] == pl.String
    assert valid_df.schema["date_of_birth"] == pl.Date
    assert valid_df.schema["created_at"] == pl.Datetime(time_zone="UTC")


def test_business_rule_and_referential_integrity() -> None:
    # 30 & 31. Business rule and referential integrity
    version = _build_full_approved_version("test_br", "id", "fn", "ln", "em", "st", "ca", dob_col="dob")
    df = pl.DataFrame(
        {
            "id": ["C-1"],
            "fn": ["Alice"],
            "ln": ["Smith"],
            "em": ["alice@ex.com"],
            "dob": ["2099-01-01"],  # Future date of birth
            "st": ["ACTIVE"],
            "ca": ["2026-01-01T00:00:00Z"],
        }
    )

    cfg = ValidationConfig(
        disallow_future_dob=True,
        reference_datasets={"customer_id": {"C-999"}},  # C-1 is not in reference set
    )
    pipeline = TransformationPipeline(validation_config=cfg)
    result = pipeline.execute(df, version)

    assert result.valid_record_count == 0
    rule_ids = {e.rule_id for e in result.all_errors}
    assert "DATE_OF_BIRTH_NOT_IN_PAST" in rule_ids
    assert "CUSTOMER_ID_REFERENTIAL_INTEGRITY" in rule_ids


def test_crm_fixture_end_to_end_transformation(crm_csv_path: Path) -> None:
    # 44. CRM fixture transformation
    defn = SourceDefinition(
        source_id="crm_e2e",
        source_name="CRM E2E",
        source_type=SourceType.CSV,
        connection_config={"file_path": str(crm_csv_path)},
    )
    adapter = CSVSourceAdapter(defn)
    df = adapter.read_data()

    enum_mapping = {
        "ACTIVE": "ACTIVE",
        "INACTIVE": "INACTIVE",
        "PENDING": "INACTIVE",
        "SUSPENDED": "INACTIVE",
        "CLOSED": "INACTIVE",
    }
    version = _build_full_approved_version(
        source_id="crm_e2e",
        id_col="Cust_ID",
        fn_col="First Name",
        ln_col="Last Name",
        email_col="Contact Email",
        status_col="Cust_Status",
        ca_col="Created_Timestamp",
        dob_col="Birth_Date",
        enum_map=enum_mapping,
    )

    pipeline = TransformationPipeline()
    result = pipeline.execute(df, version)

    assert isinstance(result, TransformationResult)
    assert result.total_records == len(df)
    assert result.valid_record_count > 0
    valid_df = result.to_valid_polars_df()
    assert valid_df.shape[0] == result.valid_record_count


def test_billing_fixture_end_to_end_transformation(billing_csv_path: Path) -> None:
    # 45. Billing fixture transformation
    defn = SourceDefinition(
        source_id="billing_e2e",
        source_name="Billing E2E",
        source_type=SourceType.CSV,
        connection_config={"file_path": str(billing_csv_path)},
    )
    adapter = CSVSourceAdapter(defn)
    df = adapter.read_data()

    # In billing fixture, full name exists instead of first/last, and acct_code is ID
    # Use CONCAT to map client_full_name to first_name, and custom static or split
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
            provenance_reason="Name",
        ),
        ApprovedMappingDefinition(
            source_field="client_full_name",
            target_field="last_name",
            mapping_type=MappingType.DIRECT,
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
            confidence=1.0,
            provenance_reason="Name",
        ),
        ApprovedMappingDefinition(
            source_field="billing_email",
            target_field="email",
            mapping_type=MappingType.TRANSFORMED,
            transformations=[TransformationStep(op=TransformationOpType.NORMALIZE_EMAIL)],
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
                    params={"enum_map": {"CURRENT": "ACTIVE", "DELINQUENT": "INACTIVE", "CLOSED": "INACTIVE"}},
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
    version = ApprovedMappingVersion(
        mapping_version_id="map_ver_billing_e2e_v1",
        source_id="billing_e2e",
        source_fingerprint="billing_fingerprint",
        canonical_schema_name="customer",
        canonical_schema_version=1,
        version_number=1,
        mappings=mappings,
        is_complete=True,
        approved_by="steward",
    )

    pipeline = TransformationPipeline()
    result = pipeline.execute(df, version)

    assert result.total_records == len(df)
    assert result.valid_record_count > 0


def test_support_fixture_end_to_end_transformation(support_csv_path: Path) -> None:
    # 46. Support fixture transformation
    defn = SourceDefinition(
        source_id="support_e2e",
        source_name="Support E2E",
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
    version = ApprovedMappingVersion(
        mapping_version_id="map_ver_support_e2e_v1",
        source_id="support_e2e",
        source_fingerprint="support_fingerprint",
        canonical_schema_name="customer",
        canonical_schema_version=1,
        version_number=1,
        mappings=mappings,
        is_complete=True,
        approved_by="steward",
    )

    pipeline = TransformationPipeline()
    result = pipeline.execute(df, version)

    assert result.total_records == len(df)
    assert result.valid_record_count > 0


def test_artifact_persistence_roundtrip(tmp_path: Path) -> None:
    # 34. Serialization and JSON roundtrip of TransformationResult
    res = TransformationResult(
        source_id="art_src",
        mapping_version_id="map_ver_1",
        canonical_schema_name="customer",
        canonical_schema_version=1,
        total_records=2,
        valid_record_count=1,
        invalid_record_count=1,
        valid_records=[
            CanonicalCustomerRecord(
                customer_id="C-1",
                first_name="Alice",
                last_name="Smith",
                email="alice@example.com",
                date_of_birth=date(1990, 1, 1),
                status="ACTIVE",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        ],
        invalid_records=[
            TransformedRecord(
                row_index=1,
                source_record_id="C-2",
                raw_values={"id": "C-2"},
                canonical_values={"customer_id": "C-2"},
                is_valid=False,
                errors=[
                    RecordValidationError(
                        row_index=1,
                        record_id="C-2",
                        rule_id="EMAIL_REQUIRED",
                        category=ValidationCategory.REQUIRED,
                        field="email",
                        observed_value=None,
                        reason="Mandatory email is missing",
                    )
                ],
            )
        ],
        all_errors=[
            RecordValidationError(
                row_index=1,
                record_id="C-2",
                rule_id="EMAIL_REQUIRED",
                category=ValidationCategory.REQUIRED,
                field="email",
                observed_value=None,
                reason="Mandatory email is missing",
            )
        ],
    )

    out_file = tmp_path / "transformation_result.json"
    saved = res.save_result_artifact(out_file)
    assert saved.exists()

    reloaded = TransformationResult.load_result_artifact(saved)
    assert reloaded.source_id == res.source_id
    assert reloaded.valid_record_count == 1
    assert reloaded.invalid_record_count == 1
    assert reloaded.valid_records[0].customer_id == "C-1"
    assert reloaded.invalid_records[0].errors[0].rule_id == "EMAIL_REQUIRED"
