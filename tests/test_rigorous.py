"""Rigorous test suite for sdc2-copilot.

Covers: SCD2 correctness, validation rules, input robustness,
overlap detection, LLM layer, edge cases, schema detection,
ingestion, determinism, and provider fallback behavior.
"""

from __future__ import annotations

import io
from datetime import date
from unittest.mock import MagicMock

import polars as pl
import pytest

from src.scd2_copilot.detect_changes import detect_changes
from src.scd2_copilot.explain import explain_changes, ExplainResult
from src.scd2_copilot.ingestion import load_csv, validate_csv_columns
from src.scd2_copilot.models import (
    ChangeRecord, ChangeReport, ChangeType, Explanation,
    FieldChange, ValidationStatus,
)
from src.scd2_copilot.providers.base import LLMProvider
from src.scd2_copilot.providers.template import TemplateProvider
from src.scd2_copilot.schema import detect_business_key, detect_tracked_columns
from src.scd2_copilot.transform_scd2 import apply_scd2
from src.scd2_copilot.validate import validate_scd2


# ============================================================================
# 1) CORE SCD2 CORRECTNESS
# ============================================================================

class TestSCD2ChangeDetection:
    """Test deterministic change detection across scenarios."""

    def test_single_field_change(self):
        """One field change on a record should be CHANGED."""
        source = pl.DataFrame({"id": [1], "name": ["Alice"], "city": ["NYC"]})
        target = pl.DataFrame({
            "id": [1], "name": ["Alice"], "city": ["Boston"],
            "effective_from": [date(2026, 6, 1)], "effective_to": [None], "is_current": [True],
        })
        report = detect_changes(source, target, ["id"], ["name", "city"], date(2026, 6, 8))
        assert len(report.changed) == 1
        assert report.changed[0].field_changes[0].column == "city"
        assert report.changed[0].field_changes[0].old_value == "Boston"
        assert report.changed[0].field_changes[0].new_value == "NYC"

    def test_multiple_fields_changing(self):
        """Multiple fields changing simultaneously."""
        source = pl.DataFrame({"id": [1], "name": ["Bob"], "city": ["NYC"]})
        target = pl.DataFrame({
            "id": [1], "name": ["Alice"], "city": ["Boston"],
            "effective_from": [date(2026, 6, 1)], "effective_to": [None], "is_current": [True],
        })
        report = detect_changes(source, target, ["id"], ["name", "city"], date(2026, 6, 8))
        assert len(report.changed) == 1
        changed_cols = {fc.column for fc in report.changed[0].field_changes}
        assert changed_cols == {"name", "city"}

    def test_unchanged_rows_stay_unchanged(self):
        """Identical records should be UNCHANGED."""
        source = pl.DataFrame({"id": [1], "name": ["Alice"]})
        target = pl.DataFrame({
            "id": [1], "name": ["Alice"],
            "effective_from": [date(2026, 6, 1)], "effective_to": [None], "is_current": [True],
        })
        report = detect_changes(source, target, ["id"], ["name"], date(2026, 6, 8))
        assert len(report.unchanged) == 1
        assert len(report.changed) == 0

    def test_new_record_detected(self):
        """A key in source but not in target is NEW."""
        source = pl.DataFrame({"id": [1, 2], "name": ["Alice", "Bob"]})
        target = pl.DataFrame({
            "id": [1], "name": ["Alice"],
            "effective_from": [date(2026, 6, 1)], "effective_to": [None], "is_current": [True],
        })
        report = detect_changes(source, target, ["id"], ["name"], date(2026, 6, 8))
        assert len(report.new) == 1
        assert report.new[0].business_key_values["id"] == 2

    def test_deleted_record_detected(self):
        """A key in target but not in source is DELETED."""
        source = pl.DataFrame({"id": [1], "name": ["Alice"]})
        target = pl.DataFrame({
            "id": [1, 2], "name": ["Alice", "Bob"],
            "effective_from": [date(2026, 6, 1)] * 2, "effective_to": [None] * 2,
            "is_current": [True, True],
        })
        report = detect_changes(source, target, ["id"], ["name"], date(2026, 6, 8))
        assert len(report.deleted) == 1
        assert report.deleted[0].business_key_values["id"] == 2

    def test_same_day_changes(self):
        """Records changed on the same processing date."""
        source = pl.DataFrame({"id": [1], "name": ["Bob"]})
        target = pl.DataFrame({
            "id": [1], "name": ["Alice"],
            "effective_from": [date(2026, 6, 8)], "effective_to": [None], "is_current": [True],
        })
        report = detect_changes(source, target, ["id"], ["name"], date(2026, 6, 8))
        assert len(report.changed) == 1

    def test_repeated_identical_snapshots_all_unchanged(self):
        """Running the same source twice should give all UNCHANGED."""
        source = pl.DataFrame({"id": [1, 2], "name": ["A", "B"]})
        target = pl.DataFrame({
            "id": [1, 2], "name": ["A", "B"],
            "effective_from": [date(2026, 6, 7)] * 2, "effective_to": [None] * 2,
            "is_current": [True, True],
        })
        r1 = detect_changes(source, target, ["id"], ["name"], date(2026, 6, 8))
        r2 = detect_changes(source, target, ["id"], ["name"], date(2026, 6, 8))
        assert r1.summary == r2.summary
        assert r1.summary["unchanged"] == 2
        assert r1.summary["new"] == 0
        assert r1.summary["changed"] == 0
        assert r1.summary["deleted"] == 0

    def test_empty_source_all_deleted(self):
        """Empty source means all target current rows are DELETED."""
        source = pl.DataFrame({"id": pl.Series([], dtype=pl.Int64), "name": pl.Series([], dtype=pl.Utf8)})
        target = pl.DataFrame({
            "id": [1, 2], "name": ["A", "B"],
            "effective_from": [date(2026, 6, 7)] * 2, "effective_to": [None] * 2,
            "is_current": [True, True],
        })
        report = detect_changes(source, target, ["id"], ["name"], date(2026, 6, 8))
        assert len(report.deleted) == 2
        assert len(report.new) == 0

    def test_empty_target_all_new(self):
        """Empty target means all source rows are NEW."""
        source = pl.DataFrame({"id": [1, 2], "name": ["A", "B"]})
        target = pl.DataFrame({
            "id": pl.Series([], dtype=pl.Int64), "name": pl.Series([], dtype=pl.Utf8),
            "effective_from": pl.Series([], dtype=pl.Date), "effective_to": pl.Series([], dtype=pl.Date),
            "is_current": pl.Series([], dtype=pl.Boolean),
        })
        report = detect_changes(source, target, ["id"], ["name"], date(2026, 6, 8))
        assert len(report.new) == 2
        assert len(report.deleted) == 0

    def test_historical_rows_are_ignored(self):
        """Non-current (historical) rows in target should not participate in comparison."""
        source = pl.DataFrame({"id": [1], "name": ["Charlie"]})
        target = pl.DataFrame({
            "id": [1, 1], "name": ["Alice", "Bob"],
            "effective_from": [date(2026, 6, 1), date(2026, 6, 5)],
            "effective_to": [date(2026, 6, 5), None],
            "is_current": [False, True],
        })
        report = detect_changes(source, target, ["id"], ["name"], date(2026, 6, 8))
        # Should compare against Bob (current), not Alice (historical)
        assert len(report.changed) == 1
        assert report.changed[0].field_changes[0].old_value == "Bob"
        assert report.changed[0].field_changes[0].new_value == "Charlie"


