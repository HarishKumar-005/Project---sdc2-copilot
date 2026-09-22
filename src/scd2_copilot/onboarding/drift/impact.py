"""Mapping impact analyzer evaluating the structural impact of schema drift on approved mappings."""

from __future__ import annotations

from typing import Optional

from ..canonical import CanonicalField, CanonicalSchema, get_canonical_customer_v1
from ..mapping.candidates import _is_type_compatible
from ..models.approval import ApprovedMappingDefinition, ApprovedMappingVersion
from ..models.drift import (
    DriftType,
    FieldMappingImpact,
    MappingCompatibilityState,
    SchemaDiffResult,
    SchemaDriftEvent,
    SchemaDriftReport,
)


class MappingImpactAnalyzer:
    """Evaluates how source schema drift affects an immutable approved mapping version.

    Enforces deterministic compatibility classification:
    - COMPATIBLE: No active mapping dependencies are broken or invalidated.
    - COMPATIBLE_WITH_REVIEW: Changes exist that may affect transformations or validation
      (e.g., castable type changes, nullability changes on required fields), but the mapping
      remains structurally usable subject to human policy review.
    - BROKEN: At least one required mapping dependency is invalid (e.g., mapped source column
      was removed, or an incompatible type change prevents execution).

    Strict Invariants:
    1. Approved mappings are never mutated in place.
    2. Zero raw customer data is processed or included in impact reports.
    3. Classification is 100% deterministic and does not depend on LLMs.
    """

    def __init__(self, canonical_schema: Optional[CanonicalSchema] = None) -> None:
        self.canonical_schema = canonical_schema or get_canonical_customer_v1()

    def analyze_impact(
        self,
        approved_mapping: ApprovedMappingVersion,
        diff_result: SchemaDiffResult,
    ) -> SchemaDriftReport:
        """Analyze the impact of diff_result on approved_mapping and return a SchemaDriftReport."""
        # Index drift events by field name and type
        renames_by_old_field: dict[str, SchemaDriftEvent] = {}
        events_by_field: dict[str, list[SchemaDriftEvent]] = {}

        for event in diff_result.drift_events:
            events_by_field.setdefault(event.field_name, []).append(event)
            if event.drift_type == DriftType.POSSIBLE_RENAME and event.old_field_name:
                renames_by_old_field[event.old_field_name] = event

        impacted_mappings: list[FieldMappingImpact] = []

        # Evaluate active mappings in the approved version
        active_mappings = approved_mapping.get_active_mappings()

        for m in active_mappings:
            field_impact = self._evaluate_mapping_field(
                mapping=m,
                approved_mapping_id=approved_mapping.mapping_version_id,
                events_for_field=events_by_field.get(m.source_field, []),
                possible_rename=renames_by_old_field.get(m.source_field),
            )
            if field_impact is not None:
                impacted_mappings.append(field_impact)

        # Deduplicate impacted mappings by source_field, keeping worst-case compatibility
        deduped_impacts = self._deduplicate_impacts(impacted_mappings)

        # Determine overall compatibility
        overall_compat = MappingCompatibilityState.COMPATIBLE
        if any(i.compatibility == MappingCompatibilityState.BROKEN for i in deduped_impacts):
            overall_compat = MappingCompatibilityState.BROKEN
        elif any(
            i.compatibility == MappingCompatibilityState.COMPATIBLE_WITH_REVIEW
            for i in deduped_impacts
        ):
            overall_compat = MappingCompatibilityState.COMPATIBLE_WITH_REVIEW

        review_required = overall_compat != MappingCompatibilityState.COMPATIBLE

        # Construct deterministic summary reason
        summary_reason = self._build_summary_reason(
            diff_result=diff_result,
            impacts=deduped_impacts,
            overall_compat=overall_compat,
        )

        return SchemaDriftReport(
            source_id=diff_result.source_id,
            prior_schema_version=diff_result.prior_schema_version,
            current_schema_version=diff_result.current_schema_version,
            prior_fingerprint=diff_result.prior_fingerprint,
            current_fingerprint=diff_result.current_fingerprint,
            mapping_version_id=approved_mapping.mapping_version_id,
            canonical_schema_version=approved_mapping.canonical_schema_version,
            drift_events=diff_result.drift_events,
            impacted_mappings=deduped_impacts,
            overall_compatibility=overall_compat,
            review_required=review_required,
            summary_reason=summary_reason,
        )

    def _evaluate_mapping_field(
        self,
        mapping: ApprovedMappingDefinition,
        approved_mapping_id: str,
        events_for_field: list[SchemaDriftEvent],
        possible_rename: Optional[SchemaDriftEvent],
    ) -> Optional[FieldMappingImpact]:
        """Evaluate how drift events on a single mapped source field affect its mapping."""
        canonical_field: Optional[CanonicalField] = None
        if mapping.target_field:
            canonical_field = self.canonical_schema.get_field(mapping.target_field)

        # Check for column removal
        has_removal = any(e.drift_type == DriftType.REMOVED_COLUMN for e in events_for_field)
        if has_removal:
            replacement_cand: Optional[str] = None
            replacement_conf: Optional[float] = None
            rename_note = ""

            if possible_rename:
                replacement_cand = possible_rename.new_field_name
                replacement_conf = possible_rename.similarity_score
                rename_note = f" Possible replacement candidate: '{replacement_cand}'."

            is_required = canonical_field.required if canonical_field else True
            reason = (
                f"Mapped source column '{mapping.source_field}' was removed from source schema."
                + rename_note
            )
            return FieldMappingImpact(
                source_field=mapping.source_field,
                target_field=mapping.target_field,
                mapping_version_id=approved_mapping_id,
                impact_category=DriftType.REMOVED_COLUMN,
                compatibility=MappingCompatibilityState.BROKEN,
                reason=reason,
                replacement_candidate=replacement_cand,
                replacement_confidence=replacement_conf,
            )

        # Check for type changed
        type_event = next((e for e in events_for_field if e.drift_type == DriftType.TYPE_CHANGED), None)
        if type_event:
            new_type_clean = (type_event.new_type or "").split(" ")[0].lower()
            canonical_type = canonical_field.data_type if canonical_field else "string"

            # Check if new type is castable/compatible
            if _is_type_compatible(new_type_clean, canonical_type):
                return FieldMappingImpact(
                    source_field=mapping.source_field,
                    target_field=mapping.target_field,
                    mapping_version_id=approved_mapping_id,
                    impact_category=DriftType.TYPE_CHANGED,
                    compatibility=MappingCompatibilityState.COMPATIBLE_WITH_REVIEW,
                    reason=(
                        f"Source column '{mapping.source_field}' type changed to '{new_type_clean}', "
                        f"which is compatible with canonical target '{mapping.target_field}' ({canonical_type}) "
                        f"but requires review of transformation steps."
                    ),
                )
            else:
                return FieldMappingImpact(
                    source_field=mapping.source_field,
                    target_field=mapping.target_field,
                    mapping_version_id=approved_mapping_id,
                    impact_category=DriftType.TYPE_CHANGED,
                    compatibility=MappingCompatibilityState.BROKEN,
                    reason=(
                        f"Source column '{mapping.source_field}' type changed to '{new_type_clean}', "
                        f"which is incompatible with canonical target '{mapping.target_field}' ({canonical_type})."
                    ),
                )

        # Check for nullability changed
        null_event = next(
            (e for e in events_for_field if e.drift_type == DriftType.NULLABILITY_CHANGED),
            None,
        )
        if null_event:
            is_target_required = canonical_field.required if canonical_field else False
            if null_event.new_nullable is True and is_target_required:
                return FieldMappingImpact(
                    source_field=mapping.source_field,
                    target_field=mapping.target_field,
                    mapping_version_id=approved_mapping_id,
                    impact_category=DriftType.NULLABILITY_CHANGED,
                    compatibility=MappingCompatibilityState.COMPATIBLE_WITH_REVIEW,
                    reason=(
                        f"Source column '{mapping.source_field}' became nullable, but target canonical "
                        f"field '{mapping.target_field}' is mandatory. May yield validation errors on null values."
                    ),
                )
            else:
                return FieldMappingImpact(
                    source_field=mapping.source_field,
                    target_field=mapping.target_field,
                    mapping_version_id=approved_mapping_id,
                    impact_category=DriftType.NULLABILITY_CHANGED,
                    compatibility=MappingCompatibilityState.COMPATIBLE,
                    reason=(
                        f"Source column '{mapping.source_field}' nullability changed, but target canonical "
                        f"field '{mapping.target_field}' constraints remain satisfied."
                    ),
                )

        return None

    def _deduplicate_impacts(
        self,
        impacts: list[FieldMappingImpact],
    ) -> list[FieldMappingImpact]:
        """Consolidate impacts for the same source field, retaining the highest severity."""
        severity_order = {
            MappingCompatibilityState.BROKEN: 3,
            MappingCompatibilityState.COMPATIBLE_WITH_REVIEW: 2,
            MappingCompatibilityState.COMPATIBLE: 1,
        }
        by_source: dict[str, FieldMappingImpact] = {}
        for imp in impacts:
            existing = by_source.get(imp.source_field)
            if existing is None:
                by_source[imp.source_field] = imp
            else:
                if severity_order[imp.compatibility] > severity_order[existing.compatibility]:
                    by_source[imp.source_field] = imp
        return sorted(by_source.values(), key=lambda i: i.source_field)

    def _build_summary_reason(
        self,
        diff_result: SchemaDiffResult,
        impacts: list[FieldMappingImpact],
        overall_compat: MappingCompatibilityState,
    ) -> str:
        """Construct deterministic explanation of the overall schema drift impact."""
        if not diff_result.has_drift:
            return "No schema drift detected between prior and current source snapshots."

        if overall_compat == MappingCompatibilityState.COMPATIBLE:
            return (
                f"Detected {len(diff_result.drift_events)} drift event(s). "
                f"All existing approved mappings remain fully COMPATIBLE."
            )

        broken_count = sum(1 for i in impacts if i.compatibility == MappingCompatibilityState.BROKEN)
        review_count = sum(
            1 for i in impacts if i.compatibility == MappingCompatibilityState.COMPATIBLE_WITH_REVIEW
        )

        parts: list[str] = [
            f"Detected {len(diff_result.drift_events)} drift event(s).",
            f"Overall mapping compatibility is {overall_compat.value}.",
        ]
        if broken_count > 0:
            broken_fields = [i.source_field for i in impacts if i.compatibility == MappingCompatibilityState.BROKEN]
            parts.append(f"{broken_count} mapping(s) BROKEN: {', '.join(broken_fields)}.")
        if review_count > 0:
            review_fields = [
                i.source_field for i in impacts if i.compatibility == MappingCompatibilityState.COMPATIBLE_WITH_REVIEW
            ]
            parts.append(f"{review_count} mapping(s) require REVIEW: {', '.join(review_fields)}.")

        return " ".join(parts)
