"""Schema drift detection and mapping impact analysis domain models."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import json
from pathlib import Path
from typing import Any, Optional, Union
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .schema_snapshot import ColumnSnapshot


class DriftType(str, Enum):
    """Categorization of structural changes between source schema versions."""

    ADDED_COLUMN = "ADDED_COLUMN"
    REMOVED_COLUMN = "REMOVED_COLUMN"
    TYPE_CHANGED = "TYPE_CHANGED"
    NULLABILITY_CHANGED = "NULLABILITY_CHANGED"
    POSSIBLE_RENAME = "POSSIBLE_RENAME"


class MappingCompatibilityState(str, Enum):
    """Deterministic compatibility classification of an approved mapping against drifted schema."""

    COMPATIBLE = "COMPATIBLE"                          # No mapping dependencies broken or affected
    COMPATIBLE_WITH_REVIEW = "COMPATIBLE_WITH_REVIEW"  # Safe to use, but changes warrant review
    BROKEN = "BROKEN"                                  # Required mapping dependency is invalid or missing


class SchemaDriftEvent(BaseModel):
    """A single deterministic schema change detected between schema snapshots."""

    drift_type: DriftType = Field(..., description="Category of drift")
    field_name: str = Field(..., description="Canonical or primary column name involved")
    old_field_name: Optional[str] = Field(default=None, description="Column name in prior schema")
    new_field_name: Optional[str] = Field(default=None, description="Column name in current schema")
    old_type: Optional[str] = Field(default=None, description="Inferred/Polars type in prior schema")
    new_type: Optional[str] = Field(default=None, description="Inferred/Polars type in current schema")
    old_nullable: Optional[bool] = Field(default=None, description="Nullable status in prior schema")
    new_nullable: Optional[bool] = Field(default=None, description="Nullable status in current schema")
    details: str = Field(..., description="Factual description of the detected change")
    similarity_score: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Similarity score if drift represents a possible rename",
    )
    matched_signals: list[str] = Field(
        default_factory=list,
        description="Diagnostic heuristics that supported the change classification",
    )

    @property
    def event_type(self) -> DriftType:
        """Convenience alias for drift_type."""
        return self.drift_type

    @property
    def column_name(self) -> str:
        """Convenience alias for field_name."""
        return self.field_name

    @property
    def prior_type(self) -> Optional[str]:
        """Convenience alias for old_type."""
        return self.old_type

    @property
    def current_type(self) -> Optional[str]:
        """Convenience alias for new_type."""
        return self.new_type



class FieldMappingImpact(BaseModel):
    """Impact analysis of schema drift on a single approved field mapping definition."""

    source_field: str = Field(..., description="Source column referenced by the mapping")
    target_field: Optional[str] = Field(default=None, description="Canonical target field")
    mapping_version_id: str = Field(..., description="Approved mapping version identifier")
    impact_category: DriftType = Field(..., description="Type of drift that caused this impact")
    compatibility: MappingCompatibilityState = Field(..., description="Compatibility state for this field")
    reason: str = Field(..., description="Deterministic explanation of why mapping is compatible or broken")
    replacement_candidate: Optional[str] = Field(
        default=None,
        description="Suggested replacement source column if rename detected",
    )
    replacement_confidence: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Confidence in replacement candidate",
    )

    @property
    def canonical_target(self) -> Optional[str]:
        """Convenience alias for target_field."""
        return self.target_field



class SchemaDiffResult(BaseModel):
    """Comparison result between two source schema snapshots."""

    source_id: str = Field(..., description="Source system identifier")
    prior_schema_version: int = Field(..., description="Prior schema snapshot version")
    current_schema_version: int = Field(..., description="Current schema snapshot version")
    prior_fingerprint: str = Field(..., description="Prior schema snapshot fingerprint hash")
    current_fingerprint: str = Field(..., description="Current schema snapshot fingerprint hash")
    has_drift: bool = Field(default=False, description="True if any drift events were detected")
    drift_events: list[SchemaDriftEvent] = Field(
        default_factory=list,
        description="List of detected drift events, sorted deterministically",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timestamp when diff was computed",
    )


class SchemaDriftReport(BaseModel):
    """Complete evaluation report linking schema drift to mapping impact."""

    model_config = ConfigDict(frozen=True)

    report_id: str = Field(
        default_factory=lambda: f"drift_{uuid4().hex[:12]}",
        description="Unique identifier for this drift evaluation report",
    )
    source_id: str = Field(..., description="Source system identifier")
    prior_schema_version: int = Field(..., description="Prior source schema version")
    current_schema_version: int = Field(..., description="Current source schema version")
    prior_fingerprint: str = Field(..., description="Prior source schema fingerprint")
    current_fingerprint: str = Field(..., description="Current source schema fingerprint")
    mapping_version_id: str = Field(..., description="Evaluated approved mapping version ID")
    canonical_schema_version: int = Field(default=1, description="Target canonical schema version")
    drift_events: list[SchemaDriftEvent] = Field(
        default_factory=list,
        description="All detected schema drift events",
    )
    impacted_mappings: list[FieldMappingImpact] = Field(
        default_factory=list,
        description="Field-level mapping impacts",
    )
    overall_compatibility: MappingCompatibilityState = Field(
        ...,
        description="Overall compatibility classification for the mapping against new schema",
    )
    review_required: bool = Field(
        default=False,
        description="True if human review is required before executing mapping against new schema",
    )
    summary_reason: str = Field(
        ...,
        description="Summary explanation of drift impact and required operational action",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timestamp when report was generated",
    )

    def save_artifact(self, path: Union[str, Path]) -> Path:
        """Persist report to a JSON artifact file."""
        target_path = Path(path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.model_dump(mode="json")
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        return target_path

    @classmethod
    def load_artifact(cls, path: Union[str, Path]) -> SchemaDriftReport:
        """Load report from a JSON artifact file."""
        target_path = Path(path)
        with open(target_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls(**data)
