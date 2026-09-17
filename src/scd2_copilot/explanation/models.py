"""Typed data contracts for operational batch explanations and grounding verification."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from ..db.models import HeldChangeBatchRow, _ensure_utc
from ..guardrail.models import (
    GuardrailDecision,
    GuardrailEvidence,
    GuardrailSeverity,
    TriggeredRule,
)


@dataclass(frozen=True)
class ExplanationContext:
    """Immutable, trusted structured context supplied to the explanation layer.

    Contains exclusively pre-evaluated deterministic evidence.
    User-provided prose and arbitrary inputs are strictly prohibited.
    """

    decision: str
    severity: str
    triggered_rules: list[TriggeredRule]
    evidence: GuardrailEvidence
    source_name: str
    batch_id: Optional[UUID] = None
    watermark_start: Optional[datetime] = None
    watermark_end: Optional[datetime] = None
    validation_passed: bool = True
    validation_failures: list[str] = field(default_factory=list)
    processing_status: str = "HELD"  # "HELD", "COMMITTED", "FAILED"
    containment_status: Optional[str] = "HELD"  # "HELD", "RELEASED", "DISCARDED", "REPROCESSED"
    records_sample: list[dict[str, Any]] = field(default_factory=list)
    hold_id: Optional[UUID] = None
    run_id: Optional[UUID] = None

    @classmethod
    def from_guardrail_decision(
        cls,
        decision: GuardrailDecision,
        source_name: str,
        batch_id: Optional[UUID] = None,
        watermark_start: Optional[datetime] = None,
        watermark_end: Optional[datetime] = None,
        validation_passed: bool = True,
        validation_failures: Optional[list[str]] = None,
        processing_status: str = "HELD",
        containment_status: Optional[str] = "HELD",
        hold_id: Optional[UUID] = None,
        records_sample: Optional[list[dict[str, Any]]] = None,
    ) -> ExplanationContext:
        """Construct context directly from an active GuardrailDecision."""
        return cls(
            decision=decision.decision.value,
            severity=decision.severity.value,
            triggered_rules=list(decision.triggered_rules),
            evidence=decision.evidence,
            source_name=source_name,
            batch_id=batch_id or decision.batch_id,
            watermark_start=watermark_start,
            watermark_end=watermark_end,
            validation_passed=validation_passed,
            validation_failures=list(validation_failures or []),
            processing_status=processing_status,
            containment_status=containment_status,
            records_sample=list(records_sample or []),
            hold_id=hold_id,
            run_id=decision.run_id,
        )

    @classmethod
    def from_held_batch(
        cls,
        held_row: HeldChangeBatchRow,
    ) -> ExplanationContext:
        """Reconstruct explanation context from a persisted HeldChangeBatchRow."""
        evidence_dict = held_row.evidence or {}

        # 1. Reconstruct GuardrailEvidence
        gev = evidence_dict.get("guardrail_evidence", {})
        evidence_obj = GuardrailEvidence(
            evaluated_records=int(gev.get("evaluated_records", held_row.records_affected)),
            changed_records=int(gev.get("changed_records", 0)),
            new_records=int(gev.get("new_records", 0)),
            unchanged_records=int(gev.get("unchanged_records", 0)),
            affected_population_ratio=float(gev.get("affected_population_ratio", 0.0)),
            warehouses_affected=int(gev.get("warehouses_affected", 1)),
            skus_affected=int(gev.get("skus_affected", 1)),
            max_quantity_relative_change=float(gev.get("max_quantity_relative_change", 0.0)),
            max_quantity_absolute_change=int(gev.get("max_quantity_absolute_change", 0)),
            total_quantity_absolute_change=int(gev.get("total_quantity_absolute_change", 0)),
            status_deactivations_count=int(gev.get("status_deactivations_count", 0)),
            event_window_seconds=float(gev.get("event_window_seconds", 0.0)),
            velocity_changes_per_second=float(gev.get("velocity_changes_per_second", 0.0)),
            validation_passed=bool(gev.get("validation_passed", True)),
            validation_failures=list(gev.get("validation_failures", [])),
            metrics=dict(gev.get("metrics", {})),
        )

        # 2. Reconstruct TriggeredRules
        raw_rules = evidence_dict.get("triggered_rules", [])
        rules_list: list[TriggeredRule] = []
        for r in raw_rules:
            sev_str = r.get("severity", held_row.severity)
            try:
                sev_enum = GuardrailSeverity(sev_str)
            except Exception:
                sev_enum = GuardrailSeverity.HIGH

            rules_list.append(
                TriggeredRule(
                    rule_id=str(r.get("rule_id", "UNKNOWN")),
                    rule_name=str(r.get("rule_name", r.get("rule_id", "Unknown Rule"))),
                    description=str(r.get("description", "")),
                    threshold=r.get("threshold"),
                    observed_value=r.get("observed_value"),
                    severity=sev_enum,
                    message=str(r.get("message", "")),
                )
            )

        # 3. Watermarks & Batch ID
        batch_id_str = evidence_dict.get("batch_id")
        batch_id = UUID(batch_id_str) if batch_id_str else None
        w_start_str = evidence_dict.get("watermark_start")
        w_start = datetime.fromisoformat(w_start_str) if w_start_str else None
        w_end_str = evidence_dict.get("watermark_end")
        w_end = datetime.fromisoformat(w_end_str) if w_end_str else None

        # Sample up to 3 frozen records if present (include all non-secret keys)
        _SECRET_SUBSTRINGS = ("pass", "secret", "token", "key", "auth", "cred")
        raw_records = evidence_dict.get("batch_records", [])
        records_sample = [
            {k: v for k, v in rec.items() if not any(s in k.lower() for s in _SECRET_SUBSTRINGS)}
            for rec in raw_records[:3]
        ]

        return cls(
            decision="SUSPICIOUS" if held_row.status == "HELD" else "NORMAL",
            severity=held_row.severity,
            triggered_rules=rules_list,
            evidence=evidence_obj,
            source_name=held_row.source_name,
            batch_id=batch_id,
            watermark_start=w_start,
            watermark_end=w_end,
            validation_passed=evidence_obj.validation_passed,
            validation_failures=evidence_obj.validation_failures,
            processing_status="HELD",
            containment_status=held_row.status,
            records_sample=records_sample,
            hold_id=held_row.hold_id,
            run_id=held_row.run_id,
        )


@dataclass(frozen=True)
class GroundingViolation:
    """Represents a detectable violation of grounding rules in generated output."""

    rule_name: str
    description: str
    claimed_value: Any
    expected_value: Any


@dataclass(frozen=True)
class BatchExplanationResult:
    """Structured, evidence-grounded operational explanation."""

    summary: str
    what_changed: str
    why_flagged: str
    evidence_points: list[str]
    validation_summary: str
    containment_summary: str
    decision: str
    severity: str
    provider: str
    model: Optional[str] = None
    explanation_version: str = "v1"
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    grounding_passed: bool = True
    is_fallback: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "what_changed": self.what_changed,
            "why_flagged": self.why_flagged,
            "evidence_points": list(self.evidence_points),
            "validation_summary": self.validation_summary,
            "containment_summary": self.containment_summary,
            "decision": self.decision,
            "severity": self.severity,
            "provider": self.provider,
            "model": self.model,
            "explanation_version": self.explanation_version,
            "generated_at": self.generated_at.isoformat(),
            "grounding_passed": self.grounding_passed,
            "is_fallback": self.is_fallback,
            "warnings": list(self.warnings),
        }
