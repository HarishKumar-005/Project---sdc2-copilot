"""Tests for the validate module."""

from datetime import date

import polars as pl
import pytest

from src.scd2_copilot.validate import validate_scd2
from src.scd2_copilot.models import ValidationStatus


class TestValidateSCD2:
    """Test suite for SCD2 validation rules."""

    @pytest.fixture
    def valid_scd2_df(self) -> pl.DataFrame:
        """A correctly formed SCD2 table."""
        return pl.DataFrame({
            "customer_id": [101, 101, 102],
            "name": ["Ravi", "Ravi", "Priya"],
            "city": ["Chennai", "Bengaluru", "Mumbai"],
            "effective_from": [date(2026, 6, 7), date(2026, 6, 8), date(2026, 6, 7)],
            "effective_to": [date(2026, 6, 8), None, None],
            "is_current": [False, True, True],
        })

    def test_valid_table_passes_all_rules(self, valid_scd2_df):
        """A correct SCD2 table should pass all validation rules."""
        report = validate_scd2(valid_scd2_df, ["customer_id"])
        assert report.passed
        assert report.summary["fail"] == 0

    def test_duplicate_current_rows_detected(self):
        """Two current rows for the same key → FAIL."""
        df = pl.DataFrame({
            "customer_id": [101, 101],
            "name": ["A", "B"],
            "effective_from": [date(2026, 6, 7), date(2026, 6, 8)],
            "effective_to": [None, None],
            "is_current": [True, True],
        })
        report = validate_scd2(df, ["customer_id"])
        one_current = [r for r in report.rules if r.name == "one_current_per_key"]
        assert len(one_current) == 1
        assert one_current[0].status == ValidationStatus.FAIL

    def test_null_business_key_detected(self):
        """A row with null business key → FAIL."""
        df = pl.DataFrame({
            "customer_id": [101, None],
            "name": ["A", "B"],
            "effective_from": [date(2026, 6, 7), date(2026, 6, 7)],
            "effective_to": [None, None],
            "is_current": [True, True],
        })
        report = validate_scd2(df, ["customer_id"])
        null_check = [r for r in report.rules if r.name == "no_null_keys"]
        assert len(null_check) == 1
        assert null_check[0].status == ValidationStatus.FAIL

    def test_schema_completeness_fails_on_missing_column(self):
        """Missing effective_to column → FAIL."""
        df = pl.DataFrame({
            "customer_id": [101],
            "name": ["A"],
            "effective_from": [date(2026, 6, 7)],
            "is_current": [True],
        })
        report = validate_scd2(df, ["customer_id"])
        schema_check = [r for r in report.rules if r.name == "schema_completeness"]
        assert len(schema_check) == 1
        assert schema_check[0].status == ValidationStatus.FAIL

    def test_date_consistency_fails(self):
        """effective_from > effective_to → FAIL."""
        df = pl.DataFrame({
            "customer_id": [101],
            "name": ["A"],
            "effective_from": [date(2026, 6, 9)],
            "effective_to": [date(2026, 6, 7)],
            "is_current": [False],
        })
        report = validate_scd2(df, ["customer_id"])
        date_check = [r for r in report.rules if r.name == "date_consistency"]
        assert len(date_check) == 1
        assert date_check[0].status == ValidationStatus.FAIL

    def test_date_consistency_passes_with_nulls(self, valid_scd2_df):
        """Null effective_to (current rows) should not trigger date consistency failure."""
        report = validate_scd2(valid_scd2_df, ["customer_id"])
        date_check = [r for r in report.rules if r.name == "date_consistency"]
        assert len(date_check) == 1
        assert date_check[0].status == ValidationStatus.PASS

    def test_overlap_scd2_cases(self):
        """Test SCD2 overlap checks across various scenarios (Cases 1-5)."""
        # Case 1 & Case 3: Valid SCD2 history with multiple versions per customer -> Expected = PASS
        df_valid_history = pl.DataFrame({
            "customer_id": [101, 101, 101],
            "effective_from": [date(2026, 6, 1), date(2026, 6, 8), date(2026, 6, 15)],
            "effective_to": [date(2026, 6, 8), date(2026, 6, 15), None],
            "is_current": [False, False, True],
        })
        report = validate_scd2(df_valid_history, ["customer_id"])
        overlap_rule = next(r for r in report.rules if r.name == "no_overlapping_dates")
        assert overlap_rule.status == ValidationStatus.PASS

        # Case 2: Overlapping periods -> Expected = FAIL
        df_overlapping = pl.DataFrame({
            "customer_id": [101, 101],
            "effective_from": [date(2026, 6, 1), date(2026, 6, 8)],
            "effective_to": [date(2026, 6, 10), None],
            "is_current": [False, True],
        })
        report = validate_scd2(df_overlapping, ["customer_id"])
        overlap_rule = next(r for r in report.rules if r.name == "no_overlapping_dates")
        assert overlap_rule.status == ValidationStatus.FAIL
        assert len(overlap_rule.details) == 1
        assert "Overlap detected for customer_id=101" in overlap_rule.details[0]
        assert "2026-06-01 → 2026-06-10" in overlap_rule.details[0]
        assert "2026-06-08 → NULL" in overlap_rule.details[0]

        # Case 4: NULL effective_to on current row -> Expected = PASS
        df_null_current = pl.DataFrame({
            "customer_id": [101, 101],
            "effective_from": [date(2026, 6, 1), date(2026, 6, 8)],
            "effective_to": [date(2026, 6, 8), None],
            "is_current": [False, True],
        })
        report = validate_scd2(df_null_current, ["customer_id"])
        overlap_rule = next(r for r in report.rules if r.name == "no_overlapping_dates")
        assert overlap_rule.status == ValidationStatus.PASS

        # Case 5: Single record customer -> Expected = PASS
        df_single_record = pl.DataFrame({
            "customer_id": [101],
            "effective_from": [date(2026, 6, 1)],
            "effective_to": [None],
            "is_current": [True],
        })
        report = validate_scd2(df_single_record, ["customer_id"])
        overlap_rule = next(r for r in report.rules if r.name == "no_overlapping_dates")
        assert overlap_rule.status == ValidationStatus.PASS
