"""CSV ingestion and schema normalization.

Loads source and target CSVs into Polars DataFrames with consistent
column naming and type handling.
"""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO, Union

import polars as pl

from .exceptions import InvalidTemporalValueError

# SCD2 metadata columns (auto-detected and excluded from tracked columns)
SCD2_META_COLUMNS = {"effective_from", "effective_to", "is_current"}

# Supported date and datetime input formats for SCD2 temporal metadata columns
SUPPORTED_DATE_FORMATS: list[str] = [
    "%Y-%m-%d",
]

SUPPORTED_DATETIME_FORMATS: list[str] = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S%.f",
    "%Y-%m-%dT%H:%M:%S%.f",
    "%Y-%m-%d %H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d %H:%M:%S%.fZ",
    "%Y-%m-%dT%H:%M:%S%.fZ",
    "%Y-%m-%d %H:%M:%S%:z",
    "%Y-%m-%dT%H:%M:%S%:z",
    "%Y-%m-%d %H:%M:%S%.f%:z",
    "%Y-%m-%dT%H:%M:%S%.f%:z",
]


def load_csv(
    source: Union[str, Path, BinaryIO], *, dataset_name: str | None = None
) -> pl.DataFrame:
    """Load a CSV file into a Polars DataFrame with normalization.

    Normalization steps:
    1. Strip whitespace from column names
    2. Lowercase all column names
    3. Strip whitespace from string values
    4. Convert 'effective_from' / 'effective_to' to Date type deterministically
    5. Convert 'is_current' to boolean if present

    Supported temporal inputs:
    - ISO 8601 date: YYYY-MM-DD
    - ISO 8601 datetime: YYYY-MM-DD HH:MM:SS or YYYY-MM-DDTHH:MM:SS
      (with optional fractional seconds and timezone indicators)
    - Already inferred pl.Date or pl.Datetime

    Normalized internal representation:
    All temporal SCD2 metadata is normalized to pl.Date. For timezone-aware
    timestamps, the calendar date of the timestamp is preserved.
    Temporal interval convention and boundary semantics are formalized in M1.3.

    Args:
        source: File path, Path object, or file-like object (e.g., UploadedFile).
        dataset_name: Optional dataset name (e.g., 'source' or 'target') for error context.

    Returns:
        Normalized Polars DataFrame.

    Raises:
        ValueError: If the CSV is empty or has no columns.
        InvalidTemporalValueError: If unparseable or invalid temporal values are encountered.
    """
    if isinstance(source, (str, Path)):
        df = pl.read_csv(source, infer_schema_length=1000, try_parse_dates=True)
    else:
        # File-like object (e.g., Streamlit UploadedFile)
        data = source.read()
        df = pl.read_csv(data, infer_schema_length=1000, try_parse_dates=True)

    if df.is_empty() and df.width == 0:
        raise ValueError("CSV file is empty or has no columns.")

    # Normalize column names: strip + lowercase
    df = df.rename({col: col.strip().lower() for col in df.columns})

    # Strip whitespace from string columns
    str_cols = [c for c in df.columns if df[c].dtype == pl.Utf8]
    if str_cols:
        df = df.with_columns(
            [pl.col(c).str.strip_chars() for c in str_cols]
        )

    # Normalize SCD2 metadata columns if present
    df = _normalize_scd2_columns(df, dataset_name=dataset_name)

    return df


