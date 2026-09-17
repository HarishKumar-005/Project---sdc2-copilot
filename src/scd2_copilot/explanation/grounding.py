"""Deterministic post-generation grounding validator for AI explanations."""

from __future__ import annotations

import re
from typing import Any

from ..guardrail.models import RuleId
from .models import BatchExplanationResult, ExplanationContext, GroundingViolation


ALL_RULE_IDS = {
    RuleId.HIGH_CHANGE_VOLUME.value,
    RuleId.HIGH_POPULATION_IMPACT.value,
    RuleId.LARGE_QUANTITY_SWING.value,
    RuleId.MASS_DEACTIVATION.value,
    RuleId.HIGH_CHANGE_VELOCITY.value,
    RuleId.WIDE_GEOGRAPHIC_IMPACT.value,
    RuleId.SCD2_VALIDATION_FAILURE.value,
}


def validate_explanation_grounding(
    result: BatchExplanationResult,
    context: ExplanationContext,
) -> tuple[bool, list[GroundingViolation]]:
    """Verify that generated explanation does not hallucinate rules, numbers, or actions.

    Returns:
        tuple[bool, list[GroundingViolation]]: (is_grounded, violations)
    """
    violations: list[GroundingViolation] = []

    # 1. Decision & Severity Invariance
    if result.decision.upper() != context.decision.upper():
        violations.append(
            GroundingViolation(
                rule_name="DECISION_IMMUTABILITY",
                description="AI modified authoritative decision.",
                claimed_value=result.decision,
                expected_value=context.decision,
            )
        )

    if result.severity.upper() != context.severity.upper():
        violations.append(
            GroundingViolation(
                rule_name="SEVERITY_IMMUTABILITY",
                description="AI modified authoritative severity.",
                claimed_value=result.severity,
                expected_value=context.severity,
            )
        )

    # 2. Rule Grounding
    # Check if any un-triggered rule was falsely claimed to have triggered
    triggered_rule_ids = {r.rule_id for r in context.triggered_rules}
    untriggered_rule_ids = ALL_RULE_IDS - triggered_rule_ids

    combined_text = f"{result.why_flagged} {' '.join(result.evidence_points)} {result.summary}".upper()
    for untriggered in untriggered_rule_ids:
        # Check if the text claims this un-triggered rule triggered
        if untriggered in combined_text:
            # Look for phrasing indicating trigger
            trigger_patterns = [
                rf"\b{untriggered}\b.*?\b(TRIGGERED|EXCEEDED|BREACHED|FLAGGED|DETECTED|CAUSED)\b",
                rf"\b(TRIGGERED|EXCEEDED|BREACHED|FLAGGED|DETECTED|DUE TO|BECAUSE OF)\b.*?\b{untriggered}\b",
            ]
            for pat in trigger_patterns:
                if re.search(pat, combined_text, re.IGNORECASE):
                    violations.append(
                        GroundingViolation(
                            rule_name="UNTRIGGERED_RULE_CLAIMED",
                            description=f"AI claimed untriggered rule '{untriggered}' was triggered.",
                            claimed_value=untriggered,
                            expected_value=sorted(list(triggered_rule_ids)),
                        )
                    )
                    break

    # 3. Validation Status Grounding
    val_text = f"{result.validation_summary} {result.summary}".lower()
    if context.validation_passed:
        if "validation failed" in val_text or "invariants failed" in val_text or "invariants were violated" in val_text:
            violations.append(
                GroundingViolation(
                    rule_name="CONTRADICTED_VALIDATION_STATUS",
                    description="AI claimed SCD2 validation failed when it actually passed.",
                    claimed_value="validation failed",
                    expected_value="validation passed",
                )
            )
    else:
        if "validation passed" in val_text or "invariants passed" in val_text:
            violations.append(
                GroundingViolation(
                    rule_name="CONTRADICTED_VALIDATION_STATUS",
                    description="AI claimed SCD2 validation passed when it actually failed.",
                    claimed_value="validation passed",
                    expected_value="validation failed",
                )
            )

    # 4. Containment Status Grounding
    cont_text = f"{result.containment_summary} {result.summary}".lower()
    if context.processing_status == "HELD":
        if "committed to history" in cont_text or "committed to inventory_history" in cont_text:
            violations.append(
                GroundingViolation(
                    rule_name="CONTRADICTED_CONTAINMENT_STATUS",
                    description="AI claimed held batch was committed to history.",
                    claimed_value="committed to history",
                    expected_value="held at boundary",
                )
            )

    is_grounded = len(violations) == 0
    return is_grounded, violations
