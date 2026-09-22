"""Tests for deterministic schema fingerprinting and column name normalization."""

from scd2_copilot.onboarding.models.schema_snapshot import ColumnSnapshot
from scd2_copilot.onboarding.profiler.fingerprint import (
    compute_schema_fingerprint,
    normalize_column_name,
)


def test_normalize_column_name() -> None:
    assert normalize_column_name("Cust_ID") == "cust_id"
    assert normalize_column_name("First Name") == "first_name"
    assert normalize_column_name("Phone-Number#") == "phone_number"
    assert normalize_column_name("  Email Address  ") == "email_address"
    assert normalize_column_name("___RAW__DATA___") == "raw_data"
    assert normalize_column_name("") == "col"


def test_schema_fingerprint_determinism() -> None:
    cols1 = [
        ColumnSnapshot(
            original_name="id",
            normalized_name="id",
            inferred_type="integer",
            polars_type="Int64",
            nullable=False,
            ordinal_position=0,
        ),
        ColumnSnapshot(
            original_name="name",
            normalized_name="name",
            inferred_type="string",
            polars_type="String",
            nullable=True,
            ordinal_position=1,
        ),
    ]

    cols2 = [
        ColumnSnapshot(
            original_name="name",
            normalized_name="name",
            inferred_type="string",
            polars_type="String",
            nullable=True,
            ordinal_position=1,
        ),
        ColumnSnapshot(
            original_name="id",
            normalized_name="id",
            inferred_type="integer",
            polars_type="Int64",
            nullable=False,
            ordinal_position=0,
        ),
    ]

    fp1 = compute_schema_fingerprint(cols1)
    fp2 = compute_schema_fingerprint(cols2)

    assert fp1.fingerprint_hash == fp2.fingerprint_hash
    assert fp1.column_count == 2
    assert fp1.normalized_signature == fp2.normalized_signature


def test_schema_fingerprint_detects_drift() -> None:
    base_cols = [
        ColumnSnapshot(
            original_name="id",
            normalized_name="id",
            inferred_type="integer",
            polars_type="Int64",
            nullable=False,
            ordinal_position=0,
        ),
        ColumnSnapshot(
            original_name="name",
            normalized_name="name",
            inferred_type="string",
            polars_type="String",
            nullable=True,
            ordinal_position=1,
        ),
    ]
    base_fp = compute_schema_fingerprint(base_cols)

    # Added column
    added_cols = base_cols + [
        ColumnSnapshot(
            original_name="email",
            normalized_name="email",
            inferred_type="string",
            polars_type="String",
            nullable=True,
            ordinal_position=2,
        )
    ]
    added_fp = compute_schema_fingerprint(added_cols)
    assert base_fp.fingerprint_hash != added_fp.fingerprint_hash

    # Changed type
    type_changed_cols = [
        ColumnSnapshot(
            original_name="id",
            normalized_name="id",
            inferred_type="string",  # changed from integer to string
            polars_type="String",
            nullable=False,
            ordinal_position=0,
        ),
        base_cols[1],
    ]
    type_fp = compute_schema_fingerprint(type_changed_cols)
    assert base_fp.fingerprint_hash != type_fp.fingerprint_hash
