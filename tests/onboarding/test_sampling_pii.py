"""Tests for PII masking heuristics, sampling policies, and categorical distributions."""

import pytest
import polars as pl

from scd2_copilot.onboarding.models.profile import SamplingConfig, SamplingPolicy
from scd2_copilot.onboarding.profiler.sampling import (
    compute_top_values,
    extract_samples,
    mask_email,
    mask_name,
    mask_phone,
    to_shape_token,
)


def test_mask_email() -> None:
    assert mask_email("alice.smith@example.com") == "a***@example.com"
    assert mask_email("bob@corporate.org") == "b***@corporate.org"
    assert mask_email("x@domain.com") == "*@domain.com"
    assert mask_email("invalid-email") == "<MASKED_EMAIL>"


def test_mask_phone() -> None:
    assert mask_phone("555-0102") == "***-0102"
    assert mask_phone("+1 (555) 234-5678") == "***-5678"
    assert mask_phone("123") == "***-****"


def test_mask_name() -> None:
    assert mask_name("Alice Smith") == "A*** S***"
    assert mask_name("George Washington Clark") == "G*** W*** C***"
    assert mask_name("Cher") == "C***"


def test_to_shape_token() -> None:
    assert to_shape_token("CUST-1001") == "AAAA-####"
    assert to_shape_token("user@corp.com") == "<EMAIL>"
    assert to_shape_token("555-0199") == "<PHONE>"
    assert to_shape_token("123-45-6789") == "<SSN>"
    assert to_shape_token("Order_99A") == "Aaaaa_##A"


def test_sampling_policies_on_series() -> None:
    series = pl.Series("email", ["alice@example.com", "bob@example.com", "charlie@example.com"])

    # Test MASKED
    masked_config = SamplingConfig(policy=SamplingPolicy.MASKED, max_samples=2)
    masked_samples = extract_samples(series, "email", "email", masked_config)
    assert len(masked_samples) == 2
    assert masked_samples[0] == "a***@example.com"
    assert masked_samples[1] == "b***@example.com"

    # Test SHAPE_ONLY
    shape_config = SamplingConfig(policy=SamplingPolicy.SHAPE_ONLY, max_samples=2)
    shape_samples = extract_samples(series, "email", "email", shape_config)
    assert shape_samples == ["<EMAIL>", "<EMAIL>"]

    # Test RAW
    raw_config = SamplingConfig(policy=SamplingPolicy.RAW, max_samples=2)
    raw_samples = extract_samples(series, "email", "email", raw_config)
    assert raw_samples == ["alice@example.com", "bob@example.com"]


def test_compute_top_values() -> None:
    series = pl.Series("status", ["ACTIVE", "ACTIVE", "ACTIVE", "INACTIVE", "PENDING", None])
    top_vals = compute_top_values(series, top_k=2)

    assert len(top_vals) == 2
    # Non-null total is 5
    assert top_vals[0].value == "ACTIVE"
    assert top_vals[0].count == 3
    assert top_vals[0].percentage == 60.0

    assert top_vals[1].value in ("INACTIVE", "PENDING")
    assert top_vals[1].count == 1
    assert top_vals[1].percentage == 20.0


def test_compute_top_values_skips_high_cardinality() -> None:
    # 30 distinct values with threshold 25 -> returns empty
    series = pl.Series("id", [f"ID_{i}" for i in range(30)])
    assert compute_top_values(series, max_distinct_threshold=25) == []


def test_compute_top_values_masks_pii() -> None:
    emails = pl.Series("email", ["alice@corp.com", "alice@corp.com", "bob@corp.com"])

    # Under MASKED policy, emails in top_values must be masked
    top_masked = compute_top_values(
        emails,
        normalized_name="email",
        policy=SamplingPolicy.MASKED,
    )
    assert len(top_masked) == 2
    assert top_masked[0].value == "a***@corp.com"
    assert top_masked[1].value == "b***@corp.com"

    # Under SHAPE_ONLY policy, converted to token
    top_shape = compute_top_values(
        emails,
        normalized_name="email",
        policy=SamplingPolicy.SHAPE_ONLY,
    )
    assert top_shape[0].value == "<EMAIL>"

    # Under RAW policy, original email is preserved
    top_raw = compute_top_values(
        emails,
        normalized_name="email",
        policy=SamplingPolicy.RAW,
    )
    assert top_raw[0].value == "alice@corp.com"