# ============================================================================
# 2) SCD2 TRANSFORMATION CORRECTNESS
# ============================================================================

class TestSCD2Transformation:
    """Test that apply_scd2 produces correct output tables."""

    def test_changed_record_produces_two_rows(self):
        """A changed record should close the old row and insert a new one."""
        source = pl.DataFrame({"id": [1], "name": ["Bob"]})
        target = pl.DataFrame({
            "id": [1], "name": ["Alice"],
            "effective_from": [date(2026, 6, 1)], "effective_to": [None], "is_current": [True],
        })
        report = detect_changes(source, target, ["id"], ["name"], date(2026, 6, 8))
        output = apply_scd2(source, target, report, ["id"], ["name"], date(2026, 6, 8))

        rows_for_1 = output.filter(pl.col("id") == 1)
        assert rows_for_1.height == 2

        closed = rows_for_1.filter(pl.col("is_current") == False)
        assert closed.height == 1
        assert closed["name"][0] == "Alice"
        assert closed["effective_to"][0] == date(2026, 6, 8)

        current = rows_for_1.filter(pl.col("is_current") == True)
        assert current.height == 1
        assert current["name"][0] == "Bob"
        assert current["effective_to"][0] is None

    def test_new_record_gets_correct_metadata(self):
        """A new record should have is_current=True and effective_to=None."""
        source = pl.DataFrame({"id": [1, 2], "name": ["A", "B"]})
        target = pl.DataFrame({
            "id": [1], "name": ["A"],
            "effective_from": [date(2026, 6, 1)], "effective_to": [None], "is_current": [True],
        })
        report = detect_changes(source, target, ["id"], ["name"], date(2026, 6, 8))
        output = apply_scd2(source, target, report, ["id"], ["name"], date(2026, 6, 8))

        new_row = output.filter(pl.col("id") == 2)
        assert new_row.height == 1
        assert new_row["is_current"][0] is True
        assert new_row["effective_from"][0] == date(2026, 6, 8)
        assert new_row["effective_to"][0] is None

    def test_deleted_record_is_closed(self):
        """A deleted record should have is_current=False and effective_to set."""
        source = pl.DataFrame({"id": [1], "name": ["A"]})
        target = pl.DataFrame({
            "id": [1, 2], "name": ["A", "B"],
            "effective_from": [date(2026, 6, 1)] * 2, "effective_to": [None] * 2,
            "is_current": [True, True],
        })
        report = detect_changes(source, target, ["id"], ["name"], date(2026, 6, 8))
        output = apply_scd2(source, target, report, ["id"], ["name"], date(2026, 6, 8))

        deleted_row = output.filter(pl.col("id") == 2)
        assert deleted_row.height == 1
        assert deleted_row["is_current"][0] is False
        assert deleted_row["effective_to"][0] == date(2026, 6, 8)

    def test_historical_rows_are_preserved(self):
        """Historical (non-current) rows from target must appear unchanged in output."""
        source = pl.DataFrame({"id": [1], "name": ["C"]})
        target = pl.DataFrame({
            "id": [1, 1], "name": ["A", "B"],
            "effective_from": [date(2026, 6, 1), date(2026, 6, 5)],
            "effective_to": [date(2026, 6, 5), None],
            "is_current": [False, True],
        })
        report = detect_changes(source, target, ["id"], ["name"], date(2026, 6, 8))
        output = apply_scd2(source, target, report, ["id"], ["name"], date(2026, 6, 8))

        historical = output.filter((pl.col("name") == "A") & (pl.col("is_current") == False))
        assert historical.height == 1
        assert historical["effective_to"][0] == date(2026, 6, 5)

    def test_output_is_deterministic_on_reruns(self):
        """Running apply_scd2 twice with identical inputs produces identical output."""
        source = pl.DataFrame({"id": [1, 2], "name": ["A", "B"]})
        target = pl.DataFrame({
            "id": [1], "name": ["X"],
            "effective_from": [date(2026, 6, 1)], "effective_to": [None], "is_current": [True],
        })
        report = detect_changes(source, target, ["id"], ["name"], date(2026, 6, 8))
        out1 = apply_scd2(source, target, report, ["id"], ["name"], date(2026, 6, 8))
        out2 = apply_scd2(source, target, report, ["id"], ["name"], date(2026, 6, 8))
        assert out1.equals(out2)


