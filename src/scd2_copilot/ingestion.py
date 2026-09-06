"""CSV ingestion and schema normalization.

Loads source and target CSVs into Polars DataFrames with consistent
column naming and type handling.
"""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO, Union

import polars as pl

# SCD2 metadata columns (auto-detected and excluded from tracked columns)
SCD2_META_COLUMNS = {"effective_from", "effective_to", "is_current"}


def load_csv(source: Union[str, Path, BinaryIO]) -> pl.DataFrame:
    """Load a CSV file into a Polars DataFrame with normalization.

    Normalization steps:
    1. Strip whitespace from column names
    2. Lowercase all column names
    3. Strip whitespace from string values
    4. Convert 'effective_from' / 'effective_to' to Date type if present
    5. Convert 'is_current' to boolean if present

    Args:
        source: File path, Path object, or file-like object (e.g., UploadedFile).

    Returns:
        Normalized Polars DataFrame.

    Raises:
        ValueError: If the CSV is empty or has no columns.
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
    df = _normalize_scd2_columns(df)

    return df


def _normalize_scd2_columns(df: pl.DataFrame) -> pl.DataFrame:
    """Coerce SCD2 metadata columns to the correct types."""

    # effective_from: ensure Date
    if "effective_from" in df.columns and df["effective_from"].dtype != pl.Date:
        df = df.with_columns(
            pl.col("effective_from").cast(pl.Utf8).str.to_date(format="%Y-%m-%d", strict=False)
        )

    # effective_to: ensure Date (nulls stay null)
    if "effective_to" in df.columns and df["effective_to"].dtype != pl.Date:
        df = df.with_columns(
            pl.col("effective_to").cast(pl.Utf8).str.to_date(format="%Y-%m-%d", strict=False)
        )

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
