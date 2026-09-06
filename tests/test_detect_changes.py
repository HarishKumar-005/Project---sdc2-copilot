"""Tests for detect_changes module."""

from datetime import date

import polars as pl
import pytest

from src.scd2_copilot.detect_changes import detect_changes
from src.scd2_copilot.models import ChangeType


class TestDetectChanges:
    """Test suite for deterministic change detection."""

    def test_sample_data_detects_all_categories(
        self, source_df, target_df, business_key, tracked_columns, processing_date
    ):
        """The standard sample data should produce 1 new, 1 changed, 2 unchanged, 0 deleted."""
        report = detect_changes(source_df, target_df, business_key, tracked_columns, processing_date)

        assert len(report.new) == 1
        assert len(report.changed) == 1
        assert len(report.unchanged) == 2
        assert len(report.deleted) == 0

    def test_new_record_detected(
        self, source_df, target_df, business_key, tracked_columns, processing_date
    ):
        """Customer 104 is in source but not in target → NEW."""
        report = detect_changes(source_df, target_df, business_key, tracked_columns, processing_date)
        new_keys = [r.business_key_values["customer_id"] for r in report.new]
        assert 104 in new_keys

    def test_changed_record_detected(
        self, source_df, target_df, business_key, tracked_columns, processing_date
    ):
        """Customer 101 city changed from Chennai to Bengaluru → CHANGED."""
        report = detect_changes(source_df, target_df, business_key, tracked_columns, processing_date)
        changed_keys = [r.business_key_values["customer_id"] for r in report.changed]
        assert 101 in changed_keys

        # Verify field change details
        record_101 = [r for r in report.changed if r.business_key_values["customer_id"] == 101][0]
        city_changes = [fc for fc in record_101.field_changes if fc.column == "city"]
        assert len(city_changes) == 1
        assert city_changes[0].old_value == "Chennai"
        assert city_changes[0].new_value == "Bengaluru"

    def test_unchanged_records_detected(
        self, source_df, target_df, business_key, tracked_columns, processing_date
    ):
        """Customers 102 and 103 are unchanged."""
        report = detect_changes(source_df, target_df, business_key, tracked_columns, processing_date)
        unchanged_keys = [r.business_key_values["customer_id"] for r in report.unchanged]
        assert 102 in unchanged_keys
        assert 103 in unchanged_keys

    def test_deleted_record_detection(self):
        """A key in target but not in source → DELETED."""
        source = pl.DataFrame({"id": [1], "name": ["Alice"]})
        target = pl.DataFrame({
            "id": [1, 2],
            "name": ["Alice", "Bob"],
            "effective_from": [date(2026, 6, 7), date(2026, 6, 7)],
            "effective_to": [None, None],
            "is_current": [True, True],
        })
        report = detect_changes(source, target, ["id"], ["name"], date(2026, 6, 8))
        assert len(report.deleted) == 1
        assert report.deleted[0].business_key_values["id"] == 2

    def test_all_new_when_target_empty(self):
        """Empty target → all source records are NEW."""
        source = pl.DataFrame({"id": [1, 2], "name": ["A", "B"]})
        target = pl.DataFrame({
            "id": pl.Series([], dtype=pl.Int64),
            "name": pl.Series([], dtype=pl.Utf8),
            "effective_from": pl.Series([], dtype=pl.Date),
            "effective_to": pl.Series([], dtype=pl.Date),
            "is_current": pl.Series([], dtype=pl.Boolean),
        })
        report = detect_changes(source, target, ["id"], ["name"], date(2026, 6, 8))
        assert len(report.new) == 2
        assert len(report.changed) == 0
        assert len(report.unchanged) == 0

    def test_all_deleted_when_source_empty(self):
        """Empty source → all target current records are DELETED."""
        source = pl.DataFrame({"id": pl.Series([], dtype=pl.Int64), "name": pl.Series([], dtype=pl.Utf8)})
        target = pl.DataFrame({
            "id": [1, 2],
            "name": ["A", "B"],
            "effective_from": [date(2026, 6, 7), date(2026, 6, 7)],
            "effective_to": [None, None],
            "is_current": [True, True],
        })
        report = detect_changes(source, target, ["id"], ["name"], date(2026, 6, 8))
        assert len(report.deleted) == 2
        assert len(report.new) == 0

    def test_all_unchanged(self):
        """Identical source and target → all UNCHANGED."""
        source = pl.DataFrame({"id": [1, 2], "name": ["A", "B"]})
        target = pl.DataFrame({
            "id": [1, 2],
            "name": ["A", "B"],
            "effective_from": [date(2026, 6, 7), date(2026, 6, 7)],
            "effective_to": [None, None],
            "is_current": [True, True],
        })
        report = detect_changes(source, target, ["id"], ["name"], date(2026, 6, 8))
        assert len(report.unchanged) == 2
        assert len(report.new) == 0
        assert len(report.changed) == 0
        assert len(report.deleted) == 0

    def test_summary_counts(
        self, source_df, target_df, business_key, tracked_columns, processing_date
    ):
        """Verify the summary dict matches individual counts."""
        report = detect_changes(source_df, target_df, business_key, tracked_columns, processing_date)
        s = report.summary
        assert s["new"] == 1
        assert s["changed"] == 1
        assert s["unchanged"] == 2
        assert s["deleted"] == 0
        assert s["total"] == 4
