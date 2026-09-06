"""Tests for transform_scd2 module."""

from datetime import date

import polars as pl
import pytest

from src.scd2_copilot.detect_changes import detect_changes
from src.scd2_copilot.transform_scd2 import apply_scd2


class TestTransformSCD2:
    """Test suite for SCD2 transformation."""

    def test_sample_data_row_count(
        self, source_df, target_df, business_key, tracked_columns, processing_date
    ):
        """Expected output has 5 rows: 1 closed + 1 new for 101, 1 each for 102/103, 1 new for 104."""
        report = detect_changes(source_df, target_df, business_key, tracked_columns, processing_date)
        result = apply_scd2(source_df, target_df, report, business_key, tracked_columns, processing_date)
        assert result.height == 5

    def test_changed_record_produces_two_rows(
        self, source_df, target_df, business_key, tracked_columns, processing_date
    ):
        """Customer 101 should have 2 rows: one closed, one current."""
        report = detect_changes(source_df, target_df, business_key, tracked_columns, processing_date)
        result = apply_scd2(source_df, target_df, report, business_key, tracked_columns, processing_date)

        rows_101 = result.filter(pl.col("customer_id") == 101)
        assert rows_101.height == 2

        # One closed row
        closed = rows_101.filter(pl.col("is_current") == False)
        assert closed.height == 1
        assert closed["effective_to"][0] == processing_date
        assert closed["city"][0] == "Chennai"  # old value

        # One current row
        current = rows_101.filter(pl.col("is_current") == True)
        assert current.height == 1
        assert current["effective_to"][0] is None
        assert current["city"][0] == "Bengaluru"  # new value
        assert current["effective_from"][0] == processing_date

    def test_new_record_inserted_correctly(
        self, source_df, target_df, business_key, tracked_columns, processing_date
    ):
        """Customer 104 should have 1 current row."""
        report = detect_changes(source_df, target_df, business_key, tracked_columns, processing_date)
        result = apply_scd2(source_df, target_df, report, business_key, tracked_columns, processing_date)

        rows_104 = result.filter(pl.col("customer_id") == 104)
        assert rows_104.height == 1
        assert rows_104["is_current"][0] == True
        assert rows_104["effective_from"][0] == processing_date
        assert rows_104["effective_to"][0] is None

    def test_unchanged_records_preserved(
        self, source_df, target_df, business_key, tracked_columns, processing_date
    ):
        """Customers 102 and 103 should be preserved as-is."""
        report = detect_changes(source_df, target_df, business_key, tracked_columns, processing_date)
        result = apply_scd2(source_df, target_df, report, business_key, tracked_columns, processing_date)

        for cid in [102, 103]:
            rows = result.filter(pl.col("customer_id") == cid)
            assert rows.height == 1
            assert rows["is_current"][0] == True

    def test_deleted_record_closed(self):
        """A key not in source but in target → closed."""
        source = pl.DataFrame({"id": [1], "name": ["Alice"]})
        target = pl.DataFrame({
            "id": [1, 2],
            "name": ["Alice", "Bob"],
            "effective_from": [date(2026, 6, 7), date(2026, 6, 7)],
            "effective_to": [None, None],
            "is_current": [True, True],
        })
        pd = date(2026, 6, 8)
        report = detect_changes(source, target, ["id"], ["name"], pd)
        result = apply_scd2(source, target, report, ["id"], ["name"], pd)

        # Bob (id=2) should be closed
        bob = result.filter(pl.col("id") == 2)
        assert bob.height == 1
        assert bob["is_current"][0] == False
        assert bob["effective_to"][0] == pd

    def test_output_has_correct_columns(
        self, source_df, target_df, business_key, tracked_columns, processing_date
    ):
        """Output must have business_key + tracked + SCD2 metadata columns."""
        report = detect_changes(source_df, target_df, business_key, tracked_columns, processing_date)
        result = apply_scd2(source_df, target_df, report, business_key, tracked_columns, processing_date)

        expected_cols = set(business_key + tracked_columns + ["effective_from", "effective_to", "is_current"])
        assert set(result.columns) == expected_cols

    def test_output_sorted_by_key_and_date(
        self, source_df, target_df, business_key, tracked_columns, processing_date
    ):
        """Output should be sorted by business key then effective_from."""
        report = detect_changes(source_df, target_df, business_key, tracked_columns, processing_date)
        result = apply_scd2(source_df, target_df, report, business_key, tracked_columns, processing_date)

        ids = result["customer_id"].to_list()
        # Should be monotonically non-decreasing
        assert ids == sorted(ids)