"""Comprehensive Correctness and Parity Tests for Vectorized Validation (M2.5).

Verifies:
1. Temporal Adjacency ([D1, D2) and [D2, D3) -> VALID)
2. Temporal Gaps ([D1, D2) and [D3, D4) -> VALID)
3. True Overlaps ([D1, D3) and [D2, D4) -> INVALID)
4. Open-ended prior version followed by another version -> INVALID
5. Zero-duration intervals ([D1, D1) -> INVALID in date consistency)
6. Reversed intervals ([D2, D1) -> INVALID in date consistency)
7. Current-row flag pairing consistency (is_current vs effective_to)
8. Duplicate active keys (one_current_per_key)
9. Historical duplicate keys allowed across distinct versions
10. Null business keys strictly rejected
11. Composite business keys with mixed valid/invalid partitions
12. Empty datasets and one-row datasets
13. Engine modes equivalence (auto vs in-memory vs streaming)
"""

from __future__ import annotations

from datetime import date
import polars as pl
import pytest

from src.scd2_copilot.models import ValidationStatus
from src.scd2_copilot.validate import (
    _check_date_consistency,
    _check_no_null_keys,
    _check_no_overlapping_dates,
    _check_one_current_per_key,
    _check_schema_completeness,
    validate_scd2,
)


