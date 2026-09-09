"""Tests for M1.5: Real Delete Policy (soft_delete vs ignore).

Verifies that:
1. DeletePolicy is a typed enum ('soft_delete', 'ignore').
2. Full + soft_delete classifies absent target current keys as DELETED and closes them in apply_scd2.
3. Full + ignore does NOT classify absent target keys as DELETED and retains them active in apply_scd2.
4. Incremental + soft_delete does NOT classify absent target keys as DELETED (absence is not meaningful).
5. Incremental + ignore does NOT classify absent target keys as DELETED.
6. Multiple missing keys and composite business keys obey the selected policy.
7. NEW, CHANGED, and UNCHANGED records remain identical under both policies.
8. Historical inactive rows are never modified by delete policy.
9. Invalid delete_policy values are rejected explicitly with ValueError.
10. detect_changes and apply_scd2 enforce policy consistency (conflicting values raise ValueError).
11. Settings and ChangeReport properly hold and default to DeletePolicy.SOFT_DELETE.
12. Backward compatibility is strictly preserved (default is soft_delete).
"""

from dataclasses import asdict
from datetime import date
import polars as pl
import pytest

from src.scd2_copilot.config import Settings
from src.scd2_copilot.detect_changes import detect_changes
from src.scd2_copilot.models import ChangeReport, ChangeType, DeletePolicy, SnapshotMode
from src.scd2_copilot.transform_scd2 import apply_scd2
from src.scd2_copilot.validate import validate_scd2
from src.scd2_copilot.workflow import detect_task, transform_task


@pytest.fixture
def target_sample_df() -> pl.DataFrame:
    """Target SCD2 table containing 3 active records and 1 historical closed record."""
    return pl.DataFrame(
        {
            "id": [101, 101, 102, 103],
            "name": ["Alice", "Alice", "Bob", "Charlie"],
            "salary": [90000, 100000, 120000, 110000],
            "effective_from": [
                date(2025, 1, 1),
                date(2026, 1, 1),
                date(2026, 1, 1),
                date(2026, 1, 1),
            ],
            "effective_to": [
                date(2026, 1, 1),
                None,
                None,
                None,
            ],
            "is_current": [False, True, True, True],
        }
    )


# ── 1. The Four-Way Interaction Matrix ──────────────────────────────────────


def test_full_soft_delete_classifies_absent_keys_as_deleted(target_sample_df):
    """Case 1: full + soft_delete -> absent keys (102, 103) become DELETED."""
    source_df = pl.DataFrame({"id": [101], "name": ["Alice"], "salary": [100000]})

    report = detect_changes(
        source_df=source_df,
        target_df=target_sample_df,
        business_key=["id"],
        tracked_columns=["name", "salary"],
        processing_date=date(2026, 9, 6),
        snapshot_mode=SnapshotMode.FULL,
        delete_policy=DeletePolicy.SOFT_DELETE,
    )

    assert report.snapshot_mode == SnapshotMode.FULL
    assert report.delete_policy == DeletePolicy.SOFT_DELETE
    assert len(report.deleted) == 2
    deleted_keys = {r.business_key_values["id"] for r in report.deleted}
    assert deleted_keys == {102, 103}


def test_full_ignore_does_not_classify_absent_keys_as_deleted(target_sample_df):
    """Case 2: full + ignore -> absent keys (102, 103) are NOT DELETED."""
    source_df = pl.DataFrame({"id": [101], "name": ["Alice"], "salary": [100000]})

    report = detect_changes(
        source_df=source_df,
        target_df=target_sample_df,
        business_key=["id"],
        tracked_columns=["name", "salary"],
        processing_date=date(2026, 9, 6),
        snapshot_mode=SnapshotMode.FULL,
        delete_policy=DeletePolicy.IGNORE,
    )

    assert report.snapshot_mode == SnapshotMode.FULL
    assert report.delete_policy == DeletePolicy.IGNORE
    assert len(report.deleted) == 0
    assert len(report.unchanged) == 1


