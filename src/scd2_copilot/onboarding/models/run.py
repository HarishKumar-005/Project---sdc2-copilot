"""Strongly typed domain models, status enum, and schemas for Onboarding Runs."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from ..exceptions import InvalidRunStateTransitionError


class RunStatus(str, Enum):
    """Lifecycle status for an Onboarding Run."""

    CREATED = "CREATED"
    PROFILING = "PROFILING"
    MAPPING_PENDING = "MAPPING_PENDING"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    TRANSFORMING = "TRANSFORMING"
    VALIDATING = "VALIDATING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# Valid state transitions matrix enforcing doc 08 lifecycle rules
VALID_RUN_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.CREATED: {
        RunStatus.PROFILING,
        RunStatus.TRANSFORMING,  # Fast-path when approved mapping is supplied
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.PROFILING: {
        RunStatus.MAPPING_PENDING,
        RunStatus.APPROVAL_PENDING,
        RunStatus.TRANSFORMING,  # When matching approved mapping exists
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.MAPPING_PENDING: {
        RunStatus.APPROVAL_PENDING,
        RunStatus.TRANSFORMING,  # When mapping approved
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.APPROVAL_PENDING: {
        RunStatus.TRANSFORMING,  # Approved by human
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.TRANSFORMING: {
        RunStatus.VALIDATING,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.VALIDATING: {
        RunStatus.COMPLETED,
        RunStatus.PARTIAL,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    # Terminal states have no forward transitions
    RunStatus.COMPLETED: set(),
    RunStatus.PARTIAL: set(),
    RunStatus.CANCELLED: set(),
    # FAILED can transition to CREATED via explicit retry
    RunStatus.FAILED: {RunStatus.CREATED},
}


def validate_run_transition(current: RunStatus, target: RunStatus) -> None:
    """Validate that transition from current to target is permissible."""
    allowed = VALID_RUN_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise InvalidRunStateTransitionError(
            f"Cannot transition onboarding run from '{current.value}' to '{target.value}'.",
            details={
                "current_status": current.value,
                "target_status": target.value,
                "allowed_targets": [s.value for s in allowed],
            },
        )


class RunMetrics(BaseModel):
    """Aggregate processing metrics for an onboarding run."""

    model_config = ConfigDict(extra="ignore")

    total_records: int = Field(0, description="Total input records processed.")
    valid_records: int = Field(0, description="Records successfully transformed and validated.")
    exception_records: int = Field(0, description="Records with validation exceptions enqueued.")
    profile_duration_ms: Optional[float] = Field(None, description="Profiling duration in milliseconds.")
    mapping_duration_ms: Optional[float] = Field(None, description="Mapping proposal duration in milliseconds.")
    transformation_duration_ms: Optional[float] = Field(None, description="Transformation duration in milliseconds.")
    validation_duration_ms: Optional[float] = Field(None, description="Validation duration in milliseconds.")
    total_duration_ms: Optional[float] = Field(None, description="Total run execution duration in milliseconds.")
    extra: dict[str, Any] = Field(default_factory=dict, description="Additional non-sensitive operational metrics.")


class RunArtifactPaths(BaseModel):
    """File paths or URIs of artifacts produced during the run."""

    model_config = ConfigDict(extra="ignore")

    profile_path: Optional[str] = Field(None, description="Path to saved DataProfile JSON artifact.")
    proposal_path: Optional[str] = Field(None, description="Path to MappingProposalBatch JSON artifact.")
    mapping_path: Optional[str] = Field(None, description="Path to ApprovedMappingVersion JSON artifact.")
    result_path: Optional[str] = Field(None, description="Path to TransformationResult JSON artifact.")
    exception_batch_path: Optional[str] = Field(None, description="Path to ExceptionBatch JSON artifact.")
    extra: dict[str, str] = Field(default_factory=dict, description="Additional custom artifact paths.")


class OnboardingRun(BaseModel):
    """Durable execution entity representing an onboarding operation."""

    model_config = ConfigDict(extra="ignore")

    run_id: str = Field(default_factory=lambda: f"onb_run_{uuid4().hex[:12]}")
    idempotency_key: str = Field(..., description="Client-supplied or derived deduplication key.")
    request_fingerprint: str = Field(..., description="SHA-256 fingerprint of the normalized request parameters.")
    source_id: str = Field(..., description="Logical identifier for the data source.")
    source_schema_fingerprint: Optional[str] = Field(None, description="SchemaFingerprint hash from M1 profiling.")
    canonical_schema_version: int = Field(1, description="Target canonical schema version.")
    mapping_version_id: Optional[str] = Field(None, description="ApprovedMappingVersion identifier if applied.")
    status: RunStatus = Field(RunStatus.CREATED, description="Current lifecycle state.")
    input_fingerprint: Optional[str] = Field(None, description="SHA-256 fingerprint of raw input data payload.")
    metrics: RunMetrics = Field(default_factory=RunMetrics)
    artifact_paths: RunArtifactPaths = Field(default_factory=RunArtifactPaths)
    error_message: Optional[str] = Field(None, description="Sanitized failure explanation if run failed.")
    error_details: Optional[dict[str, Any]] = Field(None, description="Structured failure details.")
    operator_id: Optional[str] = Field(None, description="Authenticated operator identity.")
    started_at: Optional[datetime] = Field(None, description="When run processing began.")
    completed_at: Optional[datetime] = Field(None, description="When run reached terminal state.")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def transition_to(
        self,
        target: RunStatus,
        *,
        error_message: Optional[str] = None,
        error_details: Optional[dict[str, Any]] = None,
    ) -> None:
        """Enforce transition rules and update state."""
        validate_run_transition(self.status, target)
        now = datetime.now(timezone.utc)
        self.status = target
        self.updated_at = now

        if target == RunStatus.PROFILING and self.started_at is None:
            self.started_at = now
        elif target == RunStatus.TRANSFORMING and self.started_at is None:
            self.started_at = now

        if target in {RunStatus.COMPLETED, RunStatus.PARTIAL, RunStatus.FAILED, RunStatus.CANCELLED}:
            self.completed_at = now

        if error_message is not None:
            self.error_message = error_message
        if error_details is not None:
            self.error_details = error_details

    def to_dict(self) -> dict[str, Any]:
        """Convert run to JSON-serializable dictionary."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OnboardingRun:
        """Construct run from dictionary."""
        return cls.model_validate(data)


