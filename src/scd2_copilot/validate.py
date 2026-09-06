"""Lightweight SCD2 output validation.

Runs deterministic validation rules on the SCD2 output DataFrame.
No Great Expectations or external database (e.g. DuckDB) dependency for validation.
"""

from __future__ import annotations

from datetime import date
import logging
import time
import traceback

import polars as pl

from .models import ValidationReport, ValidationRule, ValidationStatus


def validate_scd2(
    df: pl.DataFrame,
    business_key: list[str],
) -> ValidationReport:
    """Run all validation rules on the SCD2 output.

    Args:
        df: The updated SCD2 DataFrame to validate.
        business_key: Business key column(s).

    Returns:
        A ValidationReport with results for each rule.
    """
    report = ValidationReport()

    report.rules.append(_check_schema_completeness(df))
    report.rules.append(_check_one_current_per_key(df, business_key))
    report.rules.append(_check_no_null_keys(df, business_key))
    report.rules.append(_check_no_overlapping_dates(df, business_key))
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
    df: pl.DataFrame, business_key: list[str]
) -> ValidationRule:
    """No business key should have more than one is_current=true row."""
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
    df: pl.DataFrame, business_key: list[str]
) -> ValidationRule:
    """For each business key, date ranges [effective_from, effective_to] must not overlap.

    Sorts by business key and effective_from, then verifies that
    previous_effective_to <= next_effective_from for consecutive records.
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

    try:
        # Sort DataFrame to process records sequentially by key and date
        sorted_df = df.sort(business_key + ["effective_from"])

        # Group records by business key values
        groups: dict[tuple, list[dict]] = {}
        for row in sorted_df.iter_rows(named=True):
            k_val = tuple(row[k] for k in business_key)
            groups.setdefault(k_val, []).append(row)

        overlaps = []

        for key_val, group_rows in groups.items():
            for i in range(len(group_rows) - 1):
                row_i = group_rows[i]
                row_next = group_rows[i + 1]

                from_i = row_i["effective_from"]
                to_i = row_i["effective_to"]
                from_next = row_next["effective_from"]
                to_next = row_next["effective_to"]

                # Check overlap: previous_effective_to is open (None) OR previous_effective_to > next_effective_from
                if to_i is None or to_i > from_next:
                    key_str = ", ".join(f"{k}={row_i[k]}" for k in business_key)

                    from_i_str = str(from_i) if from_i is not None else "NULL"
                    to_i_str = str(to_i) if to_i is not None else "NULL"
                    from_next_str = str(from_next) if from_next is not None else "NULL"
                    to_next_str = str(to_next) if to_next is not None else "NULL"

                    detail = (
                        f"Overlap detected for {key_str}\n"
                        f"Period A:\n"
                        f"{from_i_str} → {to_i_str}\n\n"
                        f"Period B:\n"
                        f"{from_next_str} → {to_next_str}"
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
    """effective_from <= effective_to where effective_to is not null."""
    if "effective_from" not in df.columns or "effective_to" not in df.columns:
        return ValidationRule(
            name="date_consistency",
            status=ValidationStatus.WARN,
            message="Date columns missing, cannot validate consistency.",
        )

    bad_rows = df.filter(
        pl.col("effective_to").is_not_null()
        & (pl.col("effective_from") > pl.col("effective_to"))
    )

    if bad_rows.height > 0:
        return ValidationRule(
            name="date_consistency",
            status=ValidationStatus.FAIL,
            message=f"{bad_rows.height} row(s) have effective_from > effective_to.",
        )

    return ValidationRule(
        name="date_consistency",
        status=ValidationStatus.PASS,
        message="All date ranges are consistent (effective_from <= effective_to).",
    )


def _format_key(row: dict, business_key: list[str]) -> str:
    """Format a key dict for display."""
    parts = [f"{k}={row[k]}" for k in business_key if k in row]
    return ", ".join(parts)
