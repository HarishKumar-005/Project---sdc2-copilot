"""Onboarding profiling, fingerprinting, and sampling components."""

from .engine import DataProfiler, detect_date_patterns
from .fingerprint import compute_schema_fingerprint, normalize_column_name
from .sampling import (
    compute_top_values,
    extract_samples,
    mask_email,
    mask_name,
    mask_phone,
    to_shape_token,
)

__all__ = [
    "DataProfiler",
    "detect_date_patterns",
    "compute_schema_fingerprint",
    "normalize_column_name",
    "extract_samples",
    "compute_top_values",
    "mask_email",
    "mask_phone",
    "mask_name",
    "to_shape_token",
]
