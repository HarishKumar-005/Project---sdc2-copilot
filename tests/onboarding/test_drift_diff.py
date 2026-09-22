"""Unit tests for deterministic schema diffing and drift categorization."""

from __future__ import annotations

from src.scd2_copilot.onboarding.drift.engine import DeterministicSchemaDiffEngine
from src.scd2_copilot.onboarding.models.drift import DriftType
from src.scd2_copilot.onboarding.models.schema_snapshot import (
    ColumnSnapshot,
    SourceSchemaSnapshot,
)
from src.scd2_copilot.onboarding.profiler.fingerprint import compute_schema_fingerprint


def _make_snapshot(source_id: str, version: int, columns: list[ColumnSnapshot]) -> SourceSchemaSnapshot:
    fp = compute_schema_fingerprint(columns)
    return SourceSchemaSnapshot(
        source_id=source_id,
        schema_version=version,
        fingerprint=fp,
        columns=columns,
    )


def test_identical_schemas_produce_zero_drift():
    cols = [
        ColumnSnapshot(
            original_name="cust_id",
            normalized_name="cust_id",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=0,
        ),
        ColumnSnapshot(
            original_name="email",
            normalized_name="email",
            inferred_type="string",
            polars_type="String",
            nullable=True,
            ordinal_position=1,
        ),
    ]
    s1 = _make_snapshot("crm", 1, cols)
    s2 = _make_snapshot("crm", 2, cols)

    engine = DeterministicSchemaDiffEngine()
    result = engine.diff(s1, s2)

    assert result.has_drift is False
    assert len(result.drift_events) == 0
    assert result.prior_schema_version == 1
    assert result.current_schema_version == 2
    assert result.prior_fingerprint == s1.fingerprint.fingerprint_hash
    assert result.current_fingerprint == s2.fingerprint.fingerprint_hash


def test_added_column_detection():
    cols_v1 = [
        ColumnSnapshot(
            original_name="id",
            normalized_name="id",
            inferred_type="integer",
            polars_type="Int64",
            nullable=False,
            ordinal_position=0,
        )
    ]
    cols_v2 = [
        ColumnSnapshot(
            original_name="id",
            normalized_name="id",
            inferred_type="integer",
            polars_type="Int64",
            nullable=False,
            ordinal_position=0,
        ),
        ColumnSnapshot(
            original_name="tier",
            normalized_name="tier",
            inferred_type="string",
            polars_type="String",
            nullable=True,
            ordinal_position=1,
        ),
    ]
    s1 = _make_snapshot("crm", 1, cols_v1)
    s2 = _make_snapshot("crm", 2, cols_v2)

    engine = DeterministicSchemaDiffEngine()
    result = engine.diff(s1, s2)

    assert result.has_drift is True
    assert len(result.drift_events) == 1
    event = result.drift_events[0]
    assert event.drift_type == DriftType.ADDED_COLUMN
    assert event.field_name == "tier"
    assert event.new_type == "string"


def test_removed_column_detection():
    cols_v1 = [
        ColumnSnapshot(
            original_name="id",
            normalized_name="id",
            inferred_type="integer",
            polars_type="Int64",
            nullable=False,
            ordinal_position=0,
        ),
        ColumnSnapshot(
            original_name="fax",
            normalized_name="fax",
            inferred_type="string",
            polars_type="String",
            nullable=True,
            ordinal_position=1,
        ),
    ]
    cols_v2 = [
        ColumnSnapshot(
            original_name="id",
            normalized_name="id",
            inferred_type="integer",
            polars_type="Int64",
            nullable=False,
            ordinal_position=0,
        )
    ]
    s1 = _make_snapshot("crm", 1, cols_v1)
    s2 = _make_snapshot("crm", 2, cols_v2)

    engine = DeterministicSchemaDiffEngine()
    result = engine.diff(s1, s2)

    assert result.has_drift is True
    assert len(result.drift_events) == 1
    event = result.drift_events[0]
    assert event.drift_type == DriftType.REMOVED_COLUMN
    assert event.field_name == "fax"


