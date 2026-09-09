"""Lightweight SCD2 output validation.

Runs deterministic validation rules on the SCD2 output DataFrame.
No Great Expectations or external database (e.g. DuckDB) dependency for validation.
"""

from __future__ import annotations

from datetime import date
import logging
import time
import traceback
from typing import Any

import polars as pl

from .exceptions import DuplicateBusinessKeyError
from .models import ValidationReport, ValidationRule, ValidationStatus


def validate_business_keys(
    df: pl.DataFrame,
    business_key: list[str],
    dataset_name: str = "dataset",
) -> None:
    """Validate that business key values are unique within the active records.

    For an incoming source snapshot, all rows are checked.
    For a target SCD2 table, only active/current records (is_current == True)
    are checked; closed historical rows are excluded.

    Rows with NULL values in any business key column are ignored here so
    that standard null-key validation rules can handle them.

    Args:
        df: The DataFrame to validate.
        business_key: Column name(s) forming the business key.
        dataset_name: Descriptive name ('source', 'target', etc.) for error reporting.

    Raises:
        DuplicateBusinessKeyError: If any duplicate business key is found among active rows.
    """
    if df.is_empty() or df.width == 0:
        return

    # If any business key columns are missing from df, let column validation handle it
    missing_cols = [k for k in business_key if k not in df.columns]
    if missing_cols:
        return

    # Filter to active records if is_current column is present
    if "is_current" in df.columns:
        active_df = df.filter(pl.col("is_current") == True)  # noqa: E712
    else:
        active_df = df

    if active_df.is_empty():
        return

    # Exclude rows where any component of the business key is null
    null_filter = pl.any_horizontal([pl.col(k).is_null() for k in business_key])
    non_null_df = active_df.filter(~null_filter)

    if non_null_df.is_empty():
        return

    # Group by business key columns and count occurrences
    duplicates_df = (
        non_null_df
        .group_by(business_key)
        .agg(pl.len().alias("__count__"))
        .filter(pl.col("__count__") > 1)
    )

    if duplicates_df.height > 0:
        duplicate_count = duplicates_df.height
        sample_rows = list(duplicates_df.head(5).iter_rows(named=True))
        duplicate_samples: list[dict[str, Any]] = [
            {k: row[k] for k in business_key}
            for row in sample_rows
        ]

        formatted_samples = [
            "(" + ", ".join(f"{k}={row[k]}" for k in business_key) + f", count={row['__count__']})"
            for row in sample_rows
        ]
        samples_str = "; ".join(formatted_samples)
        more_str = f" and {duplicate_count - len(formatted_samples)} more" if duplicate_count > len(formatted_samples) else ""

        msg = (
            f"Duplicate business key(s) detected in {dataset_name}: "
            f"{duplicate_count} key(s) appear multiple times in active records: "
            f"[{samples_str}{more_str}]. "
            f"A business key must identify at most one row."
        )

        raise DuplicateBusinessKeyError(
            message=msg,
            dataset_name=dataset_name,
            business_key=business_key,
            duplicate_keys=duplicate_samples,
            duplicate_count=duplicate_count,
        )


def validate_scd2(
    df: pl.DataFrame,
    business_key: list[str],
    engine: str = "auto",
) -> ValidationReport:
    """Run all validation rules on the SCD2 output.

    Args:
        df: The updated SCD2 DataFrame to validate.
        business_key: Business key column(s).
        engine: Polars execution engine mode ('auto', 'in-memory', 'streaming').

    Returns:
        A ValidationReport with results for each rule.
    """
    report = ValidationReport()

    schema_rule = _check_schema_completeness(df)
    report.rules.append(schema_rule)

    # Check if required columns exist to safely perform canonical sorting
    has_schema = schema_rule.status == ValidationStatus.PASS
    has_keys = all(k in df.columns for k in business_key)

    sorted_df: pl.DataFrame | None = None
    if has_schema and has_keys and not df.is_empty():
        sort_cols = business_key + ["effective_from", "effective_to"]
        sorted_df = df.lazy().sort(sort_cols, nulls_last=True).collect(engine=engine)

    report.rules.append(_check_one_current_per_key(df, business_key, sorted_df))
    report.rules.append(_check_no_null_keys(df, business_key))
    report.rules.append(_check_no_overlapping_dates(df, business_key, sorted_df, engine=engine))
    report.rules.append(_check_date_consistency(df))

    return report


