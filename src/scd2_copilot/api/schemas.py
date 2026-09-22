"""Strongly typed Pydantic V2 schemas for FastAPI request and response contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


# ── Error Schemas ──────────────────────────────────────


class ErrorDetail(BaseModel):
    """Structured error payload without exposing credentials or internal stack traces."""

    model_config = ConfigDict(extra="ignore")

    code: str = Field(..., description="Machine-readable error classification code.")
    message: str = Field(..., description="Human-readable safe explanation.")
    request_id: Optional[str] = Field(None, description="Request correlation identifier.")
    details: Optional[Any] = Field(None, description="Additional non-sensitive error context.")


class ErrorResponse(BaseModel):
    """Standardized top-level API error envelope."""

    error: ErrorDetail


# ── Health & Readiness Schemas ─────────────────────────


class HealthResponse(BaseModel):
    """Liveness probe status confirming process availability."""

    status: str = Field("healthy", description="Operational liveness status.")
    version: str = Field("v1", description="API version identifier.")
    timestamp: datetime = Field(..., description="Timestamp of probe evaluation.")


class ReadinessResponse(BaseModel):
    """Readiness probe status verifying connectivity to backend dependencies."""

    status: str = Field("ready", description="Overall readiness: ready | unready.")
    database_connected: bool = Field(..., description="Database pool health status.")
    timestamp: datetime = Field(..., description="Timestamp of readiness evaluation.")
    checks: dict[str, bool] = Field(default_factory=dict, description="Component check results.")

    @property
    def db_connected(self) -> bool:
        """Compatibility property matching database_connected."""
        return self.database_connected


# ── Processing Run Schemas ─────────────────────────────


class RunResponse(BaseModel):
    """Representation of an incremental processing run."""

    model_config = ConfigDict(from_attributes=True)

    run_id: UUID
    source_name: str
    status: str
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    records_seen: int = 0
    records_changed: int = 0
    records_held: int = 0
    error_message: Optional[str] = None
    created_at: datetime


class RunListResponse(BaseModel):
    """Paginated collection of processing runs."""

    runs: list[RunResponse]
    total: int
    limit: int
    offset: int = 0


# ── Held Batch & Explanation Schemas ───────────────────


class HoldResponse(BaseModel):
    """Representation of a suspicious held change batch."""

    model_config = ConfigDict(from_attributes=True)

    hold_id: UUID
    run_id: UUID
    source_name: str
    severity: str
    reason: str
    records_affected: int
    status: str
    created_at: datetime
    resolved_at: Optional[datetime] = None
    evidence: Optional[dict[str, Any]] = None
    explanation: Optional[dict[str, Any]] = None


class HoldListResponse(BaseModel):
    """Paginated collection of held batches."""

    holds: list[HoldResponse]
    total: int
    limit: int
    offset: int = 0


# ── Recovery Action Schemas ────────────────────────────


class ReleaseHoldRequest(BaseModel):
    """Request payload to release a held batch downstream."""

    operator_reason: Optional[str] = Field(None, max_length=500, description="Operator's audit reason.")


class ReprocessHoldRequest(BaseModel):
    """Request payload to reprocess a held batch."""

    force_normal: bool = Field(
        False,
        description="If True, overrides remaining guardrail suspicions and forces commit.",
    )


class DiscardHoldRequest(BaseModel):
    """Request payload to permanently discard a held batch."""

    operator_reason: Optional[str] = Field(None, max_length=500, description="Operator's audit reason.")
    advance_checkpoint: bool = Field(
        True,
        description="Whether to advance stream watermark to skip this batch slice.",
    )


class RecoveryActionResponse(BaseModel):
    """Result of an operational recovery transition."""

    hold_id: UUID
    status: str
    success: bool
    message: str
    resolved_at: Optional[datetime] = None
    records_affected: int = 0
    checkpoint_advanced_to: Optional[datetime] = None
    is_idempotent: bool = False
    operator_id: Optional[UUID] = None


# ── Historical Inventory Schemas ───────────────────────


class HistoryRecordResponse(BaseModel):
    """SCD Type 2 historical record for an inventory business key."""

    model_config = ConfigDict(from_attributes=True)

    history_id: UUID
    sku_id: str
    warehouse_id: str
    quantity_on_hand: int
    reorder_level: int
    status: str
    effective_from: datetime
    effective_to: Optional[datetime] = None
    is_current: bool
    created_at: datetime


class HistoryQueryResponse(BaseModel):
    """Complete chronological history for a specific business key."""

    sku_id: str
    warehouse_id: str
    versions: list[HistoryRecordResponse]
    total_versions: int


class EntityHistoryRecordResponse(BaseModel):
    """Generic SCD Type 2 historical record for a monitored entity."""

    model_config = ConfigDict(from_attributes=True)

    history_id: UUID
    source_name: str
    entity_key: dict[str, Any]
    attributes: dict[str, Any]
    effective_from: datetime
    effective_to: Optional[datetime] = None
    is_current: bool
    created_at: datetime


class EntityHistoryQueryResponse(BaseModel):
    """Complete chronological history for a specific generic entity key."""

    source_name: str
    entity_key: dict[str, Any]
    versions: list[EntityHistoryRecordResponse]
    total_versions: int


# ── Operational Source Inventory Schemas ───────────────


class InventoryRecordResponse(BaseModel):
    """Operational source record from inventory_source."""

    model_config = ConfigDict(from_attributes=True)

    sku_id: str
    warehouse_id: str
    quantity_on_hand: int
    reorder_level: int
    status: str
    updated_at: datetime


class InventoryListResponse(BaseModel):
    """Bounded collection of operational inventory records."""

    records: list[InventoryRecordResponse]
    total: int
    limit: int


# ── Operational Metrics Schemas ────────────────────────


class OperationalMetricsResponse(BaseModel):
    """Aggregated operational metrics from database repositories."""

    total_runs: int = 0
    successful_runs: int = 0
    failed_runs: int = 0
    held_runs: int = 0
    active_holds_count: int = 0
    total_records_processed: int = 0
    total_records_changed: int = 0
    total_records_held: int = 0
    latest_checkpoint: Optional[datetime] = None
    latest_run_status: Optional[str] = None
    latest_processed_at: Optional[datetime] = None
    system_health: str = "nominal"


# ── Monitor Configuration Schemas (V3 Phase 1) ─────────


class PostgresSourceSchema(BaseModel):
    """PostgreSQL source location."""

    model_config = ConfigDict(populate_by_name=True)

    type: str = "postgresql"
    schema_name: str = Field(default="public", alias="schema")
    table_name: str = Field(..., alias="table")


class ChangeTimestampSchema(BaseModel):
    """Change timestamp column specification."""

    model_config = ConfigDict(populate_by_name=True)

    column: str


class MonitorConfigRequest(BaseModel):
    """Payload to configure or validate a PostgreSQL monitor."""

    model_config = ConfigDict(populate_by_name=True)

    name: str
    source: PostgresSourceSchema
    business_keys: list[str] = Field(..., alias="keys")
    change_timestamp: ChangeTimestampSchema
    tracked_columns: list[str]


class MonitorConfigResponse(BaseModel):
    """Monitor configuration details."""

    model_config = ConfigDict(populate_by_name=True)

    name: str
    source: PostgresSourceSchema
    business_keys: list[str] = Field(..., alias="keys")
    change_timestamp: ChangeTimestampSchema
    tracked_columns: list[str]


class MonitorListResponse(BaseModel):
    """List of active monitor configurations."""

    monitors: list[MonitorConfigResponse]
    total: int


class ColumnMetadataSchema(BaseModel):
    """Discovered PostgreSQL column metadata."""

    column_name: str
    data_type: str
    is_nullable: bool
    ordinal_position: int


class MonitorValidationResponse(BaseModel):
    """Result of validating a monitor configuration against live PostgreSQL."""

    is_valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    discovered_columns: dict[str, ColumnMetadataSchema] = Field(default_factory=dict)
    primary_keys: list[str] = Field(default_factory=list)


class MonitorRecordsResponse(BaseModel):
    """Bounded collection of generic records from a monitored source table."""

    monitor: str
    records: list[dict[str, Any]]
    total: int
    limit: int