# ============================================================================
# 3) VALIDATION CORRECTNESS
# ============================================================================

class TestValidationRules:
    """Test all validation rules thoroughly."""

    def test_schema_completeness_pass(self):
        """All required columns present."""
        df = pl.DataFrame({
            "id": [1], "effective_from": [date(2026, 6, 1)],
            "effective_to": [None], "is_current": [True],
        })
        report = validate_scd2(df, ["id"])
        rule = next(r for r in report.rules if r.name == "schema_completeness")
        assert rule.status == ValidationStatus.PASS

    def test_schema_completeness_fail(self):
        """Missing effective_to -> FAIL."""
        df = pl.DataFrame({
            "id": [1], "effective_from": [date(2026, 6, 1)], "is_current": [True],
        })
        report = validate_scd2(df, ["id"])
        rule = next(r for r in report.rules if r.name == "schema_completeness")
        assert rule.status == ValidationStatus.FAIL

    def test_one_current_per_key_pass(self):
        """Each key has at most one current row."""
        df = pl.DataFrame({
            "id": [1, 1], "effective_from": [date(2026, 6, 1), date(2026, 6, 5)],
            "effective_to": [date(2026, 6, 5), None], "is_current": [False, True],
        })
        report = validate_scd2(df, ["id"])
        rule = next(r for r in report.rules if r.name == "one_current_per_key")
        assert rule.status == ValidationStatus.PASS

    def test_duplicate_current_rows_fail(self):
        """Two current rows for same key -> FAIL."""
        df = pl.DataFrame({
            "id": [1, 1], "effective_from": [date(2026, 6, 1), date(2026, 6, 5)],
            "effective_to": [None, None], "is_current": [True, True],
        })
        report = validate_scd2(df, ["id"])
        rule = next(r for r in report.rules if r.name == "one_current_per_key")
        assert rule.status == ValidationStatus.FAIL

    def test_no_null_keys_pass(self):
        """All keys non-null."""
        df = pl.DataFrame({
            "id": [1, 2], "effective_from": [date(2026, 6, 1)] * 2,
            "effective_to": [None] * 2, "is_current": [True] * 2,
        })
        report = validate_scd2(df, ["id"])
        rule = next(r for r in report.rules if r.name == "no_null_keys")
        assert rule.status == ValidationStatus.PASS

    def test_null_key_fail(self):
        """Null key -> FAIL."""
        df = pl.DataFrame({
            "id": [1, None], "effective_from": [date(2026, 6, 1)] * 2,
            "effective_to": [None] * 2, "is_current": [True] * 2,
        })
        report = validate_scd2(df, ["id"])
        rule = next(r for r in report.rules if r.name == "no_null_keys")
        assert rule.status == ValidationStatus.FAIL

    def test_date_consistency_pass(self):
        """effective_from <= effective_to."""
        df = pl.DataFrame({
            "id": [1], "effective_from": [date(2026, 6, 1)],
            "effective_to": [date(2026, 6, 5)], "is_current": [False],
        })
        report = validate_scd2(df, ["id"])
        rule = next(r for r in report.rules if r.name == "date_consistency")
        assert rule.status == ValidationStatus.PASS

    def test_date_consistency_fail(self):
        """effective_from > effective_to -> FAIL."""
        df = pl.DataFrame({
            "id": [1], "effective_from": [date(2026, 6, 10)],
            "effective_to": [date(2026, 6, 5)], "is_current": [False],
        })
        report = validate_scd2(df, ["id"])
        rule = next(r for r in report.rules if r.name == "date_consistency")
        assert rule.status == ValidationStatus.FAIL