def _normalize_date_column(
    df: pl.DataFrame, col: str, *, dataset_name: str | None = None
) -> pl.DataFrame:
    """Normalize a temporal column to pl.Date without loss or silent null coercion.

    Strategy:
    - pl.Date: preserved unchanged.
    - pl.Null: cast to pl.Date preserving nulls.
    - pl.Datetime: converted directly via .dt.date() without text conversion.
    - Strings / other: parsed across supported ISO date and datetime formats.
      Empty strings or whitespace-only strings become null.
      Any unparseable non-null values raise InvalidTemporalValueError.

    Args:
        df: Polars DataFrame containing the column.
        col: Column name to normalize.
        dataset_name: Optional dataset context ('source' or 'target').

    Returns:
        DataFrame with the column normalized to pl.Date.

    Raises:
        InvalidTemporalValueError: If unparseable or invalid temporal values are found.
    """
    if col not in df.columns:
        return df

    dtype = df[col].dtype

    # 1. Already pl.Date: preserve directly
    if dtype == pl.Date:
        return df

    # 2. pl.Null: cast to pl.Date
    if dtype == pl.Null:
        return df.with_columns(pl.col(col).cast(pl.Date))

    # 3. Already pl.Datetime: convert directly via .dt.date() without text conversion
    if isinstance(dtype, pl.Datetime):
        return df.with_columns(pl.col(col).dt.date())

    # 4. String or other types: parse deterministically across supported ISO formats
    try:
        cleaned = (
            pl.when(pl.col(col).cast(pl.Utf8).str.strip_chars() == "")
            .then(None)
            .otherwise(pl.col(col).cast(pl.Utf8).str.strip_chars())
        )

        parse_exprs = [
            cleaned.str.to_date(fmt, strict=False) for fmt in SUPPORTED_DATE_FORMATS
        ]
        parse_exprs.extend(
            cleaned.str.to_datetime(fmt, strict=False).dt.date()
            for fmt in SUPPORTED_DATETIME_FORMATS
        )

        parsed = pl.coalesce(parse_exprs)
        invalid_mask = cleaned.is_not_null() & parsed.is_null()

        invalid_df = df.filter(invalid_mask)
        if invalid_df.height > 0:
            invalid_samples = invalid_df[col].head(5).to_list()
            sample_strs = [str(s) for s in invalid_samples]
            ctx = f" in {dataset_name}" if dataset_name else ""
            raise InvalidTemporalValueError(
                f"Invalid temporal value(s) in column '{col}'{ctx}: {sample_strs}",
                column=col,
                invalid_samples=sample_strs,
                dataset_name=dataset_name,
            )

        return df.with_columns(parsed.alias(col))
    except InvalidTemporalValueError:
        raise
    except Exception as exc:
        ctx = f" in {dataset_name}" if dataset_name else ""
        raise InvalidTemporalValueError(
            f"Failed to normalize temporal column '{col}'{ctx}: {exc}",
            column=col,
            dataset_name=dataset_name,
        ) from exc


def _normalize_scd2_columns(
    df: pl.DataFrame, *, dataset_name: str | None = None
) -> pl.DataFrame:
    """Coerce SCD2 metadata columns to the correct types."""

    # effective_from: ensure Date without data loss or silent null conversion
    if "effective_from" in df.columns:
        df = _normalize_date_column(df, "effective_from", dataset_name=dataset_name)

    # effective_to: ensure Date (nulls stay null) without silent null conversion
    if "effective_to" in df.columns:
        df = _normalize_date_column(df, "effective_to", dataset_name=dataset_name)

    # is_current: ensure Boolean
    if "is_current" in df.columns and df["is_current"].dtype != pl.Boolean:
        df = df.with_columns(
            pl.col("is_current")
            .cast(pl.Utf8)
            .str.to_lowercase()
            .map_elements(lambda v: v in ("true", "1", "yes") if v else None, return_dtype=pl.Boolean)
        )

    return df


def validate_csv_columns(source_df: pl.DataFrame, target_df: pl.DataFrame) -> list[str]:
    """Check that source and target share compatible columns.

    Returns a list of error messages (empty = valid).
    """
    errors: list[str] = []

    if source_df.width == 0:
        errors.append("Source CSV has no columns.")
    if target_df.width == 0:
        errors.append("Target CSV has no columns.")

    # Source should NOT have SCD2 metadata columns
    source_scd2 = SCD2_META_COLUMNS & set(source_df.columns)
    if source_scd2:
        errors.append(
            f"Source CSV should not contain SCD2 metadata columns: {sorted(source_scd2)}"
        )

    # Target MUST have SCD2 metadata columns
    missing_meta = SCD2_META_COLUMNS - set(target_df.columns)
    if missing_meta:
        errors.append(
            f"Target CSV is missing required SCD2 columns: {sorted(missing_meta)}"
        )

    # Source columns must be a subset of target's non-meta columns
    target_data_cols = set(target_df.columns) - SCD2_META_COLUMNS
    source_cols = set(source_df.columns)
    missing_in_target = source_cols - target_data_cols
    if missing_in_target and not missing_meta:
        # Only warn if target has the meta columns (i.e., it's a real SCD2 table)
        errors.append(
            f"Source columns not found in target: {sorted(missing_in_target)}"
        )

    return errors
