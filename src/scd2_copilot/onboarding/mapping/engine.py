"""Semantic mapping proposal engine orchestrating deterministic heuristics and GenAI proposals."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from ..canonical import CanonicalSchema, get_canonical_customer_v1
from ...config import Settings, get_settings
from ..models.mapping import (
    CandidateMapping,
    FieldMappingProposal,
    LLMFieldProposal,
    LLMMappingBatchResponse,
    MappingProposalBatch,
    MappingType,
    TransformationOpType,
    TransformationStep,
)
from ..models.profile import DataProfile
from ..models.schema_snapshot import ColumnSnapshot
from .candidates import DeterministicCandidateGenerator
from .prompt import build_mapping_prompt

logger = logging.getLogger("scd2_copilot.onboarding.mapping")


class SemanticMappingEngine:
    """Orchestrates candidate generation and structured GenAI semantic mapping proposals."""

    def __init__(
        self,
        canonical_schema: Optional[CanonicalSchema] = None,
        settings: Optional[Settings] = None,
        llm_caller: Optional[Callable[[str], LLMMappingBatchResponse]] = None,
        enable_ai: bool = True,
    ) -> None:
        self.canonical_schema = canonical_schema or get_canonical_customer_v1()
        self.settings = settings or get_settings()
        self.candidate_generator = DeterministicCandidateGenerator(self.canonical_schema)
        self.llm_caller = llm_caller
        self.enable_ai = enable_ai

    def _call_gemini_structured(self, prompt: str) -> LLMMappingBatchResponse:
        """Call Google GenAI SDK for structured mapping output."""
        from google import genai

        api_key = self.settings.gemini_api_key
        if not api_key:
            raise ValueError("GEMINI_API_KEY is not configured.")

        client = genai.Client(api_key=api_key)
        model_name = self.settings.gemini_model or "gemini-3.8-flash"

        config = {
            "response_mime_type": "application/json",
            "response_schema": LLMMappingBatchResponse,
        }

        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=config,
        )

        if hasattr(response, "parsed") and response.parsed is not None:
            return response.parsed  # type: ignore

        # Fallback to parsing raw json text
        raw_text = getattr(response, "text", "") or ""
        parsed_dict = json.loads(raw_text)
        return LLMMappingBatchResponse.model_validate(parsed_dict)

    def _sanitize_transformations(self, raw_ops: list[str]) -> list[TransformationStep]:
        """Filter and validate proposed transformation operations against the approved vocabulary."""
        valid_steps: list[TransformationStep] = []
        for op_str in raw_ops:
            cleaned = op_str.strip().upper()
            try:
                op_enum = TransformationOpType(cleaned)
                valid_steps.append(TransformationStep(op=op_enum))
            except ValueError:
                logger.warning("Rejected unsupported transformation operation from LLM: %s", op_str)
        return valid_steps

    def propose_mappings(
        self,
        source_id: str,
        columns: Sequence[ColumnSnapshot],
        profile: DataProfile,
    ) -> MappingProposalBatch:
        """Generate semantic mapping proposals combining deterministic evidence and GenAI."""
        # 1. Deterministic candidate generation (always executed as grounding evidence)
        candidates_map = self.candidate_generator.generate_candidates(columns, profile)

        # 2. Attempt GenAI Proposal Generation
        llm_response: Optional[LLMMappingBatchResponse] = None
        provider_used = "deterministic_fallback"
        is_fallback = False
        fallback_reason: Optional[str] = None

        if not self.enable_ai:
            is_fallback = True
            fallback_reason = "Deterministic heuristic engine active (AI disabled)."
        elif self.llm_caller is not None:
            try:
                prompt = build_mapping_prompt(source_id, columns, profile, self.canonical_schema, candidates_map)
                llm_response = self.llm_caller(prompt)
                provider_used = "custom_llm"
            except Exception as e:
                logger.warning("Custom LLM caller failed: %s. Falling back to deterministic candidates.", e)
                is_fallback = True
                fallback_reason = str(e)

        elif self.settings.gemini_api_key:
            try:
                prompt = build_mapping_prompt(source_id, columns, profile, self.canonical_schema, candidates_map)
                llm_response = self._call_gemini_structured(prompt)
                provider_used = f"gemini ({self.settings.gemini_model})"
            except Exception as e:
                logger.warning("Gemini mapping call failed: %s. Falling back to deterministic candidates.", e)
                is_fallback = True
                fallback_reason = str(e)
        else:
            is_fallback = True
            fallback_reason = "No active LLM API key configured; using deterministic heuristic engine."

        # 3. Assemble Proposals
        proposals: list[FieldMappingProposal] = []
        valid_canonical_names = set(self.canonical_schema.field_names)

        if llm_response is not None and llm_response.proposals:
            llm_map = {p.source_field: p for p in llm_response.proposals}

            for col in columns:
                llm_p = llm_map.get(col.original_name)
                cand_list = candidates_map.get(col.original_name, [])
                top_cand = cand_list[0] if cand_list else None

                if llm_p and llm_p.target_field in valid_canonical_names:
                    # Valid LLM target field
                    steps = self._sanitize_transformations(llm_p.operations)
                    # If LLM didn't suggest operations but candidate generator did, preserve candidate steps
                    if not steps and top_cand and top_cand.candidate_target_field == llm_p.target_field:
                        steps = top_cand.suggested_transformations

                    m_type = (
                        MappingType.TRANSFORMED if steps else MappingType.DIRECT
                    )
                    proposals.append(
                        FieldMappingProposal(
                            source_field=col.original_name,
                            target_field=llm_p.target_field,
                            mapping_type=m_type,
                            confidence=round(llm_p.confidence, 2),
                            reason=llm_p.reason,
                            transformations=steps,
                            deterministic_evidence=top_cand.evidence if top_cand else None,
                        )
                    )
                elif llm_p and llm_p.target_field is None:
                    # LLM explicitly decided UNMAPPED
                    proposals.append(
                        FieldMappingProposal(
                            source_field=col.original_name,
                            target_field=None,
                            mapping_type=MappingType.UNMAPPED,
                            confidence=round(llm_p.confidence, 2),
                            reason=llm_p.reason,
                            deterministic_evidence=top_cand.evidence if top_cand else None,
                        )
                    )
                else:
                    # Target invalid or missing from LLM response -> fallback to deterministic top candidate
                    proposals.append(self._build_deterministic_proposal(col, cand_list))
        else:
            # Complete deterministic fallback
            is_fallback = True
            for col in columns:
                cand_list = candidates_map.get(col.original_name, [])
                proposals.append(self._build_deterministic_proposal(col, cand_list))

        # 4. Check for Target Conflicts and Ambiguities
        proposals = self._resolve_conflicts_and_ambiguities(proposals)

        # 5. Check for Unmapped Required Canonical Fields
        mapped_targets = {p.target_field for p in proposals if p.target_field is not None}
        unmapped_required = [
            req.name for req in self.canonical_schema.get_required_fields()
            if req.name not in mapped_targets
        ]

        return MappingProposalBatch(
            source_id=source_id,
            source_fingerprint=profile.fingerprint_hash,
            canonical_schema_name=self.canonical_schema.schema_name,
            canonical_schema_version=self.canonical_schema.version,
            proposals=proposals,
            unmapped_canonical_fields=unmapped_required,
            provider_used=provider_used,
            is_fallback=is_fallback,
            fallback_reason=fallback_reason,
            created_at=datetime.now(timezone.utc),
        )

    def _build_deterministic_proposal(
        self,
        col: ColumnSnapshot,
        cand_list: list[CandidateMapping],
    ) -> FieldMappingProposal:
        """Build proposal from deterministic candidate list when AI is unavailable or invalid."""
        if cand_list and cand_list[0].heuristic_confidence >= 0.40:
            best = cand_list[0]
            m_type = MappingType.TRANSFORMED if best.suggested_transformations else MappingType.DIRECT
            signals_str = ", ".join(best.evidence.matched_signals)
            reason = (
                f"Deterministically matched to canonical '{best.candidate_target_field}' "
                f"based on: {signals_str or 'name similarity'}."
            )
            return FieldMappingProposal(
                source_field=col.original_name,
                target_field=best.candidate_target_field,
                mapping_type=m_type,
                confidence=best.heuristic_confidence,
                reason=reason,
                transformations=best.suggested_transformations,
                deterministic_evidence=best.evidence,
            )

        return FieldMappingProposal(
            source_field=col.original_name,
            target_field=None,
            mapping_type=MappingType.UNMAPPED,
            confidence=0.90,
            reason="No canonical field matched with sufficient confidence (score < 0.40).",
            deterministic_evidence=cand_list[0].evidence if cand_list else None,
        )

    def _resolve_conflicts_and_ambiguities(
        self,
        proposals: list[FieldMappingProposal],
    ) -> list[FieldMappingProposal]:
        """Detect when multiple source columns map to the same canonical target and return updated immutable proposals."""
        target_counts: dict[str, list[FieldMappingProposal]] = {}
        for p in proposals:
            if p.target_field:
                target_counts.setdefault(p.target_field, []).append(p)

        resolved: list[FieldMappingProposal] = []
        for p in proposals:
            if p.target_field and len(target_counts.get(p.target_field, [])) > 1:
                conflicting = [
                    other.source_field
                    for other in target_counts[p.target_field]
                    if other.source_field != p.source_field
                ]
                resolved.append(
                    p.model_copy(
                        update={
                            "is_ambiguous": True,
                            "mapping_type": MappingType.AMBIGUOUS,
                            "conflicting_targets": conflicting,
                            "reason": (
                                f"Ambiguous proposal: Target '{p.target_field}' is also claimed by: "
                                f"{', '.join(conflicting)}. Human review required."
                            ),
                        }
                    )
                )
            else:
                resolved.append(p)
        return resolved

    @staticmethod
    def save_proposal_artifact(batch: MappingProposalBatch, path: Path | str) -> Path:
        """Save proposal batch to deterministic JSON artifact."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        content = batch.model_dump_json(indent=2)
        p.write_text(content, encoding="utf-8")
        return p
