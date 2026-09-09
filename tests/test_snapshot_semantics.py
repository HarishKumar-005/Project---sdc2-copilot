"""Tests for M1.4: Explicit Snapshot Semantics (Full vs Incremental).

Verifies that:
1. SnapshotMode is a typed enum ('full', 'incremental').
2. Full snapshot mode (default) infers DELETED for absent target current keys.
3. Incremental snapshot mode infers ZERO deletions for absent target current keys.
4. NEW, CHANGED, and UNCHANGED classifications function identically in both modes.
5. End-to-end SCD2 transformation preserves absent active rows as current in incremental mode.
6. Composite business keys work correctly in both modes.
7. Case-insensitive string input is coerced properly; invalid values raise ValueError.
8. Backward compatibility is strictly preserved (default is full).
"""

from datetime import date
import polars as pl
import pytest

from src.scd2_copilot.config import Settings
from src.scd2_copilot.detect_changes import detect_changes
from src.scd2_copilot.models import ChangeReport, ChangeType, SnapshotMode
from src.scd2_copilot.transform_scd2 import apply_scd2
from src.scd2_copilot.validate import validate_scd2
from src.scd2_copilot.workflow import run_pipeline


@pytest.fixture
def target_sample_df() -> pl.DataFrame:
    """Target SCD2 table containing 3 active records and 1 closed historical record."""
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


def test_full_snapshot_deletes_absent_target_keys(target_sample_df):
    """In FULL mode, active target keys absent from source must be classified as DELETED."""
    source_df = pl.DataFrame(
        {
            "id": [101],
            "name": ["Alice"],
            "salary": [100000],
        }
    )

    report = detect_changes(
        source_df=source_df,
        target_df=target_sample_df,
        business_key=["id"],
        tracked_columns=["name", "salary"],
        processing_date=date(2026, 9, 6),
        snapshot_mode=SnapshotMode.FULL,
    )

    assert report.snapshot_mode == SnapshotMode.FULL
    assert len(report.unchanged) == 1
    assert report.unchanged[0].business_key_values == {"id": 101}
    assert len(report.new) == 0
    assert len(report.changed) == 0
    assert len(report.deleted) == 2

    deleted_keys = {r.business_key_values["id"] for r in report.deleted}
    assert deleted_keys == {102, 103}


def test_incremental_snapshot_does_not_delete_absent_target_keys(target_sample_df):
    """In INCREMENTAL mode, absent active keys must NOT be classified as DELETED."""
    source_df = pl.DataFrame(
        {
            "id": [101],
            "name": ["Alice"],
            "salary": [100000],
        }
    )

    report = detect_changes(
        source_df=source_df,
        target_df=target_sample_df,
        business_key=["id"],
        tracked_columns=["name", "salary"],
        processing_date=date(2026, 9, 6),
        snapshot_mode=SnapshotMode.INCREMENTAL,
    )

    assert report.snapshot_mode == SnapshotMode.INCREMENTAL
    assert len(report.unchanged) == 1
    assert report.unchanged[0].business_key_values == {"id": 101}
    assert len(report.new) == 0
    assert len(report.changed) == 0
    assert len(report.deleted) == 0


def test_default_snapshot_mode_is_full_for_backward_compatibility(target_sample_df):
    """Omitting snapshot_mode must default to SnapshotMode.FULL."""
    source_df = pl.DataFrame(
        {
            "id": [101],
            "name": ["Alice"],
            "salary": [100000],
        }
    )

    # Calling detect_changes without snapshot_mode parameter
    report = detect_changes(
        source_df=source_df,
        target_df=target_sample_df,
        business_key=["id"],
        tracked_columns=["name", "salary"],
        processing_date=date(2026, 9, 6),
    )

    assert report.snapshot_mode == SnapshotMode.FULL
    assert len(report.deleted) == 2


