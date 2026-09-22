"""Deterministic candidate mapping generator using source profiling evidence and canonical schema rules."""

from __future__ import annotations

import re
from typing import Optional, Sequence
import difflib

from ..canonical import CanonicalField, CanonicalSchema, get_canonical_customer_v1
from ..models.mapping import (
    CandidateMapping,
    DeterministicEvidence,
    TransformationOpType,
    TransformationStep,
)
from ..models.profile import ColumnProfile, DataProfile
from ..models.schema_snapshot import ColumnSnapshot

# Domain alias vocabulary for deterministic heuristics
ALIAS_DICTIONARY: dict[str, set[str]] = {
    "customer_id": {
        "customer_id", "cust_id", "cust_no", "customer_no", "client_id", "client_no",
        "account_id", "account_no", "acct_code", "acct_no", "user_id", "user_identifier",
        "member_id", "id", "custid", "account_code",
    },
    "first_name": {
        "first_name", "fname", "given_name", "forename", "first", "firstname",
    },
    "last_name": {
        "last_name", "lname", "surname", "family_name", "last", "lastname",
    },
    "email": {
        "email", "email_address", "contact_email", "billing_email", "reporter_email",
        "mail", "user_email", "customer_email", "e_mail",
    },
    "date_of_birth": {
        "date_of_birth", "birth_date", "birthdate", "dob", "bday", "birthday", "born_date",
    },
    "status": {
        "status", "cust_status", "customer_status", "account_state", "state",
        "user_status", "account_status", "membership_status",
    },
    "created_at": {
        "created_at", "created_timestamp", "create_date", "signup_date", "opened_date",
        "registration_date", "enrolled_at", "joined_date", "timestamp", "create_time",
    },
}


def _calculate_token_similarity(source_name: str, target_name: str) -> float:
    """Calculate token-based string similarity ratio between 0.0 and 1.0."""
    clean_src = source_name.lower().replace("_", " ").strip()
    clean_tgt = target_name.lower().replace("_", " ").strip()
    if clean_src == clean_tgt:
        return 1.0
    return difflib.SequenceMatcher(None, clean_src, clean_tgt).ratio()


def _is_type_compatible(inferred_type: str, canonical_type: str) -> bool:
    """Check if source inferred type can be cast to canonical type."""
    if canonical_type == "string":
        return True
    if canonical_type in ("date", "datetime"):
        return inferred_type in ("date", "datetime", "string")
    if canonical_type == "enum":
        return inferred_type in ("string", "integer")
    return inferred_type == canonical_type