def test_incremental_soft_delete_does_not_classify_absent_keys_as_deleted(target_sample_df):
    """Case 3: incremental + soft_delete -> absent keys are NOT DELETED (absence not meaningful)."""
    source_df = pl.DataFrame({"id": [101], "name": ["Alice"], "salary": [100000]})

    report = detect_changes(
        source_df=source_df,
        target_df=target_sample_df,
        business_key=["id"],
        tracked_columns=["name", "salary"],
        processing_date=date(2026, 9, 6),
        snapshot_mode=SnapshotMode.INCREMENTAL,
        delete_policy=DeletePolicy.SOFT_DELETE,
    )

    assert report.snapshot_mode == SnapshotMode.INCREMENTAL
    assert report.delete_policy == DeletePolicy.SOFT_DELETE
    assert len(report.deleted) == 0


def test_incremental_ignore_does_not_classify_absent_keys_as_deleted(target_sample_df):
    """Case 4: incremental + ignore -> absent keys are NOT DELETED."""
    source_df = pl.DataFrame({"id": [101], "name": ["Alice"], "salary": [100000]})

    report = detect_changes(
        source_df=source_df,
        target_df=target_sample_df,
        business_key=["id"],
        tracked_columns=["name", "salary"],
        processing_date=date(2026, 9, 6),
        snapshot_mode=SnapshotMode.INCREMENTAL,
        delete_policy=DeletePolicy.IGNORE,
    )

    assert report.snapshot_mode == SnapshotMode.INCREMENTAL
    assert report.delete_policy == DeletePolicy.IGNORE
    assert len(report.deleted) == 0


# ── 2. SCD2 Transformation State Transitions ────────────────────────────────


def test_full_soft_delete_closes_active_rows_in_apply_scd2(target_sample_df):
    """full + soft_delete closes deleted rows (effective_to=proc_date, is_current=False)."""
    source_df = pl.DataFrame({"id": [101], "name": ["Alice"], "salary": [100000]})
    proc_date = date(2026, 9, 6)

    report = detect_changes(
        source_df=source_df,
        target_df=target_sample_df,
        business_key=["id"],
        tracked_columns=["name", "salary"],
        processing_date=proc_date,
        snapshot_mode=SnapshotMode.FULL,
        delete_policy=DeletePolicy.SOFT_DELETE,
    )

    transformed = apply_scd2(
        source_df=source_df,
        target_df=target_sample_df,
        change_report=report,
        business_key=["id"],
        tracked_columns=["name", "salary"],
        processing_date=proc_date,
        delete_policy=DeletePolicy.SOFT_DELETE,
    )

    val = validate_scd2(transformed, ["id"])
    assert val.passed

    # 102 and 103 must be closed
    row_102 = transformed.filter(pl.col("id") == 102)
    assert row_102.height == 1
    assert row_102[0, "is_current"] is False
    assert row_102[0, "effective_to"] == proc_date

    row_103 = transformed.filter(pl.col("id") == 103)
    assert row_103.height == 1
    assert row_103[0, "is_current"] is False
    assert row_103[0, "effective_to"] == proc_date


def test_full_ignore_keeps_absent_active_rows_active_in_apply_scd2(target_sample_df):
    """full + ignore leaves absent active rows active and untouched."""
    source_df = pl.DataFrame({"id": [101], "name": ["Alice"], "salary": [100000]})
    proc_date = date(2026, 9, 6)

    report = detect_changes(
        source_df=source_df,
        target_df=target_sample_df,
        business_key=["id"],
        tracked_columns=["name", "salary"],
        processing_date=proc_date,
        snapshot_mode=SnapshotMode.FULL,
        delete_policy=DeletePolicy.IGNORE,
    )

    transformed = apply_scd2(
        source_df=source_df,
        target_df=target_sample_df,
        change_report=report,
        business_key=["id"],
        tracked_columns=["name", "salary"],
        processing_date=proc_date,
        delete_policy=DeletePolicy.IGNORE,
    )

    val = validate_scd2(transformed, ["id"])
    assert val.passed

    # 102 and 103 must remain active
    row_102 = transformed.filter(pl.col("id") == 102)
    assert row_102.height == 1
    assert row_102[0, "is_current"] is True
    assert row_102[0, "effective_to"] is None

    row_103 = transformed.filter(pl.col("id") == 103)
    assert row_103.height == 1
    assert row_103[0, "is_current"] is True
    assert row_103[0, "effective_to"] is None


