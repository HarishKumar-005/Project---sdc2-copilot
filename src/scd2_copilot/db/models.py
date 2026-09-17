"""Typed data models and row representations for Supabase/PostgreSQL tables."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timezone
from enum import Enum
import json
from typing import Any, Optional, Union
from uuid import UUID, uuid4


class RunStatus(str, Enum):
    """Lifecycle status for a processing run in PostgreSQL."""

    RECEIVED = "RECEIVED"
    BUFFERED = "BUFFERED"
    PROCESSING = "PROCESSING"
    VALIDATED = "VALIDATED"
    COMMITTED = "COMMITTED"
    HELD = "HELD"
    FAILED = "FAILED"

    # Aliases
    STARTED = "PROCESSING"
    COMPLETED = "COMMITTED"


class HoldStatus(str, Enum):
    """Containment gate resolution status for a held change batch."""

    HELD = "HELD"
    RELEASED = "RELEASED"
    DISCARDED = "DISCARDED"
    REPROCESSED = "REPROCESSED"

    # Aliases
    APPROVED = "RELEASED"
    REJECTED = "DISCARDED"


class HoldSeverity(str, Enum):
    """Severity classification for held change batches."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


def _ensure_utc(dt: Any) -> Optional[datetime]:
    """Ensure a datetime or date object is timezone-aware in UTC."""
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except Exception:
            return None
    if isinstance(dt, date) and not isinstance(dt, datetime):
        return datetime.combine(dt, time.min, tzinfo=timezone.utc)
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


