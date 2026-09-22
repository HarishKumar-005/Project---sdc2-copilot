"""Deterministic schema fingerprinting and column name normalization."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Sequence

from ..models.schema_snapshot import ColumnSnapshot, SchemaFingerprint


def normalize_column_name(name: str) -> str:
    """Convert an arbitrary external column name into a clean, lowercased snake_case slug.

    Examples:
        'Cust_ID' -> 'cust_id'
        'First Name' -> 'first_name'
        'Phone-Number#' -> 'phone_number'
        '  Email  ' -> 'email'
    """
    cleaned = name.strip()
    # Replace non-alphanumeric characters with underscores
    cleaned = re.sub(r"[^\w\s]", "_", cleaned)
    # Replace whitespace and runs of underscores with a single underscore
    cleaned = re.sub(r"[\s_]+", "_", cleaned)
    # Remove leading/trailing underscores and lowercase
    slug = cleaned.strip("_").lower()
    return slug or "col"


def compute_schema_fingerprint(columns: Sequence[ColumnSnapshot]) -> SchemaFingerprint:
    """Compute an immutable, reproducible SHA-256 hash for a sequence of column snapshots.

    The columns are ordered by their ordinal_position. The canonical signature string is
    constructed as a strictly formatted JSON array containing canonical column specifications.
    """
    sorted_cols = sorted(columns, key=lambda c: c.ordinal_position)
    canonical_list = [
        {
            "original_name": c.original_name,
            "normalized_name": c.normalized_name,
            "inferred_type": c.inferred_type,
            "polars_type": c.polars_type,
            "nullable": c.nullable,
            "ordinal_position": c.ordinal_position,
        }
        for c in sorted_cols
    ]

    canonical_signature = json.dumps(canonical_list, sort_keys=True, separators=(",", ":"))
    hasher = hashlib.sha256(canonical_signature.encode("utf-8"))
    fingerprint_hash = hasher.hexdigest()

    return SchemaFingerprint(
        fingerprint_hash=fingerprint_hash,
        column_count=len(columns),
        normalized_signature=canonical_signature,
    )