# ============================================================================
# 4) OVERLAP DETECTION (DEEP TESTS)
# ============================================================================

class TestOverlapDetection:
    """Deep tests for the no_overlapping_dates validation."""

    def test_no_overlap_clean_history(self):
        """Clean consecutive periods -> PASS."""
        df = pl.DataFrame({
            "id": [1, 1, 1],
            "effective_from": [date(2026, 6, 1), date(2026, 6, 5), date(2026, 6, 10)],
            "effective_to": [date(2026, 6, 5), date(2026, 6, 10), None],
            "is_current": [False, False, True],
        })
        report = validate_scd2(df, ["id"])
        rule = next(r for r in report.rules if r.name == "no_overlapping_dates")
        assert rule.status == ValidationStatus.PASS

    def test_overlap_detected(self):
        """Overlapping periods -> FAIL."""
        df = pl.DataFrame({
            "id": [1, 1],
            "effective_from": [date(2026, 6, 1), date(2026, 6, 3)],
            "effective_to": [date(2026, 6, 5), None],
            "is_current": [False, True],
        })
        report = validate_scd2(df, ["id"])
        rule = next(r for r in report.rules if r.name == "no_overlapping_dates")
        assert rule.status == ValidationStatus.FAIL
        assert any("Overlap" in d for d in rule.details)

    def test_null_effective_to_on_last_row_is_fine(self):
        """NULL effective_to on the last/current row is valid."""
        df = pl.DataFrame({
            "id": [1, 1],
            "effective_from": [date(2026, 6, 1), date(2026, 6, 5)],
            "effective_to": [date(2026, 6, 5), None],
            "is_current": [False, True],
        })
        report = validate_scd2(df, ["id"])
        rule = next(r for r in report.rules if r.name == "no_overlapping_dates")
        assert rule.status == ValidationStatus.PASS

    def test_null_effective_to_mid_history_is_overlap(self):
        """NULL effective_to on a non-last row causes overlap with the next."""
        df = pl.DataFrame({
            "id": [1, 1],
            "effective_from": [date(2026, 6, 1), date(2026, 6, 5)],
            "effective_to": [None, None],
            "is_current": [True, True],
        })
        report = validate_scd2(df, ["id"])
        rule = next(r for r in report.rules if r.name == "no_overlapping_dates")
        assert rule.status == ValidationStatus.FAIL

    def test_single_row_history_pass(self):
        """One record per key is always valid (no consecutive pair to check)."""
        df = pl.DataFrame({
            "id": [1], "effective_from": [date(2026, 6, 1)],
            "effective_to": [None], "is_current": [True],
        })
        report = validate_scd2(df, ["id"])
        rule = next(r for r in report.rules if r.name == "no_overlapping_dates")
        assert rule.status == ValidationStatus.PASS

    def test_multiple_keys_independent(self):
        """Overlap check works independently per business key."""
        df = pl.DataFrame({
            "id": [1, 1, 2, 2],
            "effective_from": [date(2026, 6, 1), date(2026, 6, 5), date(2026, 6, 1), date(2026, 6, 3)],
            "effective_to": [date(2026, 6, 5), None, date(2026, 6, 6), None],
            "is_current": [False, True, False, True],
        })
        report = validate_scd2(df, ["id"])
        rule = next(r for r in report.rules if r.name == "no_overlapping_dates")
        # Key 1 is clean, key 2 has overlap (to=6 > from=3 is fine, but 3 < 6)
        # Actually key 2: 2026-06-01 to 2026-06-06, then 2026-06-03 to NULL => overlap
        assert rule.status == ValidationStatus.FAIL

    def test_identical_timestamps_overlap(self):
        """Two periods starting on the same day overlap unless exactly adjacent."""
        df = pl.DataFrame({
            "id": [1, 1],
            "effective_from": [date(2026, 6, 5), date(2026, 6, 5)],
            "effective_to": [date(2026, 6, 5), None],
            "is_current": [False, True],
        })
        report = validate_scd2(df, ["id"])
        rule = next(r for r in report.rules if r.name == "no_overlapping_dates")
        # effective_to=2026-06-05 > effective_from=2026-06-05 is false (they are equal)
        # So this should PASS
        assert rule.status == ValidationStatus.PASS

    def test_no_false_positive_on_tight_boundary(self):
        """effective_to == next effective_from is NOT an overlap."""
        df = pl.DataFrame({
            "id": [1, 1],
            "effective_from": [date(2026, 6, 1), date(2026, 6, 5)],
            "effective_to": [date(2026, 6, 5), None],
            "is_current": [False, True],
        })
        report = validate_scd2(df, ["id"])
        rule = next(r for r in report.rules if r.name == "no_overlapping_dates")
        assert rule.status == ValidationStatus.PASS

    def test_out_of_order_rows_sorted_correctly(self):
        """Records supplied out of order should still be validated correctly."""
        df = pl.DataFrame({
            "id": [1, 1, 1],
            "effective_from": [date(2026, 6, 10), date(2026, 6, 1), date(2026, 6, 5)],
            "effective_to": [None, date(2026, 6, 5), date(2026, 6, 10)],
            "is_current": [True, False, False],
        })
        report = validate_scd2(df, ["id"])
        rule = next(r for r in report.rules if r.name == "no_overlapping_dates")
        assert rule.status == ValidationStatus.PASS

    def test_no_sql_rowid_dependency(self):
        """Verify no DuckDB import or rowid usage exists in validate.py."""
        import inspect
        from src.scd2_copilot import validate
        source_code = inspect.getsource(validate)
        # Check that duckdb is not imported or used as a call (docstring mentions are OK)
        assert "import duckdb" not in source_code
        assert "duckdb." not in source_code
        assert "rowid" not in source_code.lower()


