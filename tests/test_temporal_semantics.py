"""Regression tests for M1.3: Formalize SCD2 Temporal Semantics.

Verifies:
1. Validity intervals strictly follow [effective_from, effective_to).
2. Adjacent versions where old.effective_to == new.effective_from are VALID.
3. At the exact boundary instant, only the new version is active.
4. True overlaps (next.effective_from < previous.effective_to) are rejected.
5. Zero-duration intervals (effective_from == effective_to) are rejected.
6. Reversed intervals (effective_from > effective_to) are rejected.
7. Open-ended current versions (effective_to is None) are valid.
8. Chronological gaps (previous.effective_to < next.effective_from) are valid.
9. apply_scd2 preserves half-open intervals across NEW, CHANGED, UNCHANGED, DELETED.
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from src.scd2_copilot.detect_changes import detect_changes
from src.scd2_copilot.models import ValidationStatus
from src.scd2_copilot.transform_scd2 import apply_scd2
from src.scd2_copilot.validate import validate_scd2


class TestTemporalSemantics:
    """Test suite for [effective_from, effective_to) temporal contract."""

    def test_01_single_current_version_valid(self):
        """A single record with open-ended effective_to is valid."""
        df = pl.DataFrame({
            "customer_id": [101],
            "tier": ["Gold"],
            "effective_from": [date(2026, 9, 1)],
            "effective_to": [None],
            "is_current": [True],
        })
        report = validate_scd2(df, ["customer_id"])
        assert report.passed
        rule_overlap = next(r for r in report.rules if r.name == "no_overlapping_dates")
        rule_consistency = next(r for r in report.rules if r.name == "date_consistency")
        assert rule_overlap.status == ValidationStatus.PASS
        assert rule_consistency.status == ValidationStatus.PASS

    def test_02_adjacent_versions_equal_boundaries_valid(self):
        """Adjacent versions where old.effective_to == new.effective_from are VALID."""
        df = pl.DataFrame({
            "customer_id": [101, 101],
            "tier": ["Silver", "Gold"],
            "effective_from": [date(2026, 9, 1), date(2026, 9, 6)],
            "effective_to": [date(2026, 9, 6), None],
            "is_current": [False, True],
        })
        report = validate_scd2(df, ["customer_id"])
        assert report.passed
        rule_overlap = next(r for r in report.rules if r.name == "no_overlapping_dates")
        rule_consistency = next(r for r in report.rules if r.name == "date_consistency")
        assert rule_overlap.status == ValidationStatus.PASS
        assert rule_consistency.status == ValidationStatus.PASS

    def test_03_exact_boundary_belongs_only_to_new_version(self):
        """Under [effective_from, effective_to), the boundary date belongs exclusively to the new version."""
        # Version A: [2026-09-01, 2026-09-06)
        # Version B: [2026-09-06, NULL)
        df = pl.DataFrame({
            "customer_id": [101, 101],
            "version": ["A", "B"],
            "effective_from": [date(2026, 9, 1), date(2026, 9, 6)],
            "effective_to": [date(2026, 9, 6), None],
            "is_current": [False, True],
        })

        # Point-in-time query condition: effective_from <= T AND (effective_to > T OR effective_to IS NULL)
        def query_at(target_date: date) -> list[str]:
            matching = df.filter(
                (pl.col("effective_from") <= target_date)
                & (pl.col("effective_to").is_null() | (pl.col("effective_to") > target_date))
            )
            return matching["version"].to_list()

        # Day before boundary: Version A only
        assert query_at(date(2026, 9, 5)) == ["A"]

        # Exact boundary date (2026-09-06): Version B ONLY
        assert query_at(date(2026, 9, 6)) == ["B"]

        # Day after boundary: Version B only
        assert query_at(date(2026, 9, 7)) == ["B"]

    def test_04_true_overlap_rejected(self):
        """When next.effective_from < previous.effective_to, overlap is detected and rejected."""
        df = pl.DataFrame({
            "customer_id": [101, 101],
            "effective_from": [date(2026, 9, 1), date(2026, 9, 5)],
            "effective_to": [date(2026, 9, 8), None],
            "is_current": [False, True],
        })
        report = validate_scd2(df, ["customer_id"])
        assert not report.passed
        rule = next(r for r in report.rules if r.name == "no_overlapping_dates")
        assert rule.status == ValidationStatus.FAIL
        assert any("customer_id=101" in d for d in rule.details)
        assert any("2026-09-01 → 2026-09-08" in d for d in rule.details)
        assert any("2026-09-05 → NULL" in d for d in rule.details)

    def test_05_zero_duration_interval_rejected(self):
        """Zero-duration interval (effective_from == effective_to) is invalid under half-open semantics."""
        df = pl.DataFrame({
            "customer_id": [101],
            "effective_from": [date(2026, 9, 6)],
            "effective_to": [date(2026, 9, 6)],
            "is_current": [False],
        })
        report = validate_scd2(df, ["customer_id"])
        assert not report.passed
        rule = next(r for r in report.rules if r.name == "date_consistency")
        assert rule.status == ValidationStatus.FAIL
        assert "effective_from >= effective_to" in rule.message
        assert any("2026-09-06" in d for d in rule.details)

    def test_06_reversed_interval_rejected(self):
        """Reversed interval (effective_from > effective_to) is invalid."""
        df = pl.DataFrame({
            "customer_id": [101],
            "effective_from": [date(2026, 9, 10)],
            "effective_to": [date(2026, 9, 6)],
            "is_current": [False],
        })
        report = validate_scd2(df, ["customer_id"])
        assert not report.passed
        rule = next(r for r in report.rules if r.name == "date_consistency")
        assert rule.status == ValidationStatus.FAIL

    def test_07_open_ended_current_interval_valid(self):
        """Open-ended current version (effective_to is None) is valid in date_consistency."""
        df = pl.DataFrame({
            "customer_id": [101],
            "effective_from": [date(2026, 9, 6)],
            "effective_to": [None],
            "is_current": [True],
        })
        report = validate_scd2(df, ["customer_id"])
        rule = next(r for r in report.rules if r.name == "date_consistency")
        assert rule.status == ValidationStatus.PASS

    def test_08_multiple_historical_versions_adjacent_valid(self):
        """Multiple consecutive historical versions with adjacent boundaries are VALID."""
        df = pl.DataFrame({
            "customer_id": [101, 101, 101, 101],
            "effective_from": [date(2026, 9, 1), date(2026, 9, 5), date(2026, 9, 10), date(2026, 9, 15)],
            "effective_to": [date(2026, 9, 5), date(2026, 9, 10), date(2026, 9, 15), None],
            "is_current": [False, False, False, True],
        })
        report = validate_scd2(df, ["customer_id"])
        assert report.passed

    def test_09_multiple_historical_versions_overlap_rejected(self):
        """Multiple historical versions where an intermediate version overlaps are rejected."""
        df = pl.DataFrame({
            "customer_id": [101, 101, 101],
            "effective_from": [date(2026, 9, 1), date(2026, 9, 5), date(2026, 9, 12)],
            "effective_to": [date(2026, 9, 8), date(2026, 9, 15), None],  # 8 > 5: overlap!
            "is_current": [False, False, True],
        })
        report = validate_scd2(df, ["customer_id"])
        assert not report.passed
        rule = next(r for r in report.rules if r.name == "no_overlapping_dates")
        assert rule.status == ValidationStatus.FAIL

    def test_10_multiple_historical_versions_gap_allowed(self):
        """Chronological gaps (previous.effective_to < next.effective_from) are valid non-contiguous history."""
        df = pl.DataFrame({
            "customer_id": [101, 101],
            "effective_from": [date(2026, 9, 1), date(2026, 9, 10)],  # Gap between 5th and 10th
            "effective_to": [date(2026, 9, 5), None],
            "is_current": [False, True],
        })
        report = validate_scd2(df, ["customer_id"])
        assert report.passed
        rule = next(r for r in report.rules if r.name == "no_overlapping_dates")
        assert rule.status == ValidationStatus.PASS

    def test_11_apply_scd2_produces_half_open_boundaries(self):
        """apply_scd2 sets old.effective_to = D and new.effective_from = D on change."""
        source_df = pl.DataFrame({
            "customer_id": [101],
            "name": ["Ravi"],
            "city": ["Bengaluru"],  # Changed from Chennai
        })
        target_df = pl.DataFrame({
            "customer_id": [101],
            "name": ["Ravi"],
            "city": ["Chennai"],
            "effective_from": [date(2026, 6, 1)],
            "effective_to": [None],
            "is_current": [True],
        })
        proc_date = date(2026, 6, 8)
        change_rep = detect_changes(source_df, target_df, ["customer_id"], ["city"], proc_date)
        result = apply_scd2(source_df, target_df, change_rep, ["customer_id"], ["city"], proc_date)

        # Verify previous row closed at proc_date
        old_row = result.filter(pl.col("is_current") == False)  # noqa: E712
        assert old_row["effective_to"][0] == proc_date
        assert old_row["effective_from"][0] == date(2026, 6, 1)

        # Verify new row starts at proc_date
        new_row = result.filter(pl.col("is_current") == True)  # noqa: E712
        assert new_row["effective_from"][0] == proc_date
        assert new_row["effective_to"][0] is None

        # Verify the entire result table passes SCD2 validation under half-open rules
        val_report = validate_scd2(result, ["customer_id"])
        assert val_report.passed

    def test_12_existing_change_detection_and_transform_semantics_green(self):
        """End-to-end combination of NEW, CHANGED, UNCHANGED, and DELETED produces valid half-open intervals."""
        source_df = pl.DataFrame({
            "customer_id": [101, 102, 104],  # 101 changed, 102 unchanged, 103 deleted, 104 new
            "tier": ["Platinum", "Silver", "Bronze"],
        })
        target_df = pl.DataFrame({
            "customer_id": [101, 102, 103],
            "tier": ["Gold", "Silver", "Gold"],
            "effective_from": [date(2026, 6, 1), date(2026, 6, 1), date(2026, 6, 1)],
            "effective_to": [None, None, None],
            "is_current": [True, True, True],
        })
        proc_date = date(2026, 6, 8)
        report = detect_changes(source_df, target_df, ["customer_id"], ["tier"], proc_date)
        updated_scd2 = apply_scd2(source_df, target_df, report, ["customer_id"], ["tier"], proc_date)

        # Validate resulting SCD2 table
        val_report = validate_scd2(updated_scd2, ["customer_id"])
        assert val_report.passed

    def test_13_regression_boundary_equality_is_not_overlap(self):
        """Regression test explicitly proving that effective_to == next.effective_from is NOT an overlap.

        Under [effective_from, effective_to), Version A [T1, T2) excludes T2.
        Version B [T2, T3) includes T2.
        Therefore, T2 belongs exclusively to Version B, and no instant is shared.
        """
        boundary = date(2026, 9, 6)
        df = pl.DataFrame({
            "id": [1, 1],
            "effective_from": [date(2026, 9, 1), boundary],
            "effective_to": [boundary, None],
            "is_current": [False, True],
        })
        val = validate_scd2(df, ["id"])
        overlap_rule = next(r for r in val.rules if r.name == "no_overlapping_dates")
        assert overlap_rule.status == ValidationStatus.PASS
        assert val.passed