# ── API Request and Response Schemas ───────────────────────


class RunCreateRequest(BaseModel):
    """Payload to initiate or submit an onboarding run."""

    model_config = ConfigDict(extra="ignore")

    source_id: str = Field(..., description="Identifier for the source system/entity.")
    idempotency_key: Optional[str] = Field(
        None,
        description="Optional body idempotency key. Header 'Idempotency-Key' takes precedence.",
    )
    file_path: Optional[str] = Field(None, description="Local or artifact path to source input file.")
    data_payload: Optional[list[dict[str, Any]]] = Field(
        None, description="Inline raw records payload (e.g. from REST or mock API)."
    )
    mapping_version_id: Optional[str] = Field(None, description="Pre-approved mapping version ID if available.")
    canonical_schema_version: int = Field(1, description="Target canonical schema version.")
    auto_execute: bool = Field(True, description="Whether to execute pipeline phases immediately.")
    options: dict[str, Any] = Field(default_factory=dict, description="Execution options (e.g. allow_partial).")


class RunResponse(BaseModel):
    """API response envelope for an OnboardingRun."""

    model_config = ConfigDict(extra="ignore")

    run_id: str
    idempotency_key: str
    request_fingerprint: str
    source_id: str
    source_schema_fingerprint: Optional[str] = None
    canonical_schema_version: int
    mapping_version_id: Optional[str] = None
    status: RunStatus
    input_fingerprint: Optional[str] = None
    metrics: RunMetrics
    artifact_paths: RunArtifactPaths
    error_message: Optional[str] = None
    error_details: Optional[dict[str, Any]] = None
    operator_id: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class RunListResponse(BaseModel):
    """Paginated list response for onboarding runs."""

    model_config = ConfigDict(extra="ignore")

    runs: list[RunResponse]
    total: int
    limit: int
    offset: int


class RunRetryRequest(BaseModel):
    """Payload to retry a failed onboarding run."""

    model_config = ConfigDict(extra="ignore")

    options: Optional[dict[str, Any]] = Field(None, description="Optional overrides for retry execution.")
    file_path: Optional[str] = Field(None, description="Optional path to source data file for re-execution.")
    data_payload: Optional[list[dict[str, Any]]] = Field(None, description="Optional in-memory rows for re-execution.")


class RunCancelRequest(BaseModel):
    """Payload to explicitly cancel an in-progress onboarding run."""

    model_config = ConfigDict(extra="ignore")

    reason: Optional[str] = Field(None, description="Reason for run cancellation.")