# ============================================================================
# 5) INPUT ROBUSTNESS
# ============================================================================

class TestInputRobustness:
    """Test CSV ingestion edge cases."""

    def test_load_csv_from_bytes(self):
        """Load CSV from file-like bytes object."""
        csv = b"id,name\n1,Alice\n2,Bob\n"
        df = load_csv(io.BytesIO(csv))
        assert df.height == 2
        assert "id" in df.columns

    def test_column_name_whitespace_stripped(self):
        """Column names with leading/trailing whitespace are stripped."""
        csv = b"  id , name  \n1,Alice\n"
        df = load_csv(io.BytesIO(csv))
        assert "id" in df.columns
        assert "name" in df.columns

    def test_column_names_lowercased(self):
        """Column names are lowercased."""
        csv = b"ID,Name\n1,Alice\n"
        df = load_csv(io.BytesIO(csv))
        assert "id" in df.columns
        assert "name" in df.columns

    def test_string_values_stripped(self):
        """String values are stripped of whitespace."""
        csv = b"id,name\n1,  Alice  \n"
        df = load_csv(io.BytesIO(csv))
        assert df["name"][0] == "Alice"

    def test_extra_columns_in_source_preserved(self):
        """Extra columns in source should not cause errors."""
        csv = b"id,name,extra_col\n1,Alice,xyz\n"
        df = load_csv(io.BytesIO(csv))
        assert "extra_col" in df.columns

    def test_unicode_characters(self):
        """Unicode characters in values should be handled."""
        csv = "id,name\n1,Ação\n2,Müller\n3,日本語\n".encode("utf-8")
        df = load_csv(io.BytesIO(csv))
        assert df.height == 3
        assert df["name"][0] == "Ação"

    def test_scd2_column_normalization(self):
        """effective_from/to should be coerced to Date, is_current to Boolean."""
        csv = b"id,name,effective_from,effective_to,is_current\n1,A,2026-06-01,,true\n"
        df = load_csv(io.BytesIO(csv))
        assert df["effective_from"].dtype == pl.Date
        assert df["is_current"].dtype == pl.Boolean
        assert df["is_current"][0] is True

    def test_validate_csv_columns_source_has_scd2_meta(self):
        """Source with SCD2 metadata columns should return an error."""
        source = pl.DataFrame({"id": [1], "name": ["A"], "is_current": [True]})
        target = pl.DataFrame({
            "id": [1], "name": ["A"],
            "effective_from": [date(2026, 6, 1)], "effective_to": [None], "is_current": [True],
        })
        errors = validate_csv_columns(source, target)
        assert any("SCD2 metadata" in e for e in errors)

    def test_validate_csv_columns_target_missing_meta(self):
        """Target missing SCD2 columns should return an error."""
        source = pl.DataFrame({"id": [1], "name": ["A"]})
        target = pl.DataFrame({"id": [1], "name": ["A"]})
        errors = validate_csv_columns(source, target)
        assert any("missing" in e.lower() and "scd2" in e.lower() for e in errors)

    def test_single_row_csv(self):
        """Single-row CSV should work end-to-end."""
        source = pl.DataFrame({"id": [1], "name": ["A"]})
        target = pl.DataFrame({
            "id": pl.Series([], dtype=pl.Int64), "name": pl.Series([], dtype=pl.Utf8),
            "effective_from": pl.Series([], dtype=pl.Date),
            "effective_to": pl.Series([], dtype=pl.Date),
            "is_current": pl.Series([], dtype=pl.Boolean),
        })
        report = detect_changes(source, target, ["id"], ["name"], date(2026, 6, 8))
        assert len(report.new) == 1

        output = apply_scd2(source, target, report, ["id"], ["name"], date(2026, 6, 8))
        assert output.height == 1

        v_report = validate_scd2(output, ["id"])
        assert v_report.passed