class DeterministicCandidateGenerator:
    """Generates reproducible mapping candidates from profiling evidence without LLM."""

    def __init__(self, canonical_schema: Optional[CanonicalSchema] = None) -> None:
        self.canonical_schema = canonical_schema or get_canonical_customer_v1()

    def generate_candidates(
        self,
        columns: Sequence[ColumnSnapshot],
        profile: DataProfile,
    ) -> dict[str, list[CandidateMapping]]:
        """Generate candidate mappings for each source column.

        Returns:
            dict mapping source_field (original_name) to a list of CandidateMapping objects,
            ordered by heuristic confidence descending.
        """
        results: dict[str, list[CandidateMapping]] = {}

        for col in columns:
            col_prof = profile.get_column(col.original_name)
            candidates_for_col: list[CandidateMapping] = []

            for canonical_field in self.canonical_schema.fields:
                evidence, score, steps = self._evaluate_field_pair(col, col_prof, canonical_field)
                if score >= 0.35:
                    candidates_for_col.append(
                        CandidateMapping(
                            source_field=col.original_name,
                            candidate_target_field=canonical_field.name,
                            evidence=evidence,
                            heuristic_confidence=round(score, 2),
                            suggested_transformations=steps,
                        )
                    )

            # Sort by heuristic confidence descending
            candidates_for_col.sort(key=lambda c: c.heuristic_confidence, reverse=True)
            results[col.original_name] = candidates_for_col

        return results

    def _evaluate_field_pair(
        self,
        col: ColumnSnapshot,
        col_prof: Optional[ColumnProfile],
        canonical_field: CanonicalField,
    ) -> tuple[DeterministicEvidence, float, list[TransformationStep]]:
        """Score compatibility between a source column and a canonical field."""
        norm_name = col.normalized_name
        target_name = canonical_field.name
        matched_signals: list[str] = []
        transformations: list[TransformationStep] = []

        # 1. Name token matching & dictionary lookup
        alias_set = ALIAS_DICTIONARY.get(target_name, {target_name})
        is_exact_alias = norm_name in alias_set
        similarity = _calculate_token_similarity(norm_name, target_name)

        if is_exact_alias:
            base_score = 0.85
            matched_signals.append("exact_alias_match")
        else:
            base_score = similarity * 0.70
            if similarity > 0.6:
                matched_signals.append(f"token_similarity_{round(similarity, 2)}")

        # 2. Type compatibility
        inferred = col.inferred_type
        type_compat = _is_type_compatible(inferred, canonical_field.data_type)
        if type_compat:
            base_score += 0.10
            matched_signals.append("type_compatible")
        else:
            base_score -= 0.30

        # 3. Uniqueness constraint validation for canonical primary key
        uniqueness_compat = True
        if canonical_field.unique:
            if col_prof and (col_prof.is_unique or col_prof.uniqueness_rate == 1.0):
                base_score += 0.15
                matched_signals.append("unique_identifier_confirmed")
            elif col_prof and col_prof.uniqueness_rate < 0.8:
                # Non-unique column cannot be primary key
                base_score -= 0.50
                uniqueness_compat = False
                matched_signals.append("uniqueness_violation_penalty")

        # 4. Domain-specific value and pattern heuristics
        if target_name == "email":
            has_email_signal = False
            if col_prof:
                for s in col_prof.samples:
                    if "@" in s:
                        has_email_signal = True
                        break
            if has_email_signal or "email" in norm_name:
                base_score += 0.20
                matched_signals.append("email_pattern_verified")
                transformations.extend([
                    TransformationStep(op=TransformationOpType.TRIM),
                    TransformationStep(op=TransformationOpType.NORMALIZE_EMAIL),
                ])

        elif target_name in ("created_at", "date_of_birth"):
            has_date_signal = False
            if col_prof and col_prof.date_patterns:
                has_date_signal = True
                matched_signals.append(f"date_pattern_{col_prof.date_patterns[0].pattern}")
            if inferred in ("date", "datetime") or has_date_signal:
                base_score += 0.15
                if inferred == "string":
                    fmt = col_prof.date_patterns[0].pattern if (col_prof and col_prof.date_patterns) else "%Y-%m-%d"
                    transformations.append(
                        TransformationStep(op=TransformationOpType.PARSE_DATE, params={"format": fmt})
                    )

        elif target_name == "status":
            if canonical_field.allowed_values and col_prof and col_prof.top_values:
                # Check enum value overlap
                top_str = {v.value.upper() for v in col_prof.top_values}
                allowed_set = set(canonical_field.allowed_values)
                overlap = top_str.intersection(allowed_set)
                if overlap:
                    base_score += 0.20
                    matched_signals.append(f"enum_value_overlap_{len(overlap)}")
                transformations.extend([
                    TransformationStep(op=TransformationOpType.TRIM),
                    TransformationStep(op=TransformationOpType.UPPERCASE),
                    TransformationStep(op=TransformationOpType.MAP_ENUM),
                ])

        elif target_name == "first_name":
            has_first = any(tok in norm_name for tok in ("first", "fname", "given"))
            if not is_exact_alias and not has_first:
                base_score -= 0.30
            elif col.inferred_type == "string":
                transformations.append(TransformationStep(op=TransformationOpType.TRIM))

        elif target_name == "last_name":
            has_last = any(tok in norm_name for tok in ("last", "lname", "surname", "family"))
            if not is_exact_alias and not has_last:
                base_score -= 0.30
            elif col.inferred_type == "string":
                transformations.append(TransformationStep(op=TransformationOpType.TRIM))

        # Clamp final score between 0.0 and 1.0
        final_score = max(0.0, min(1.0, base_score))

        evidence = DeterministicEvidence(
            name_similarity_score=round(similarity, 2),
            type_compatible=type_compat,
            uniqueness_compatible=uniqueness_compat,
            matched_signals=matched_signals,
        )

        return evidence, final_score, transformations