@dataclass(frozen=True)
class InventorySourceRow:
    """Represents a row in the operational inventory_source table."""

    sku_id: str
    warehouse_id: str
    quantity_on_hand: int
    reorder_level: int
    status: str
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.sku_id or not self.sku_id.strip():
            raise ValueError("sku_id cannot be empty")
        if not self.warehouse_id or not self.warehouse_id.strip():
            raise ValueError("warehouse_id cannot be empty")
        if self.quantity_on_hand < 0:
            raise ValueError("quantity_on_hand cannot be negative")
        if self.reorder_level < 0:
            raise ValueError("reorder_level cannot be negative")

    @property
    def business_key(self) -> tuple[str, str]:
        """Return the composite primary business key."""
        return (self.sku_id, self.warehouse_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sku_id": self.sku_id,
            "warehouse_id": self.warehouse_id,
            "quantity_on_hand": self.quantity_on_hand,
            "reorder_level": self.reorder_level,
            "status": self.status,
            "updated_at": _ensure_utc(self.updated_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InventorySourceRow:
        return cls(
            sku_id=str(data["sku_id"]),
            warehouse_id=str(data["warehouse_id"]),
            quantity_on_hand=int(data["quantity_on_hand"]),
            reorder_level=int(data["reorder_level"]),
            status=str(data.get("status", "ACTIVE")),
            updated_at=_ensure_utc(data["updated_at"]) or datetime.now(timezone.utc),
        )


@dataclass(frozen=True)
class InventoryHistoryRow:
    """Represents an SCD Type 2 row in the inventory_history table.

    Temporal validity model: [effective_from, effective_to)
    - effective_from is inclusive
    - effective_to is exclusive (None for the active/current version)
    """

    sku_id: str
    warehouse_id: str
    quantity_on_hand: int
    reorder_level: int
    status: str
    effective_from: Union[datetime, date]
    is_current: bool
    history_id: UUID = field(default_factory=uuid4)
    effective_to: Optional[Union[datetime, date]] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not self.sku_id or not self.sku_id.strip():
            raise ValueError("sku_id cannot be empty")
        if not self.warehouse_id or not self.warehouse_id.strip():
            raise ValueError("warehouse_id cannot be empty")

        eff_from = _ensure_utc(self.effective_from)
        eff_to = _ensure_utc(self.effective_to)

        # Invariant: effective_to must be after or equal to effective_from when closed
        if eff_to is not None and eff_from is not None:
            if eff_to < eff_from:
                raise ValueError(
                    f"effective_from must be before or equal to effective_to: {eff_from} vs {eff_to}"
                )

        # Invariant: is_current must match effective_to is None
        if self.is_current and eff_to is not None:
            raise ValueError("Active row (is_current=True) must have effective_to = None")
        if not self.is_current and eff_to is None:
            raise ValueError("Closed row (is_current=False) must have a non-null effective_to")

    @property
    def business_key(self) -> tuple[str, str]:
        """Return the composite primary business key."""
        return (self.sku_id, self.warehouse_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "history_id": self.history_id,
            "sku_id": self.sku_id,
            "warehouse_id": self.warehouse_id,
            "quantity_on_hand": self.quantity_on_hand,
            "reorder_level": self.reorder_level,
            "status": self.status,
            "effective_from": _ensure_utc(self.effective_from),
            "effective_to": _ensure_utc(self.effective_to),
            "is_current": self.is_current,
            "created_at": _ensure_utc(self.created_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InventoryHistoryRow:
        hist_id = data.get("history_id")
        if isinstance(hist_id, str):
            hist_id = UUID(hist_id)
        elif hist_id is None:
            hist_id = uuid4()

        return cls(
            history_id=hist_id,
            sku_id=str(data["sku_id"]),
            warehouse_id=str(data["warehouse_id"]),
            quantity_on_hand=int(data["quantity_on_hand"]),
            reorder_level=int(data["reorder_level"]),
            status=str(data["status"]),
            effective_from=_ensure_utc(data["effective_from"]) or datetime.now(timezone.utc),
            effective_to=_ensure_utc(data.get("effective_to")),
            is_current=bool(data["is_current"]),
            created_at=_ensure_utc(data.get("created_at")) or datetime.now(timezone.utc),
        )


@dataclass(frozen=True)
class MonitoredEntityHistoryRow:
    """Represents a generic SCD Type 2 row in the monitored_entity_history table.

    Temporal validity model: [effective_from, effective_to)
    - effective_from is inclusive
    - effective_to is exclusive (None for the active/current version)
    """

    source_name: str
    entity_key: dict[str, Any]
    attributes: dict[str, Any]
    effective_from: Union[datetime, date]
    is_current: bool
    history_id: UUID = field(default_factory=uuid4)
    effective_to: Optional[Union[datetime, date]] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not self.source_name or not self.source_name.strip():
            raise ValueError("source_name cannot be empty")
        if not isinstance(self.entity_key, dict) or not self.entity_key:
            raise ValueError("entity_key must be a non-empty dictionary")
        if not isinstance(self.attributes, dict):
            raise ValueError("attributes must be a dictionary")

        eff_from = _ensure_utc(self.effective_from)
        eff_to = _ensure_utc(self.effective_to)

        # Invariant: effective_to must be after or equal to effective_from when closed
        if eff_to is not None and eff_from is not None:
            if eff_to < eff_from:
                raise ValueError(
                    f"effective_from must be before or equal to effective_to: {eff_from} vs {eff_to}"
                )

        # Invariant: is_current must match effective_to is None
        if self.is_current and eff_to is not None:
            raise ValueError("Active row (is_current=True) must have effective_to = None")
        if not self.is_current and eff_to is None:
            raise ValueError("Closed row (is_current=False) must have a non-null effective_to")

    @property
    def business_key_tuple(self) -> tuple[Any, ...]:
        """Return the tuple of business key values sorted by key name."""
        return tuple(self.entity_key[k] for k in sorted(self.entity_key.keys()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "history_id": self.history_id,
            "source_name": self.source_name,
            "entity_key": dict(self.entity_key),
            "attributes": dict(self.attributes),
            "effective_from": _ensure_utc(self.effective_from),
            "effective_to": _ensure_utc(self.effective_to),
            "is_current": self.is_current,
            "created_at": _ensure_utc(self.created_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MonitoredEntityHistoryRow:
        hist_id = data.get("history_id")
        if isinstance(hist_id, str):
            hist_id = UUID(hist_id)
        elif hist_id is None:
            hist_id = uuid4()

        ek = data["entity_key"]
        if isinstance(ek, str):
            ek = json.loads(ek)

        attrs = data.get("attributes", {})
        if isinstance(attrs, str):
            attrs = json.loads(attrs)

        return cls(
            history_id=hist_id,
            source_name=str(data["source_name"]),
            entity_key=dict(ek),
            attributes=dict(attrs),
            effective_from=_ensure_utc(data["effective_from"]) or datetime.now(timezone.utc),
            effective_to=_ensure_utc(data.get("effective_to")),
            is_current=bool(data["is_current"]),
            created_at=_ensure_utc(data.get("created_at")) or datetime.now(timezone.utc),
        )


@dataclass(frozen=True)
class ProcessingRunRow:
    """Represents an execution audit log entry in the processing_run table."""

    source_name: str
    status: str
    started_at: datetime
    run_id: UUID = field(default_factory=uuid4)
    completed_at: Optional[datetime] = None
    records_seen: int = 0
    records_changed: int = 0
    records_held: int = 0
    error_message: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "source_name": self.source_name,
            "status": self.status,
            "started_at": _ensure_utc(self.started_at),
            "completed_at": _ensure_utc(self.completed_at),
            "records_seen": self.records_seen,
            "records_changed": self.records_changed,
            "records_held": self.records_held,
            "error_message": self.error_message,
            "created_at": _ensure_utc(self.created_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProcessingRunRow:
        run_id = data.get("run_id")
        if isinstance(run_id, str):
            run_id = UUID(run_id)
        elif run_id is None:
            run_id = uuid4()

        return cls(
            run_id=run_id,
            source_name=str(data["source_name"]),
            status=str(data["status"]),
            started_at=_ensure_utc(data["started_at"]) or datetime.now(timezone.utc),
            completed_at=_ensure_utc(data.get("completed_at")),
            records_seen=int(data.get("records_seen", 0)),
            records_changed=int(data.get("records_changed", 0)),
            records_held=int(data.get("records_held", 0)),
            error_message=str(data["error_message"]) if data.get("error_message") else None,
            created_at=_ensure_utc(data.get("created_at")) or datetime.now(timezone.utc),
        )


@dataclass(frozen=True)
class ProcessingCheckpointRow:
    """Represents a stream watermark and cursor checkpoint in processing_checkpoint."""

    source_name: str
    table_name: str = "inventory_source"
    watermark_value: Optional[datetime] = None
    last_successful_run_id: Optional[UUID] = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    cursor_keys: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "table_name": self.table_name,
            "watermark_value": _ensure_utc(self.watermark_value),
            "last_successful_run_id": self.last_successful_run_id,
            "updated_at": _ensure_utc(self.updated_at),
            "cursor_keys": self.cursor_keys,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProcessingCheckpointRow:
        import json

        run_id = data.get("last_successful_run_id")
        if isinstance(run_id, str):
            run_id = UUID(run_id)

        c_keys = data.get("cursor_keys")
        if isinstance(c_keys, str):
            try:
                c_keys = json.loads(c_keys)
            except Exception:
                c_keys = None

        return cls(
            source_name=str(data["source_name"]),
            table_name=str(data.get("table_name", "inventory_source")),
            watermark_value=_ensure_utc(data.get("watermark_value")),
            last_successful_run_id=run_id,
            updated_at=_ensure_utc(data.get("updated_at")) or datetime.now(timezone.utc),
            cursor_keys=c_keys,
        )

    def to_source_cursor(self, key_columns: Optional[list[str]] = None) -> Any:
        """Construct a SourceCursor representation if watermark_value is present."""
        if self.watermark_value is None:
            return None
        from ..source.models import SourceCursor

        return SourceCursor(
            timestamp=self.watermark_value,
            keys=self.cursor_keys or {},
            key_columns=key_columns or list((self.cursor_keys or {}).keys()),
        )


@dataclass(frozen=True)
class HeldChangeBatchRow:
    """Represents a suspicious batch held at the downstream boundary."""

    run_id: UUID
    source_name: str
    severity: str
    reason: str
    records_affected: int
    evidence: dict[str, Any]
    status: str = "HELD"
    hold_id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "hold_id": self.hold_id,
            "run_id": self.run_id,
            "source_name": self.source_name,
            "severity": self.severity,
            "reason": self.reason,
            "records_affected": self.records_affected,
            "evidence": self.evidence,
            "status": self.status,
            "created_at": _ensure_utc(self.created_at),
            "resolved_at": _ensure_utc(self.resolved_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HeldChangeBatchRow:
        hold_id = data.get("hold_id")
        if isinstance(hold_id, str):
            hold_id = UUID(hold_id)
        elif hold_id is None:
            hold_id = uuid4()

        run_id = data["run_id"]
        if isinstance(run_id, str):
            run_id = UUID(run_id)

        evidence = data.get("evidence", {})
        if isinstance(evidence, str):
            try:
                evidence = json.loads(evidence)
            except Exception:
                evidence = {"raw": evidence}

        return cls(
            hold_id=hold_id,
            run_id=run_id,
            source_name=str(data["source_name"]),
            severity=str(data["severity"]),
            reason=str(data["reason"]),
            records_affected=int(data.get("records_affected", 0)),
            evidence=dict(evidence),
            status=str(data.get("status", "HELD")),
            created_at=_ensure_utc(data.get("created_at")) or datetime.now(timezone.utc),
            resolved_at=_ensure_utc(data.get("resolved_at")),
        )
