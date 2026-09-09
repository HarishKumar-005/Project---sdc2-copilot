"""Deterministic change detection between source and target SCD2 tables.

Compares every business key in the source against the current rows
in the target and categorizes each as NEW, CHANGED, UNCHANGED, or DELETED
using native Polars relational operations and expressions.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import polars as pl

from .models import (
    ChangeRecord,
    ChangeReport,
    ChangeType,
    DeletePolicy,
    FieldChange,
    LazyRecordSequence,
    SnapshotMode,
    _normalize_scalar,
)
from .validate import validate_business_keys


def _normalize_expr(col_expr: pl.Expr, dtype: pl.DataType | None = None) -> pl.Expr:
    """Vectorized column expression normalizing strings (trim whitespace, empty -> null)."""
    if dtype in (pl.String, pl.Utf8, None):
        s = col_expr.cast(pl.String).str.strip_chars()
        return pl.when(s == "").then(None).otherwise(s)
    return col_expr


def detect_changes(
    source_df: pl.DataFrame | pl.LazyFrame,
    target_df: pl.DataFrame | pl.LazyFrame,
    business_key: list[str],
    tracked_columns: list[str],
    processing_date: date,
    snapshot_mode: SnapshotMode | str = SnapshotMode.FULL,
    delete_policy: DeletePolicy | str = DeletePolicy.SOFT_DELETE,
    engine: str = "auto",
) -> ChangeReport:
    """Compare source against target current rows and produce a ChangeReport.

    Uses native Polars relational operations:
    - Primary matching: source LEFT JOIN active_target ON business_key
    - Deletion detection: active_target ANTI JOIN source (when FULL + SOFT_DELETE)
    - Vectorized column difference expressions with explicit null-safe equality

    Args:
        source_df: Today's source snapshot or incremental feed.
        target_df: Yesterday's SCD2 table (may contain historical rows).
        business_key: Column name(s) forming the business key.
        tracked_columns: Column names to compare for changes.
        processing_date: The date to stamp on new/changed rows.
        snapshot_mode: SnapshotMode.FULL or SnapshotMode.INCREMENTAL (default: FULL).
        delete_policy: DeletePolicy.SOFT_DELETE or DeletePolicy.IGNORE (default: SOFT_DELETE).
        engine: Execution engine: "auto", "in-memory", or "streaming" (default: "auto").

    Returns:
        A ChangeReport with categorized change records and vectorized key DataFrames.
    """
    if isinstance(snapshot_mode, str):
        try:
            snapshot_mode = SnapshotMode(snapshot_mode.lower().strip())
        except ValueError:
            raise ValueError(
                f"Invalid snapshot_mode: '{snapshot_mode}'. Must be 'full' or 'incremental'."
            )
    elif not isinstance(snapshot_mode, SnapshotMode):
        raise ValueError(
            f"Invalid snapshot_mode: {snapshot_mode!r}. Must be a SnapshotMode enum or string ('full', 'incremental')."
        )

    if isinstance(delete_policy, str):
        try:
            delete_policy = DeletePolicy(delete_policy.lower().strip())
        except ValueError:
            raise ValueError(
                f"Invalid delete_policy: '{delete_policy}'. Must be 'soft_delete' or 'ignore'."
            )
    elif not isinstance(delete_policy, DeletePolicy):
        raise ValueError(
            f"Invalid delete_policy: {delete_policy!r}. Must be a DeletePolicy enum or string ('soft_delete', 'ignore')."
        )

    # Collect LazyFrames if provided to ensure business key validation and schema access
    src_df = source_df.collect(engine=engine) if isinstance(source_df, pl.LazyFrame) else source_df
    tgt_df = target_df.collect(engine=engine) if isinstance(target_df, pl.LazyFrame) else target_df

    # Guard against duplicate business keys before any relational operations
    validate_business_keys(src_df, business_key, dataset_name="source")
    validate_business_keys(tgt_df, business_key, dataset_name="target")

    report = ChangeReport(
        processing_date=processing_date,
        snapshot_mode=snapshot_mode,
        delete_policy=delete_policy,
    )

    # Fast path: if source and target are both empty
    if src_df.is_empty() and tgt_df.is_empty():
        empty_keys_src = src_df.select(business_key)
        empty_keys_tgt = tgt_df.select(business_key)
        report.new_keys_df = empty_keys_src
        report.changed_keys_df = empty_keys_src
        report.unchanged_keys_df = empty_keys_tgt
        report.deleted_keys_df = empty_keys_tgt
        return report

    # Extract current rows from target
    if "is_current" in tgt_df.columns:
        target_current = tgt_df.filter(pl.col("is_current") == True)  # noqa: E712
    else:
        target_current = tgt_df

    # Fast path: target_current is empty (e.g. Day 1 initial load)
    if target_current.is_empty():
        report.new_keys_df = src_df.select(business_key)
        report.changed_keys_df = src_df.select(business_key).clear()
        report.unchanged_keys_df = src_df.select(business_key).clear()
        report.deleted_keys_df = tgt_df.select(business_key).clear() if tgt_df.width > 0 else src_df.select(business_key).clear()

        report.new = LazyRecordSequence(src_df.select(business_key), business_key, ChangeType.NEW)
        report.changed = LazyRecordSequence(src_df.select(business_key).clear(), business_key, ChangeType.CHANGED, tracked_columns)
        report.unchanged = LazyRecordSequence(src_df.select(business_key).clear(), business_key, ChangeType.UNCHANGED)
        report.deleted = LazyRecordSequence(report.deleted_keys_df, business_key, ChangeType.DELETED)
        return report

    # Fast path: source_df is empty
    if src_df.is_empty():
        report.new_keys_df = src_df.select(business_key)
        report.changed_keys_df = src_df.select(business_key)

        if snapshot_mode == SnapshotMode.FULL and delete_policy == DeletePolicy.SOFT_DELETE:
            report.deleted_keys_df = target_current.select(business_key)
            report.unchanged_keys_df = target_current.select(business_key).clear()
            report.deleted = LazyRecordSequence(target_current.select(business_key), business_key, ChangeType.DELETED)
            report.unchanged = LazyRecordSequence(target_current.select(business_key).clear(), business_key, ChangeType.UNCHANGED)
        else:
            report.deleted_keys_df = target_current.select(business_key).clear()
            report.unchanged_keys_df = src_df.select(business_key)
            report.deleted = LazyRecordSequence(target_current.select(business_key).clear(), business_key, ChangeType.DELETED)
            report.unchanged = LazyRecordSequence(src_df.select(business_key), business_key, ChangeType.UNCHANGED)

        report.new = LazyRecordSequence(src_df.select(business_key), business_key, ChangeType.NEW)
        report.changed = LazyRecordSequence(src_df.select(business_key), business_key, ChangeType.CHANGED, tracked_columns)
        return report

    # ── 1. Relational Matching: source LEFT JOIN target_current ────────
    target_matched = target_current.with_columns(pl.lit(True).alias("_target_match"))

    joined = src_df.join(
        target_matched,
        on=business_key,
        how="left",
        suffix="_target",
        validate="m:1",
    )

    # ── 2. Vectorized Column Comparison Expressions ─────────────────────
    if tracked_columns:
        diff_exprs = []
        for col in tracked_columns:
            s_dtype = src_df.schema.get(col, pl.String)
            t_dtype = tgt_df.schema.get(col, pl.String)
            if s_dtype != t_dtype or s_dtype in (pl.String, pl.Utf8) or t_dtype in (pl.String, pl.Utf8):
                s_norm = _normalize_expr(pl.col(col).cast(pl.String), pl.String)
                t_norm = _normalize_expr(pl.col(f"{col}_target").cast(pl.String), pl.String)
            else:
                s_norm = pl.col(col)
                t_norm = pl.col(f"{col}_target")
            diff_exprs.append(s_norm.ne_missing(t_norm))
        is_changed_expr = pl.any_horizontal(diff_exprs)
    else:
        is_changed_expr = pl.lit(False)

    # ── 3. Classify into Vectorized Slices ────────────────────────────────
    new_mask = pl.col("_target_match").is_null()
    changed_mask = pl.col("_target_match").is_not_null() & is_changed_expr
    unchanged_mask = pl.col("_target_match").is_not_null() & (~is_changed_expr)

    new_slice = joined.filter(new_mask)
    changed_slice = joined.filter(changed_mask)
    unchanged_slice = joined.filter(unchanged_mask)

    # ── 4. Deletions (active_target ANTI JOIN source) ───────────────────
    if snapshot_mode == SnapshotMode.FULL and delete_policy == DeletePolicy.SOFT_DELETE:
        deleted_slice = target_current.join(src_df, on=business_key, how="anti")
    else:
        deleted_slice = target_current.clear()

    # ── 5. Attach Vectorized Key DataFrames to Report ───────────────────
    report.new_keys_df = new_slice.select(business_key)
    report.changed_keys_df = changed_slice.select(business_key)
    report.unchanged_keys_df = unchanged_slice.select(business_key)
    report.deleted_keys_df = deleted_slice.select(business_key)

    # ── 6. Attach High-Performance Lazy Record Sequences ─────────────────
    report.new = LazyRecordSequence(new_slice.select(business_key), business_key, ChangeType.NEW)
    report.changed = LazyRecordSequence(changed_slice, business_key, ChangeType.CHANGED, tracked_columns)
    report.unchanged = LazyRecordSequence(unchanged_slice.select(business_key), business_key, ChangeType.UNCHANGED)
    report.deleted = LazyRecordSequence(deleted_slice.select(business_key), business_key, ChangeType.DELETED)

    return report


def _compare_fields(
    source_row: dict,
    target_row: dict,
    tracked_columns: list[str],
) -> list[FieldChange]:
    """Field-by-field comparison of tracked columns for backward compatibility."""
    changes: list[FieldChange] = []
    for col in tracked_columns:
        src_val = source_row.get(col)
        tgt_val = target_row.get(col)
        if _normalize_scalar(src_val) != _normalize_scalar(tgt_val):
            changes.append(FieldChange(column=col, old_value=tgt_val, new_value=src_val))
    return changes


def _normalize(value) -> str | None:
    """Normalize a value for comparison (backward compatibility alias)."""
    return _normalize_scalar(value)
