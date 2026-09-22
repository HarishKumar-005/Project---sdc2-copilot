"""Data models and configuration for customer historical guardrail integration."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Optional, Union
from uuid import UUID

from pydantic import BaseModel, Field

from ...guardrail.models import (
    GuardrailDecision,
    GuardrailDecisionType,
    GuardrailEvidence,
    GuardrailSeverity,
    TriggeredRule,
)
from ..scd2.models import CustomerSCD2ExecutionResult


class CustomerGuardrailConfig(BaseModel):
    """Configuration for deterministic guardrail thresholds on customer historical changes."""

    enabled: bool = Field(default=True, description="Enable/disable guardrail evaluation.")
    source_name: str = Field(
        default="customer", description="Source stream/entity identifier for guardrail."
    )
    max_changed_records: int = Field(
        default=100,
        ge=1,
        description="Maximum allowed changed customer records before HIGH_CHANGE_VOLUME fires.",
    )
    max_affected_population_ratio: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Maximum ratio of changed customers to total evaluated before HIGH_POPULATION_IMPACT fires.",
    )
    min_evaluated_records_for_ratio: int = Field(
        default=5,
        ge=1,
        description="Minimum evaluated records required to activate population ratio rule.",
    )
    max_deactivation_count: int = Field(
        default=10,
        ge=1,
        description="Maximum allowed status deactivations (ACTIVE -> INACTIVE/SUSPENDED) before MASS_DEACTIVATION fires.",
    )
    max_changes_per_second: float = Field(
        default=50.0,
        ge=0.1,
        description="Maximum allowed change velocity per second before HIGH_CHANGE_VELOCITY fires.",
    )
    min_velocity_records: int = Field(
        default=5,
        ge=2,
        description="Minimum records required to calculate event-time velocity.",
    )
    max_warehouses_affected: int = Field(
        default=100_000,
        ge=1,
        description="Maximum allowed key dispersion/warehouses before WIDE_GEOGRAPHIC_IMPACT fires.",
    )
    ai_explanation_enabled: bool = Field(
        default=False,
        description="Enable/disable AI explanation generation for held batches.",
    )

    from pydantic import model_validator

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            d = dict(data)
            if "max_change_volume" in d and "max_changed_records" not in d:
                d["max_changed_records"] = d.pop("max_change_volume")
            if "max_population_impact_ratio" in d and "max_affected_population_ratio" not in d:
                d["max_affected_population_ratio"] = d.pop("max_population_impact_ratio")
            if "max_change_velocity" in d and "max_changes_per_second" not in d:
                d["max_changes_per_second"] = d.pop("max_change_velocity")
            if "max_deactivations" in d and "max_deactivation_count" not in d:
                d["max_deactivation_count"] = d.pop("max_deactivations")
            return d
        return data

    @property
    def max_change_volume(self) -> int:
        return self.max_changed_records

    @max_change_volume.setter
    def max_change_volume(self, val: int) -> None:
        self.max_changed_records = val

    @property
    def max_population_impact_ratio(self) -> float:
        return self.max_affected_population_ratio

    @max_population_impact_ratio.setter
    def max_population_impact_ratio(self, val: float) -> None:
        self.max_affected_population_ratio = val

    @property
    def max_change_velocity(self) -> float:
        return self.max_changes_per_second

    @max_change_velocity.setter
    def max_change_velocity(self, val: float) -> None:
        self.max_changes_per_second = val


@dataclass(frozen=True)
class CustomerGuardrailEvaluationResult:
    """Outcome of guardrail evaluation for a candidate customer SCD2 batch."""

    decision: GuardrailDecisionType
    severity: GuardrailSeverity
    is_held: bool
    hold_id: Optional[UUID]
    triggered_rules: list[TriggeredRule]
    reasons: list[str]
    evidence: GuardrailEvidence
    persisted: bool
    candidate_execution_result: Optional[CustomerSCD2ExecutionResult] = None
    explanation: Optional[dict[str, Any]] = None
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_normal(self) -> bool:
        """Return True if batch passed guardrail without being held."""
        return self.decision == GuardrailDecisionType.NORMAL and not self.is_held

    @property
    def is_suspicious(self) -> bool:
        """Return True if batch was flagged as suspicious and held."""
        return self.decision == GuardrailDecisionType.SUSPICIOUS or self.is_held

    def to_dict(self) -> dict[str, Any]:
        """Convert result to serializable dictionary."""
        return {
            "decision": self.decision.value,
            "severity": self.severity.value,
            "is_held": self.is_held,
            "hold_id": str(self.hold_id) if self.hold_id else None,
            "triggered_rules": [r.to_dict() for r in self.triggered_rules],
            "reasons": list(self.reasons),
            "evidence": self.evidence.to_dict(),
            "persisted": self.persisted,
            "candidate_execution_result": (
                self.candidate_execution_result.to_dict()
                if self.candidate_execution_result
                else None
            ),
            "explanation": self.explanation,
            "evaluated_at": self.evaluated_at.isoformat(),
        }

    def save_artifact(self, path: Union[str, Path]) -> Path:
        """Save evaluation result as an auditable JSON artifact."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, default=str)
        return target
