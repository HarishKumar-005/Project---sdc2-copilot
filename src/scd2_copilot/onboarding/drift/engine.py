"""Deterministic source schema diff engine for detecting structural evolution."""

from __future__ import annotations

import difflib
from typing import Optional, Sequence

from ..mapping.candidates import ALIAS_DICTIONARY, _calculate_token_similarity, _is_type_compatible
from ..models.drift import DriftType, SchemaDiffResult, SchemaDriftEvent
from ..models.schema_snapshot import ColumnSnapshot, SourceSchemaSnapshot
from ..profiler.fingerprint import normalize_column_name


class DeterministicSchemaDiffEngine:
    """Detects structural and semantic differences between two source schema snapshots.

    Implements strict deterministic diff rules:
    - ADDED_COLUMN: Column present in current schema, absent from prior schema.
    - REMOVED_COLUMN: Column present in prior schema, absent from current schema.
    - TYPE_CHANGED: Column present in both schemas, but inferred or Polars data type changed.
    - NULLABILITY_CHANGED: Column present in both schemas, but nullable state changed.
    - POSSIBLE_RENAME: A removed column and an added column share strong lexical or alias
      similarity and compatible data types. Emitted as a proposal/interpretation signal,
      never as an automatic assertion.
    """

    def __init__(self, rename_similarity_threshold: float = 0.6) -> None:
        self.rename_similarity_threshold = rename_similarity_threshold

    def diff(
        self,
        prior_schema: SourceSchemaSnapshot,
        current_schema: SourceSchemaSnapshot,
    ) -> SchemaDiffResult:
        """Compare prior and current schema snapshots and return a deterministic SchemaDiffResult."""
        events: list[SchemaDriftEvent] = []

        # Index columns by normalized name
        prior_by_norm: dict[str, ColumnSnapshot] = {
            col.normalized_name: col for col in prior_schema.columns
        }
        curr_by_norm: dict[str, ColumnSnapshot] = {
            col.normalized_name: col for col in current_schema.columns
        }

        # 1. Detect common columns: TYPE_CHANGED and NULLABILITY_CHANGED
        common_norms = sorted(set(prior_by_norm.keys()) & set(curr_by_norm.keys()))
        for norm in common_norms:
            p_col = prior_by_norm[norm]
            c_col = curr_by_norm[norm]

            # Type change check (inferred logical type or technical Polars type)
            if (
                p_col.inferred_type != c_col.inferred_type
                or p_col.polars_type != c_col.polars_type
            ):
                events.append(
                    SchemaDriftEvent(
                        drift_type=DriftType.TYPE_CHANGED,
                        field_name=c_col.original_name,
                        old_field_name=p_col.original_name,
                        new_field_name=c_col.original_name,
                        old_type=f"{p_col.inferred_type} ({p_col.polars_type})",
                        new_type=f"{c_col.inferred_type} ({c_col.polars_type})",
                        old_nullable=p_col.nullable,
                        new_nullable=c_col.nullable,
                        details=(
                            f"Column '{c_col.original_name}' type changed from "
                            f"'{p_col.inferred_type}' to '{c_col.inferred_type}'."
                        ),
                        matched_signals=["type_mismatch"],
                    )
                )

            # Nullability change check
            if p_col.nullable != c_col.nullable:
                events.append(
                    SchemaDriftEvent(
                        drift_type=DriftType.NULLABILITY_CHANGED,
                        field_name=c_col.original_name,
                        old_field_name=p_col.original_name,
                        new_field_name=c_col.original_name,
                        old_type=c_col.inferred_type,
                        new_type=c_col.inferred_type,
                        old_nullable=p_col.nullable,
                        new_nullable=c_col.nullable,
                        details=(
                            f"Column '{c_col.original_name}' nullability changed from "
                            f"{p_col.nullable} to {c_col.nullable}."
                        ),
                        matched_signals=["nullability_changed"],
                    )
                )

        # 2. Detect REMOVED columns
        removed_norms = sorted(set(prior_by_norm.keys()) - set(curr_by_norm.keys()))
        removed_cols = [prior_by_norm[norm] for norm in removed_norms]
        for p_col in removed_cols:
            events.append(
                SchemaDriftEvent(
                    drift_type=DriftType.REMOVED_COLUMN,
                    field_name=p_col.original_name,
                    old_field_name=p_col.original_name,
                    new_field_name=None,
                    old_type=p_col.inferred_type,
                    new_type=None,
                    old_nullable=p_col.nullable,
                    new_nullable=None,
                    details=f"Column '{p_col.original_name}' was removed in current schema.",
                    matched_signals=["column_absent_in_target"],
                )
            )

        # 3. Detect ADDED columns
        added_norms = sorted(set(curr_by_norm.keys()) - set(prior_by_norm.keys()))
        added_cols = [curr_by_norm[norm] for norm in added_norms]
        for c_col in added_cols:
            events.append(
                SchemaDriftEvent(
                    drift_type=DriftType.ADDED_COLUMN,
                    field_name=c_col.original_name,
                    old_field_name=None,
                    new_field_name=c_col.original_name,
                    old_type=None,
                    new_type=c_col.inferred_type,
                    old_nullable=None,
                    new_nullable=c_col.nullable,
                    details=f"Column '{c_col.original_name}' was added in current schema.",
                    matched_signals=["column_new_in_target"],
                )
            )

        # 4. Detect POSSIBLE_RENAME candidates between removed and added columns
        for p_col in removed_cols:
            best_candidate: Optional[ColumnSnapshot] = None
            best_score: float = 0.0
            best_signals: list[str] = []

            for c_col in added_cols:
                # Calculate lexical and alias similarity
                sim = _calculate_token_similarity(p_col.normalized_name, c_col.normalized_name)
                is_alias = self._is_known_alias(p_col.normalized_name, c_col.normalized_name)
                type_compat = _is_type_compatible(c_col.inferred_type, p_col.inferred_type)

                signals: list[str] = []
                score = sim
                if is_alias:
                    score = max(score, 0.88)
                    signals.append("known_domain_alias")
                if type_compat:
                    signals.append("type_compatible")
                else:
                    score -= 0.25

                if score >= self.rename_similarity_threshold and score > best_score:
                    best_candidate = c_col
                    best_score = score
                    best_signals = signals

            if best_candidate is not None:
                events.append(
                    SchemaDriftEvent(
                        drift_type=DriftType.POSSIBLE_RENAME,
                        field_name=p_col.original_name,
                        old_field_name=p_col.original_name,
                        new_field_name=best_candidate.original_name,
                        old_type=p_col.inferred_type,
                        new_type=best_candidate.inferred_type,
                        old_nullable=p_col.nullable,
                        new_nullable=best_candidate.nullable,
                        details=(
                            f"Possible rename: '{p_col.original_name}' may have been renamed "
                            f"to '{best_candidate.original_name}' (similarity: {round(best_score, 2)})."
                        ),
                        similarity_score=round(best_score, 2),
                        matched_signals=best_signals,
                    )
                )

        # Sort events deterministically: by field_name, drift_type, new_field_name
        events.sort(
            key=lambda e: (e.field_name, e.drift_type.value, e.new_field_name or "")
        )

        has_drift = len(events) > 0

        return SchemaDiffResult(
            source_id=current_schema.source_id,
            prior_schema_version=prior_schema.schema_version,
            current_schema_version=current_schema.schema_version,
            prior_fingerprint=prior_schema.fingerprint.fingerprint_hash,
            current_fingerprint=current_schema.fingerprint.fingerprint_hash,
            has_drift=has_drift,
            drift_events=events,
        )

    def _is_known_alias(self, name_a: str, name_b: str) -> bool:
        """Check if two column slugs belong to the same alias set in ALIAS_DICTIONARY."""
        for target_concept, aliases in ALIAS_DICTIONARY.items():
            if name_a in aliases and name_b in aliases:
                return True
        return False