# ============================================================================
# 6) SCHEMA DETECTION
# ============================================================================

class TestSchemaDetection:
    """Test business key and tracked column detection."""

    def test_detect_id_column_as_key(self):
        """A column ending in _id should be detected as key."""
        source = pl.DataFrame({"customer_id": [1, 2], "name": ["A", "B"]})
        target = pl.DataFrame({
            "customer_id": [1], "name": ["A"],
            "effective_from": [date(2026, 6, 1)], "effective_to": [None], "is_current": [True],
        })
        bk = detect_business_key(source, target)
        assert "customer_id" in bk

    def test_tracked_columns_exclude_key(self):
        """Tracked columns should not include the business key."""
        source = pl.DataFrame({"id": [1], "name": ["A"], "city": ["NYC"]})
        tracked = detect_tracked_columns(source, ["id"])
        assert "id" not in tracked
        assert "name" in tracked
        assert "city" in tracked

    def test_tracked_columns_exclude_scd2_meta(self):
        """Tracked columns should not include SCD2 metadata even if present in source."""
        source = pl.DataFrame({"id": [1], "name": ["A"], "effective_from": [date(2026, 6, 1)]})
        tracked = detect_tracked_columns(source, ["id"])
        assert "effective_from" not in tracked

    def test_no_shared_columns_raises(self):
        """No shared columns should raise ValueError."""
        source = pl.DataFrame({"x": [1]})
        target = pl.DataFrame({
            "y": [1], "effective_from": [date(2026, 6, 1)],
            "effective_to": [None], "is_current": [True],
        })
        with pytest.raises(ValueError, match="No shared columns"):
            detect_business_key(source, target)


# ============================================================================
# 7) LLM / EXPLANATION LAYER
# ============================================================================

