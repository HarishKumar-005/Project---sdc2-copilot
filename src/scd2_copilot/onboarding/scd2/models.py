"""Domain models and typed schemas for Customer Data Onboarding SCD2 integration."""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
from typing import Any, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from ...models import DeletePolicy, SnapshotMode


class CustomerSCD2Config(BaseModel):
    """Configuration contract for mapping canonical customer records into the SCD2 engine."""

    model_config = ConfigDict(frozen=True)

    entity_source_name: str = Field(
        default="customer",
        description="Logical source name for monitored_entity_history table",
    )
    business_key: list[str] = Field(
        default_factory=lambda: ["customer_id"],
        description="Authoritative business key columns (customer_id)",
    )
    tracked_columns: list[str] = Field(
        default_factory=lambda: [
            "first_name",
            "last_name",
            "email",
            "date_of_birth",
            "status",
        ],
        description="Canonical customer attributes compared for business-state changes",
    )
    snapshot_mode: SnapshotMode = Field(
        default=SnapshotMode.INCREMENTAL,
        description="Snapshot mode for customer batch (INCREMENTAL or FULL)",
    )
    delete_policy: DeletePolicy = Field(
        default=DeletePolicy.IGNORE,
        description="Delete policy for absent records (IGNORE or SOFT_DELETE)",
    )
    engine: str = Field(
        default="auto",
        description="Polars execution engine: 'auto', 'in-memory', or 'streaming'",
    )


class CustomerSCD2ExecutionResult(BaseModel):
    """Authoritative structured outcome from executing the SCD2 engine on canonical customer records."""

    model_config = ConfigDict(frozen=True)

    execution_id: str = Field(
        default_factory=lambda: f"scd2_exec_{uuid4().hex[:12]}",
        description="Unique execution identifier for this SCD2 run",
    )
    run_id: Optional[str] = Field(
        default=None,
        description="Associated onboarding run ID if triggered from an onboarding pipeline",
    )
    batch_id: str = Field(
        default_factory=lambda: f"batch_{uuid4().hex[:8]}",
        description="Identifier of the processed canonical customer batch",
    )
    source_id: str = Field(
        default="customer",
        description="Source system or canonical entity name",
    )
    processing_date: date = Field(
        ...,
        description="Effective date stamped on new versions and closure of prior versions",
    )
    total_records_seen: int = Field(default=0, ge=0)
    new_count: int = Field(default=0, ge=0)
    changed_count: int = Field(default=0, ge=0)
    unchanged_count: int = Field(default=0, ge=0)
    deleted_count: int = Field(default=0, ge=0)
    closed_versions_count: int = Field(default=0, ge=0)
    new_versions_count: int = Field(default=0, ge=0)
    scd2_rows_total: int = Field(default=0, ge=0)
    validation_passed: bool = Field(default=True)
    validation_report: Optional[dict[str, Any]] = None
    change_report_summary: dict[str, Any] = Field(default_factory=dict)
    persisted_history_rows_count: int = Field(default=0, ge=0)
    executed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp of execution",
    )

    def to_dict(self) -> dict[str, Any]:
        """Convert execution result to a serializable dictionary."""
        data = self.model_dump()
        data["processing_date"] = self.processing_date.isoformat()
        data["executed_at"] = self.executed_at.isoformat()
        return data

    def save_artifact(self, destination_path: Path | str) -> Path:
        """Save execution result as a structured JSON artifact."""
        dest = Path(destination_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, default=str)
        return dest


class CustomerPointInTimeState(BaseModel):
    """Point-in-time historical customer state conforming to half-open interval semantics."""

    model_config = ConfigDict(frozen=True)

    customer_id: str = Field(..., description="Canonical customer business key")
    as_of: datetime = Field(..., description="Timestamp of the point-in-time query")
    found: bool = Field(..., description="True if an active version existed at timestamp as_of")
    history_id: Optional[UUID] = Field(default=None, description="MonitoredEntityHistory UUID")
    effective_from: Optional[datetime] = Field(default=None, description="Inclusive start instant")
    effective_to: Optional[datetime] = Field(default=None, description="Exclusive end instant")
    is_current: Optional[bool] = Field(default=None, description="True if currently active")
    attributes: dict[str, Any] = Field(default_factory=dict, description="Customer attributes at as_of")