def test_incremental_identical_change_detection_for_present_keys(target_sample_df):
    """In incremental mode, keys present in source are classified accurately (NEW, CHANGED, UNCHANGED)."""
    # 101 is UNCHANGED, 102 is CHANGED, 104 is NEW, 103 is absent
    source_df = pl.DataFrame(
        {
            "id": [101, 102, 104],
            "name": ["Alice", "Bob", "Diana"],
            "salary": [100000, 135000, 115000],
        }
    )

    # Test under FULL
    full_report = detect_changes(
        source_df=source_df,
        target_df=target_sample_df,
        business_key=["id"],
        tracked_columns=["name", "salary"],
        processing_date=date(2026, 9, 6),
        snapshot_mode=SnapshotMode.FULL,
    )
    assert len(full_report.unchanged) == 1
    assert full_report.unchanged[0].business_key_values == {"id": 101}
    assert len(full_report.changed) == 1
    assert full_report.changed[0].business_key_values == {"id": 102}
    assert len(full_report.new) == 1
    assert full_report.new[0].business_key_values == {"id": 104}
    assert len(full_report.deleted) == 1
    assert full_report.deleted[0].business_key_values == {"id": 103}

    # Test under INCREMENTAL
    inc_report = detect_changes(
        source_df=source_df,
        target_df=target_sample_df,
        business_key=["id"],
        tracked_columns=["name", "salary"],
        processing_date=date(2026, 9, 6),
        snapshot_mode=SnapshotMode.INCREMENTAL,
    )
    assert len(inc_report.unchanged) == 1
    assert inc_report.unchanged[0].business_key_values == {"id": 101}
    assert len(inc_report.changed) == 1
    assert inc_report.changed[0].business_key_values == {"id": 102}
    assert len(inc_report.new) == 1
    assert inc_report.new[0].business_key_values == {"id": 104}
    # Crucial difference: 103 is NOT deleted
    assert len(inc_report.deleted) == 0


def test_incremental_scd2_transformation_preserves_absent_active_rows(target_sample_df):
    """apply_scd2 in incremental mode retains absent active rows without closing them."""
    # Source has only 102 (changed) and 104 (new). 101 and 103 are absent from feed.
    source_df = pl.DataFrame(
        {
            "id": [102, 104],
            "name": ["Bob", "Diana"],
            "salary": [135000, 115000],
        }
    )
    proc_date = date(2026, 9, 6)

    report = detect_changes(
        source_df=source_df,
        target_df=target_sample_df,
        business_key=["id"],
        tracked_columns=["name", "salary"],
        processing_date=proc_date,
        snapshot_mode=SnapshotMode.INCREMENTAL,
    )

    transformed = apply_scd2(
        source_df=source_df,
        target_df=target_sample_df,
        change_report=report,
        business_key=["id"],
        tracked_columns=["name", "salary"],
        processing_date=proc_date,
    )

    # Validate output meets all SCD2 invariants
    val_report = validate_scd2(transformed, business_key=["id"])
    assert val_report.passed, f"Validation failed: {val_report.summary}"

    # Check 101: historical closed row and active row must BOTH still exist
    rows_101 = transformed.filter(pl.col("id") == 101).sort("effective_from")
    assert rows_101.height == 2
    # Historical
    assert rows_101[0, "effective_from"] == date(2025, 1, 1)
    assert rows_101[0, "effective_to"] == date(2026, 1, 1)
    assert rows_101[0, "is_current"] is False
    assert rows_101[0, "salary"] == 90000
    # Active retained unmodified
    assert rows_101[1, "effective_from"] == date(2026, 1, 1)
    assert rows_101[1, "effective_to"] is None
    assert rows_101[1, "is_current"] is True
    assert rows_101[1, "salary"] == 100000

    # Check 103: was absent, must be retained active and unmodified
    rows_103 = transformed.filter(pl.col("id") == 103)
    assert rows_103.height == 1
    assert rows_103[0, "effective_from"] == date(2026, 1, 1)
    assert rows_103[0, "effective_to"] is None
    assert rows_103[0, "is_current"] is True
    assert rows_103[0, "salary"] == 110000

    # Check 102: was changed, so old row closed, new row opened
    rows_102 = transformed.filter(pl.col("id") == 102).sort("effective_from")
    assert rows_102.height == 2
    assert rows_102[0, "effective_from"] == date(2026, 1, 1)
    assert rows_102[0, "effective_to"] == proc_date
    assert rows_102[0, "is_current"] is False
    assert rows_102[1, "effective_from"] == proc_date
    assert rows_102[1, "effective_to"] is None
    assert rows_102[1, "is_current"] is True
    assert rows_102[1, "salary"] == 135000

    # Check 104: was new, so 1 active row added
    rows_104 = transformed.filter(pl.col("id") == 104)
    assert rows_104.height == 1
    assert rows_104[0, "effective_from"] == proc_date
    assert rows_104[0, "effective_to"] is None
    assert rows_104[0, "is_current"] is True
    assert rows_104[0, "salary"] == 115000


