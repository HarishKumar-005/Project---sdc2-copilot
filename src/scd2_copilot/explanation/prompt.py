"""Deterministic prompt construction for evidence-grounded AI explanations."""

from __future__ import annotations

import json
from typing import Any

from .models import ExplanationContext

PROMPT_VERSION = "v1"

SYSTEM_INSTRUCTIONS = """You are a Data Engineering Reliability Assistant explaining automated Slowly Changing Dimension Type 2 (SCD2) state changes.

YOUR ROLE:
Explain an ALREADY-DECIDED deterministic guardrail evaluation based strictly on the provided factual evidence.

ABSOLUTE CONSTRAINTS:
1. DECISION IMMUTABILITY: You CANNOT change the decision (NORMAL vs SUSPICIOUS) or severity (LOW, MEDIUM, HIGH, CRITICAL).
2. NUMERICAL GROUNDING: Every number, ratio, and count in your explanation MUST exist in the provided evidence. Do NOT invent numbers.
3. RULE GROUNDING: Only mention guardrail rules that are explicitly listed under "TRIGGERED RULES". Do NOT claim other rules were triggered.
4. NO SPECULATIVE ROOT CAUSES: Do NOT speculate on unverified external causes (such as "network outage", "buggy frontend", "malicious attack") unless explicitly stated in the evidence.
5. NO UNSUBSTANTIATED CORRUPTION CLAIMS: Do NOT claim the source data is wrong or corrupted. "Suspicious" means an unusual or high-significance pattern was detected.
6. VALIDATION VS SIGNIFICANCE: Clearly distinguish technical SCD2 validity (e.g. valid half-open intervals, unique keys) from business significance (e.g. large volume, mass deactivation).
7. OUTPUT FORMAT: Return ONLY a valid JSON object matching the requested schema. No markdown formatting, no code fencing, no prose before or after.
"""


def build_explanation_prompt(context: ExplanationContext) -> str:
    """Construct a constrained, secret-free JSON prompt containing only trusted evidence."""
    # Summarize triggered rules
    rules_payload: list[dict[str, Any]] = [
        {
            "rule_id": r.rule_id,
            "rule_name": r.rule_name,
            "severity": r.severity.value if hasattr(r.severity, "value") else str(r.severity),
            "threshold": r.threshold,
            "observed_value": r.observed_value,
            "message": r.message,
        }
        for r in context.triggered_rules
    ]

    # Clean evidence vector
    ev = context.evidence
    evidence_payload: dict[str, Any] = {
        "evaluated_records": ev.evaluated_records,
        "changed_records": ev.changed_records,
        "new_records": ev.new_records,
        "unchanged_records": ev.unchanged_records,
        "affected_population_ratio": ev.affected_population_ratio,
        "warehouses_affected": ev.warehouses_affected,
        "skus_affected": ev.skus_affected,
        "max_quantity_relative_change": ev.max_quantity_relative_change,
        "max_quantity_absolute_change": ev.max_quantity_absolute_change,
        "total_quantity_absolute_change": ev.total_quantity_absolute_change,
        "status_deactivations_count": ev.status_deactivations_count,
        "event_window_seconds": ev.event_window_seconds,
        "velocity_changes_per_second": ev.velocity_changes_per_second,
    }

    watermark_str = "None"
    if context.watermark_start or context.watermark_end:
        start_ts = context.watermark_start.isoformat() if context.watermark_start else "beginning"
        end_ts = context.watermark_end.isoformat() if context.watermark_end else "current"
        watermark_str = f"{start_ts} -> {end_ts}"

    prompt_data = {
        "context_version": PROMPT_VERSION,
        "stream_name": context.source_name,
        "watermark_range": watermark_str,
        "authoritative_decision": context.decision,
        "authoritative_severity": context.severity,
        "scd2_validation": {
            "passed": context.validation_passed,
            "failures": context.validation_failures,
        },
        "processing_disposition": {
            "processing_status": context.processing_status,
            "containment_status": context.containment_status,
        },
        "triggered_rules": rules_payload,
        "batch_metrics": evidence_payload,
    }

    if context.records_sample:
        safe_samples = []
        for rec in context.records_sample:
            safe_rec = {
                k: v
                for k, v in rec.items()
                if not any(s in k.lower() for s in ("pass", "secret", "token", "key", "auth", "cred"))
            }
            safe_samples.append(safe_rec)
        prompt_data["sample_changed_records"] = safe_samples

    schema_instruction = """
JSON OUTPUT CONTRACT:
{
  "summary": "1-2 sentence executive operational summary.",
  "what_changed": "Concise operational description of record counts and observed transitions.",
  "why_flagged": "Clear description of which deterministic rules triggered and specific thresholds exceeded.",
  "evidence_points": [
    "Specific numerical or categorical evidence bullet point 1",
    "Specific numerical or categorical evidence bullet point 2"
  ],
  "validation_summary": "Description of SCD2 technical invariant validity vs significance.",
  "containment_summary": "Description of operational disposition (e.g. batch held at boundary, watermark preserved)."
}
"""

    return f"EVIDENCE CONTEXT:\n{json.dumps(prompt_data, indent=2)}\n\n{schema_instruction}"
