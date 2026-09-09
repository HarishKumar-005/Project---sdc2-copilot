"""M1.7 — SCD2 Invariant and Trustworthiness Hardening Tests.

Verifies:
1. Empty and minimal feed edge cases (empty source, empty target, bare schemas).
2. Key invariants including partial composite key collisions and historical duplicates.
3. Current-row flag pairing consistency (is_current vs effective_to).
4. Pipeline stability and idempotence under repeated identical snapshots.
5. Golden reference fixture for Milestone 2 Polars optimization comparison.
"""

from __future__ import annotations

from datetime import date
import polars as pl
import pytest

from src.scd2_copilot.detect_changes import detect_changes
from src.scd2_copilot.models import (
    ChangeType,
    DeletePolicy,
    SnapshotMode,
    ValidationStatus,
)
from src.scd2_copilot.transform_scd2 import apply_scd2
from src.scd2_copilot.validate import validate_scd2


# ============================================================================
# 1. EMPTY AND MINIMAL FEED EDGE CASES
# ============================================================================


class TestEmptyAndMinimalFeeds:
    """Edge cases for empty source feeds, empty target tables, and first snapshots."""

    def test_empty_source_full_soft_delete_closes_all_active(self):
        """In full snapshot mode with soft_delete, an empty source closes all active target rows."""
        target = pl.DataFrame({
            "id": [101, 102],
            "name": ["Alice", "Bob"],
            "effective_from": [date(2026, 6, 1), date(2026, 6, 1)],
            "effective_to": [None, None],
            "is_current": [True, True],
        })
        empty_source = pl.DataFrame({
            "id": pl.Series([], dtype=pl.Int64),
            "name": pl.Series([], dtype=pl.Utf8),
        })
        proc_date = date(2026, 6, 8)

        report = detect_changes(
            empty_source, target, ["id"], ["name"], proc_date,
            snapshot_mode=SnapshotMode.FULL,
            delete_policy=DeletePolicy.SOFT_DELETE,
        )
        assert len(report.deleted) == 2
        assert len(report.new) == 0
        assert len(report.changed) == 0
        assert len(report.unchanged) == 0

        output = apply_scd2(
            empty_source, target, report, ["id"], ["name"], proc_date,
            delete_policy=DeletePolicy.SOFT_DELETE,
        )
        # Both active rows are now closed at proc_date
        assert output.height == 2
        assert output.filter(pl.col("is_current") == True).height == 0  # noqa: E712
        assert output.filter(pl.col("is_current") == False).height == 2  # noqa: E712
        assert all(d == proc_date for d in output["effective_to"].to_list())

        val = validate_scd2(output, ["id"])
        assert val.passed

    def test_empty_source_incremental_preserves_all_active(self):
        """In incremental mode, an empty delta feed must produce 0 deletions and preserve active rows."""
        target = pl.DataFrame({
            "id": [101, 102],
            "name": ["Alice", "Bob"],
            "effective_from": [date(2026, 6, 1), date(2026, 6, 1)],
            "effective_to": [None, None],
            "is_current": [True, True],
        })
        empty_source = pl.DataFrame({
            "id": pl.Series([], dtype=pl.Int64),
            "name": pl.Series([], dtype=pl.Utf8),
        })
        proc_date = date(2026, 6, 8)

        report = detect_changes(
            empty_source, target, ["id"], ["name"], proc_date,
            snapshot_mode=SnapshotMode.INCREMENTAL,
            delete_policy=DeletePolicy.SOFT_DELETE,
        )
        assert len(report.deleted) == 0
        assert report.total == 0

        output = apply_scd2(
            empty_source, target, report, ["id"], ["name"], proc_date,
            delete_policy=DeletePolicy.SOFT_DELETE,
        )
        assert output.height == 2
        assert output.filter(pl.col("is_current") == True).height == 2  # noqa: E712
        val = validate_scd2(output, ["id"])
        assert val.passed

    def test_empty_source_full_ignore_preserves_all_active(self):
        """In full mode with delete_policy=ignore, empty source infers 0 deletions."""
        target = pl.DataFrame({
            "id": [101],
            "name": ["Alice"],
            "effective_from": [date(2026, 6, 1)],
            "effective_to": [None],
            "is_current": [True],
        })
        empty_source = pl.DataFrame({
            "id": pl.Series([], dtype=pl.Int64),
            "name": pl.Series([], dtype=pl.Utf8),
        })
        proc_date = date(2026, 6, 8)

        report = detect_changes(
            empty_source, target, ["id"], ["name"], proc_date,
            snapshot_mode=SnapshotMode.FULL,
            delete_policy=DeletePolicy.IGNORE,
        )
        assert len(report.deleted) == 0

        output = apply_scd2(
            empty_source, target, report, ["id"], ["name"], proc_date,
            delete_policy=DeletePolicy.IGNORE,
        )
        assert output.height == 1
        assert output["is_current"][0] is True
        val = validate_scd2(output, ["id"])
        assert val.passed

    def test_first_snapshot_bare_empty_target(self):
        """Initial load with bare empty target (without SCD2 columns) creates valid SCD2 table."""
        source = pl.DataFrame({
            "id": [1, 2, 3],
            "tier": ["Gold", "Silver", "Bronze"],
        })
        bare_empty_target = pl.DataFrame({
            "id": pl.Series([], dtype=pl.Int64),
            "tier": pl.Series([], dtype=pl.Utf8),
        })
        proc_date = date(2026, 6, 8)

        report = detect_changes(source, bare_empty_target, ["id"], ["tier"], proc_date)
        assert len(report.new) == 3

        output = apply_scd2(source, bare_empty_target, report, ["id"], ["tier"], proc_date)
        assert output.height == 3
        assert set(output.columns) == {"id", "tier", "effective_from", "effective_to", "is_current"}
        assert output["is_current"].to_list() == [True, True, True]
        assert output["effective_from"].to_list() == [proc_date, proc_date, proc_date]
        assert output["effective_to"].to_list() == [None, None, None]

        val = validate_scd2(output, ["id"])
        assert val.passed

    def test_both_empty_preserves_schema_dtypes(self):
        """When both source and target are empty, output schema preserves typed temporal and boolean columns."""
        empty_source = pl.DataFrame({
            "id": pl.Series([], dtype=pl.Int64),
            "name": pl.Series([], dtype=pl.Utf8),
        })
        empty_target = pl.DataFrame({
            "id": pl.Series([], dtype=pl.Int64),
            "name": pl.Series([], dtype=pl.Utf8),
            "effective_from": pl.Series([], dtype=pl.Date),
            "effective_to": pl.Series([], dtype=pl.Date),
            "is_current": pl.Series([], dtype=pl.Boolean),
        })
        proc_date = date(2026, 6, 8)

        report = detect_changes(empty_source, empty_target, ["id"], ["name"], proc_date)
        output = apply_scd2(empty_source, empty_target, report, ["id"], ["name"], proc_date)

        assert output.height == 0
        assert output.schema["id"] == pl.Int64
        assert output.schema["effective_from"] == pl.Date
        assert output.schema["effective_to"] == pl.Date
        assert output.schema["is_current"] == pl.Boolean

        val = validate_scd2(output, ["id"])
        assert val.passed