def test_composite_business_keys_in_incremental_mode():
    """Composite business keys are correctly handled in incremental mode."""
    target_df = pl.DataFrame(
        {
            "org_id": ["ORG1", "ORG1", "ORG2"],
            "dept_id": ["ENG", "HR", "ENG"],
            "budget": [500000, 200000, 300000],
            "effective_from": [date(2026, 1, 1)] * 3,
            "effective_to": [None] * 3,
            "is_current": [True] * 3,
        }
    )

    # Source has only ORG1/ENG (budget increase) and ORG2/SALES (new)
    source_df = pl.DataFrame(
        {
            "org_id": ["ORG1", "ORG2"],
            "dept_id": ["ENG", "SALES"],
            "budget": [550000, 150000],
        }
    )
    proc_date = date(2026, 9, 6)

    report = detect_changes(
        source_df=source_df,
        target_df=target_df,
        business_key=["org_id", "dept_id"],
        tracked_columns=["budget"],
        processing_date=proc_date,
        snapshot_mode=SnapshotMode.INCREMENTAL,
    )

    assert len(report.changed) == 1
    assert len(report.new) == 1
    assert len(report.deleted) == 0

    transformed = apply_scd2(
        source_df=source_df,
        target_df=target_df,
        change_report=report,
        business_key=["org_id", "dept_id"],
        tracked_columns=["budget"],
        processing_date=proc_date,
    )

    val_report = validate_scd2(transformed, business_key=["org_id", "dept_id"])
    assert val_report.passed

    # Verify absent keys ('ORG1', 'HR') and ('ORG2', 'ENG') are still current
    hr_row = transformed.filter((pl.col("org_id") == "ORG1") & (pl.col("dept_id") == "HR"))
    assert hr_row.height == 1
    assert hr_row[0, "is_current"] is True
    assert hr_row[0, "budget"] == 200000

    org2_eng_row = transformed.filter((pl.col("org_id") == "ORG2") & (pl.col("dept_id") == "ENG"))
    assert org2_eng_row.height == 1
    assert org2_eng_row[0, "is_current"] is True
    assert org2_eng_row[0, "budget"] == 300000


def test_string_coercion_and_case_insensitivity(target_sample_df):
    """Strings like 'full', 'FULL', 'incremental', 'Incremental' should be valid."""
    source_df = pl.DataFrame(
        {
            "id": [101],
            "name": ["Alice"],
            "salary": [100000],
        }
    )

    for full_str in ["full", "FULL", "Full", "  FULL  "]:
        r = detect_changes(
            source_df, target_sample_df, ["id"], ["name", "salary"], date(2026, 9, 6),
            snapshot_mode=full_str.strip(),
        )
        assert r.snapshot_mode == SnapshotMode.FULL
        assert len(r.deleted) == 2

    for inc_str in ["incremental", "INCREMENTAL", "Incremental", "  INCREMENTAL  "]:
        r = detect_changes(
            source_df, target_sample_df, ["id"], ["name", "salary"], date(2026, 9, 6),
            snapshot_mode=inc_str.strip(),
        )
        assert r.snapshot_mode == SnapshotMode.INCREMENTAL
        assert len(r.deleted) == 0


def test_invalid_snapshot_mode_raises_value_error(target_sample_df):
    """Invalid snapshot modes should raise a clear ValueError."""
    source_df = pl.DataFrame(
        {
            "id": [101],
            "name": ["Alice"],
            "salary": [100000],
        }
    )

    for invalid_val in ["delta", "partial", "all", "", 123, None]:
        with pytest.raises(ValueError, match="Invalid snapshot_mode"):
            detect_changes(
                source_df, target_sample_df, ["id"], ["name", "salary"], date(2026, 9, 6),
                snapshot_mode=invalid_val,
            )


def test_config_settings_snapshot_mode():
    """Settings can be initialized with snapshot_mode string or enum."""
    s_default = Settings()
    assert s_default.snapshot_mode == SnapshotMode.FULL

    s_inc = Settings(snapshot_mode="incremental")
    assert s_inc.snapshot_mode == SnapshotMode.INCREMENTAL

    s_full = Settings(snapshot_mode=SnapshotMode.FULL)
    assert s_full.snapshot_mode == SnapshotMode.FULL


from dataclasses import asdict


def test_change_report_model_serialization():
    """ChangeReport includes snapshot_mode in dump and serialization."""
    report = ChangeReport(
        processing_date=date(2026, 9, 6),
        snapshot_mode=SnapshotMode.INCREMENTAL,
    )
    dumped = asdict(report)
    assert dumped["snapshot_mode"] == SnapshotMode.INCREMENTAL
    assert dumped["snapshot_mode"].value == "incremental"
    assert report.snapshot_mode == SnapshotMode.INCREMENTAL


from src.scd2_copilot.workflow import detect_task


def test_workflow_detect_task_incremental_mode():
    """detect_task forwards snapshot_mode=SnapshotMode.INCREMENTAL properly."""
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
    report = detect_task.fn(
        source_df=source_df,
        target_df=target_df,
        business_key=["id"],
        tracked_columns=["name", "salary"],
        processing_date=date(2026, 9, 6),
        snapshot_mode=SnapshotMode.INCREMENTAL,
    )
    assert report.snapshot_mode == SnapshotMode.INCREMENTAL
    assert len(report.deleted) == 0
    assert len(report.unchanged) == 1
