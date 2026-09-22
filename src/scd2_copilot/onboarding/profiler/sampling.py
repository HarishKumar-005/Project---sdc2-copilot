"""Deterministic sampling and PII masking engine for external source profiling."""

from __future__ import annotations

import re
from typing import Optional, Sequence
import polars as pl

from ..models.profile import SamplingConfig, SamplingPolicy, ValueFrequency

# Common PII regex patterns
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")
PHONE_REGEX = re.compile(r"^(\+?\d{1,3}[-.\s]?)?(\(?\d{3}\)?[-.\s]?)?\d{3}[-.\s]?\d{4}$")
SSN_REGEX = re.compile(r"^\d{3}-\d{2}-\d{4}$")
SENSITIVE_NAME_COLUMNS = {"name", "first_name", "last_name", "full_name", "client_name", "client_full_name", "contact_name"}


def mask_email(val: str) -> str:
    """Mask email preserving first letter and domain (e.g. 'alice@corp.com' -> 'a***@corp.com')."""
    if "@" not in val:
        return "<MASKED_EMAIL>"
    parts = val.split("@", 1)
    user, domain = parts[0], parts[1]
    if len(user) <= 1:
        masked_user = "*"
    else:
        masked_user = f"{user[0]}***"
    return f"{masked_user}@{domain}"


def mask_phone(val: str) -> str:
    """Mask phone preserving only the last 4 digits (e.g. '555-0102' -> '***-0102')."""
    digits = re.sub(r"\D", "", val)
    if len(digits) >= 4:
        last4 = digits[-4:]
        return f"***-{last4}"
    return "***-****"


def mask_name(val: str) -> str:
    """Mask personal name leaving first initials (e.g. 'Alice Smith' -> 'A*** S***')."""
    words = val.split()
    masked_words = []
    for w in words:
        if len(w) > 0:
            masked_words.append(f"{w[0].upper()}***")
    return " ".join(masked_words) if masked_words else "***"


def to_shape_token(val: str) -> str:
    """Convert value to structural shape token (e.g. 'CUST-1001' -> 'AAAA-####')."""
    if EMAIL_REGEX.match(val):
        return "<EMAIL>"
    if PHONE_REGEX.match(val):
        return "<PHONE>"
    if SSN_REGEX.match(val):
        return "<SSN>"
    
    # Structural replacement: uppercase letters -> A, lowercase -> a, digits -> #
    chars = []
    for ch in val:
        if ch.isupper():
            chars.append("A")
        elif ch.islower():
            chars.append("a")
        elif ch.isdigit():
            chars.append("#")
        else:
            chars.append(ch)
    return "".join(chars)


def extract_samples(
    series: pl.Series,
    column_name: str,
    normalized_name: str,
    config: SamplingConfig,
) -> list[str]:
    """Extract up to max_samples deterministic, unique values according to the active SamplingPolicy."""
    non_null = series.drop_nulls()
    if len(non_null) == 0:
        return []

    # Get distinct non-null values up to max_samples (preserve deterministic appearance order)
    unique_vals = non_null.unique(maintain_order=True).head(config.max_samples).to_list()
    samples: list[str] = []

    is_name_col = normalized_name in SENSITIVE_NAME_COLUMNS

    for raw in unique_vals:
        val_str = str(raw).strip()
        if config.policy == SamplingPolicy.RAW:
            samples.append(val_str)
        elif config.policy == SamplingPolicy.SHAPE_ONLY:
            samples.append(to_shape_token(val_str))
        elif config.policy == SamplingPolicy.MASKED:
            if EMAIL_REGEX.match(val_str) or "email" in normalized_name:
                samples.append(mask_email(val_str))
            elif PHONE_REGEX.match(val_str) or "phone" in normalized_name:
                samples.append(mask_phone(val_str))
            elif SSN_REGEX.match(val_str) or "ssn" in normalized_name:
                samples.append("***-**-1234")
            elif is_name_col and not val_str.isdigit():
                samples.append(mask_name(val_str))
            else:
                # Truncate long generic values to prevent prompt bloating
                samples.append(val_str[:50] + ("..." if len(val_str) > 50 else ""))

    return samples


def compute_top_values(
    series: pl.Series,
    top_k: int = 5,
    max_distinct_threshold: int = 25,
    normalized_name: str = "",
    policy: SamplingPolicy = SamplingPolicy.MASKED,
) -> list[ValueFrequency]:
    """Calculate frequency distribution for categorical columns with distinct_count <= max_distinct_threshold.

    Applies the active SamplingPolicy to ensure sensitive identifiers and PII are not leaked
    into top-K frequency values.
    """
    non_null = series.drop_nulls()
    total_non_null = len(non_null)
    if total_non_null == 0:
        return []

    distinct_count = series.n_unique()
    if distinct_count > max_distinct_threshold:
        return []

    is_name_col = normalized_name in SENSITIVE_NAME_COLUMNS
    counts_df = non_null.value_counts(sort=True)
    results: list[ValueFrequency] = []

    for row in counts_df.head(top_k).iter_rows(named=True):
        raw_str = str(row[series.name]).strip()
        cnt = int(row["count"])
        pct = round((cnt / total_non_null) * 100.0, 2)

        if policy == SamplingPolicy.RAW:
            val_str = raw_str
        elif policy == SamplingPolicy.SHAPE_ONLY:
            val_str = to_shape_token(raw_str)
        elif policy == SamplingPolicy.MASKED:
            if EMAIL_REGEX.match(raw_str) or "email" in normalized_name:
                val_str = mask_email(raw_str)
            elif PHONE_REGEX.match(raw_str) or "phone" in normalized_name:
                val_str = mask_phone(raw_str)
            elif SSN_REGEX.match(raw_str) or "ssn" in normalized_name:
                val_str = "***-**-1234"
            elif is_name_col and not raw_str.isdigit():
                val_str = mask_name(raw_str)
            else:
                val_str = raw_str[:50]
        else:
            val_str = raw_str

        results.append(ValueFrequency(value=val_str, count=cnt, percentage=pct))

    return results