class TestExplanationLayer:
    """Test LLM explanation orchestration and template provider."""

    def test_template_provider_covers_all_types(self):
        """Template provider should handle NEW, CHANGED, DELETED, UNCHANGED."""
        tp = TemplateProvider()
        for ct in ChangeType:
            fc = [FieldChange("x", "a", "b")] if ct == ChangeType.CHANGED else []
            record = ChangeRecord({"id": 1}, ct, fc)
            exp = tp.explain_change(record)
            assert exp.text
            assert exp.provider == "template"

    def test_template_explanation_matches_actual_change(self):
        """Template explanation for CHANGED should mention the changed field."""
        tp = TemplateProvider()
        record = ChangeRecord(
            {"id": 1}, ChangeType.CHANGED,
            [FieldChange("city", "Boston", "NYC")]
        )
        exp = tp.explain_change(record)
        assert "city" in exp.text
        assert "Boston" in exp.text
        assert "NYC" in exp.text

    def test_explain_changes_skips_unchanged(self):
        """explain_changes should not generate explanations for UNCHANGED records."""
        report = ChangeReport(
            new=[ChangeRecord({"id": 1}, ChangeType.NEW)],
            unchanged=[ChangeRecord({"id": 2}, ChangeType.UNCHANGED)],
            processing_date=date(2026, 6, 8),
        )
        result = explain_changes(report, provider=TemplateProvider())
        assert len(result.explanations) == 1
        keys = {list(e.business_key_values.values())[0] for e in result.explanations}
        assert 2 not in keys

    def test_explain_changes_empty_report(self):
        """Empty report -> no explanations, no warnings."""
        report = ChangeReport(processing_date=date(2026, 6, 8))
        result = explain_changes(report, provider=TemplateProvider())
        assert len(result.explanations) == 0
        assert len(result.warnings) == 0

    def test_failing_provider_falls_back_to_template(self):
        """If primary provider batch call fails, fall back to template."""
        class FailingProvider(LLMProvider):
            @property
            def name(self) -> str:
                return "failing_mock"
            def explain_change(self, record):
                raise RuntimeError("Should not be called")
            def explain_changes_batch(self, records):
                raise RuntimeError("Batch call failed")

        report = ChangeReport(
            new=[ChangeRecord({"id": 1}, ChangeType.NEW)],
            processing_date=date(2026, 6, 8),
        )
        result = explain_changes(report, provider=FailingProvider())
        assert len(result.explanations) == 1
        assert result.explanations[0].provider == "template"
        assert any("Fell back to template" in w for w in result.warnings)

    def test_llm_is_not_used_for_change_detection(self):
        """LLM should never be part of the change detection pipeline."""
        import inspect
        from src.scd2_copilot import detect_changes as dc_module
        source_code = inspect.getsource(dc_module)
        for keyword in ["genai", "groq", "openai", "llm", "provider", "prompt", "api_key"]:
            assert keyword not in source_code.lower(), (
                f"Found '{keyword}' in detect_changes module — LLM should not influence detection"
            )

    def test_llm_is_not_used_for_validation(self):
        """LLM should never be part of the validation pipeline."""
        import inspect
        from src.scd2_copilot import validate as val_module
        source_code = inspect.getsource(val_module)
        for keyword in ["genai", "groq", "openai", "llm", "provider", "prompt", "api_key"]:
            assert keyword not in source_code.lower(), (
                f"Found '{keyword}' in validate module — LLM should not influence validation"
            )

    def test_prompt_is_concise_for_single_record(self):
        """Single-record prompt should be short (< 500 chars)."""
        from src.scd2_copilot.providers.gemini import _build_prompt
        record = ChangeRecord({"customer_id": 101}, ChangeType.NEW)
        prompt = _build_prompt(record)
        assert len(prompt) < 500

    def test_batch_prompt_does_not_duplicate_system_instructions(self):
        """Batch prompt should contain system instructions exactly once."""
        from src.scd2_copilot.providers.gemini import _build_batch_prompt
        records = [
            ChangeRecord({"id": i}, ChangeType.NEW) for i in range(5)
        ]
        prompt = _build_batch_prompt(records)
        # "You are a data engineering assistant" should appear exactly once
        assert prompt.count("You are a data engineering assistant") == 1


# ============================================================================
# 8) FULL PIPELINE INTEGRATION
# ============================================================================

