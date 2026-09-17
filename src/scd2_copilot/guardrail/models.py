"""Typed data models and enums for the deterministic guardrail engine.

All outputs are strictly deterministic, transparent, and serializable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import UUID


class GuardrailDecisionType(str, Enum):
    """Categorical outcome of guardrail evaluation."""

    NORMAL = "NORMAL"
    SUSPICIOUS = "SUSPICIOUS"


class GuardrailSeverity(str, Enum):
    """Deterministic severity classification for change significance.

    Aligned with database HoldSeverity: LOW, MEDIUM, HIGH, CRITICAL.
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class RuleId(str, Enum):
    """Stable identifiers for deterministic guardrail rules."""

    HIGH_CHANGE_VOLUME = "HIGH_CHANGE_VOLUME"
    HIGH_POPULATION_IMPACT = "HIGH_POPULATION_IMPACT"
    LARGE_QUANTITY_SWING = "LARGE_QUANTITY_SWING"
    MASS_DEACTIVATION = "MASS_DEACTIVATION"
    HIGH_CHANGE_VELOCITY = "HIGH_CHANGE_VELOCITY"
    WIDE_GEOGRAPHIC_IMPACT = "WIDE_GEOGRAPHIC_IMPACT"
    SCD2_VALIDATION_FAILURE = "SCD2_VALIDATION_FAILURE"


@dataclass(frozen=True)
class TriggeredRule:
    """Represents an individual guardrail rule that was triggered by the batch."""

    rule_id: str
    rule_name: str
    description: str
    threshold: Any
    observed_value: Any
    severity: GuardrailSeverity
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "description": self.description,
            "threshold": self.threshold,
            "observed_value": self.observed_value,
            "severity": self.severity.value,
            "message": self.message,
        }


@dataclass(frozen=True)
class GuardrailEvidence:
    """Structured, machine-readable evidence vector extracted from the evaluated batch.

    Fields are intentionally domain-agnostic for generic monitors.  The
    inventory-specific names (warehouses_affected, skus_affected,
    max_quantity_relative_change, status_deactivations_count) are **preserved
    for backward compatibility** with existing held-batch evidence already
    serialized in PostgreSQL JSONB columns.  New generic fields are added
    alongside them.
    """

    evaluated_records: int
    changed_records: int
    new_records: int
    unchanged_records: int
    affected_population_ratio: float
    # ── Legacy inventory-specific fields (preserved for stored JSON compat) ──
    warehouses_affected: int
    skus_affected: int
    max_quantity_relative_change: float
    max_quantity_absolute_change: int
    total_quantity_absolute_change: int
    status_deactivations_count: int
    # ── Generic fields (V3 Phase 3) ──────────────────────────────────────────
    # Distinct values of the highest-cardinality business key across changed+new records.
    # Populated for all monitors; for inventory this equals warehouses_affected when
    # warehouse_id has the highest cardinality among keys.
    key_value_dispersion: int = 0
    # Which business key column drove the dispersion count above.
    dispersion_key_column: Optional[str] = None
    # Worst-case numeric relative change across ALL numeric tracked columns.
    # Populated as max_quantity_relative_change for backward compat.
    # This field names which specific column produced that worst-case value.
    numeric_column_analyzed: Optional[str] = None
    # Which categorical column produced the categorical transition count.
    categorical_column_analyzed: Optional[str] = None
    # Count of FieldChange records keyed by column name across all changed records.
    per_column_change_counts: dict[str, int] = field(default_factory=dict)
    # ── Common ───────────────────────────────────────────────────────────────
    event_window_seconds: float = 0.0
    velocity_changes_per_second: float = 0.0
    validation_passed: bool = True
    validation_failures: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluated_records": self.evaluated_records,
            "changed_records": self.changed_records,
            "new_records": self.new_records,
            "unchanged_records": self.unchanged_records,
            "affected_population_ratio": self.affected_population_ratio,
            "warehouses_affected": self.warehouses_affected,
            "skus_affected": self.skus_affected,
            "max_quantity_relative_change": self.max_quantity_relative_change,
            "max_quantity_absolute_change": self.max_quantity_absolute_change,
            "total_quantity_absolute_change": self.total_quantity_absolute_change,
            "status_deactivations_count": self.status_deactivations_count,
            "key_value_dispersion": self.key_value_dispersion,
            "dispersion_key_column": self.dispersion_key_column,
            "numeric_column_analyzed": self.numeric_column_analyzed,
            "categorical_column_analyzed": self.categorical_column_analyzed,
            "per_column_change_counts": dict(self.per_column_change_counts),
            "event_window_seconds": self.event_window_seconds,
            "velocity_changes_per_second": self.velocity_changes_per_second,
            "validation_passed": self.validation_passed,
            "validation_failures": list(self.validation_failures),
            "metrics": dict(self.metrics),
        }


@dataclass(frozen=True)
class GuardrailDecision:
    """Complete, deterministic decision produced by the guardrail engine."""

    decision: GuardrailDecisionType
    severity: GuardrailSeverity
    triggered_rules: list[TriggeredRule]
    reasons: list[str]
    evidence: GuardrailEvidence
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    batch_id: Optional[UUID] = None
    run_id: Optional[UUID] = None

    @property
    def is_suspicious(self) -> bool:
        """True if the batch triggered suspicious significance rules or validation failure."""
        return self.decision == GuardrailDecisionType.SUSPICIOUS

    @property
    def is_normal(self) -> bool:
        """True if the batch is within all normal operational boundaries."""
        return self.decision == GuardrailDecisionType.NORMAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "severity": self.severity.value,
            "triggered_rules": [r.to_dict() for r in self.triggered_rules],
            "reasons": list(self.reasons),
            "evidence": self.evidence.to_dict(),
            "evaluated_at": self.evaluated_at.isoformat(),
            "batch_id": str(self.batch_id) if self.batch_id else None,
            "run_id": str(self.run_id) if self.run_id else None,
        }
