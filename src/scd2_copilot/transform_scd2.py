"""SCD2 transformation: apply change report to produce updated table.

Takes the existing target SCD2 table and the detected changes, then
produces the new SCD2 table with closed rows, new rows, and preserved
historical rows.
"""

from __future__ import annotations

from datetime import date

import polars as pl

from .models import ChangeReport


def apply_scd2(
    source_df: pl.DataFrame,
    target_df: pl.DataFrame,
    change_report: ChangeReport,
    business_key: list[str],
    tracked_columns: list[str],
    processing_date: date,
) -> pl.DataFrame:
    """Generate the updated SCD2 table.

    Logic:
    1. **Historical rows** (is_current=false): preserved unchanged.
    2. **Unchanged current rows**: preserved unchanged.
    3. **Changed current rows**: old row closed (effective_to=today,
       is_current=false), new row inserted from source.
    4. **Deleted current rows**: closed (effective_to=today, is_current=false).
    5. **New records**: inserted from source with SCD2 metadata.

    Args:
        source_df: Today's source data.
        target_df: Yesterday's SCD2 table.
        change_report: The output of detect_changes().
        business_key: Business key column(s).
        tracked_columns: Tracked attribute columns.
        processing_date: Date to stamp on new/changed rows.

    Returns:
        Updated SCD2 Polars DataFrame.
    """
    output_columns = business_key + tracked_columns + [
        "effective_from", "effective_to", "is_current"
    ]

    result_rows: list[dict] = []

    # ── 1. Preserve historical (non-current) rows ─────────────
    if "is_current" in target_df.columns:
        historical = target_df.filter(pl.col("is_current") == False)  # noqa: E712
        for row in historical.iter_rows(named=True):
            result_rows.append(_pick(row, output_columns))

    # ── 2. Build key sets for fast lookup ─────────────────────
    changed_keys = {
        _key_tuple(r.business_key_values, business_key)
        for r in change_report.changed
    }
    deleted_keys = {
        _key_tuple(r.business_key_values, business_key)
        for r in change_report.deleted
    }
    new_keys = {
        _key_tuple(r.business_key_values, business_key)
        for r in change_report.new
    }
    unchanged_keys = {
        _key_tuple(r.business_key_values, business_key)
        for r in change_report.unchanged
    }

    # ── 3. Process current rows in target ─────────────────────
    if "is_current" in target_df.columns:
        target_current = target_df.filter(pl.col("is_current") == True)  # noqa: E712
    else:
        target_current = target_df

    for row in target_current.iter_rows(named=True):
        key = tuple(row[k] for k in business_key)

        if key in unchanged_keys:
            # Keep as-is
            result_rows.append(_pick(row, output_columns))

        elif key in changed_keys:
            # Close old row
            closed = _pick(row, output_columns)
            closed["effective_to"] = processing_date
            closed["is_current"] = False
            result_rows.append(closed)

        elif key in deleted_keys:
            # Soft delete: close row
            closed = _pick(row, output_columns)
            closed["effective_to"] = processing_date
            closed["is_current"] = False
            result_rows.append(closed)

    # ── 4. Insert new current rows for CHANGED records ────────
    source_lookup = _build_source_lookup(source_df, business_key)

    for key in changed_keys:
        src_row = source_lookup.get(key, {})
        new_row = {col: src_row.get(col) for col in business_key + tracked_columns}
        new_row["effective_from"] = processing_date
        new_row["effective_to"] = None
        new_row["is_current"] = True
        result_rows.append(new_row)

    # ── 5. Insert new records ─────────────────────────────────
    for key in new_keys:
        src_row = source_lookup.get(key, {})
        new_row = {col: src_row.get(col) for col in business_key + tracked_columns}
        new_row["effective_from"] = processing_date
        new_row["effective_to"] = None
        new_row["is_current"] = True
        result_rows.append(new_row)

    # ── 6. Build output DataFrame ─────────────────────────────
    if not result_rows:
        return pl.DataFrame(schema={
            c: pl.Utf8 for c in output_columns
        })

    result_df = pl.DataFrame(result_rows, schema_overrides={
        "effective_from": pl.Date,
        "effective_to": pl.Date,
        "is_current": pl.Boolean,
    })

    # Ensure column order
    result_df = result_df.select(output_columns)

    # Sort for deterministic output: business key, effective_from
    result_df = result_df.sort(business_key + ["effective_from"])

    return result_df


# ── Helpers ────────────────────────────────────────────


def _key_tuple(key_dict: dict, business_key: list[str]) -> tuple:
    """Convert a key dict to a hashable tuple."""
    return tuple(key_dict[k] for k in business_key)


def _pick(row: dict, columns: list[str]) -> dict:
    """Pick only the specified columns from a row dict."""
    return {c: row.get(c) for c in columns}


def _build_source_lookup(
    source_df: pl.DataFrame, business_key: list[str]
) -> dict[tuple, dict]:
    """Build a key → row lookup from the source DataFrame."""
    lookup: dict[tuple, dict] = {}
    for row in source_df.iter_rows(named=True):
        key = tuple(row[k] for k in business_key)
        lookup[key] = row
    return lookup