def test_type_changed_detection():
    cols_v1 = [
        ColumnSnapshot(
            original_name="balance",
            normalized_name="balance",
            inferred_type="float",
            polars_type="Float64",
            nullable=False,
            ordinal_position=0,
        )
    ]
    cols_v2 = [
        ColumnSnapshot(
            original_name="balance",
            normalized_name="balance",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=0,
        )
    ]
    s1 = _make_snapshot("billing", 1, cols_v1)
    s2 = _make_snapshot("billing", 2, cols_v2)

    engine = DeterministicSchemaDiffEngine()
    result = engine.diff(s1, s2)

    assert result.has_drift is True
    assert len(result.drift_events) == 1
    event = result.drift_events[0]
    assert event.drift_type == DriftType.TYPE_CHANGED
    assert event.field_name == "balance"
    assert "float" in event.old_type.lower()
    assert "string" in event.new_type.lower()


def test_nullability_changed_detection():
    cols_v1 = [
        ColumnSnapshot(
            original_name="email",
            normalized_name="email",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=0,
        )
    ]
    cols_v2 = [
        ColumnSnapshot(
            original_name="email",
            normalized_name="email",
            inferred_type="string",
            polars_type="String",
            nullable=True,
            ordinal_position=0,
        )
    ]
    s1 = _make_snapshot("crm", 1, cols_v1)
    s2 = _make_snapshot("crm", 2, cols_v2)

    engine = DeterministicSchemaDiffEngine()
    result = engine.diff(s1, s2)

    assert result.has_drift is True
    assert len(result.drift_events) == 1
    event = result.drift_events[0]
    assert event.drift_type == DriftType.NULLABILITY_CHANGED
    assert event.field_name == "email"
    assert event.old_nullable is False
    assert event.new_nullable is True


def test_possible_rename_detection():
    cols_v1 = [
        ColumnSnapshot(
            original_name="signup_date",
            normalized_name="signup_date",
            inferred_type="date",
            polars_type="Date",
            nullable=False,
            ordinal_position=0,
        )
    ]
    cols_v2 = [
        ColumnSnapshot(
            original_name="registration_date",
            normalized_name="registration_date",
            inferred_type="date",
            polars_type="Date",
            nullable=False,
            ordinal_position=0,
        )
    ]
    s1 = _make_snapshot("crm", 1, cols_v1)
    s2 = _make_snapshot("crm", 2, cols_v2)

    engine = DeterministicSchemaDiffEngine()
    result = engine.diff(s1, s2)

    assert result.has_drift is True
    # Should have REMOVED, ADDED, and POSSIBLE_RENAME events
    types = [e.drift_type for e in result.drift_events]
    assert DriftType.REMOVED_COLUMN in types
    assert DriftType.ADDED_COLUMN in types
    assert DriftType.POSSIBLE_RENAME in types

    rename_event = next(e for e in result.drift_events if e.drift_type == DriftType.POSSIBLE_RENAME)
    assert rename_event.old_field_name == "signup_date"
    assert rename_event.new_field_name == "registration_date"
    assert rename_event.similarity_score is not None
    assert rename_event.similarity_score >= 0.6


def test_deterministic_diff_reproducibility():
    cols_v1 = [
        ColumnSnapshot(
            original_name="user_id",
            normalized_name="user_id",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=0,
        ),
        ColumnSnapshot(
            original_name="balance",
            normalized_name="balance",
            inferred_type="float",
            polars_type="Float64",
            nullable=False,
            ordinal_position=1,
        ),
    ]
    cols_v2 = [
        ColumnSnapshot(
            original_name="user_id",
            normalized_name="user_id",
            inferred_type="string",
            polars_type="String",
            nullable=True,
            ordinal_position=0,
        ),
        ColumnSnapshot(
            original_name="balance",
            normalized_name="balance",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=1,
        ),
        ColumnSnapshot(
            original_name="new_flag",
            normalized_name="new_flag",
            inferred_type="boolean",
            polars_type="Boolean",
            nullable=False,
            ordinal_position=2,
        ),
    ]
    s1 = _make_snapshot("mix", 1, cols_v1)
    s2 = _make_snapshot("mix", 2, cols_v2)

    engine = DeterministicSchemaDiffEngine()
    r1 = engine.diff(s1, s2)
    r2 = engine.diff(s1, s2)

    assert r1.has_drift == r2.has_drift
    assert len(r1.drift_events) == len(r2.drift_events)
    for e1, e2 in zip(r1.drift_events, r2.drift_events):
        assert e1.drift_type == e2.drift_type
        assert e1.field_name == e2.field_name
        assert e1.details == e2.details