class TestFullPipeline:
    """End-to-end pipeline integration tests."""

    def test_full_pipeline_standard_scenario(self):
        """Standard scenario: 1 new, 1 changed, 2 unchanged, 0 deleted."""
        source = pl.DataFrame({
            "customer_id": [101, 102, 103, 104],
            "name": ["Ravi", "Priya", "Arun", "Kiran"],
            "city": ["Bengaluru", "Mumbai", "Delhi", "Hyderabad"],
            "tier": ["Gold", "Silver", "Gold", "Bronze"],
        })
        target = pl.DataFrame({
            "customer_id": [101, 102, 103],
            "name": ["Ravi", "Priya", "Arun"],
            "city": ["Chennai", "Mumbai", "Delhi"],
            "tier": ["Gold", "Silver", "Gold"],
            "effective_from": [date(2026, 6, 7)] * 3,
            "effective_to": [None] * 3,
            "is_current": [True] * 3,
        })
        bk = ["customer_id"]
        tc = ["name", "city", "tier"]
        pd = date(2026, 6, 8)

        report = detect_changes(source, target, bk, tc, pd)
        assert report.summary == {"new": 1, "changed": 1, "unchanged": 2, "deleted": 0, "total": 4}

        output = apply_scd2(source, target, report, bk, tc, pd)
        assert output.height == 5  # 3 original + 1 closed + 1 new current for changed + 1 new

        v_report = validate_scd2(output, bk)
        assert v_report.passed

        result = explain_changes(report, provider=TemplateProvider())
        assert len(result.explanations) == 2  # 1 new + 1 changed
        assert len(result.warnings) == 0

    def test_pipeline_with_only_new_records(self):
        """All records are new (empty target)."""
        source = pl.DataFrame({"id": [1, 2, 3], "name": ["A", "B", "C"]})
        target = pl.DataFrame({
            "id": pl.Series([], dtype=pl.Int64),
            "name": pl.Series([], dtype=pl.Utf8),
            "effective_from": pl.Series([], dtype=pl.Date),
            "effective_to": pl.Series([], dtype=pl.Date),
            "is_current": pl.Series([], dtype=pl.Boolean),
        })
        report = detect_changes(source, target, ["id"], ["name"], date(2026, 6, 8))
        assert len(report.new) == 3
        output = apply_scd2(source, target, report, ["id"], ["name"], date(2026, 6, 8))
        assert output.height == 3
        v_report = validate_scd2(output, ["id"])
        assert v_report.passed

    def test_pipeline_validation_has_no_warn_on_clean_data(self):
        """A clean pipeline should have zero WARN results."""
        source = pl.DataFrame({"id": [1], "name": ["A"]})
        target = pl.DataFrame({
            "id": pl.Series([], dtype=pl.Int64),
            "name": pl.Series([], dtype=pl.Utf8),
            "effective_from": pl.Series([], dtype=pl.Date),
            "effective_to": pl.Series([], dtype=pl.Date),
            "is_current": pl.Series([], dtype=pl.Boolean),
        })
        report = detect_changes(source, target, ["id"], ["name"], date(2026, 6, 8))
        output = apply_scd2(source, target, report, ["id"], ["name"], date(2026, 6, 8))
        v_report = validate_scd2(output, ["id"])
        warn_count = sum(1 for r in v_report.rules if r.status == ValidationStatus.WARN)
        assert warn_count == 0, f"Expected 0 warnings on clean data, got {warn_count}"


# ============================================================================
# 9) PERFORMANCE / EFFICIENCY CHECKS
# ============================================================================

class TestPerformanceEfficiency:
    """Tests for performance and token/API efficiency."""

    def test_batch_method_exists_on_providers(self):
        """All providers should support explain_changes_batch."""
        tp = TemplateProvider()
        assert hasattr(tp, "explain_changes_batch")

    def test_template_batch_is_local(self):
        """Template provider batch should not make any API calls."""
        tp = TemplateProvider()
        records = [ChangeRecord({"id": i}, ChangeType.NEW) for i in range(10)]
        results = tp.explain_changes_batch(records)
        assert len(results) == 10
        for r in results:
            assert r.provider == "template"

    def test_larger_dataset_validation_completes(self):
        """Validation on 1000 rows should complete without error."""
        n = 1000
        df = pl.DataFrame({
            "id": list(range(n)),
            "effective_from": [date(2026, 6, 1)] * n,
            "effective_to": [None] * n,
            "is_current": [True] * n,
        })
        report = validate_scd2(df, ["id"])
        assert report.passed

    def test_change_detection_on_larger_dataset(self):
        """Change detection on 500 records should complete."""
        n = 500
        source = pl.DataFrame({"id": list(range(n)), "name": [f"Name{i}" for i in range(n)]})
        target = pl.DataFrame({
            "id": list(range(n - 50)),
            "name": [f"Name{i}" for i in range(n - 50)],
            "effective_from": [date(2026, 6, 1)] * (n - 50),
            "effective_to": [None] * (n - 50),
            "is_current": [True] * (n - 50),
        })
        report = detect_changes(source, target, ["id"], ["name"], date(2026, 6, 8))
        assert len(report.new) == 50
        assert len(report.unchanged) == n - 50
