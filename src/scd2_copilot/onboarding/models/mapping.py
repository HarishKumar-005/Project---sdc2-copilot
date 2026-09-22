"""Mapping proposal contracts, transformation operations, and structured output models."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field


class TransformationOpType(str, Enum):
    """Constrained, approved transformation vocabulary."""

    TRIM = "TRIM"
    LOWERCASE = "LOWERCASE"
    UPPERCASE = "UPPERCASE"
    CAST = "CAST"
    PARSE_DATE = "PARSE_DATE"
    NORMALIZE_EMAIL = "NORMALIZE_EMAIL"
    MAP_ENUM = "MAP_ENUM"
    CONCAT = "CONCAT"


class TransformationStep(BaseModel):
    """A single constrained deterministic transformation step."""

    op: TransformationOpType = Field(..., description="Approved operation type")
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured arguments for the operation (e.g. format, enum_map, target_type)",
    )


class MappingType(str, Enum):
    """Classification of the field-level semantic mapping relationship."""

    DIRECT = "DIRECT"              # Exact 1-to-1 without value modifications
    TRANSFORMED = "TRANSFORMED"    # Requires constrained transformation steps
    UNMAPPED = "UNMAPPED"          # Source column does not map to any canonical target
    AMBIGUOUS = "AMBIGUOUS"        # Plausible multiple candidate matches requiring human review


class DeterministicEvidence(BaseModel):
    """Heuristic evidence gathered from source profiling and metadata."""

    name_similarity_score: float = Field(..., ge=0.0, le=1.0, description="Token/string similarity ratio")
    type_compatible: bool = Field(..., description="Whether source inferred type is compatible with canonical target")
    uniqueness_compatible: bool = Field(..., description="Whether source uniqueness meets canonical requirement")
    matched_signals: list[str] = Field(default_factory=list, description="Diagnostic signals (e.g. email_regex, id_token)")


class CandidateMapping(BaseModel):
    """A candidate mapping proposed by the deterministic heuristic generator."""

    source_field: str = Field(..., description="Source column original_name")
    candidate_target_field: str = Field(..., description="Canonical field name")
    evidence: DeterministicEvidence = Field(..., description="Deterministic scoring evidence")
    heuristic_confidence: float = Field(..., ge=0.0, le=1.0, description="Heuristic score (0.0 to 1.0)")
    suggested_transformations: list[TransformationStep] = Field(
        default_factory=list,
        description="Suggested transformations inferred from types and values",
    )


class FieldMappingProposal(BaseModel):
    """A complete semantic proposal for a single source field."""

    model_config = ConfigDict(frozen=True)

    source_field: str = Field(..., description="Source column original_name")
    target_field: Optional[str] = Field(default=None, description="Proposed canonical field name, or None if unmapped")
    mapping_type: MappingType = Field(..., description="DIRECT, TRANSFORMED, UNMAPPED, or AMBIGUOUS")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence score as a workflow signal")
    reason: str = Field(..., description="Human-readable explanation of why this mapping is proposed")
    transformations: list[TransformationStep] = Field(
        default_factory=list,
        description="Constrained operations to apply to this source field",
    )
    deterministic_evidence: Optional[DeterministicEvidence] = Field(
        default=None,
        description="Heuristic signals that supported this proposal",
    )
    is_ambiguous: bool = Field(default=False, description="True if multiple conflicting targets are plausible")
    conflicting_targets: list[str] = Field(
        default_factory=list,
        description="Alternative candidate targets if ambiguous",
    )

    @property
    def canonical_target(self) -> Optional[str]:
        """Convenience alias for target_field."""
        return self.target_field



class MappingProposalBatch(BaseModel):
    """The complete batch of mapping proposals for an ingested source."""

    model_config = ConfigDict(frozen=True)

    source_id: str = Field(..., description="Source system identifier")
    source_fingerprint: str = Field(..., description="Schema fingerprint hash of the profiled source")
    canonical_schema_name: str = Field(..., description="Canonical schema name (e.g. customer)")
    canonical_schema_version: int = Field(..., description="Canonical schema version")
    proposals: list[FieldMappingProposal] = Field(
        default_factory=list,
        description="Proposals for each source column",
    )
    unmapped_canonical_fields: list[str] = Field(
        default_factory=list,
        description="Required canonical fields that received no mapping proposal",
    )
    provider_used: str = Field(..., description="LLM provider name or deterministic fallback")
    is_fallback: bool = Field(default=False, description="Whether fallback logic was activated")
    fallback_reason: Optional[str] = Field(default=None, description="Reason if fallback was triggered")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp when proposal batch was generated",
    )

    def get_proposal_for_source(self, source_field: str) -> Optional[FieldMappingProposal]:
        """Look up proposal by source field name."""
        for p in self.proposals:
            if p.source_field == source_field:
                return p
        return None

    def get_proposals_for_target(self, target_field: str) -> list[FieldMappingProposal]:
        """Look up all proposals targeting a specific canonical field."""
        return [p for p in self.proposals if p.target_field == target_field]


# ── Structured Output Models for GenAI ─────────────────────────

class LLMFieldProposal(BaseModel):
    """Pydantic schema for structured output from LLM provider."""

    source_field: str = Field(..., description="Source column name being mapped")
    target_field: Optional[str] = Field(default=None, description="Canonical customer field name or null if unmapped")
    mapping_type: str = Field(..., description="DIRECT, TRANSFORMED, or UNMAPPED")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence score from 0.0 to 1.0")
    reason: str = Field(..., description="Factual explanation referencing source profile evidence")
    operations: list[str] = Field(
        default_factory=list,
        description="List of operation names from approved vocabulary: TRIM, LOWERCASE, UPPERCASE, CAST, PARSE_DATE, NORMALIZE_EMAIL, MAP_ENUM, CONCAT",
    )


class LLMMappingBatchResponse(BaseModel):
    """Complete structured response schema expected from LLM."""

    proposals: list[LLMFieldProposal] = Field(
        default_factory=list,
        description="List of field mapping proposals",
    )
