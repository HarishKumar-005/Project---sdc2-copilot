"""Schema detection: business key and tracked column inference.

Auto-detects which column(s) are the business key and which are
tracked attributes, based on column names and SCD2 conventions.
"""

from __future__ import annotations

import polars as pl

from .ingestion import SCD2_META_COLUMNS


# Common business key column name patterns (lowercase)
_KEY_PATTERNS = (
    "_id", "_key", "_code", "_no", "_num", "_number",
    "id", "key", "code",
)


def detect_business_key(
    source_df: pl.DataFrame,
    target_df: pl.DataFrame,
) -> list[str]:
    """Infer the business key column(s) from the source and target schemas.

    Heuristic:
    1. Find columns present in both source and target (excluding SCD2 metadata).
    2. Among those, prefer columns whose names end with common key suffixes.
    3. Among candidates, prefer columns with all-unique values in the source.
    4. If no heuristic matches, return the first shared column.

    Returns:
        List of column names forming the business key (usually length 1).

    Raises:
        ValueError: If no shared columns exist.
    """
    source_cols = set(source_df.columns)
    target_data_cols = set(target_df.columns) - SCD2_META_COLUMNS
    shared = sorted(source_cols & target_data_cols)

    if not shared:
        raise ValueError(
            "No shared columns between source and target. "
            "Cannot detect a business key."
        )

    # Score each shared column
    candidates: list[tuple[str, int]] = []
    for col in shared:
        score = 0
        name = col.lower()
        # Suffix match
        if any(name.endswith(pat) for pat in _KEY_PATTERNS):
            score += 30
        # Exact match for common names
        if name in ("id", "key"):
            score += 15
        # Uniqueness in source
        if source_df[col].n_unique() == source_df.height:
            score += 10
        candidates.append((col, score))

    candidates.sort(key=lambda x: -x[1])

    # Return the top candidate (or top candidates if tied at max score)
    if not candidates:
        return [shared[0]]

    top_score = candidates[0][1]
    return [c for c, s in candidates if s == top_score and s > 0] or [candidates[0][0]]


def detect_tracked_columns(
    source_df: pl.DataFrame,
    business_key: list[str],
) -> list[str]:
    """Determine which source columns are tracked attributes.

    Tracked = all source columns minus business key columns.
    (Source should not have SCD2 metadata columns per the data contract.)

    Args:
        source_df: The source DataFrame.
        business_key: The detected business key column(s).

    Returns:
        Sorted list of tracked column names.
    """
    excluded = set(business_key) | SCD2_META_COLUMNS
    tracked = [c for c in source_df.columns if c not in excluded]
    return sorted(tracked)
