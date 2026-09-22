"""Data contracts and lifecycle models for M5: Exception Queue & Reprocessing."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import json
from pathlib import Path
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field

from ..exceptions import InvalidExceptionStateTransitionError, NonDismissibleExceptionError
from .mapping import TransformationOpType, TransformationStep
from .transformation import CanonicalCustomerRecord, RecordValidationError, ValidationCategory, ValidationErrorSeverity


class ExceptionCategory(str, Enum):
    """Processing stage / category of an onboarding exception (per 06 contract)."""

    SOURCE_ERROR = "SOURCE_ERROR"
    MAPPING_ERROR = "MAPPING_ERROR"
    TRANSFORMATION_ERROR = "TRANSFORMATION_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    DUPLICATE_ERROR = "DUPLICATE_ERROR"
    REFERENTIAL_INTEGRITY_ERROR = "REFERENTIAL_INTEGRITY_ERROR"
    BUSINESS_RULE_ERROR = "BUSINESS_RULE_ERROR"
    SCHEMA_DRIFT_ERROR = "SCHEMA_DRIFT_ERROR"


class ExceptionStatus(str, Enum):
    """Governed lifecycle state of an onboarding record exception."""

    OPEN = "OPEN"
    CORRECTED = "CORRECTED"
    REPROCESSED = "REPROCESSED"
    RESOLVED = "RESOLVED"
    DISMISSED = "DISMISSED"


class CorrectionType(str, Enum):
    """Permitted deterministic correction action types."""

    VALUE_OVERRIDE = "VALUE_OVERRIDE"
    APPLY_TRANSFORMATION = "APPLY_TRANSFORMATION"
    DISMISS = "DISMISS"


class RecordCorrection(BaseModel):
    """Structured, immutable audit record of an explicit correction applied to an exception."""

    model_config = ConfigDict(frozen=True)

    correction_id: str = Field(..., description="Unique deterministic or generated correction identifier")
    exception_id: str = Field(..., description="Target exception identifier")
    correction_type: CorrectionType = Field(..., description="Type of correction action")
    field: str = Field(..., description="Field to which the correction applies (canonical or source)")
    corrected_value: Optional[Any] = Field(default=None, description="Corrected value if VALUE_OVERRIDE")
    transformations: list[TransformationStep] = Field(
        default_factory=list,
        description="Approved deterministic transformation steps if APPLY_TRANSFORMATION",
    )
    applied_by: str = Field(..., description="Operator or steward who submitted the correction")
    applied_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp when correction was submitted",
    )
    reason: str = Field(..., description="Audit reason or rationale for the correction")


class ReprocessingResult(BaseModel):
    """Structured result of a single deterministic reprocessing attempt for an exception."""

    model_config = ConfigDict(frozen=True)

    attempt_id: str = Field(..., description="Unique identifier for the reprocessing execution attempt")
    exception_id: str = Field(..., description="Target exception identifier")
    attempted_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp of the attempt",
    )
    reprocessed_by: str = Field(default="system", description="Actor or service triggering the attempt")
    success: bool = Field(..., description="True if record successfully passed all deterministic validation")
    errors: list[RecordValidationError] = Field(
        default_factory=list,
        description="Validation findings remaining after reprocessing attempt",
    )
    canonical_record: Optional[CanonicalCustomerRecord] = Field(
        default=None,
        description="Validated canonical customer record if reprocessing succeeded",
    )


# Rules that are non-dismissible by default to protect canonical invariants
DEFAULT_NON_DISMISSIBLE_RULES: set[str] = {
    "CUSTOMER_ID_REQUIRED",
    "DUPLICATE_CUSTOMER_ID",
    "REQUIRED_FIELD_MISSING",
}


class CustomerRecordException(BaseModel):
    """Durable, auditable record-level exception representing a deterministic failure."""

    model_config = ConfigDict(frozen=True)

    exception_id: str = Field(..., description="Stable deterministic exception identifier")
    run_id: Optional[str] = Field(default=None, description="Optional onboarding run reference for lineage")
    source_id: str = Field(..., description="Source system identifier")
    source_record_id: Optional[str] = Field(default=None, description="Source record key if available")
    row_index: int = Field(..., description="0-indexed position in the original source batch")
    rule_id: str = Field(..., description="Stable deterministic rule identifier (e.g. EMAIL_FORMAT, STATUS_ENUM)")
    category: ExceptionCategory = Field(..., description="Category of the failure")
    field: str = Field(..., description="Canonical or source field associated with the failure")
    observed_value: Optional[str] = Field(
        default=None,
        description="Safe/masked string representation of observed value",
    )
    expected: Optional[str] = Field(default=None, description="Deterministic expected condition")
    reason: str = Field(..., description="Deterministic explanation of why the rule failed")
    suggested_fix: Optional[str] = Field(default=None, description="Actionable suggestion for operator review")
    status: ExceptionStatus = Field(default=ExceptionStatus.OPEN, description="Current lifecycle status")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC creation timestamp",
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp of last status or data update",
    )
    resolved_at: Optional[datetime] = Field(default=None, description="UTC timestamp when resolved")
    dismissed_at: Optional[datetime] = Field(default=None, description="UTC timestamp when dismissed")
    dismissed_by: Optional[str] = Field(default=None, description="Operator who dismissed the exception")
    dismissal_reason: Optional[str] = Field(default=None, description="Rationale for dismissal")
    source_schema_version: Optional[int] = Field(default=None, description="Source schema version for lineage")
    canonical_schema_version: int = Field(default=1, description="Target canonical schema version")
    mapping_version_id: str = Field(..., description="Approved mapping version ID used when error occurred")
    raw_record: dict[str, Any] = Field(
        default_factory=dict,
        description="Original raw source row dictionary preserved for replay and audit",
    )
    corrections: list[RecordCorrection] = Field(
        default_factory=list,
        description="Audit list of explicit corrections applied to this exception",
    )
    reprocessing_history: list[ReprocessingResult] = Field(
        default_factory=list,
        description="Traceable history of all reprocessing attempts",
    )

    def with_correction(self, correction: RecordCorrection) -> CustomerRecordException:
        """Record an explicit correction and advance lifecycle to CORRECTED."""
        if self.status in (ExceptionStatus.RESOLVED, ExceptionStatus.DISMISSED):
            raise InvalidExceptionStateTransitionError(
                f"Cannot apply correction to exception '{self.exception_id}' in state '{self.status}'.",
                details={"exception_id": self.exception_id, "current_status": self.status.value},
            )

        now = datetime.now(timezone.utc)
        return self.model_copy(
            update={
                "status": ExceptionStatus.CORRECTED,
                "updated_at": now,
                "corrections": [*self.corrections, correction],
            }
        )

    def with_reprocessing(self, result: ReprocessingResult) -> CustomerRecordException:
        """Record a reprocessing outcome, transitioning to RESOLVED on success or REPROCESSED on failure."""
        if self.status not in (ExceptionStatus.CORRECTED, ExceptionStatus.REPROCESSED, ExceptionStatus.OPEN):
            raise InvalidExceptionStateTransitionError(
                f"Cannot reprocess exception '{self.exception_id}' in terminal state '{self.status}'.",
                details={"exception_id": self.exception_id, "current_status": self.status.value},
            )

        now = datetime.now(timezone.utc)
        if result.success:
            return self.model_copy(
                update={
                    "status": ExceptionStatus.RESOLVED,
                    "updated_at": now,
                    "resolved_at": result.attempted_at,
                    "reprocessing_history": [*self.reprocessing_history, result],
                }
            )
        else:
            return self.model_copy(
                update={
                    "status": ExceptionStatus.REPROCESSED,
                    "updated_at": now,
                    "resolved_at": None,
                    "reprocessing_history": [*self.reprocessing_history, result],
                }
            )

    def with_dismissal(
        self,
        dismissed_by: str,
        reason: str,
        non_dismissible_rules: Optional[set[str]] = None,
    ) -> CustomerRecordException:
        """Dismiss the exception if permitted by policy."""
        if self.status == ExceptionStatus.RESOLVED:
            raise InvalidExceptionStateTransitionError(
                f"Cannot dismiss an already resolved exception '{self.exception_id}'.",
                details={"exception_id": self.exception_id, "current_status": self.status.value},
            )

        blocked_rules = non_dismissible_rules if non_dismissible_rules is not None else DEFAULT_NON_DISMISSIBLE_RULES
        if self.rule_id in blocked_rules:
            raise NonDismissibleExceptionError(
                f"Exception '{self.exception_id}' with rule '{self.rule_id}' is non-dismissible under active policy.",
                details={"exception_id": self.exception_id, "rule_id": self.rule_id, "field": self.field},
            )

        now = datetime.now(timezone.utc)
        return self.model_copy(
            update={
                "status": ExceptionStatus.DISMISSED,
                "updated_at": now,
                "dismissed_at": now,
                "dismissed_by": dismissed_by,
                "dismissal_reason": reason,
            }
        )

    @property
    def is_resolved(self) -> bool:
        """True if the exception has reached the terminal RESOLVED state."""
        return self.status == ExceptionStatus.RESOLVED

    @property
    def record_id(self) -> Optional[str]:
        """Convenience alias for source_record_id."""
        return self.source_record_id

    @property
    def correction_history(self) -> list[RecordCorrection]:
        """Convenience alias for corrections."""
        return self.corrections



class ExceptionBatch(BaseModel):
    """Collection of record exceptions for an onboarding run, with deterministic artifact persistence."""

    model_config = ConfigDict(frozen=True)

    batch_id: str = Field(..., description="Batch identifier")
    source_id: str = Field(..., description="Source system identifier")
    run_id: Optional[str] = Field(default=None, description="Onboarding run reference")
    mapping_version_id: str = Field(..., description="Mapping version identifier")
    exceptions: list[CustomerRecordException] = Field(default_factory=list, description="Record exceptions")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC creation timestamp",
    )

    def save_artifact(self, path: Path | str) -> Path:
        """Persist exception batch as a deterministic JSON artifact."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        content = self.model_dump_json(indent=2)
        p.write_text(content, encoding="utf-8")
        return p

    @classmethod
    def load_artifact(cls, path: Path | str) -> ExceptionBatch:
        """Load an exception batch artifact from JSON."""
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Exception batch artifact not found at: {p}")
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls.model_validate(data)
