"""Data profiling models, sampling policies, and statistical metric contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field


class SamplingPolicy(str, Enum):
    """Policy for extracting sample values from external source records."""

    MASKED = "masked"          # Default: Heuristic regex masking of PII (emails, phones, names)
    SHAPE_ONLY = "shape_only"  # Replaces characters with structural tokens (e.g. AAAA-####, <EMAIL>)
    RAW = "raw"                # Unmasked values (strictly for verified non-PII/synthetic test datasets)


class SamplingConfig(BaseModel):
    """Configuration for sample extraction and masking."""

    policy: SamplingPolicy = Field(default=SamplingPolicy.MASKED, description="PII sampling policy")
    max_samples: int = Field(default=5, ge=1, le=20, description="Maximum sample values per column")
    top_k_frequent: int = Field(default=5, ge=1, le=50, description="Top frequent values to record for low-cardinality columns")


class ValueFrequency(BaseModel):
    """Frequency count and percentage of a distinct value."""

    value: str = Field(..., description="Observed value (stringified)")
    count: int = Field(..., description="Frequency count")
    percentage: float = Field(..., description="Percentage of non-null rows (0.0 - 100.0)")


class DatePatternReport(BaseModel):
    """Report of detected date/time format pattern."""

    pattern: str = Field(..., description="Detected date format (e.g. %Y-%m-%d, %d/%m/%Y)")
    sample_count: int = Field(..., description="Number of matching values tested")
    is_ambiguous: bool = Field(..., description="True if pattern is ambiguous (e.g. 01/02/2024)")


class ColumnProfile(BaseModel):
    """Deterministic statistical profile of a single column."""

    column_name: str = Field(..., description="Original column name as found in source")
    normalized_name: str = Field(..., description="Sanitized snake_case column identifier")
    inferred_type: str = Field(..., description="Inferred logical type: string, integer, float, boolean, date, datetime")
    polars_type: str = Field(..., description="Polars data type string")
    total_count: int = Field(..., description="Total row count evaluated")
    null_count: int = Field(..., description="Number of null/missing values")
    null_rate: float = Field(..., description="Ratio of null values (0.0 to 1.0)")
    distinct_count: int = Field(..., description="Number of distinct non-null values")
    uniqueness_rate: float = Field(..., description="Ratio of distinct values to non-null count (0.0 to 1.0)")
    is_unique: bool = Field(..., description="True if distinct_count == (total_count - null_count) > 0")
    min_length: Optional[int] = Field(default=None, description="Minimum string length in bytes/chars")
    max_length: Optional[int] = Field(default=None, description="Maximum string length in bytes/chars")
    samples: list[str] = Field(default_factory=list, description="Extracted sample values under active sampling policy")
    top_values: list[ValueFrequency] = Field(default_factory=list, description="Top-K frequent values for categorical columns")
    date_patterns: list[DatePatternReport] = Field(default_factory=list, description="Detected date patterns if temporal")

    @property
    def null_percentage(self) -> float:
        """Convenience property returning null percentage (0.0 to 100.0)."""
        return self.null_rate * 100.0

    @property
    def uniqueness_ratio(self) -> float:
        """Convenience alias for uniqueness_rate."""
        return self.uniqueness_rate



class DataProfile(BaseModel):
    """Complete deterministic statistical profile of an ingested source."""

    source_id: str = Field(..., description="Source system identifier")
    fingerprint_hash: str = Field(..., description="Hash of the schema snapshot profiled")
    total_rows: int = Field(..., description="Total number of records profiled")
    total_columns: int = Field(..., description="Total number of columns profiled")
    columns: list[ColumnProfile] = Field(default_factory=list, description="Profile for each column")
    profiled_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp of profiling",
    )
    sampling_policy: SamplingPolicy = Field(..., description="Sampling policy used for sample extraction")

    def get_column(self, name: str) -> Optional[ColumnProfile]:
        """Look up column profile by original or normalized name."""
        for col in self.columns:
            if col.column_name == name or col.normalized_name == name:
                return col
        return None
