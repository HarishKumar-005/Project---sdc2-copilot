"""Data contracts for deterministic transformation, canonical customer records, and validation results."""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
import json
from pathlib import Path
from typing import Any, Optional
import polars as pl
from pydantic import BaseModel, ConfigDict, Field


class ValidationCategory(str, Enum):
    """Categories of deterministic data quality and contract validation."""

    REQUIRED = "REQUIRED"
    TYPE = "TYPE"
    FORMAT = "FORMAT"
    ENUM = "ENUM"
    UNIQUE = "UNIQUE"
    DUPLICATE = "DUPLICATE"
    TRANSFORMATION = "TRANSFORMATION"
    BUSINESS_RULE = "BUSINESS_RULE"
    REFERENTIAL_INTEGRITY = "REFERENTIAL_INTEGRITY"


class ValidationErrorSeverity(str, Enum):
    """Severity of a validation finding."""

    BLOCKING = "BLOCKING"  # Default: prevents record from entering downstream canonical store
    WARNING = "WARNING"    # Informational finding, does not block execution unless configured


class RecordValidationError(BaseModel):
    """Structured, machine-readable validation finding on a single record."""

    model_config = ConfigDict(frozen=True)

    row_index: int = Field(..., description="0-indexed position of the record in the incoming source dataset")
    record_id: Optional[str] = Field(default=None, description="Stable source record identifier or customer_id if available")
    rule_id: str = Field(..., description="Stable deterministic rule identifier (e.g. CUSTOMER_ID_REQUIRED, EMAIL_FORMAT)")
    category: ValidationCategory = Field(..., description="Validation rule category")
    field: str = Field(..., description="Canonical field (or source field) associated with the finding")
    observed_value: Optional[str] = Field(
        default=None,
        description="Safe/masked string representation of observed value that triggered the finding",
    )
    reason: str = Field(..., description="Deterministic explanation of why the finding was raised")
    severity: ValidationErrorSeverity = Field(
        default=ValidationErrorSeverity.BLOCKING,
        description="Severity classification of this finding",
    )

    @property
    def message(self) -> str:
        """Convenience alias for reason."""
        return self.reason


class CanonicalCustomerRecord(BaseModel):
    """Authoritative canonical customer entity record conforming to customer.v1."""

    model_config = ConfigDict(frozen=True)

    customer_id: str = Field(..., description="Stable canonical customer identifier")
    first_name: str = Field(..., description="Customer given name")
    last_name: str = Field(..., description="Customer family/surname")
    email: str = Field(..., description="Canonical validated email address")
    date_of_birth: Optional[date] = Field(default=None, description="Customer date of birth")
    status: str = Field(..., description="Canonical customer status enum: ACTIVE or INACTIVE")
    created_at: datetime = Field(..., description="UTC creation timestamp")


class TransformedRecord(BaseModel):
    """Record-level outcome from transformation and validation, preserving lineage and raw state."""

    model_config = ConfigDict(frozen=True)

    row_index: int = Field(..., description="0-indexed position in the source batch")
    source_record_id: Optional[str] = Field(default=None, description="Identifier extracted from source record")
    raw_values: dict[str, Any] = Field(default_factory=dict, description="Raw source values for audit and correction")
    canonical_values: dict[str, Any] = Field(default_factory=dict, description="Transformed canonical field values")
    is_valid: bool = Field(..., description="True if record satisfies all blocking canonical validation rules")
    errors: list[RecordValidationError] = Field(default_factory=list, description="List of validation errors on this record")

    @property
    def record_id(self) -> Optional[str]:
        """Convenience alias for source_record_id."""
        return self.source_record_id

    @property
    def validation_errors(self) -> list[RecordValidationError]:
        """Convenience alias for errors."""
        return self.errors


class TransformationResult(BaseModel):
    """Complete structured result of executing an approved mapping version against source data."""

    model_config = ConfigDict(frozen=True)

    source_id: str = Field(..., description="Source system identifier")
    mapping_version_id: str = Field(..., description="Approved mapping version identifier used for execution")
    canonical_schema_name: str = Field(default="customer", description="Canonical entity name")
    canonical_schema_version: int = Field(default=1, description="Canonical schema version")
    total_records: int = Field(..., ge=0, description="Total number of source records processed")
    valid_record_count: int = Field(..., ge=0, description="Count of valid records")
    invalid_record_count: int = Field(..., ge=0, description="Count of invalid records")
    valid_records: list[CanonicalCustomerRecord] = Field(
        default_factory=list,
        description="Records that passed all deterministic validation checks",
    )
    invalid_records: list[TransformedRecord] = Field(
        default_factory=list,
        description="Records that failed one or more blocking validation checks",
    )
    all_errors: list[RecordValidationError] = Field(
        default_factory=list,
        description="All validation findings across all records",
    )
    executed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp when transformation was executed",
    )

    @property
    def valid_count(self) -> int:
        """Convenience alias for valid_record_count."""
        return self.valid_record_count

    @property
    def invalid_count(self) -> int:
        """Convenience alias for invalid_record_count."""
        return self.invalid_record_count

    def to_valid_polars_df(self) -> pl.DataFrame:
        """Produce a typed Polars DataFrame of validated canonical records conforming to customer.v1."""
        if not self.valid_records:
            return pl.DataFrame(
                schema={
                    "customer_id": pl.String,
                    "first_name": pl.String,
                    "last_name": pl.String,
                    "email": pl.String,
                    "date_of_birth": pl.Date,
                    "status": pl.String,
                    "created_at": pl.Datetime(time_zone="UTC"),
                }
            )

        rows = [r.model_dump() for r in self.valid_records]
        df = pl.DataFrame(rows)
        return df.with_columns(
            [
                pl.col("customer_id").cast(pl.String),
                pl.col("first_name").cast(pl.String),
                pl.col("last_name").cast(pl.String),
                pl.col("email").cast(pl.String),
                pl.col("date_of_birth").cast(pl.Date),
                pl.col("status").cast(pl.String),
                pl.col("created_at").cast(pl.Datetime(time_zone="UTC")),
            ]
        )

    def save_result_artifact(self, path: Path | str) -> Path:
        """Persist transformation result as a deterministic JSON artifact."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        content = self.model_dump_json(indent=2)
        p.write_text(content, encoding="utf-8")
        return p

    def save_artifact(self, path: Path | str) -> Path:
        """Alias for save_result_artifact for uniform artifact saving API."""
        return self.save_result_artifact(path)

    @classmethod
    def load_result_artifact(cls, path: Path | str) -> TransformationResult:
        """Load and validate a transformation result artifact from JSON."""
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Transformation result artifact not found at: {p}")
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls.model_validate(data)