def test_incremental_soft_delete_preserves_absent_active_rows(target_sample_df):
    """incremental + soft_delete preserves absent active rows (zero deletions)."""
    source_df = pl.DataFrame({"id": [101], "name": ["Alice"], "salary": [100000]})
    proc_date = date(2026, 9, 6)

    report = detect_changes(
        source_df=source_df,
        target_df=target_sample_df,
        business_key=["id"],
        tracked_columns=["name", "salary"],
        processing_date=proc_date,
        snapshot_mode=SnapshotMode.INCREMENTAL,
        delete_policy=DeletePolicy.SOFT_DELETE,
    )

    transformed = apply_scd2(
        source_df=source_df,
        target_df=target_sample_df,
        change_report=report,
        business_key=["id"],
        tracked_columns=["name", "salary"],
        processing_date=proc_date,
        delete_policy=DeletePolicy.SOFT_DELETE,
    )

    val = validate_scd2(transformed, ["id"])
    assert val.passed

    assert transformed.filter(pl.col("id") == 102)[0, "is_current"] is True
    assert transformed.filter(pl.col("id") == 103)[0, "is_current"] is True


def test_incremental_ignore_preserves_absent_active_rows(target_sample_df):
    """incremental + ignore preserves absent active rows."""
    source_df = pl.DataFrame({"id": [101], "name": ["Alice"], "salary": [100000]})
    proc_date = date(2026, 9, 6)

    report = detect_changes(
        source_df=source_df,
        target_df=target_sample_df,
        business_key=["id"],
        tracked_columns=["name", "salary"],
        processing_date=proc_date,
        snapshot_mode=SnapshotMode.INCREMENTAL,
        delete_policy=DeletePolicy.IGNORE,
    )

    transformed = apply_scd2(
        source_df=source_df,
        target_df=target_sample_df,
        change_report=report,
        business_key=["id"],
        tracked_columns=["name", "salary"],
        processing_date=proc_date,
        delete_policy=DeletePolicy.IGNORE,
    )

    val = validate_scd2(transformed, ["id"])
    assert val.passed

    assert transformed.filter(pl.col("id") == 102)[0, "is_current"] is True
    assert transformed.filter(pl.col("id") == 103)[0, "is_current"] is True


# ── 3. Record Types Invariance (NEW, CHANGED, UNCHANGED, HISTORICAL) ────────


def test_record_types_invariant_under_both_policies(target_sample_df):
    """NEW, CHANGED, UNCHANGED, and HISTORICAL records behave identically under both policies."""
    # 101 is UNCHANGED, 102 is CHANGED, 104 is NEW. 103 is absent.
    source_df = pl.DataFrame(
        {
            "id": [101, 102, 104],
            "name": ["Alice", "Bob", "Diana"],
            "salary": [100000, 130000, 140000],
        }
    )
    proc_date = date(2026, 9, 6)

    # Policy 1: soft_delete
    rep_soft = detect_changes(
        source_df, target_sample_df, ["id"], ["name", "salary"], proc_date,
        delete_policy=DeletePolicy.SOFT_DELETE,
    )
    # Policy 2: ignore
    rep_ign = detect_changes(
        source_df, target_sample_df, ["id"], ["name", "salary"], proc_date,
        delete_policy=DeletePolicy.IGNORE,
    )

    # Non-deleted categories must match exactly
    assert rep_soft.unchanged == rep_ign.unchanged
    assert rep_soft.changed == rep_ign.changed
    assert rep_soft.new == rep_ign.new

    # Deleted differs
    assert len(rep_soft.deleted) == 1
    assert rep_soft.deleted[0].business_key_values["id"] == 103
    assert len(rep_ign.deleted) == 0

    # Test historical rows preservation in apply_scd2
    for policy in [DeletePolicy.SOFT_DELETE, DeletePolicy.IGNORE]:
        rep = rep_soft if policy == DeletePolicy.SOFT_DELETE else rep_ign
        tx = apply_scd2(
            source_df, target_sample_df, rep, ["id"], ["name", "salary"], proc_date,
            delete_policy=policy,
        )
        # 101 historical closed row must still exist untouched
        hist = tx.filter((pl.col("id") == 101) & (pl.col("is_current") == False))  # noqa: E712
        assert hist.height == 1
        assert hist[0, "effective_from"] == date(2025, 1, 1)
        assert hist[0, "effective_to"] == date(2026, 1, 1)
        assert hist[0, "salary"] == 90000

        # 104 new row must exist
        new_row = tx.filter(pl.col("id") == 104)
        assert new_row.height == 1
        assert new_row[0, "is_current"] is True
        assert new_row[0, "effective_from"] == proc_date


