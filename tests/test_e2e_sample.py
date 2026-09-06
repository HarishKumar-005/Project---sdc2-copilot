"""End-to-end test: full pipeline against sample data → expected output."""

from datetime import date

import polars as pl
import pytest

from src.scd2_copilot.ingestion import load_csv
from src.scd2_copilot.detect_changes import detect_changes
from src.scd2_copilot.transform_scd2 import apply_scd2
from src.scd2_copilot.validate import validate_scd2


class TestE2ESample:
    """End-to-end test using sample-data/ files."""

    def test_output_matches_expected(
        self, source_df, target_df, expected_output_df,
        business_key, tracked_columns, processing_date
    ):
        """The full pipeline should produce output matching expected_output.csv."""
        # Run change detection
        report = detect_changes(source_df, target_df, business_key, tracked_columns, processing_date)

        # Run transformation
        result = apply_scd2(
            source_df, target_df, report,
            business_key, tracked_columns, processing_date
        )

        # Compare row count
        assert result.height == expected_output_df.height, (
            f"Row count mismatch: got {result.height}, expected {expected_output_df.height}"
        )

        # Compare column set
        assert set(result.columns) == set(expected_output_df.columns), (
            f"Column mismatch: got {set(result.columns)}, expected {set(expected_output_df.columns)}"
        )

        # Sort both for deterministic comparison
        sort_cols = business_key + ["effective_from"]
        result_sorted = result.select(sorted(result.columns)).sort(sort_cols)
        expected_sorted = expected_output_df.select(sorted(expected_output_df.columns)).sort(sort_cols)

        # Compare each row
        for i in range(result_sorted.height):
            result_row = result_sorted.row(i, named=True)
            expected_row = expected_sorted.row(i, named=True)
            for col in result_sorted.columns:
                r_val = result_row[col]
                e_val = expected_row[col]
                assert r_val == e_val, (
                    f"Row {i}, column '{col}': got {r_val!r}, expected {e_val!r}"
                )

    def test_validation_passes_on_output(
        self, source_df, target_df, business_key, tracked_columns, processing_date
    ):
        """The pipeline output should pass all validation rules."""
        report = detect_changes(source_df, target_df, business_key, tracked_columns, processing_date)
        result = apply_scd2(
            source_df, target_df, report,
            business_key, tracked_columns, processing_date
        )

        validation = validate_scd2(result, business_key)
        assert validation.passed, (
            f"Validation failed: {[r.message for r in validation.rules if r.status.value == 'fail']}"
        )

    def test_change_summary_correct(
        self, source_df, target_df, business_key, tracked_columns, processing_date
    ):
        """Verify the change summary matches expected scenario counts."""
        report = detect_changes(source_df, target_df, business_key, tracked_columns, processing_date)
        assert report.summary == {
            "new": 1,
            "changed": 1,
            "unchanged": 2,
            "deleted": 0,
            "total": 4,
        }
