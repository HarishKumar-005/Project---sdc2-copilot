"""Regression tests for duplicate business-key integrity (M1.1).

Verifies that duplicate business keys in incoming source snapshots or active target
records are caught deterministically before any dictionary building or SCD2
transformation can occur, preventing silent data overwrite/loss.
"""

from __future__ import annotations

from datetime import date
import polars as pl
import pytest

from src.scd2_copilot.detect_changes import detect_changes
from src.scd2_copilot.exceptions import DuplicateBusinessKeyError
from src.scd2_copilot.models import ChangeType
from src.scd2_copilot.transform_scd2 import apply_scd2
from src.scd2_copilot.validate import validate_business_keys, validate_scd2


class TestDuplicateBusinessKeys:
    """Test suite for duplicate business-key detection and error behavior."""

    def test_unique_source_keys_succeed(self):
        """Standard dataset with unique keys passes validation and change detection."""
        source = pl.DataFrame({
            "id": [101, 102, 103],
            "name": ["Alice", "Bob", "Charlie"]
        })
        target = pl.DataFrame({
            "id": [101, 102],
            "name": ["Alice", "Robert"],
            "effective_from": [date(2026, 6, 1), date(2026, 6, 1)],
            "effective_to": [None, None],
            "is_current": [True, True]
        })
        # Should not raise any error
        report = detect_changes(source, target, ["id"], ["name"], date(2026, 6, 8))
        assert report.total == 3
        assert len(report.unchanged) == 1  # 101
        assert len(report.changed) == 1    # 102
        assert len(report.new) == 1        # 103

    def test_duplicate_source_single_key_fails(self):
        """Source with two identical business keys raises DuplicateBusinessKeyError."""
        source = pl.DataFrame({
            "id": [101, 101],
            "name": ["Alice", "Bob"]
        })
        target = pl.DataFrame({
            "id": [101],
            "name": ["Charlie"],
            "effective_from": [date(2026, 6, 1)],
            "effective_to": [None],
            "is_current": [True]
        })
        with pytest.raises(DuplicateBusinessKeyError) as exc_info:
            detect_changes(source, target, ["id"], ["name"], date(2026, 6, 8))

        err = exc_info.value
        assert err.dataset_name == "source"
        assert err.business_key == ["id"]
        assert err.duplicate_count == 1
        assert {"id": 101} in err.duplicate_keys
        assert "Duplicate business key(s) detected in source" in str(err)
        assert "id=101" in str(err)

    def test_duplicate_source_three_occurrences_fails(self):
        """Source with 3 occurrences of a business key reports count=3."""
        source = pl.DataFrame({
            "id": [101, 101, 101, 102],
            "name": ["Alice", "Bob", "Charlie", "David"]
        })
        target = pl.DataFrame({
            "id": [102],
            "name": ["David"],
            "effective_from": [date(2026, 6, 1)],
            "effective_to": [None],
            "is_current": [True]
        })
        with pytest.raises(DuplicateBusinessKeyError) as exc_info:
            detect_changes(source, target, ["id"], ["name"], date(2026, 6, 8))

        err = exc_info.value
        assert err.dataset_name == "source"
        assert err.duplicate_count == 1
        assert "count=3" in str(err)

    def test_duplicate_active_target_key_fails(self):
        """Target having multiple active current rows for the same key raises error."""
        source = pl.DataFrame({
            "id": [101],
            "name": ["Alice"]
        })
        target = pl.DataFrame({
            "id": [101, 101],
            "name": ["Alice_Old1", "Alice_Old2"],
            "effective_from": [date(2026, 6, 1), date(2026, 6, 3)],
            "effective_to": [None, None],
            "is_current": [True, True]
        })
        with pytest.raises(DuplicateBusinessKeyError) as exc_info:
            detect_changes(source, target, ["id"], ["name"], date(2026, 6, 8))

        err = exc_info.value
        assert err.dataset_name == "target"
        assert err.business_key == ["id"]
        assert err.duplicate_count == 1
        assert "Duplicate business key(s) detected in target" in str(err)

    def test_duplicate_target_historical_allowed(self):
        """Target having historical (is_current=False) rows with same key is valid."""
        source = pl.DataFrame({
            "id": [101],
            "name": ["Alice_New"]
        })
        # Historical row and 1 current row for id=101 -> Perfectly valid SCD2
        target = pl.DataFrame({
            "id": [101, 101],
            "name": ["Alice_V1", "Alice_V2"],
            "effective_from": [date(2026, 6, 1), date(2026, 6, 5)],
            "effective_to": [date(2026, 6, 5), None],
            "is_current": [False, True]
        })
        report = detect_changes(source, target, ["id"], ["name"], date(2026, 6, 8))
        assert len(report.changed) == 1
        assert report.changed[0].business_key_values == {"id": 101}

    def test_duplicate_composite_key_fails(self):
        """Composite key duplicate raises DuplicateBusinessKeyError."""
        source = pl.DataFrame({
            "store_id": [10, 10, 20],
            "product_id": ["P1", "P1", "P1"],
            "qty": [100, 150, 200]
        })
        target = pl.DataFrame({
            "store_id": [10],
            "product_id": ["P1"],
            "qty": [100],
            "effective_from": [date(2026, 6, 1)],
            "effective_to": [None],
            "is_current": [True]
        })
        with pytest.raises(DuplicateBusinessKeyError) as exc_info:
            detect_changes(source, target, ["store_id", "product_id"], ["qty"], date(2026, 6, 8))

        err = exc_info.value
        assert err.dataset_name == "source"
        assert err.business_key == ["store_id", "product_id"]
        assert err.duplicate_count == 1
        assert {"store_id": 10, "product_id": "P1"} in err.duplicate_keys
        assert "store_id=10" in str(err)
        assert "product_id=P1" in str(err)

    def test_different_composite_combinations_succeed(self):
        """Composite keys sharing one column but different in another are valid."""
        source = pl.DataFrame({
            "store_id": [10, 10, 20],
            "product_id": ["P1", "P2", "P1"],  # (10, P1), (10, P2), (20, P1) - all distinct
            "qty": [100, 150, 200]
        })
        target = pl.DataFrame({
            "store_id": [10],
            "product_id": ["P1"],
            "qty": [100],
            "effective_from": [date(2026, 6, 1)],
            "effective_to": [None],
            "is_current": [True]
        })
        report = detect_changes(source, target, ["store_id", "product_id"], ["qty"], date(2026, 6, 8))
        assert report.total == 3
        assert len(report.unchanged) == 1
        assert len(report.new) == 2

    def test_null_key_not_treated_as_duplicate(self):
        """Rows with NULL keys are not grouped as duplicate business keys."""
        # Two rows with None: validate_business_keys ignores nulls so no_null_keys catches it
        source = pl.DataFrame({
            "id": [None, None, 101],
            "name": ["A", "B", "C"]
        })
        # validate_business_keys directly should not raise DuplicateBusinessKeyError on nulls
        validate_business_keys(source, ["id"], dataset_name="source")

    def test_detected_before_transformation_dictionary_overwrite(self):
        """apply_scd2 also validates and prevents silent source_lookup overwrite."""
        source = pl.DataFrame({
            "id": [101, 101],
            "name": ["Alice", "Bob"]
        })
        target = pl.DataFrame({
            "id": [101],
            "name": ["Alice"],
            "effective_from": [date(2026, 6, 1)],
            "effective_to": [None],
            "is_current": [True]
        })
        from src.scd2_copilot.models import ChangeReport
        dummy_report = ChangeReport()

        with pytest.raises(DuplicateBusinessKeyError) as exc_info:
            apply_scd2(source, target, dummy_report, ["id"], ["name"], date(2026, 6, 8))

        assert exc_info.value.dataset_name == "source"

    def test_multiple_distinct_duplicate_keys_reported(self):
        """Multiple distinct duplicate keys report accurate duplicate_count."""
        source = pl.DataFrame({
            "id": [101, 101, 102, 102, 103],
            "name": ["A1", "A2", "B1", "B2", "C"]
        })
        target = pl.DataFrame({
            "id": [103],
            "name": ["C"],
            "effective_from": [date(2026, 6, 1)],
            "effective_to": [None],
            "is_current": [True]
        })
        with pytest.raises(DuplicateBusinessKeyError) as exc_info:
            detect_changes(source, target, ["id"], ["name"], date(2026, 6, 8))

        err = exc_info.value
        assert err.duplicate_count == 2
        assert len(err.duplicate_keys) == 2
