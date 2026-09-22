"""Source schema snapshot and deterministic fingerprint contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
import polars as pl
from pydantic import BaseModel, Field


def map_polars_type_to_inferred(dtype: pl.DataType) -> str:
    """Map a Polars data type to a high-level logical type."""
    if dtype.is_integer():
        return "integer"
    if dtype.is_float() or dtype.is_decimal():
        return "float"
    if dtype == pl.Boolean:
        return "boolean"
    if dtype == pl.Date:
        return "date"
    if dtype == pl.Datetime:
        return "datetime"
    return "string"


class ColumnSnapshot(BaseModel):
    """Snapshot of a single discovered column in an external source."""

    original_name: str = Field(..., description="Raw column header as observed in source data")
    normalized_name: str = Field(..., description="Sanitized, lowercase snake_case column identifier")
    inferred_type: str = Field(..., description="Inferred logical type: string, integer, float, boolean, date, datetime, unknown")
    polars_type: str = Field(..., description="String representation of Polars data type")
    nullable: bool = Field(..., description="Whether column contains null values in the observed snapshot")
    ordinal_position: int = Field(..., description="0-indexed position in source schema")


class SchemaFingerprint(BaseModel):
    """Immutable deterministic fingerprint of a schema definition."""

    fingerprint_hash: str = Field(..., description="SHA-256 hexadecimal hash of the canonical schema definition")
    column_count: int = Field(..., description="Total number of columns")
    normalized_signature: str = Field(..., description="Canonical string signature used to compute the hash")


class SourceSchemaSnapshot(BaseModel):
    """Complete discovered schema snapshot for a source at a point in time."""

    source_id: str = Field(..., description="Source system identifier")
    schema_version: int = Field(default=1, description="Sequential schema version for this source")
    fingerprint: SchemaFingerprint = Field(..., description="Cryptographic schema fingerprint")
    columns: list[ColumnSnapshot] = Field(default_factory=list, description="Discovered column snapshots")
    captured_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp when schema was captured",
    )

    @property
    def schema_fingerprint(self) -> str:
        """Alias property returning the fingerprint hash string."""
        return self.fingerprint.fingerprint_hash

    def get_column(self, name: str) -> Optional[ColumnSnapshot]:
        """Find column by either original_name or normalized_name."""
        for col in self.columns:
            if col.original_name == name or col.normalized_name == name:
                return col
        return None

