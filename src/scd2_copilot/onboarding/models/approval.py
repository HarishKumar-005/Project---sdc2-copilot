"""Human review decision models and immutable approved mapping version contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field

from .mapping import MappingType, TransformationStep


class ReviewDecisionType(str, Enum):
    """Types of human review decisions on semantic mapping proposals."""

    APPROVE = "APPROVE"    # Accept the AI or deterministic proposal as-is
    REJECT = "REJECT"      # Reject the mapping (source field remains unmapped)
    OVERRIDE = "OVERRIDE"  # Human overrides target field, transformations, or both


class FieldReviewDecision(BaseModel):
    """A human reviewer's explicit decision for a single source field."""

    source_field: str = Field(..., description="Source column name being reviewed")
    decision: ReviewDecisionType = Field(..., description="APPROVE, REJECT, or OVERRIDE")
    target_field: Optional[str] = Field(
        default=None,
        description="Canonical target field name (None if rejected or explicitly unmapped)",
    )
    transformations: list[TransformationStep] = Field(
        default_factory=list,
        description="Transformations to execute (original proposal if approved, custom if overridden)",
    )
    reviewer: str = Field(..., description="Identifier of the human reviewer (e.g. username or steward ID)")
    review_notes: Optional[str] = Field(default=None, description="Auditable reasoning or context for the decision")
    decided_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timestamp when the review decision was made",
    )


class ApprovedMappingDefinition(BaseModel):
    """The auditable, approved mapping definition for a single source field."""

    source_field: str = Field(..., description="Source column name")
    target_field: Optional[str] = Field(
        default=None,
        description="Approved canonical target field name, or None if unmapped",
    )
    mapping_type: MappingType = Field(
        ...,
        description="Resulting mapping type: DIRECT, TRANSFORMED, or UNMAPPED",
    )
    transformations: list[TransformationStep] = Field(
        default_factory=list,
        description="Approved deterministic transformation steps",
    )
    decision: ReviewDecisionType = Field(
        ...,
        description="Decision type that established this mapping",
    )
    reviewer: str = Field(..., description="User who approved or established this mapping")
    review_notes: Optional[str] = Field(default=None, description="Review notes or justification")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Proposal confidence or 1.0 for human overrides")
    provenance_reason: str = Field(..., description="Summary of proposal rationale and review outcome")


class ApprovedMappingVersion(BaseModel):
    """An immutable, auditable, versioned mapping configuration ready for transformation."""

    model_config = ConfigDict(frozen=True)

    mapping_version_id: str = Field(..., description="Unique mapping version identifier (e.g. map_ver_crm_v1)")
    source_id: str = Field(..., description="Source system identifier")
    source_fingerprint: str = Field(..., description="Fingerprint hash of the source schema snapshot")
    source_schema_version: int = Field(default=1, description="Observed source schema version")
    canonical_schema_name: str = Field(default="customer", description="Canonical entity name")
    canonical_schema_version: int = Field(default=1, description="Target canonical schema version")
    version_number: int = Field(default=1, description="Sequential version integer (1, 2, ...)")
    mappings: list[ApprovedMappingDefinition] = Field(
        default_factory=list,
        description="Approved field-level mapping definitions",
    )
    is_complete: bool = Field(
        default=False,
        description="True if all required canonical fields have active mappings",
    )
    unmapped_required_fields: list[str] = Field(
        default_factory=list,
        description="List of mandatory canonical fields missing an active mapping",
    )
    approved_by: str = Field(..., description="Operator or steward who approved this version")
    approved_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timestamp when version was approved and frozen",
    )
    supersedes_version_id: Optional[str] = Field(
        default=None,
        description="Identifier of the previous mapping version if this is a revision",
    )

    def get_mapping_for_source(self, source_field: str) -> Optional[ApprovedMappingDefinition]:
        """Look up approved mapping definition by source column name."""
        for m in self.mappings:
            if m.source_field == source_field:
                return m
        return None

    def get_mapping_for_target(self, target_field: str) -> Optional[ApprovedMappingDefinition]:
        """Look up active approved mapping definition by canonical target field name."""
        for m in self.mappings:
            if m.target_field == target_field:
                return m
        return None

    @property
    def version_id(self) -> str:
        """Alias for mapping_version_id."""
        return self.mapping_version_id

    @property
    def version_str(self) -> str:
        """Convenience formatted version string."""
        return f"v{self.version_number}"

    @property
    def content_hash(self) -> str:
        """Convenience alias for source_fingerprint."""
        return self.source_fingerprint

    def get_active_mappings(self) -> list[ApprovedMappingDefinition]:
        """Return all mappings that actively map a source field to a canonical target."""
        return [m for m in self.mappings if m.target_field is not None]