# ── 4. Composite Business Keys ──────────────────────────────────────────────


def test_composite_business_keys_obey_delete_policy():
    """Composite keys obey the chosen delete_policy."""
    target_df = pl.DataFrame(
        {
            "org_id": ["ORG1", "ORG1"],
            "dept_id": ["ENG", "HR"],
            "budget": [500000, 200000],
            "effective_from": [date(2026, 1, 1)] * 2,
            "effective_to": [None] * 2,
            "is_current": [True] * 2,
        }
    )
    # Source only has ENG; HR is absent
    source_df = pl.DataFrame(
        {
            "org_id": ["ORG1"],
            "dept_id": ["ENG"],
            "budget": [500000],
        }
    )
    proc_date = date(2026, 9, 6)

    # soft_delete -> HR deleted
    rep_soft = detect_changes(
        source_df, target_df, ["org_id", "dept_id"], ["budget"], proc_date,
        delete_policy=DeletePolicy.SOFT_DELETE,
    )
    assert len(rep_soft.deleted) == 1
    assert rep_soft.deleted[0].business_key_values == {"org_id": "ORG1", "dept_id": "HR"}

    # ignore -> HR not deleted
    rep_ign = detect_changes(
        source_df, target_df, ["org_id", "dept_id"], ["budget"], proc_date,
        delete_policy=DeletePolicy.IGNORE,
    )
    assert len(rep_ign.deleted) == 0

    tx_ign = apply_scd2(
        source_df, target_df, rep_ign, ["org_id", "dept_id"], ["budget"], proc_date,
        delete_policy=DeletePolicy.IGNORE,
    )
    hr_row = tx_ign.filter((pl.col("org_id") == "ORG1") & (pl.col("dept_id") == "HR"))
    assert hr_row[0, "is_current"] is True
    assert hr_row[0, "effective_to"] is None


# ── 5. Policy Validation & Consistency Enforcement ──────────────────────────


def test_invalid_delete_policy_raises_value_error(target_sample_df):
    """Invalid delete_policy values must raise a descriptive ValueError."""
    source_df = pl.DataFrame({"id": [101], "name": ["Alice"], "salary": [100000]})

    for invalid in ["hard_delete", "purge", "remove", "", 123]:
        with pytest.raises(ValueError, match="Invalid delete_policy"):
            detect_changes(
                source_df, target_sample_df, ["id"], ["name", "salary"], date(2026, 9, 6),
                delete_policy=invalid,
            )

        with pytest.raises(ValueError, match="Invalid delete_policy"):
            apply_scd2(
                source_df, target_sample_df, ChangeReport(), ["id"], ["name", "salary"], date(2026, 9, 6),
                delete_policy=invalid,
            )

    # In detect_changes, None is rejected as it requires a concrete policy or uses its default
    with pytest.raises(ValueError, match="Invalid delete_policy"):
        detect_changes(
            source_df, target_sample_df, ["id"], ["name", "salary"], date(2026, 9, 6),
            delete_policy=None,
        )