def _check_schema_completeness(df: pl.DataFrame) -> ValidationRule:
    """Verify that all required SCD2 columns exist."""
    required = {"effective_from", "effective_to", "is_current"}
    present = set(df.columns)
    missing = required - present

    if missing:
        return ValidationRule(
            name="schema_completeness",
            status=ValidationStatus.FAIL,
            message=f"Missing required SCD2 columns: {sorted(missing)}",
            details=[f"Missing: {c}" for c in sorted(missing)],
        )
    return ValidationRule(
        name="schema_completeness",
        status=ValidationStatus.PASS,
        message="All required SCD2 columns present.",
    )


def _check_one_current_per_key(
    df: pl.DataFrame,
    business_key: list[str],
    sorted_df: pl.DataFrame | None = None,
) -> ValidationRule:
    """No business key should have more than one is_current=true row."""
    if "is_current" not in df.columns:
        return ValidationRule(
            name="one_current_per_key",
            status=ValidationStatus.PASS,
            message="Each business key has at most one current row.",
        )

    missing_keys = [k for k in business_key if k not in df.columns]
    if missing_keys:
        return ValidationRule(
            name="one_current_per_key",
            status=ValidationStatus.WARN,
            message=f"Business key column(s) {missing_keys} missing.",
        )

    # Fast path: if sorted_df is provided, records are already sorted by business_key
    if sorted_df is not None:
        current = sorted_df.filter(pl.col("is_current") == True)  # noqa: E712
        is_same = pl.all_horizontal(
            [(pl.col(k) == pl.col(k).shift(1)).fill_null(False) for k in business_key]
        )
        dup_active = current.filter(is_same)
        if dup_active.is_empty():
            return ValidationRule(
                name="one_current_per_key",
                status=ValidationStatus.PASS,
                message="Each business key has at most one current row.",
            )

    # Standard / fallback path: group by to format exact duplicate details
    current = df.filter(pl.col("is_current") == True)  # noqa: E712
    duplicates = (
        current
        .group_by(business_key)
        .agg(pl.len().alias("cnt"))
        .filter(pl.col("cnt") > 1)
    )

    if duplicates.height > 0:
        detail_rows = duplicates.iter_rows(named=True)
        details = [
            f"Key {_format_key(row, business_key)} has {row['cnt']} current rows"
            for row in detail_rows
        ]
        return ValidationRule(
            name="one_current_per_key",
            status=ValidationStatus.FAIL,
            message=f"{duplicates.height} business key(s) have multiple current rows.",
            details=details,
        )

    return ValidationRule(
        name="one_current_per_key",
        status=ValidationStatus.PASS,
        message="Each business key has at most one current row.",
    )


def _check_no_null_keys(
    df: pl.DataFrame, business_key: list[str]
) -> ValidationRule:
    """Every row must have non-null business key value(s)."""
    null_filter = pl.lit(False)
    for k in business_key:
        null_filter = null_filter | pl.col(k).is_null()

    null_rows = df.filter(null_filter)

    if null_rows.height > 0:
        return ValidationRule(
            name="no_null_keys",
            status=ValidationStatus.FAIL,
            message=f"{null_rows.height} row(s) have null business key values.",
            details=[f"Row with null key found (row count: {null_rows.height})"],
        )

    return ValidationRule(
        name="no_null_keys",
        status=ValidationStatus.PASS,
        message="No null business keys found.",
    )