class TestValidationVectorizedParity:
    """Test suite ensuring 100% semantic correctness of optimized vectorized validation."""

    def test_valid_single_current_row(self):
        """A single active version is valid."""
        df = pl.DataFrame({
            "id": [101],
            "effective_from": [date(2026, 6, 1)],
            "effective_to": [None],
            "is_current": [True],
        })
        rep = validate_scd2(df, ["id"])
        assert rep.passed
        assert all(r.status == ValidationStatus.PASS for r in rep.rules)

    def test_valid_multiple_history_versions_with_adjacency(self):
        """[D1, D2) and [D2, D3) and [D3, None) is valid adjacent history."""
        df = pl.DataFrame({
            "id": [101, 101, 101],
            "effective_from": [date(2026, 6, 1), date(2026, 6, 8), date(2026, 6, 15)],
            "effective_to": [date(2026, 6, 8), date(2026, 6, 15), None],
            "is_current": [False, False, True],
        })
        rep = validate_scd2(df, ["id"])
        assert rep.passed
        rule = next(r for r in rep.rules if r.name == "no_overlapping_dates")
        assert rule.status == ValidationStatus.PASS

    def test_valid_chronological_gaps(self):
        """[D1, D2) and [D3, D4) where D2 < D3 is valid non-contiguous history."""
        df = pl.DataFrame({
            "id": [101, 101],
            "effective_from": [date(2026, 6, 1), date(2026, 6, 10)],
            "effective_to": [date(2026, 6, 5), None],
            "is_current": [False, True],
        })
        rep = validate_scd2(df, ["id"])
        assert rep.passed
        rule = next(r for r in rep.rules if r.name == "no_overlapping_dates")
        assert rule.status == ValidationStatus.PASS

    def test_invalid_true_overlap(self):
        """[D1, D3) and [D2, D4) where D2 < D3 is a true invalid overlap."""
        df = pl.DataFrame({
            "id": [101, 101],
            "effective_from": [date(2026, 6, 1), date(2026, 6, 8)],
            "effective_to": [date(2026, 6, 10), None],
            "is_current": [False, True],
        })
        rep = validate_scd2(df, ["id"])
        assert not rep.passed
        rule = next(r for r in rep.rules if r.name == "no_overlapping_dates")
        assert rule.status == ValidationStatus.FAIL
        assert len(rule.details) == 1
        assert "Overlap detected for id=101" in rule.details[0]
        assert "2026-06-01 → 2026-06-10" in rule.details[0]
        assert "2026-06-08 → NULL" in rule.details[0]

    def test_invalid_open_ended_prior_version(self):
        """A previous version with effective_to=None followed by another version is invalid."""
        df = pl.DataFrame({
            "id": [101, 101],
            "effective_from": [date(2026, 6, 1), date(2026, 6, 8)],
            "effective_to": [None, None],
            "is_current": [False, True],
        })
        rep = validate_scd2(df, ["id"])
        assert not rep.passed
        rule = next(r for r in rep.rules if r.name == "no_overlapping_dates")
        assert rule.status == ValidationStatus.FAIL

    def test_zero_duration_interval_rejected(self):
        """effective_from == effective_to is rejected by date_consistency."""
        df = pl.DataFrame({
            "id": [101],
            "effective_from": [date(2026, 6, 1)],
            "effective_to": [date(2026, 6, 1)],
            "is_current": [False],
        })
        rep = validate_scd2(df, ["id"])
        assert not rep.passed
        rule = next(r for r in rep.rules if r.name == "date_consistency")
        assert rule.status == ValidationStatus.FAIL
        assert "effective_from (2026-06-01) >= effective_to (2026-06-01)" in rule.details[0]

    def test_reversed_interval_rejected(self):
        """effective_from > effective_to is rejected by date_consistency."""
        df = pl.DataFrame({
            "id": [101],
            "effective_from": [date(2026, 6, 10)],
            "effective_to": [date(2026, 6, 1)],
            "is_current": [False],
        })
        rep = validate_scd2(df, ["id"])
        assert not rep.passed
        rule = next(r for r in rep.rules if r.name == "date_consistency")
        assert rule.status == ValidationStatus.FAIL

    def test_current_flag_pairings(self):
        """is_current=True with non-null effective_to, and is_current=False with null effective_to."""
        df_bad_active = pl.DataFrame({
            "id": [101],
            "effective_from": [date(2026, 6, 1)],
            "effective_to": [date(2026, 6, 8)],
            "is_current": [True],
        })
        rep1 = validate_scd2(df_bad_active, ["id"])
        assert not rep1.passed
        rule1 = next(r for r in rep1.rules if r.name == "date_consistency")
        assert rule1.status == ValidationStatus.FAIL
        assert "Active row (is_current=True) must have null effective_to" in rule1.details[0]

        df_bad_closed = pl.DataFrame({
            "id": [102],
            "effective_from": [date(2026, 6, 1)],
            "effective_to": [None],
            "is_current": [False],
        })
        rep2 = validate_scd2(df_bad_closed, ["id"])
        assert not rep2.passed
        rule2 = next(r for r in rep2.rules if r.name == "date_consistency")
        assert rule2.status == ValidationStatus.FAIL
        assert "Closed row (is_current=False) must have non-null effective_to" in rule2.details[0]

    def test_duplicate_current_rows_rejected(self):
        """Multiple is_current=True rows for the same key -> one_current_per_key FAIL."""
        df = pl.DataFrame({
            "id": [101, 101],
            "effective_from": [date(2026, 6, 1), date(2026, 6, 8)],
            "effective_to": [None, None],
            "is_current": [True, True],
        })
        rep = validate_scd2(df, ["id"])
        assert not rep.passed
        rule = next(r for r in rep.rules if r.name == "one_current_per_key")
        assert rule.status == ValidationStatus.FAIL
        assert "1 business key(s) have multiple current rows." in rule.message

    def test_historical_duplicates_allowed(self):
        """Multiple closed historical rows for the same key across distinct periods are allowed."""
        df = pl.DataFrame({
            "id": [101, 101, 101],
            "effective_from": [date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)],
            "effective_to": [date(2026, 2, 1), date(2026, 3, 1), None],
            "is_current": [False, False, True],
        })
        rep = validate_scd2(df, ["id"])
        assert rep.passed
        rule = next(r for r in rep.rules if r.name == "one_current_per_key")
        assert rule.status == ValidationStatus.PASS

    def test_null_business_keys_rejected(self):
        """Null business key values -> no_null_keys FAIL."""
        df = pl.DataFrame({
            "id": [101, None],
            "effective_from": [date(2026, 6, 1), date(2026, 6, 1)],
            "effective_to": [None, None],
            "is_current": [True, True],
        })
        rep = validate_scd2(df, ["id"])
        assert not rep.passed
        rule = next(r for r in rep.rules if r.name == "no_null_keys")
        assert rule.status == ValidationStatus.FAIL

    def test_composite_key_overlap_isolated(self):
        """Overlap on one partition of a composite key flags only that partition."""
        df = pl.DataFrame({
            "region": ["East", "East", "West", "West"],
            "id": [101, 101, 101, 101],
            "effective_from": [date(2026, 6, 1), date(2026, 6, 8), date(2026, 6, 1), date(2026, 6, 5)],
            "effective_to": [date(2026, 6, 8), None, date(2026, 6, 10), None],
            "is_current": [False, True, False, True],
        })
        rep = validate_scd2(df, ["region", "id"])
        assert not rep.passed
        rule = next(r for r in rep.rules if r.name == "no_overlapping_dates")
        assert rule.status == ValidationStatus.FAIL
        assert len(rule.details) == 1
        assert "region=West, id=101" in rule.details[0]

    def test_empty_dataset(self):
        """Empty DataFrame with valid schema passes all rules."""
        df = pl.DataFrame(schema={
            "id": pl.Int64,
            "effective_from": pl.Date,
            "effective_to": pl.Date,
            "is_current": pl.Boolean,
        })
        rep = validate_scd2(df, ["id"])
        assert rep.passed
        assert all(r.status == ValidationStatus.PASS for r in rep.rules)

    def test_one_row_dataset(self):
        """1-row DataFrame passes validation."""
        df = pl.DataFrame({
            "id": [1],
            "effective_from": [date(2026, 6, 1)],
            "effective_to": [None],
            "is_current": [True],
        })
        rep = validate_scd2(df, ["id"])
        assert rep.passed

    def test_engine_mode_equivalence(self):
        """Validation gives identical reports under auto, in-memory, and streaming."""
        df = pl.DataFrame({
            "id": [101, 101, 102, 102, 103],
            "effective_from": [
                date(2026, 6, 1), date(2026, 6, 8),
                date(2026, 6, 1), date(2026, 6, 10),
                date(2026, 6, 1)
            ],
            "effective_to": [
                date(2026, 6, 8), None,
                date(2026, 6, 10), None,
                None
            ],
            "is_current": [False, True, False, True, True],
        })
        rep_auto = validate_scd2(df, ["id"], engine="auto")
        rep_mem = validate_scd2(df, ["id"], engine="in-memory")
        rep_stream = validate_scd2(df, ["id"], engine="streaming")

        assert rep_auto.passed == rep_mem.passed == rep_stream.passed == True
        assert rep_auto.summary == rep_mem.summary == rep_stream.summary
        for r_a, r_m, r_s in zip(rep_auto.rules, rep_mem.rules, rep_stream.rules):
            assert r_a.name == r_m.name == r_s.name
            assert r_a.status == r_m.status == r_s.status
            assert r_a.message == r_m.message == r_s.message
            assert r_a.details == r_m.details == r_s.details
