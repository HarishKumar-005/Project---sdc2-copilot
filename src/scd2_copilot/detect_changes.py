"""Deterministic change detection between source and target SCD2 tables.

Compares every business key in the source against the current rows
in the target and categorizes each as NEW, CHANGED, UNCHANGED, or DELETED.
"""

from __future__ import annotations

from datetime import date

import polars as pl

from .models import ChangeRecord, ChangeReport, ChangeType, FieldChange


def detect_changes(
    source_df: pl.DataFrame,
    target_df: pl.DataFrame,
    business_key: list[str],
    tracked_columns: list[str],
    processing_date: date,
) -> ChangeReport:
    """Compare source against target current rows and produce a ChangeReport.

    Only rows with ``is_current == true`` in the target participate in
    comparison. Historical (closed) rows are ignored.

    Args:
        source_df: Today's full snapshot.
        target_df: Yesterday's SCD2 table (may contain historical rows).
        business_key: Column name(s) forming the business key.
        tracked_columns: Column names to compare for changes.
        processing_date: The date to stamp on new/changed rows.

    Returns:
        A ChangeReport with categorized change records.
    """
    report = ChangeReport(processing_date=processing_date)

    # Extract current rows from target
    if "is_current" in target_df.columns:
        target_current = target_df.filter(pl.col("is_current") == True)  # noqa: E712
    else:
        target_current = target_df

    # Build lookup: business key → row dict for current target rows
    target_lookup: dict[tuple, dict] = {}
    for row in target_current.iter_rows(named=True):
        key = tuple(row[k] for k in business_key)
        target_lookup[key] = row

    # Build source key set
    source_keys: set[tuple] = set()

    for row in source_df.iter_rows(named=True):
        key = tuple(row[k] for k in business_key)
        source_keys.add(key)

        key_dict = {k: row[k] for k in business_key}

        if key not in target_lookup:
            # NEW record
            report.new.append(
                ChangeRecord(
                    business_key_values=key_dict,
                    change_type=ChangeType.NEW,
                )
            )
        else:
            # Compare tracked columns
            target_row = target_lookup[key]
            field_changes = _compare_fields(row, target_row, tracked_columns)

            if field_changes:
                report.changed.append(
                    ChangeRecord(
                        business_key_values=key_dict,
                        change_type=ChangeType.CHANGED,
                        field_changes=field_changes,
                    )
                )
            else:
                report.unchanged.append(
                    ChangeRecord(
                        business_key_values=key_dict,
                        change_type=ChangeType.UNCHANGED,
                    )
                )

    # DELETED: keys in target current but not in source
    for key, target_row in target_lookup.items():
        if key not in source_keys:
            key_dict = {k: target_row[k] for k in business_key}
            report.deleted.append(
                ChangeRecord(
                    business_key_values=key_dict,
                    change_type=ChangeType.DELETED,
                )
            )

    return report


def _compare_fields(
    source_row: dict,
    target_row: dict,
    tracked_columns: list[str],
) -> list[FieldChange]:
    """Field-by-field comparison of tracked columns."""
    changes: list[FieldChange] = []
    for col in tracked_columns:
        src_val = source_row.get(col)
        tgt_val = target_row.get(col)
        # Normalize None vs empty string
        if _normalize(src_val) != _normalize(tgt_val):
            changes.append(FieldChange(column=col, old_value=tgt_val, new_value=src_val))
    return changes


def _normalize(value) -> str | None:
    """Normalize a value for comparison (handle None, strip strings)."""
    if value is None:
        return None
    s = str(value).strip()
    return None if s == "" else s