# ============================================================================
# 2. KEY INVARIANTS & PARTIAL COMPOSITE COLLISIONS
# ============================================================================


class TestKeyInvariants:
    """Verifies composite keys with partial overlap and null key handling."""

    def test_partial_composite_key_collision_handled_correctly(self):
        """Entities sharing one key attribute but differing on another must not collide or misclassify.

        Example:
        - Entity (1, 10): UNCHANGED
        - Entity (1, 20): CHANGED
        - Entity (2, 10): NEW
        - Entity (2, 20): DELETED
        """
        bk = ["org_id", "dept_id"]
        tc = ["headcount"]
        proc_date = date(2026, 6, 8)

        target = pl.DataFrame({
            "org_id": [1, 1, 2],
            "dept_id": [10, 20, 20],
            "headcount": [50, 100, 200],
            "effective_from": [date(2026, 6, 1)] * 3,
            "effective_to": [None] * 3,
            "is_current": [True] * 3,
        })

        source = pl.DataFrame({
            "org_id": [1, 1, 2],
            "dept_id": [10, 20, 10],
            "headcount": [50, 120, 15],  # (1, 10) unchanged, (1, 20) changed, (2, 10) new
        })

        report = detect_changes(source, target, bk, tc, proc_date)
        assert len(report.unchanged) == 1
        assert report.unchanged[0].business_key_values == {"org_id": 1, "dept_id": 10}

        assert len(report.changed) == 1
        assert report.changed[0].business_key_values == {"org_id": 1, "dept_id": 20}

        assert len(report.new) == 1
        assert report.new[0].business_key_values == {"org_id": 2, "dept_id": 10}

        assert len(report.deleted) == 1
        assert report.deleted[0].business_key_values == {"org_id": 2, "dept_id": 20}

        output = apply_scd2(source, target, report, bk, tc, proc_date)
        assert output.height == 5  # (1, 10) active, (1, 20) closed + new, (2, 10) new, (2, 20) closed

        val = validate_scd2(output, bk)
        assert val.passed

    def test_null_business_key_fails_validation(self):
        """A row containing null in any business key component triggers FAIL in validation."""
        df = pl.DataFrame({
            "org_id": [1, None],
            "dept_id": [10, 20],
            "val": ["A", "B"],
            "effective_from": [date(2026, 6, 1), date(2026, 6, 1)],
            "effective_to": [None, None],
            "is_current": [True, True],
        })
        val = validate_scd2(df, ["org_id", "dept_id"])
        assert not val.passed
        rule = next(r for r in val.rules if r.name == "no_null_keys")
        assert rule.status == ValidationStatus.FAIL


