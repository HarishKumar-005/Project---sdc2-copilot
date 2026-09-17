"""Micro-batch container and DataFrame adaptation for incremental ingestion."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

import polars as pl

from ..db.models import InventoryHistoryRow, InventorySourceRow, MonitoredEntityHistoryRow


@dataclass(frozen=True)
class MicroBatch:
    """Represents a bounded slice of operational source records."""

    source_records: list[Any]
    watermark_start: Optional[datetime] = None
    watermark_end: Optional[datetime] = None
    batch_id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    key_columns: list[str] = field(default_factory=lambda: ["sku_id", "warehouse_id"])
    tracked_columns: list[str] = field(
        default_factory=lambda: ["quantity_on_hand", "reorder_level", "status"]
    )
    timestamp_column: str = "updated_at"
    source_name: str = "inventory_source"
    first_cursor: Optional[Any] = None
    last_cursor: Optional[Any] = None

    def __post_init__(self) -> None:
        if self.source_records:
            from ..source.models import SourceCursor

            if self.first_cursor is None:
                first_r = self.source_records[0]
                first_ts = (
                    first_r.get(self.timestamp_column)
                    if isinstance(first_r, dict)
                    else getattr(first_r, self.timestamp_column, getattr(first_r, "updated_at", None))
                )
                if first_ts is not None:
                    first_k = {
                        c: (first_r.get(c) if isinstance(first_r, dict) else getattr(first_r, c, None))
                        for c in self.key_columns
                    }
                    object.__setattr__(
                        self,
                        "first_cursor",
                        SourceCursor(
                            timestamp=first_ts,
                            keys=first_k,
                            key_columns=self.key_columns,
                        ),
                    )

            if self.last_cursor is None:
                last_r = self.source_records[-1]
                last_ts = (
                    last_r.get(self.timestamp_column)
                    if isinstance(last_r, dict)
                    else getattr(last_r, self.timestamp_column, getattr(last_r, "updated_at", None))
                )
                if last_ts is not None:
                    last_k = {
                        c: (last_r.get(c) if isinstance(last_r, dict) else getattr(last_r, c, None))
                        for c in self.key_columns
                    }
                    object.__setattr__(
                        self,
                        "last_cursor",
                        SourceCursor(
                            timestamp=last_ts,
                            keys=last_k,
                            key_columns=self.key_columns,
                        ),
                    )

    @property
    def size(self) -> int:
        """Return count of records in this micro-batch."""
        return len(self.source_records)

    @property
    def is_empty(self) -> bool:
        """Return True if batch contains no records."""
        return len(self.source_records) == 0

    @property
    def business_keys(self) -> list[tuple[Any, ...]]:
        """Return deduplicated list of business key tuples in this batch."""
        seen = set()
        keys = []
        for r in self.source_records:
            if isinstance(r, dict):
                k = tuple(r.get(c) for c in self.key_columns)
            else:
                k = tuple(getattr(r, c, None) for c in self.key_columns)
            if k not in seen:
                seen.add(k)
                keys.append(k)
        return keys

    @property
    def max_updated_at(self) -> Optional[datetime]:
        """Return the maximum timestamp in this batch."""
        if not self.source_records:
            return None
        ts_vals = []
        for r in self.source_records:
            if isinstance(r, dict):
                val = r.get(self.timestamp_column)
            else:
                val = getattr(r, self.timestamp_column, getattr(r, "updated_at", None))
            if val is not None:
                if isinstance(val, str):
                    try:
                        val = datetime.fromisoformat(val)
                    except (ValueError, TypeError):
                        pass
                ts_vals.append(val)
        return max(ts_vals) if ts_vals else None

    def to_source_df(self) -> pl.DataFrame:
        """Convert source records into a Polars DataFrame matching SCD2 engine inputs."""
        cols = self.key_columns + self.tracked_columns
        if not self.source_records:
            # Generic empty-batch schema: all columns as String.
            # The SCD2 engine handles empty DataFrames with any consistent schema.
            return pl.DataFrame(schema={c: pl.String for c in cols})

        data = []
        for r in self.source_records:
            if isinstance(r, dict):
                row_dict = {c: r.get(c) for c in cols}
            else:
                row_dict = {c: getattr(r, c, None) for c in cols}
            data.append(row_dict)

        df = pl.DataFrame(data)

        # Build a dynamic cast map:
        # - All key columns → String (business keys are always string-compared)
        # - Tracked columns: attempt Int64 cast for columns that look numeric
        #   (all non-null values parseable as int).  Keep String for the rest.
        # Explicit overrides for well-known inventory demo columns.
        KNOWN_INT_COLUMNS = {"quantity_on_hand", "reorder_level"}
        cast_map = {}
        for c in self.key_columns:
            if c in df.columns:
                cast_map[c] = pl.String

        for c in self.tracked_columns:
            if c not in df.columns:
                continue
            if c in KNOWN_INT_COLUMNS:
                cast_map[c] = pl.Int64
                continue
            # Dynamically detect numeric columns
            col_vals = [row_dict.get(c) for row_dict in data if row_dict.get(c) is not None]
            if col_vals:
                try:
                    [int(v) for v in col_vals]
                    cast_map[c] = pl.Int64
                except (TypeError, ValueError):
                    cast_map[c] = pl.String
            else:
                cast_map[c] = pl.String

        if cast_map:
            return df.cast(cast_map)
        return df

    def to_dict(self) -> list[dict[str, Any]]:
        """Return list of dict representations of source records."""
        res = []
        for r in self.source_records:
            if isinstance(r, dict):
                res.append(dict(r))
            elif hasattr(r, "to_dict"):
                res.append(r.to_dict())
            else:
                res.append(dict(r.__dict__))
        return res

    def cursor_metadata(self) -> dict[str, Any]:
        """Return serialized cursor boundary metadata."""
        return {
            "batch_id": str(self.batch_id),
            "size": self.size,
            "first_cursor": self.first_cursor.to_dict() if self.first_cursor else None,
            "last_cursor": self.last_cursor.to_dict() if self.last_cursor else None,
        }


def history_rows_to_target_df(rows: list[InventoryHistoryRow]) -> pl.DataFrame:
    """Convert a list of InventoryHistoryRows into a typed Polars DataFrame for SCD2 processing."""
    schema = {
        "sku_id": pl.String,
        "warehouse_id": pl.String,
        "quantity_on_hand": pl.Int64,
        "reorder_level": pl.Int64,
        "status": pl.String,
        "effective_from": pl.Date,
        "effective_to": pl.Date,
        "is_current": pl.Boolean,
    }

    if not rows:
        return pl.DataFrame(schema=schema)

    def _to_date(val: Any) -> Optional[date]:
        if val is None:
            return None
        if isinstance(val, datetime):
            return val.date()
        if isinstance(val, date):
            return val
        return None

    data = [
        {
            "sku_id": r.sku_id,
            "warehouse_id": r.warehouse_id,
            "quantity_on_hand": r.quantity_on_hand,
            "reorder_level": r.reorder_level,
            "status": r.status,
            "effective_from": _to_date(r.effective_from),
            "effective_to": _to_date(r.effective_to),
            "is_current": bool(r.is_current),
        }
        for r in rows
    ]

    return pl.DataFrame(data, schema=schema)


def generic_history_rows_to_target_df(
    rows: list[MonitoredEntityHistoryRow],
    key_columns: list[str],
    tracked_columns: list[str],
) -> pl.DataFrame:
    """Convert a list of MonitoredEntityHistoryRows into a typed Polars DataFrame for SCD2 processing."""
    schema: dict[str, Any] = {c: pl.String for c in key_columns}
    for c in tracked_columns:
        schema[c] = pl.String
    schema["effective_from"] = pl.Date
    schema["effective_to"] = pl.Date
    schema["is_current"] = pl.Boolean

    if not rows:
        return pl.DataFrame(schema=schema)

    def _to_date(val: Any) -> Optional[date]:
        if val is None:
            return None
        if isinstance(val, datetime):
            return val.date()
        if isinstance(val, date):
            return val
        return None

    data = []
    for r in rows:
        row_dict: dict[str, Any] = {}
        for k in key_columns:
            row_dict[k] = r.entity_key.get(k)
        for t in tracked_columns:
            row_dict[t] = r.attributes.get(t)
        row_dict["effective_from"] = _to_date(r.effective_from)
        row_dict["effective_to"] = _to_date(r.effective_to)
        row_dict["is_current"] = bool(r.is_current)
        data.append(row_dict)

    df = pl.DataFrame(data)

    KNOWN_INT_COLUMNS = {"quantity_on_hand", "reorder_level"}
    cast_map: dict[str, Any] = {}
    for c in key_columns:
        if c in df.columns:
            cast_map[c] = pl.String

    for c in tracked_columns:
        if c not in df.columns:
            continue
        if c in KNOWN_INT_COLUMNS:
            cast_map[c] = pl.Int64
            continue
        col_vals = [row_dict.get(c) for row_dict in data if row_dict.get(c) is not None]
        if col_vals:
            try:
                [int(v) for v in col_vals]
                cast_map[c] = pl.Int64
            except (TypeError, ValueError):
                cast_map[c] = pl.String
        else:
            cast_map[c] = pl.String

    cast_map["effective_from"] = pl.Date
    cast_map["effective_to"] = pl.Date
    cast_map["is_current"] = pl.Boolean

    return df.cast(cast_map)
