"""Prompt construction for GenAI semantic mapping proposals with privacy preservation."""

from __future__ import annotations

import json
from typing import Sequence

from ..canonical import CanonicalSchema
from ..models.mapping import CandidateMapping
from ..models.profile import ColumnProfile, DataProfile
from ..models.schema_snapshot import ColumnSnapshot


def build_mapping_prompt(
    source_id: str,
    columns: Sequence[ColumnSnapshot],
    profile: DataProfile,
    canonical_schema: CanonicalSchema,
    candidates: dict[str, list[CandidateMapping]],
) -> str:
    """Build a deterministic, privacy-preserving prompt for LLM semantic mapping proposal."""
    # 1. Format Canonical Target Contract
    canonical_spec = []
    for f in canonical_schema.fields:
        canonical_spec.append({
            "name": f.name,
            "type": f.data_type,
            "required": f.required,
            "unique": f.unique,
            "allowed_values": f.allowed_values,
            "description": f.description,
        })

    # 2. Format Source Column Profiles (privacy-preserved: only masked/shape samples)
    source_spec = []
    for col in columns:
        col_prof = profile.get_column(col.original_name)
        col_info = {
            "source_field": col.original_name,
            "normalized_name": col.normalized_name,
            "inferred_type": col.inferred_type,
            "nullable": col.nullable,
            "uniqueness_rate": col_prof.uniqueness_rate if col_prof else 0.0,
            "is_unique": col_prof.is_unique if col_prof else False,
            "masked_samples": col_prof.samples if col_prof else [],
            "top_categories": [
                {"value": tv.value, "percentage": tv.percentage}
                for tv in (col_prof.top_values if col_prof else [])
            ],
            "date_patterns": [
                {"pattern": dp.pattern, "ambiguous": dp.is_ambiguous}
                for dp in (col_prof.date_patterns if col_prof else [])
            ],
        }
        # Add deterministic candidates as reference evidence
        if col.original_name in candidates and candidates[col.original_name]:
            col_info["deterministic_candidates"] = [
                {
                    "target_field": c.candidate_target_field,
                    "confidence": c.heuristic_confidence,
                    "signals": c.evidence.matched_signals,
                }
                for c in candidates[col.original_name][:3]
            ]
        source_spec.append(col_info)

    allowed_ops = ["TRIM", "LOWERCASE", "UPPERCASE", "CAST", "PARSE_DATE", "NORMALIZE_EMAIL", "MAP_ENUM", "CONCAT"]

    prompt_data = {
        "instruction": (
            "You are a strict data engineering assistant proposing semantic mappings from external customer data "
            "to a canonical Customer entity schema. Evaluate each source field and propose the single most appropriate "
            "canonical target field (or null if unmapped). Suggest constrained transformations only from the approved vocabulary."
        ),
        "source_system_id": source_id,
        "canonical_schema": {
            "entity": canonical_schema.schema_name,
            "version": canonical_schema.version,
            "fields": canonical_spec,
        },
        "source_fields": source_spec,
        "approved_transformation_vocabulary": allowed_ops,
        "rules": [
            "Every source field must have an entry in 'proposals'.",
            "If a source field does not correspond to any canonical field, set 'target_field' to null and 'mapping_type' to 'UNMAPPED'.",
            "Never propose Python code, SQL expressions, or operations outside the approved vocabulary.",
            "Confidence must be a decimal between 0.0 and 1.0 reflecting semantic certainty.",
            "In 'reason', cite specific evidence from the source name, type, patterns, or uniqueness.",
        ],
    }

    return json.dumps(prompt_data, indent=2)