# ============================================================================
# 3. CURRENT-ROW FLAG VS DATE CONSISTENCY
# ============================================================================


class TestCurrentRowFlagConsistency:
    """Verifies that is_current pairing with effective_to is strictly enforced in date_consistency."""

    def test_active_row_with_non_null_effective_to_fails(self):
        """Active row (is_current=True) must have effective_to is None. Non-null effective_to must FAIL."""
        df = pl.DataFrame({
            "id": [101],
            "effective_from": [date(2026, 6, 1)],
            "effective_to": [date(2026, 6, 8)],  # Invalid: active row has closing date!
            "is_current": [True],
        })
        val = validate_scd2(df, ["id"])
        assert not val.passed
        rule = next(r for r in val.rules if r.name == "date_consistency")
        assert rule.status == ValidationStatus.FAIL
        assert "invalid current-row flag pairings" in rule.message
        assert any("is_current=True" in d and "null effective_to" in d for d in rule.details)

    def test_closed_row_with_null_effective_to_fails(self):
        """Closed row (is_current=False) must have non-null effective_to. NULL effective_to must FAIL."""
        df = pl.DataFrame({
            "id": [101],
            "effective_from": [date(2026, 6, 1)],
            "effective_to": [None],  # Invalid: closed row has no closing date!
            "is_current": [False],
        })
        val = validate_scd2(df, ["id"])
        assert not val.passed
        rule = next(r for r in val.rules if r.name == "date_consistency")
        assert rule.status == ValidationStatus.FAIL
        assert "invalid current-row flag pairings" in rule.message
        assert any("is_current=False" in d and "non-null effective_to" in d for d in rule.details)

    def test_proper_current_and_closed_pairings_pass(self):
        """Correct pairing: is_current=False with date, is_current=True with None passes date_consistency."""
        df = pl.DataFrame({
            "id": [101, 101],
            "effective_from": [date(2026, 6, 1), date(2026, 6, 8)],
            "effective_to": [date(2026, 6, 8), None],
            "is_current": [False, True],
        })
        val = validate_scd2(df, ["id"])
        assert val.passed
        rule = next(r for r in val.rules if r.name == "date_consistency")
        assert rule.status == ValidationStatus.PASS


# ============================================================================
# 4. PIPELINE STABILITY & IDEMPOTENCE
# ============================================================================