def test_detect_changes_and_apply_scd2_policy_consistency(target_sample_df):
    """apply_scd2 raises ValueError if explicit delete_policy conflicts with change_report."""
    source_df = pl.DataFrame({"id": [101], "name": ["Alice"], "salary": [100000]})
    proc_date = date(2026, 9, 6)

    report_soft = detect_changes(
        source_df, target_sample_df, ["id"], ["name", "salary"], proc_date,
        delete_policy=DeletePolicy.SOFT_DELETE,
    )

    # Calling apply_scd2 with conflicting ignore policy must be rejected
    with pytest.raises(ValueError, match="Conflict in delete_policy"):
        apply_scd2(
            source_df, target_sample_df, report_soft, ["id"], ["name", "salary"], proc_date,
            delete_policy=DeletePolicy.IGNORE,
        )

    # Calling apply_scd2 with matching policy succeeds
    tx = apply_scd2(
        source_df, target_sample_df, report_soft, ["id"], ["name", "salary"], proc_date,
        delete_policy=DeletePolicy.SOFT_DELETE,
    )
    assert tx.height > 0


def test_apply_scd2_rejects_deleted_records_with_ignore_policy(target_sample_df):
    """If delete_policy is ignore but change_report has DELETED records, apply_scd2 rejects it."""
    source_df = pl.DataFrame({"id": [101], "name": ["Alice"], "salary": [100000]})
    proc_date = date(2026, 9, 6)

    # Craft an inconsistent change report
    inconsistent_report = ChangeReport(
        delete_policy=DeletePolicy.IGNORE,
        deleted=[
            detect_changes(
                source_df, target_sample_df, ["id"], ["name", "salary"], proc_date,
                delete_policy=DeletePolicy.SOFT_DELETE,
            ).deleted[0]
        ],
    )

    with pytest.raises(ValueError, match="Conflict: delete_policy is 'ignore'"):
        apply_scd2(
            source_df, target_sample_df, inconsistent_report, ["id"], ["name", "salary"], proc_date,
        )


# ── 6. Defaults, Configuration & Workflow Integration ───────────────────────


def test_default_delete_policy_is_soft_delete(target_sample_df):
    """Default delete_policy must be SOFT_DELETE for backward compatibility."""
    source_df = pl.DataFrame({"id": [101], "name": ["Alice"], "salary": [100000]})

    report = detect_changes(
        source_df, target_sample_df, ["id"], ["name", "salary"], date(2026, 9, 6),
    )
    assert report.delete_policy == DeletePolicy.SOFT_DELETE
    assert len(report.deleted) == 2


def test_settings_configures_delete_policy():
    """Settings defaults to SOFT_DELETE and accepts string or enum override."""
    s_default = Settings()
    assert s_default.delete_policy == DeletePolicy.SOFT_DELETE

    s_ign = Settings(delete_policy="ignore")
    assert s_ign.delete_policy == DeletePolicy.IGNORE

    s_soft = Settings(delete_policy=DeletePolicy.SOFT_DELETE)
    assert s_soft.delete_policy == DeletePolicy.SOFT_DELETE


def test_change_report_dataclass_includes_delete_policy():
    """ChangeReport includes delete_policy in serialization and dict export."""
    report = ChangeReport(delete_policy=DeletePolicy.IGNORE)
    dumped = asdict(report)
    assert dumped["delete_policy"] == DeletePolicy.IGNORE
    assert dumped["delete_policy"].value == "ignore"


def test_workflow_tasks_forward_delete_policy():
    """Workflow detect_task and transform_task forward delete_policy accurately."""
    source_df = pl.DataFrame({"id": [101], "name": ["Alice"], "salary": [100000]})
    target_df = pl.DataFrame(
        {
            "id": [101, 102],
            "name": ["Alice", "Bob"],
            "salary": [100000, 120000],
            "effective_from": [date(2026, 1, 1), date(2026, 1, 1)],
            "effective_to": [None, None],
            "is_current": [True, True],
        }
    )
    proc_date = date(2026, 9, 6)

    # Test detect_task.fn with ignore
    rep = detect_task.fn(
        source_df, target_df, ["id"], ["name", "salary"], proc_date,
        delete_policy=DeletePolicy.IGNORE,
    )
    assert rep.delete_policy == DeletePolicy.IGNORE
    assert len(rep.deleted) == 0

    # Test transform_task.fn with ignore
    tx = transform_task.fn(
        source_df, target_df, rep, ["id"], ["name", "salary"], proc_date,
        delete_policy=DeletePolicy.IGNORE,
    )
    row_102 = tx.filter(pl.col("id") == 102)
    assert row_102[0, "is_current"] is True
    assert row_102[0, "effective_to"] is None
