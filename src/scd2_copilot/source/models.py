"""Pydantic data models and schemas for configurable PostgreSQL sources."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SQL_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")
RESERVED_SCD2_COLUMNS = {"effective_from", "effective_to", "is_current"}


def _validate_sql_identifier(val: str, field_name: str) -> str:
    """Validate that a string conforms to a safe, valid PostgreSQL unquoted identifier."""
    if not isinstance(val, str):
        raise ValueError(f"{field_name} must be a string, got {type(val).__name__}")
    s = val.strip()
    if not s:
        raise ValueError(f"{field_name} cannot be empty or whitespace")
    if not SQL_IDENTIFIER_PATTERN.match(s):
        raise ValueError(
            f"{field_name} '{s}' is not a valid SQL identifier (must start with letter/underscore, "
            f"contain only letters, digits, underscores, and be <= 63 characters)"
        )
    return s


class PostgresSourceDefinition(BaseModel):
    """PostgreSQL source database location."""

    model_config = ConfigDict(populate_by_name=True)

    type: Literal["postgresql"] = "postgresql"
    schema_name: str = Field(default="public", alias="schema")
    table_name: str = Field(..., alias="table")

    @field_validator("schema_name")
    @classmethod
    def _validate_schema(cls, v: str) -> str:
        return _validate_sql_identifier(v, "schema")

    @field_validator("table_name")
    @classmethod
    def _validate_table(cls, v: str) -> str:
        return _validate_sql_identifier(v, "table")


class ChangeTimestampDefinition(BaseModel):
    """Configuration defining the change-timestamp watermark column."""

    model_config = ConfigDict(populate_by_name=True)

    column: str

    @field_validator("column")
    @classmethod
    def _validate_column(cls, v: str) -> str:
        return _validate_sql_identifier(v, "change_timestamp.column")


class MonitorConfig(BaseModel):
    """Canonical monitor configuration describing a PostgreSQL table and its SCD2 mapping."""

    model_config = ConfigDict(populate_by_name=True)

    name: str
    source: PostgresSourceDefinition
    business_keys: list[str] = Field(..., alias="keys")
    change_timestamp: ChangeTimestampDefinition
    tracked_columns: list[str]

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("Monitor name cannot be empty")
        return s

    @field_validator("change_timestamp", mode="before")
    @classmethod
    def _normalize_change_timestamp(cls, v: Any) -> Any:
        if isinstance(v, str):
            return ChangeTimestampDefinition(column=v)
        return v

    @field_validator("business_keys", mode="before")
    @classmethod
    def _validate_business_keys(cls, v: Any) -> list[str]:
        if not isinstance(v, (list, tuple)):
            raise ValueError("business_keys must be a list of column names")
        cleaned = [_validate_sql_identifier(str(k), "business_key") for k in v]
        if not cleaned:
            raise ValueError("At least one business_key must be configured")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError(f"Duplicate business keys detected: {cleaned}")
        return cleaned

    @field_validator("tracked_columns", mode="before")
    @classmethod
    def _validate_tracked_columns(cls, v: Any) -> list[str]:
        if not isinstance(v, (list, tuple)):
            raise ValueError("tracked_columns must be a list of column names")
        cleaned = [_validate_sql_identifier(str(c), "tracked_column") for c in v]
        if not cleaned:
            raise ValueError("At least one tracked_column must be configured")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError(f"Duplicate tracked columns detected: {cleaned}")
        return cleaned

    @model_validator(mode="after")
    def _validate_column_disjointness_and_semantics(self) -> MonitorConfig:
        key_set = set(self.business_keys)
        tracked_set = set(self.tracked_columns)
        ts_col = self.change_timestamp.column

        # Disjoint keys and tracked columns
        overlap = key_set & tracked_set
        if overlap:
            raise ValueError(f"Columns cannot be both business keys and tracked columns: {sorted(overlap)}")

        # Timestamp cannot be a key or tracked column
        if ts_col in key_set:
            raise ValueError(f"change_timestamp column '{ts_col}' cannot be listed in business_keys")
        if ts_col in tracked_set:
            raise ValueError(f"change_timestamp column '{ts_col}' cannot be listed in tracked_columns")

        # Reserved SCD2 system names
        all_cols = key_set | tracked_set | {ts_col}
        reserved_overlap = all_cols & RESERVED_SCD2_COLUMNS
        if reserved_overlap:
            raise ValueError(
                f"Configured columns conflict with reserved SCD2 system columns: {sorted(reserved_overlap)}"
            )

        return self


class SourceColumnMetadata(BaseModel):
    """Metadata describing a single column discovered in the PostgreSQL source table."""

    column_name: str
    data_type: str
    is_nullable: bool
    ordinal_position: int


class SourceTableMetadata(BaseModel):
    """Metadata describing a discovered PostgreSQL table schema."""

    schema_name: str
    table_name: str
    columns: dict[str, SourceColumnMetadata] = Field(default_factory=dict)
    primary_keys: list[str] = Field(default_factory=list)

    @property
    def column_names(self) -> list[str]:
        return list(self.columns.keys())


class SourceValidationResult(BaseModel):
    """Validation report for a MonitorConfig against live PostgreSQL database."""

    is_valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metadata: Optional[SourceTableMetadata] = None


@dataclass(frozen=True)
class NormalizedSourceRecord:
    """Represents a normalized dictionary-backed row read from a configured PostgreSQL source."""

    data: dict[str, Any]
    business_key_values: dict[str, Any]
    timestamp_value: Optional[datetime] = None

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]


class SourceCursor(BaseModel):
    """Canonical deterministic composite source cursor for incremental ingestion.

    Identifies an exact source position via (change_timestamp, business_key_values).
    Ordering is strictly lexicographic:
      1. change_timestamp ASC
      2. business_key_1 ASC, business_key_2 ASC, ...
    """

    model_config = ConfigDict(populate_by_name=True)

    timestamp: datetime
    keys: dict[str, Any] = Field(default_factory=dict)
    key_columns: list[str] = Field(default_factory=list)

    @field_validator("timestamp")
    @classmethod
    def _normalize_ts(cls, v: datetime) -> datetime:
        from datetime import timezone

        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)

    def key_tuple(self, key_columns: Optional[list[str]] = None) -> tuple[Any, ...]:
        """Return the tuple of business key values ordered by key_columns or self.key_columns."""
        cols = key_columns or self.key_columns or sorted(self.keys.keys())
        return tuple(self.keys.get(col) for col in cols)

    def sort_key(self, key_columns: Optional[list[str]] = None) -> tuple[datetime, tuple[Any, ...]]:
        """Return deterministic comparison tuple: (timestamp, key_values_tuple)."""
        return (self.timestamp, self.key_tuple(key_columns))

    def __lt__(self, other: Any) -> bool:
        if not isinstance(other, SourceCursor):
            return NotImplemented
        cols = self.key_columns or other.key_columns or sorted(set(self.keys) | set(other.keys))
        return self.sort_key(cols) < other.sort_key(cols)

    def __le__(self, other: Any) -> bool:
        if not isinstance(other, SourceCursor):
            return NotImplemented
        cols = self.key_columns or other.key_columns or sorted(set(self.keys) | set(other.keys))
        return self.sort_key(cols) <= other.sort_key(cols)

    def __gt__(self, other: Any) -> bool:
        if not isinstance(other, SourceCursor):
            return NotImplemented
        cols = self.key_columns or other.key_columns or sorted(set(self.keys) | set(other.keys))
        return self.sort_key(cols) > other.sort_key(cols)

    def __ge__(self, other: Any) -> bool:
        if not isinstance(other, SourceCursor):
            return NotImplemented
        cols = self.key_columns or other.key_columns or sorted(set(self.keys) | set(other.keys))
        return self.sort_key(cols) >= other.sort_key(cols)

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, SourceCursor):
            return False
        cols = self.key_columns or other.key_columns or sorted(set(self.keys) | set(other.keys))
        return self.sort_key(cols) == other.sort_key(cols)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "keys": self.keys,
            "key_columns": self.key_columns,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SourceCursor:
        ts_raw = data["timestamp"]
        if isinstance(ts_raw, str):
            ts = datetime.fromisoformat(ts_raw)
        else:
            ts = ts_raw
        return cls(
            timestamp=ts,
            keys=dict(data.get("keys", {})),
            key_columns=list(data.get("key_columns", [])),
        )