class TestPipelineStabilityAndIdempotence:
    """Verifies that repeatedly running identical snapshots produces idempotent results."""

    def test_repeated_identical_snapshot_is_idempotent(self):
        """Running Day 2 snapshot with identical data as Day 1 produces zero new versions and preserves state."""
        source_day1 = pl.DataFrame({
            "id": [101, 102],
            "tier": ["Gold", "Silver"],
        })
        empty_target = pl.DataFrame({
            "id": pl.Series([], dtype=pl.Int64),
            "tier": pl.Series([], dtype=pl.Utf8),
            "effective_from": pl.Series([], dtype=pl.Date),
            "effective_to": pl.Series([], dtype=pl.Date),
            "is_current": pl.Series([], dtype=pl.Boolean),
        })

        # Day 1 execution
        proc_date_1 = date(2026, 6, 1)
        rep1 = detect_changes(source_day1, empty_target, ["id"], ["tier"], proc_date_1)
        target_day1 = apply_scd2(source_day1, empty_target, rep1, ["id"], ["tier"], proc_date_1)
        assert target_day1.height == 2

        # Day 2 execution with identical source data
        source_day2 = pl.DataFrame({
            "id": [101, 102],
            "tier": ["Gold", "Silver"],
        })
        proc_date_2 = date(2026, 6, 2)
        rep2 = detect_changes(source_day2, target_day1, ["id"], ["tier"], proc_date_2)

        # Invariant: 100% UNCHANGED
        assert len(rep2.unchanged) == 2
        assert len(rep2.new) == 0
        assert len(rep2.changed) == 0
        assert len(rep2.deleted) == 0

        target_day2 = apply_scd2(source_day2, target_day1, rep2, ["id"], ["tier"], proc_date_2)

        # Invariant: Table is completely unchanged, height remains 2, effective_from stays 2026-06-01
        assert target_day2.height == 2
        assert target_day2.equals(target_day1)
        assert target_day2["effective_from"].to_list() == [proc_date_1, proc_date_1]

        val = validate_scd2(target_day2, ["id"])
        assert val.passed

    def test_output_deterministic_sorting_and_columns(self):
        """Output DataFrame must deterministically sort by business_key + effective_from with ordered columns."""
        source = pl.DataFrame({
            "id": [200, 100],
            "val": ["B", "A"],
        })
        empty_tgt = pl.DataFrame({
            "id": pl.Series([], dtype=pl.Int64),
            "val": pl.Series([], dtype=pl.Utf8),
        })
        out = apply_scd2(source, empty_tgt, detect_changes(source, empty_tgt, ["id"], ["val"], date(2026, 6, 8)), ["id"], ["val"], date(2026, 6, 8))

        # Expected column ordering
        assert out.columns == ["id", "val", "effective_from", "effective_to", "is_current"]
        # Expected sort ordering
        assert out["id"].to_list() == [100, 200]


# ============================================================================
# 5. M2 GOLDEN REFERENCE BENCHMARK
# ============================================================================


