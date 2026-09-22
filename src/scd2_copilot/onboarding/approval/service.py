"""Mapping review session, governance invariants, immutable versioning, and artifact persistence."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Optional, Sequence

from ..canonical import CanonicalSchema, get_canonical_customer_v1
from ..exceptions import (
    DuplicateTargetMappingError,
    IncompleteMappingError,
    InvalidSourceFieldError,
    InvalidTargetFieldError,
    InvalidTransformationError,
    MappingApprovalError,
    UnresolvedAmbiguityError,
)
from ..models.approval import (
    ApprovedMappingDefinition,
    ApprovedMappingVersion,
    FieldReviewDecision,
    ReviewDecisionType,
)
from ..models.mapping import (
    FieldMappingProposal,
    MappingProposalBatch,
    MappingType,
    TransformationOpType,
    TransformationStep,
)
from ..models.schema_snapshot import SourceSchemaSnapshot

logger = logging.getLogger("scd2_copilot.onboarding.approval")


class MappingReviewSession:
    """Stateful interactive review session for evaluating and deciding upon mapping proposals."""

    def __init__(
        self,
        proposal_batch: MappingProposalBatch,
        canonical_schema: CanonicalSchema,
        source_schema: Optional[SourceSchemaSnapshot] = None,
    ) -> None:
        self.proposal_batch = proposal_batch
        self.canonical_schema = canonical_schema
        self.source_schema = source_schema
        self._proposals_by_source: dict[str, FieldMappingProposal] = {
            p.source_field: p for p in proposal_batch.proposals
        }
        self._decisions: dict[str, FieldReviewDecision] = {}

    @property
    def proposals(self) -> list[FieldMappingProposal]:
        """Return list of all proposals in the batch."""
        return self.proposal_batch.proposals

    @property
    def decisions(self) -> dict[str, FieldReviewDecision]:
        """Return all recorded field review decisions."""
        return dict(self._decisions)

    @property
    def is_fully_reviewed(self) -> bool:
        """True if every proposed source field has an explicit review decision."""
        return all(p.source_field in self._decisions for p in self.proposal_batch.proposals)

    def get_decision(self, source_field: str) -> Optional[FieldReviewDecision]:
        """Return decision for a specific source field if recorded."""
        return self._decisions.get(source_field)

    def get_unresolved_proposals(self) -> list[FieldMappingProposal]:
        """Return proposals that either lack a decision or remain ambiguous without resolution."""
        unresolved: list[FieldMappingProposal] = []
        for p in self.proposal_batch.proposals:
            if p.source_field not in self._decisions:
                unresolved.append(p)
            elif p.is_ambiguous or p.mapping_type == MappingType.AMBIGUOUS:
                decision = self._decisions[p.source_field]
                if decision.decision == ReviewDecisionType.APPROVE:
                    unresolved.append(p)
        return unresolved

    def apply_decision(self, decision: FieldReviewDecision) -> None:
        """Apply a human review decision to a source field with strict validation."""
        if decision.source_field not in self._proposals_by_source:
            raise InvalidSourceFieldError(
                f"Source field '{decision.source_field}' does not exist in the proposal batch for source '{self.proposal_batch.source_id}'.",
                details={"source_field": decision.source_field, "available_fields": list(self._proposals_by_source.keys())},
            )

        proposal = self._proposals_by_source[decision.source_field]

        if decision.decision == ReviewDecisionType.APPROVE:
            # Cannot approve ambiguous proposal without explicit override or reject
            if proposal.is_ambiguous or proposal.mapping_type == MappingType.AMBIGUOUS:
                raise UnresolvedAmbiguityError(
                    f"Cannot directly approve ambiguous proposal for '{decision.source_field}'. "
                    f"Conflicting candidates: {proposal.conflicting_targets}. "
                    "Reviewer must resolve ambiguity using OVERRIDE or REJECT.",
                    details={"source_field": decision.source_field, "conflicting_targets": proposal.conflicting_targets},
                )

            # Target field and transformations are inherited from the proposal
            target = proposal.target_field
            if target is not None:
                if target not in self.canonical_schema.field_names:
                    raise InvalidTargetFieldError(
                        f"Proposed target field '{target}' is not valid in canonical schema '{self.canonical_schema.schema_name}.v{self.canonical_schema.version}'.",
                        details={"target_field": target},
                    )

            # Record decision with proposal's target and transformations
            self._decisions[decision.source_field] = FieldReviewDecision(
                source_field=decision.source_field,
                decision=ReviewDecisionType.APPROVE,
                target_field=target,
                transformations=list(proposal.transformations),
                reviewer=decision.reviewer,
                review_notes=decision.review_notes,
                decided_at=decision.decided_at,
            )

        elif decision.decision == ReviewDecisionType.OVERRIDE:
            # Overriding allows changing the target field and/or transformations
            target = decision.target_field
            if target is not None and target not in self.canonical_schema.field_names:
                raise InvalidTargetFieldError(
                    f"Override target field '{target}' does not exist in canonical schema '{self.canonical_schema.schema_name}.v{self.canonical_schema.version}'.",
                    details={"target_field": target, "valid_targets": self.canonical_schema.field_names},
                )

            # Validate transformations against approved vocabulary
            for step in decision.transformations:
                if not isinstance(step.op, TransformationOpType):
                    try:
                        step.op = TransformationOpType(str(step.op).strip().upper())
                    except ValueError:
                        raise InvalidTransformationError(
                            f"Unsupported transformation operation '{step.op}' in override for '{decision.source_field}'. "
                            f"Allowed vocabulary: {[op.value for op in TransformationOpType]}.",
                            details={"invalid_op": str(step.op)},
                        )

            self._decisions[decision.source_field] = decision

        elif decision.decision == ReviewDecisionType.REJECT:
            # Rejection marks field as unmapped with empty transformations
            self._decisions[decision.source_field] = FieldReviewDecision(
                source_field=decision.source_field,
                decision=ReviewDecisionType.REJECT,
                target_field=None,
                transformations=[],
                reviewer=decision.reviewer,
                review_notes=decision.review_notes,
                decided_at=decision.decided_at,
            )

    def approve_all_unambiguous(
        self,
        reviewer: str,
        review_notes: Optional[str] = "Bulk approved unambiguous proposal.",
    ) -> list[str]:
        """Approve all proposals that are unambiguous and have not yet been decided.

        Returns list of source fields that were approved.
        """
        approved_fields: list[str] = []
        for proposal in self.proposal_batch.proposals:
            if proposal.source_field in self._decisions:
                continue

            if proposal.is_ambiguous or proposal.mapping_type == MappingType.AMBIGUOUS:
                continue

            decision = FieldReviewDecision(
                source_field=proposal.source_field,
                decision=ReviewDecisionType.APPROVE,
                target_field=proposal.target_field,
                transformations=list(proposal.transformations),
                reviewer=reviewer,
                review_notes=review_notes,
            )
            self.apply_decision(decision)
            approved_fields.append(proposal.source_field)

        return approved_fields


class MappingReviewService:
    """Service governing review sessions, deterministic approval invariants, and immutable mapping versions."""

    def __init__(self, default_canonical_schema: Optional[CanonicalSchema] = None) -> None:
        self.default_canonical_schema = default_canonical_schema or get_canonical_customer_v1()

    def create_session(
        self,
        proposal_batch: MappingProposalBatch,
        canonical_schema: Optional[CanonicalSchema] = None,
        source_schema: Optional[SourceSchemaSnapshot] = None,
    ) -> MappingReviewSession:
        """Create a new interactive review session for a mapping proposal batch."""
        schema = canonical_schema or self.default_canonical_schema
        return MappingReviewSession(
            proposal_batch=proposal_batch,
            canonical_schema=schema,
            source_schema=source_schema,
        )

    def finalize_version(
        self,
        session: MappingReviewSession,
        approved_by: str,
        version_number: int = 1,
        source_schema_version: int = 1,
        mapping_version_id: Optional[str] = None,
        allow_incomplete: bool = False,
        supersedes_version_id: Optional[str] = None,
    ) -> ApprovedMappingVersion:
        """Enforce all approval invariants and produce a frozen, immutable ApprovedMappingVersion."""
        # Invariant 1: Completeness of review
        unreviewed = [
            p.source_field for p in session.proposal_batch.proposals
            if p.source_field not in session.decisions
        ]
        if unreviewed:
            raise MappingApprovalError(
                f"Cannot finalize mapping version: the following source fields have not been reviewed: {unreviewed}.",
                details={"unreviewed_fields": unreviewed},
            )

        # Invariant 2: No unresolved ambiguity
        unresolved_ambiguity = session.get_unresolved_proposals()
        if unresolved_ambiguity:
            ambiguous_fields = [p.source_field for p in unresolved_ambiguity]
            raise UnresolvedAmbiguityError(
                f"Cannot finalize mapping version with unresolved ambiguous proposals: {ambiguous_fields}. "
                "Each ambiguous proposal must be explicitly resolved with OVERRIDE or REJECT.",
                details={"ambiguous_fields": ambiguous_fields},
            )

        # Invariant 3: Validate canonical targets and detect duplicate target mappings
        target_to_sources: dict[str, list[str]] = {}
        valid_canonical_names = set(session.canonical_schema.field_names)

        for source_field, dec in session.decisions.items():
            if dec.target_field is not None:
                if dec.target_field not in valid_canonical_names:
                    raise InvalidTargetFieldError(
                        f"Approved target field '{dec.target_field}' does not exist in canonical schema.",
                        details={"target_field": dec.target_field},
                    )
                target_to_sources.setdefault(dec.target_field, []).append(source_field)

        # Invariant 4: No multiple source fields mapping to the same canonical target
        duplicate_targets = {
            target: sources
            for target, sources in target_to_sources.items()
            if len(sources) > 1
        }
        if duplicate_targets:
            raise DuplicateTargetMappingError(
                f"Duplicate canonical target mapping detected: {duplicate_targets}. "
                "Multiple source columns cannot map to the same canonical target field.",
                details={"duplicate_targets": duplicate_targets},
            )

        # Invariant 5: Required canonical field completeness
        required_canonical = [f.name for f in session.canonical_schema.get_required_fields()]
        mapped_targets = set(target_to_sources.keys())
        unmapped_required = [req for req in required_canonical if req not in mapped_targets]

        if unmapped_required and not allow_incomplete:
            raise IncompleteMappingError(
                f"Cannot finalize mapping version: Required canonical fields are not mapped: {unmapped_required}. "
                "To create a partial/draft mapping version, explicitly set allow_incomplete=True.",
                details={"unmapped_required": unmapped_required},
            )

        # Invariant 6: Assemble immutable ApprovedMappingDefinitions
        approved_mappings: list[ApprovedMappingDefinition] = []
        for prop in session.proposal_batch.proposals:
            dec = session.decisions[prop.source_field]

            if dec.decision == ReviewDecisionType.APPROVE:
                target = dec.target_field
                steps = dec.transformations
                m_type = MappingType.UNMAPPED if target is None else (
                    MappingType.TRANSFORMED if steps else MappingType.DIRECT
                )
                confidence = prop.confidence
                reason = f"Approved proposal: {prop.reason}"

            elif dec.decision == ReviewDecisionType.OVERRIDE:
                target = dec.target_field
                steps = dec.transformations
                m_type = MappingType.UNMAPPED if target is None else (
                    MappingType.TRANSFORMED if steps else MappingType.DIRECT
                )
                confidence = 1.0  # Human authoritative override
                notes = f" ({dec.review_notes})" if dec.review_notes else ""
                reason = f"Human override by {dec.reviewer}{notes}"

            elif dec.decision == ReviewDecisionType.REJECT:
                target = None
                steps = []
                m_type = MappingType.UNMAPPED
                confidence = 1.0
                notes = f" ({dec.review_notes})" if dec.review_notes else ""
                reason = f"Rejected by {dec.reviewer}{notes}"

            approved_mappings.append(
                ApprovedMappingDefinition(
                    source_field=prop.source_field,
                    target_field=target,
                    mapping_type=m_type,
                    transformations=steps,
                    decision=dec.decision,
                    reviewer=dec.reviewer,
                    review_notes=dec.review_notes,
                    confidence=confidence,
                    provenance_reason=reason,
                )
            )

        ver_id = mapping_version_id or f"map_ver_{session.proposal_batch.source_id}_v{version_number}"

        return ApprovedMappingVersion(
            mapping_version_id=ver_id,
            source_id=session.proposal_batch.source_id,
            source_fingerprint=session.proposal_batch.source_fingerprint,
            source_schema_version=source_schema_version,
            canonical_schema_name=session.canonical_schema.schema_name,
            canonical_schema_version=session.canonical_schema.version,
            version_number=version_number,
            mappings=approved_mappings,
            is_complete=len(unmapped_required) == 0,
            unmapped_required_fields=unmapped_required,
            approved_by=approved_by,
            approved_at=datetime.now(timezone.utc),
            supersedes_version_id=supersedes_version_id,
        )

    def revise_version(
        self,
        existing_version: ApprovedMappingVersion,
        canonical_schema: Optional[CanonicalSchema] = None,
    ) -> MappingReviewSession:
        """Create a new review session seeded from an existing approved mapping version for revision."""
        schema = canonical_schema or self.default_canonical_schema

        # Convert approved definitions into proposals
        proposals: list[FieldMappingProposal] = []
        for m in existing_version.mappings:
            proposals.append(
                FieldMappingProposal(
                    source_field=m.source_field,
                    target_field=m.target_field,
                    mapping_type=m.mapping_type,
                    confidence=m.confidence,
                    reason=f"Carried over from {existing_version.mapping_version_id}: {m.provenance_reason}",
                    transformations=list(m.transformations),
                )
            )

        mapped_targets = {m.target_field for m in existing_version.mappings if m.target_field is not None}
        unmapped_req = [req.name for req in schema.get_required_fields() if req.name not in mapped_targets]

        proposal_batch = MappingProposalBatch(
            source_id=existing_version.source_id,
            source_fingerprint=existing_version.source_fingerprint,
            canonical_schema_name=existing_version.canonical_schema_name,
            canonical_schema_version=existing_version.canonical_schema_version,
            proposals=proposals,
            unmapped_canonical_fields=unmapped_req,
            provider_used=f"revision_from_{existing_version.mapping_version_id}",
            is_fallback=False,
            created_at=datetime.now(timezone.utc),
        )

        session = self.create_session(proposal_batch, canonical_schema=schema)

        # Pre-populate decisions from existing approved definitions
        for m in existing_version.mappings:
            session.apply_decision(
                FieldReviewDecision(
                    source_field=m.source_field,
                    decision=ReviewDecisionType.APPROVE,
                    target_field=m.target_field,
                    transformations=list(m.transformations),
                    reviewer=existing_version.approved_by,
                    review_notes=f"Retained from {existing_version.mapping_version_id}",
                )
            )

        return session

    @staticmethod
    def save_version_artifact(version: ApprovedMappingVersion, path: Path | str) -> Path:
        """Persist approved mapping version as a deterministic JSON artifact."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        content = version.model_dump_json(indent=2)
        p.write_text(content, encoding="utf-8")
        return p

    @staticmethod
    def load_version_artifact(path: Path | str) -> ApprovedMappingVersion:
        """Load and validate an approved mapping version from a JSON artifact."""
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Mapping version artifact not found at: {p}")
        data = json.loads(p.read_text(encoding="utf-8"))
        return ApprovedMappingVersion.model_validate(data)