def _check_no_overlapping_dates(
    df: pl.DataFrame,
    business_key: list[str],
    sorted_df: pl.DataFrame | None = None,
    engine: str = "auto",
) -> ValidationRule:
    """For each business key, date ranges [effective_from, effective_to) must not overlap.

    Under [effective_from, effective_to) half-open interval semantics:
    - effective_from is inclusive, effective_to is exclusive.
    - Adjacent versions with previous.effective_to == next.effective_from are VALID.
    - Chronological gaps with previous.effective_to < next.effective_from are VALID.
    - Overlaps with previous.effective_to > next.effective_from are INVALID.
    - Open-ended previous version (previous.effective_to is NULL) followed by another
      version is an unclosed overlap and is INVALID.
    """
    logger = logging.getLogger(__name__)
    start_time = time.perf_counter()
    logger.info("Validation starting: checking for overlapping dates.")

    if "effective_from" not in df.columns or "effective_to" not in df.columns:
        logger.warning("Overlap check skipped: date columns missing from DataFrame.")
        return ValidationRule(
            name="no_overlapping_dates",
            status=ValidationStatus.WARN,
            message="Required date columns missing. Skipping overlap check.",
        )

    for k in business_key:
        if k not in df.columns:
            logger.warning("Overlap check skipped: business key column '%s' missing.", k)
            return ValidationRule(
                name="no_overlapping_dates",
                status=ValidationStatus.WARN,
                message=f"Business key column '{k}' missing. Skipping overlap check.",
            )

    rows_inspected = df.height
    logger.info("Inspecting %d rows for overlaps.", rows_inspected)

    if rows_inspected == 0:
        return ValidationRule(
            name="no_overlapping_dates",
            status=ValidationStatus.PASS,
            message="No overlapping validity periods detected.",
        )

    try:
        if sorted_df is not None:
            work_df = sorted_df
        else:
            sort_cols = business_key + ["effective_from", "effective_to"]
            work_df = df.lazy().sort(sort_cols, nulls_last=True).collect(engine=engine)

        is_same = pl.all_horizontal(
            [(pl.col(k) == pl.col(k).shift(1)).fill_null(False) for k in business_key]
        )

        check_df = work_df.with_columns(
            pl.when(is_same)
            .then(pl.col("effective_to").shift(1))
            .otherwise(None)
            .alias("__prev_to"),
            pl.when(is_same)
            .then(pl.col("effective_from").shift(1))
            .otherwise(None)
            .alias("__prev_from"),
        )

        overlap_filter = (
            pl.col("__prev_from").is_not_null()
            & (
                pl.col("__prev_to").is_null()
                | (pl.col("__prev_to") > pl.col("effective_from"))
            )
        )

        overlap_rows = check_df.filter(overlap_filter)
        overlaps: list[str] = []

        if overlap_rows.height > 0:
            for row in overlap_rows.iter_rows(named=True):
                key_str = ", ".join(f"{k}={row[k]}" for k in business_key)
                prev_from = row["__prev_from"]
                prev_to = row["__prev_to"]
                curr_from = row["effective_from"]
                curr_to = row["effective_to"]

                prev_from_str = str(prev_from) if prev_from is not None else "NULL"
                prev_to_str = str(prev_to) if prev_to is not None else "NULL"
                curr_from_str = str(curr_from) if curr_from is not None else "NULL"
                curr_to_str = str(curr_to) if curr_to is not None else "NULL"

                detail = (
                    f"Overlap detected for {key_str}\n"
                    f"Period A:\n"
                    f"{prev_from_str} → {prev_to_str}\n\n"
                    f"Period B:\n"
                    f"{curr_from_str} → {curr_to_str}"
                )
                overlaps.append(detail)

        elapsed = time.perf_counter() - start_time
        logger.info(
            "Overlap check finished. Inspected: %d rows, Overlaps found: %d, Time elapsed: %.4fs",
            rows_inspected, len(overlaps), elapsed
        )

        if overlaps:
            return ValidationRule(
                name="no_overlapping_dates",
                status=ValidationStatus.FAIL,
                message="Overlapping date ranges detected.",
                details=overlaps,
            )

        return ValidationRule(
            name="no_overlapping_dates",
            status=ValidationStatus.PASS,
            message="No overlapping validity periods detected.",
        )

    except Exception as e:
        elapsed = time.perf_counter() - start_time
        tb = traceback.format_exc()
        logger.error(
            "Exception occurred during overlap validation check after %.4fs:\n%s",
            elapsed, tb
        )
        return ValidationRule(
            name="no_overlapping_dates",
            status=ValidationStatus.FAIL,
            message=f"Validation failed due to error: {e}",
            details=[f"Error: {e}", f"Traceback:\n{tb}"],
        )