class TestM2GoldenReference:
    """Golden reference behavior fixture.

    This test exercises all SCD2 transitions simultaneously:
    - Key 101: UNCHANGED (retains active row)
    - Key 102: CHANGED (preserves old history, closes active row, opens new active row)
    - Key 103: DELETED (closes active row)
    - Key 104: NEW (inserts new active row)
    - Key 105: Chronological gap in historical versions, current version changed

    Milestone 2 Polars optimization must preserve this exact output table.
    """

    def test_m2_golden_reference_behavior(self):
        bk = ["customer_id"]
        tc = ["tier", "city"]
        proc_date = date(2026, 6, 8)

        target_df = pl.DataFrame({
            "customer_id": [101, 102, 102, 103, 105, 105],
            "tier": ["Gold", "Bronze", "Silver", "Platinum", "Bronze", "Gold"],
            "city": ["New York", "London", "London", "Paris", "Berlin", "Munich"],
            "effective_from": [
                date(2026, 6, 1),
                date(2026, 5, 1),
                date(2026, 6, 1),
                date(2026, 6, 1),
                date(2026, 1, 1),  # Gap between March and May
                date(2026, 5, 1),
            ],
            "effective_to": [
                None,
                date(2026, 6, 1),
                None,
                None,
                date(2026, 3, 1),
                None,
            ],
            "is_current": [
                True,
                False,
                True,
                True,
                False,
                True,
            ],
        })

        source_df = pl.DataFrame({
            "customer_id": [101, 102, 104, 105],
            "tier": ["Gold", "Silver", "Bronze", "Platinum"],
            "city": ["New York", "Manchester", "Tokyo", "Munich"],  # 101 unchanged, 102 changed, 104 new, 105 changed
        })

        report = detect_changes(
            source_df, target_df, bk, tc, proc_date,
            snapshot_mode=SnapshotMode.FULL,
            delete_policy=DeletePolicy.SOFT_DELETE,
        )

        assert report.summary == {
            "new": 1,        # 104
            "changed": 2,    # 102, 105
            "unchanged": 1,  # 101
            "deleted": 1,    # 103
            "total": 5,
        }

        output_df = apply_scd2(
            source_df, target_df, report, bk, tc, proc_date,
            delete_policy=DeletePolicy.SOFT_DELETE,
        )

        # Expected output height:
        # 101: 1 row (active)
        # 102: 3 rows (1 old history + 1 closed + 1 new active)
        # 103: 1 row (closed)
        # 104: 1 row (new active)
        # 105: 3 rows (1 gap history + 1 closed + 1 new active)
        # Total = 9 rows
        assert output_df.height == 9

        # Verify sorting
        assert output_df["customer_id"].to_list() == [101, 102, 102, 102, 103, 104, 105, 105, 105]

        # Verify Key 101: Unchanged
        row_101 = output_df.filter(pl.col("customer_id") == 101)
        assert row_101.to_dicts() == [{
            "customer_id": 101,
            "tier": "Gold",
            "city": "New York",
            "effective_from": date(2026, 6, 1),
            "effective_to": None,
            "is_current": True,
        }]

        # Verify Key 102: Changed
        rows_102 = output_df.filter(pl.col("customer_id") == 102).to_dicts()
        assert rows_102 == [
            {"customer_id": 102, "tier": "Bronze", "city": "London", "effective_from": date(2026, 5, 1), "effective_to": date(2026, 6, 1), "is_current": False},
            {"customer_id": 102, "tier": "Silver", "city": "London", "effective_from": date(2026, 6, 1), "effective_to": date(2026, 6, 8), "is_current": False},
            {"customer_id": 102, "tier": "Silver", "city": "Manchester", "effective_from": date(2026, 6, 8), "effective_to": None, "is_current": True},
        ]

        # Verify Key 103: Deleted
        row_103 = output_df.filter(pl.col("customer_id") == 103)
        assert row_103.to_dicts() == [{
            "customer_id": 103,
            "tier": "Platinum",
            "city": "Paris",
            "effective_from": date(2026, 6, 1),
            "effective_to": date(2026, 6, 8),
            "is_current": False,
        }]

        # Verify Key 104: New
        row_104 = output_df.filter(pl.col("customer_id") == 104)
        assert row_104.to_dicts() == [{
            "customer_id": 104,
            "tier": "Bronze",
            "city": "Tokyo",
            "effective_from": date(2026, 6, 8),
            "effective_to": None,
            "is_current": True,
        }]

        # Verify Key 105: Gap history preserved + changed
        rows_105 = output_df.filter(pl.col("customer_id") == 105).to_dicts()
        assert rows_105 == [
            {"customer_id": 105, "tier": "Bronze", "city": "Berlin", "effective_from": date(2026, 1, 1), "effective_to": date(2026, 3, 1), "is_current": False},
            {"customer_id": 105, "tier": "Gold", "city": "Munich", "effective_from": date(2026, 5, 1), "effective_to": date(2026, 6, 8), "is_current": False},
            {"customer_id": 105, "tier": "Platinum", "city": "Munich", "effective_from": date(2026, 6, 8), "effective_to": None, "is_current": True},
        ]

        # Invariant validation passes 100%
        val = validate_scd2(output_df, bk)
        assert val.passed
        assert val.summary["fail"] == 0
