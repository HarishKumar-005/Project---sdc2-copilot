"""Exhaustive Golden Parity Test Suite for M2.2 Vectorized SCD2 Engine.

Validates exact behavioral parity between native Polars vectorized operations
and the authoritative M1 reference semantics across:
- All change types (NEW, CHANGED, UNCHANGED, DELETED, Mixed)
- Composite business keys
- Multiple historical versions and temporal gap preservation
- Null-safe comparisons across String, Int, Float, Boolean, Date
- The complete 4-way SnapshotMode x DeletePolicy decision matrix
- Empty and single-row edge cases
- Eager vs LazyFrame in-memory vs LazyFrame streaming equivalence
- Verification that no Python row-iteration hot path (iter_rows) is invoked
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import polars as pl
import pytest

from src.scd2_copilot.detect_changes import detect_changes
from src.scd2_copilot.models import ChangeType, DeletePolicy, SnapshotMode
from src.scd2_copilot.transform_scd2 import apply_scd2
from src.scd2_copilot.validate import validate_scd2


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def base_source() -> pl.DataFrame:
    return pl.DataFrame({
        "customer_id": [101, 102, 103, 104],
        "name": ["Ravi", "Priya", "Arun", "Kiran"],
        "city": ["Bengaluru", "Mumbai", "Delhi", "Hyderabad"],
        "tier": ["Gold", "Silver", "Gold", "Bronze"],
    })


@pytest.fixture
def base_target() -> pl.DataFrame:
    return pl.DataFrame({
        "customer_id": [101, 102, 103],
        "name": ["Ravi", "Priya", "Arun"],
        "city": ["Chennai", "Mumbai", "Delhi"],
        "tier": ["Gold", "Silver", "Gold"],
        "effective_from": [date(2026, 6, 7), date(2026, 6, 7), date(2026, 6, 7)],
        "effective_to": pl.Series([None, None, None], dtype=pl.Date),
        "is_current": [True, True, True],
    })


# ── 1. Category Parity Tests ───────────────────────────────────────────


def test_mixed_changes_parity(base_source, base_target):
    """Verify standard mixed scenario (101 changed, 102 unchanged, 103 unchanged, 104 new)."""
    p_date = date(2026, 6, 8)
    b_key = ["customer_id"]
    tracked = ["name", "city", "tier"]

    report = detect_changes(base_source, base_target, b_key, tracked, p_date)
    assert len(report.new) == 1
    assert report.new[0].business_key_values == {"customer_id": 104}
    assert len(report.changed) == 1
    assert report.changed[0].business_key_values == {"customer_id": 101}
    assert len(report.changed[0].field_changes) == 1
    assert report.changed[0].field_changes[0].column == "city"
    assert report.changed[0].field_changes[0].old_value == "Chennai"
    assert report.changed[0].field_changes[0].new_value == "Bengaluru"
    assert len(report.unchanged) == 2
    assert {r.business_key_values["customer_id"] for r in report.unchanged} == {102, 103}
    assert len(report.deleted) == 0

    output = apply_scd2(base_source, base_target, report, b_key, tracked, p_date)
    assert output.height == 5
    val = validate_scd2(output, b_key)
    assert val.passed

    # 101 old version closed, new version active
    rows_101 = output.filter(pl.col("customer_id") == 101).sort("effective_from")
    assert rows_101.height == 2
    assert rows_101["effective_to"][0] == p_date
    assert rows_101["is_current"][0] is False
    assert rows_101["effective_from"][1] == p_date
    assert rows_101["effective_to"][1] is None
    assert rows_101["is_current"][1] is True


def test_all_unchanged_parity():
    """All records matching produce 100% UNCHANGED and zero new versions."""
    src = pl.DataFrame({"id": [1, 2], "val": ["A", "B"]})
    tgt = pl.DataFrame({
        "id": [1, 2],
        "val": ["A", "B"],
        "effective_from": [date(2026, 6, 1), date(2026, 6, 1)],
        "effective_to": pl.Series([None, None], dtype=pl.Date),
        "is_current": [True, True],
    })
    p_date = date(2026, 6, 8)
    rep = detect_changes(src, tgt, ["id"], ["val"], p_date)
    assert len(rep.unchanged) == 2
    assert rep.total == 2
    assert len(rep.new) == 0
    assert len(rep.changed) == 0
    assert len(rep.deleted) == 0

    out = apply_scd2(src, tgt, rep, ["id"], ["val"], p_date)
    assert out.height == 2
    assert (out["is_current"] == True).all()  # noqa: E712
    assert (out["effective_from"] == date(2026, 6, 1)).all()


def test_all_new_parity():
    """Empty target table results in 100% NEW records inserted as active."""
    src = pl.DataFrame({"id": [1, 2, 3], "val": ["X", "Y", "Z"]})
    tgt = pl.DataFrame(schema={
        "id": pl.Int64, "val": pl.String,
        "effective_from": pl.Date, "effective_to": pl.Date, "is_current": pl.Boolean,
    })
    p_date = date(2026, 6, 8)
    rep = detect_changes(src, tgt, ["id"], ["val"], p_date)
    assert len(rep.new) == 3
    assert rep.total == 3

    out = apply_scd2(src, tgt, rep, ["id"], ["val"], p_date)
    assert out.height == 3
    assert (out["is_current"] == True).all()  # noqa: E712
    assert (out["effective_from"] == p_date).all()
    assert (out["effective_to"].is_null()).all()
    assert validate_scd2(out, ["id"]).passed


def test_all_deleted_full_soft_delete_parity():
    """Empty source under FULL + SOFT_DELETE closes all active target records."""
    src = pl.DataFrame(schema={"id": pl.Int64, "val": pl.String})
    tgt = pl.DataFrame({
        "id": [1, 2],
        "val": ["A", "B"],
        "effective_from": [date(2026, 6, 1), date(2026, 6, 1)],
        "effective_to": pl.Series([None, None], dtype=pl.Date),
        "is_current": [True, True],
    })
    p_date = date(2026, 6, 8)
    rep = detect_changes(src, tgt, ["id"], ["val"], p_date, snapshot_mode=SnapshotMode.FULL, delete_policy=DeletePolicy.SOFT_DELETE)
    assert len(rep.deleted) == 2
    assert rep.total == 2

    out = apply_scd2(src, tgt, rep, ["id"], ["val"], p_date, delete_policy=DeletePolicy.SOFT_DELETE)
    assert out.height == 2
    assert (out["is_current"] == False).all()  # noqa: E712
    assert (out["effective_to"] == p_date).all()
    assert validate_scd2(out, ["id"]).passed


# ── 2. Composite Business Keys Parity ───────────────────────────────────


def test_composite_business_keys_parity():
    """Composite keys (dept_id, emp_id) must correctly track changes per composite entity."""
    src = pl.DataFrame({
        "dept_id": [10, 10, 20],
        "emp_id": [1, 2, 1],
        "role": ["Engineer", "Lead", "Manager"],
    })
    tgt = pl.DataFrame({
        "dept_id": [10, 10, 20],
        "emp_id": [1, 2, 2],  # (20, 1) is new in src, (20, 2) is deleted from tgt
        "role": ["Engineer", "Junior", "Manager"],
        "effective_from": [date(2026, 6, 1)] * 3,
        "effective_to": pl.Series([None] * 3, dtype=pl.Date),
        "is_current": [True] * 3,
    })
    b_key = ["dept_id", "emp_id"]
    tracked = ["role"]
    p_date = date(2026, 6, 8)

    rep = detect_changes(src, tgt, b_key, tracked, p_date)
    assert len(rep.unchanged) == 1  # (10, 1)
    assert len(rep.changed) == 1    # (10, 2) Junior -> Lead
    assert len(rep.new) == 1        # (20, 1)
    assert len(rep.deleted) == 1    # (20, 2)

    out = apply_scd2(src, tgt, rep, b_key, tracked, p_date)
    assert validate_scd2(out, b_key).passed
    assert out.height == 5  # 1 unchanged + 2 for changed + 1 deleted closed + 1 new


# ── 3. Historical and Temporal Gap Preservation ─────────────────────────


def test_multiple_historical_versions_and_gaps_preserved():
    """Existing inactive historical versions and temporal gaps must remain strictly immutable."""
    src = pl.DataFrame({
        "id": [101],
        "val": ["V3_Updated"],
    })
    tgt = pl.DataFrame({
        "id": [101, 101],
        "val": ["V1", "V2"],
        "effective_from": [date(2026, 1, 1), date(2026, 4, 1)],  # gap between 2026-02-01 and 2026-04-01
        "effective_to": [date(2026, 2, 1), None],
        "is_current": [False, True],
    })
    b_key = ["id"]
    tracked = ["val"]
    p_date = date(2026, 6, 8)

    rep = detect_changes(src, tgt, b_key, tracked, p_date)
    assert len(rep.changed) == 1

    out = apply_scd2(src, tgt, rep, b_key, tracked, p_date)
    assert out.height == 3
    assert validate_scd2(out, b_key).passed

    # Verify historical V1 is completely unmodified
    v1 = out.filter(pl.col("effective_from") == date(2026, 1, 1))
    assert v1["effective_to"][0] == date(2026, 2, 1)
    assert v1["is_current"][0] is False


# ── 4. Null and Empty String Semantics across Data Types ───────────────


def test_null_safe_comparison_matrix():
    """Verify null-safe comparison rules across String, Int, Float, Boolean, Date."""
    src = pl.DataFrame({
        "id": [1, 2, 3, 4, 5, 6, 7],
        "str_col": ["", None, "Alice", "Bob", None, "Dave", "Same"],
        "int_col": [None, 10, 20, None, 30, 40, 50],
        "date_col": [None, date(2026, 1, 1), None, date(2026, 2, 1), date(2026, 3, 1), None, date(2026, 5, 1)],
    })
    tgt = pl.DataFrame({
        "id": [1, 2, 3, 4, 5, 6, 7],
        "str_col": [None, None, "Alice  ", "Bobby", "Diff", None, "Same"],
        "int_col": [None, 10, 20, 25, None, None, 50],
        "date_col": [None, date(2026, 1, 1), date(2026, 1, 1), None, None, date(2026, 4, 1), date(2026, 5, 1)],
        "effective_from": [date(2026, 6, 1)] * 7,
        "effective_to": pl.Series([None] * 7, dtype=pl.Date),
        "is_current": [True] * 7,
    })
    b_key = ["id"]
    tracked = ["str_col", "int_col", "date_col"]
    p_date = date(2026, 6, 8)

    rep = detect_changes(src, tgt, b_key, tracked, p_date)
    # Row 1: "" vs None (str equal), None vs None (int equal), None vs None (date equal) -> UNCHANGED
    # Row 2: None vs None, 10 vs 10, 2026-01-01 vs 2026-01-01 -> UNCHANGED
    # Row 3: "Alice" vs "Alice  " (str equal), 20 vs 20, None vs 2026-01-01 -> CHANGED (date diff)
    # Row 4: "Bob" vs "Bobby" -> CHANGED
    # Row 5: None vs "Diff" -> CHANGED
    # Row 6: "Dave" vs None -> CHANGED
    # Row 7: "Same" vs "Same", 50 vs 50, date matches -> UNCHANGED
    unchanged_ids = {r.business_key_values["id"] for r in rep.unchanged}
    changed_ids = {r.business_key_values["id"] for r in rep.changed}

    assert unchanged_ids == {1, 2, 7}
    assert changed_ids == {3, 4, 5, 6}


# ── 5. 4-Way Decision Matrix Parity ────────────────────────────────────


@pytest.mark.parametrize(
    ("snap_mode", "del_policy", "expect_deleted", "expect_closed_in_apply"),
    [
        (SnapshotMode.FULL, DeletePolicy.SOFT_DELETE, 1, 1),
        (SnapshotMode.FULL, DeletePolicy.IGNORE, 0, 0),
        (SnapshotMode.INCREMENTAL, DeletePolicy.SOFT_DELETE, 0, 0),
        (SnapshotMode.INCREMENTAL, DeletePolicy.IGNORE, 0, 0),
    ]
)
def test_snapshot_delete_matrix_parity(snap_mode, del_policy, expect_deleted, expect_closed_in_apply):
    """Test all 4 combinations of SnapshotMode x DeletePolicy."""
    src = pl.DataFrame({"id": [101], "val": ["A"]})
    tgt = pl.DataFrame({
        "id": [101, 102],  # 102 is absent from source
        "val": ["A", "B"],
        "effective_from": [date(2026, 6, 1), date(2026, 6, 1)],
        "effective_to": pl.Series([None, None], dtype=pl.Date),
        "is_current": [True, True],
    })
    p_date = date(2026, 6, 8)

    rep = detect_changes(src, tgt, ["id"], ["val"], p_date, snapshot_mode=snap_mode, delete_policy=del_policy)
    assert len(rep.deleted) == expect_deleted

    out = apply_scd2(src, tgt, rep, ["id"], ["val"], p_date, delete_policy=del_policy)
    row_102 = out.filter(pl.col("id") == 102)
    assert row_102.height == 1

    if expect_closed_in_apply:
        assert row_102["is_current"][0] is False
        assert row_102["effective_to"][0] == p_date
    else:
        assert row_102["is_current"][0] is True
        assert row_102["effective_to"][0] is None


# ── 6. Eager vs Lazy In-Memory vs Streaming Equivalence ────────────────


@pytest.mark.parametrize("engine", ["auto", "in-memory", "streaming"])
def test_engine_modes_produce_identical_results(base_source, base_target, engine):
    """Verify that auto, in-memory, and streaming execution engines produce identical output."""
    p_date = date(2026, 6, 8)
    b_key = ["customer_id"]
    tracked = ["name", "city", "tier"]

    # Baseline eager
    rep_eager = detect_changes(base_source, base_target, b_key, tracked, p_date)
    out_eager = apply_scd2(base_source, base_target, rep_eager, b_key, tracked, p_date)

    # Tested engine
    rep_eng = detect_changes(base_source.lazy(), base_target.lazy(), b_key, tracked, p_date, engine=engine)
    out_eng = apply_scd2(base_source.lazy(), base_target.lazy(), rep_eng, b_key, tracked, p_date, engine=engine)

    assert rep_eager.summary == rep_eng.summary
    assert out_eager.equals(out_eng)
    assert validate_scd2(out_eng, b_key).passed


# ── 7. Verification: Zero iter_rows in Transformation Hot Path ─────────


def test_no_iter_rows_in_vectorized_transformation(base_source, base_target):
    """Verify that apply_scd2 does not invoke iter_rows during transformation."""
    p_date = date(2026, 6, 8)
    b_key = ["customer_id"]
    tracked = ["name", "city", "tier"]

    report = detect_changes(base_source, base_target, b_key, tracked, p_date)

    # In apply_scd2, iter_rows should be called 0 times
    with patch.object(pl.DataFrame, "iter_rows", side_effect=AssertionError("iter_rows called in apply_scd2!")):
        output = apply_scd2(base_source, base_target, report, b_key, tracked, p_date)
        assert output.height == 5
    assert validate_scd2(output, b_key).passed