def _check_date_consistency(df: pl.DataFrame) -> ValidationRule:
    """Validate that effective_from < effective_to for all closed records.

    Under [effective_from, effective_to) half-open semantics:
    - effective_from < effective_to is required when effective_to is non-null.
    - Zero-duration intervals (effective_from == effective_to) are invalid.
    - Reversed intervals (effective_from > effective_to) are invalid.
    - Current records (effective_to is null) are valid open-ended intervals.
    """
    if "effective_from" not in df.columns or "effective_to" not in df.columns:
        return ValidationRule(
            name="date_consistency",
            status=ValidationStatus.WARN,
            message="Date columns missing, cannot validate consistency.",
        )

    cond_bad_dates = (
        pl.col("effective_to").is_not_null()
        & (pl.col("effective_from") >= pl.col("effective_to"))
    )
    has_is_current = "is_current" in df.columns

    if has_is_current:
        cond_bad_active = (
            (pl.col("is_current") == True) & pl.col("effective_to").is_not_null()  # noqa: E712
        )
        cond_bad_closed = (
            (pl.col("is_current") == False) & pl.col("effective_to").is_null()  # noqa: E712
        )
        any_bad_cond = cond_bad_dates | cond_bad_active | cond_bad_closed
    else:
        any_bad_cond = cond_bad_dates

    any_bad_df = df.filter(any_bad_cond)
    if any_bad_df.height == 0:
        return ValidationRule(
            name="date_consistency",
            status=ValidationStatus.PASS,
            message="All date ranges are consistent (effective_from < effective_to).",
        )

    details = []
    bad_rows = any_bad_df.filter(cond_bad_dates)
    for r in bad_rows.iter_rows(named=True):
        ef = r.get("effective_from")
        et = r.get("effective_to")
        details.append(f"Row has invalid range: effective_from ({ef}) >= effective_to ({et})")

    bad_flags_count = 0
    if has_is_current:
        bad_active = any_bad_df.filter(cond_bad_active)
        for r in bad_active.iter_rows(named=True):
            et = r.get("effective_to")
            details.append(f"Active row (is_current=True) must have null effective_to, got {et}")
        bad_flags_count += bad_active.height

        bad_closed = any_bad_df.filter(cond_bad_closed)
        for r in bad_closed.iter_rows(named=True):
            details.append("Closed row (is_current=False) must have non-null effective_to")
        bad_flags_count += bad_closed.height

    if bad_rows.height > 0 and bad_flags_count > 0:
        msg = f"{bad_rows.height} row(s) have invalid date ranges (effective_from >= effective_to) and {bad_flags_count} row(s) have invalid current-row flag pairings."
    elif bad_rows.height > 0:
        msg = f"{bad_rows.height} row(s) have invalid date ranges (effective_from >= effective_to)."
    else:
        msg = f"{bad_flags_count} row(s) have invalid current-row flag pairings."

    return ValidationRule(
        name="date_consistency",
        status=ValidationStatus.FAIL,
        message=msg,
        details=details,
    )




def _format_key(row: dict, business_key: list[str]) -> str:
    """Format a key dict for display."""
    parts = [f"{k}={row[k]}" for k in business_key if k in row]
    return ", ".join(parts)
