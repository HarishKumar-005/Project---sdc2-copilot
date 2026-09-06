"""Typed data models for the SCD2 Copilot pipeline.

All inter-module data contracts are defined here as dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Optional


# ── Change types ───────────────────────────────────────


class ChangeType(str, Enum):
    """Category of change detected for a business key."""

    NEW = "new"
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    DELETED = "deleted"


# ── Change records ─────────────────────────────────────


@dataclass(frozen=True)
class FieldChange:
    """A single field-level change within a record."""

    column: str
    old_value: Any
    new_value: Any


@dataclass(frozen=True)
class ChangeRecord:
    """A detected change for one business key."""

    business_key_values: dict[str, Any]
    change_type: ChangeType
    field_changes: list[FieldChange] = field(default_factory=list)


@dataclass
class ChangeReport:
    """Aggregated result of change detection across all records."""

    new: list[ChangeRecord] = field(default_factory=list)
    changed: list[ChangeRecord] = field(default_factory=list)
    unchanged: list[ChangeRecord] = field(default_factory=list)
    deleted: list[ChangeRecord] = field(default_factory=list)
    processing_date: date = field(default_factory=date.today)

    @property
    def total(self) -> int:
        return len(self.new) + len(self.changed) + len(self.unchanged) + len(self.deleted)

    @property
    def summary(self) -> dict[str, int]:
        return {
            "new": len(self.new),
            "changed": len(self.changed),
            "unchanged": len(self.unchanged),
            "deleted": len(self.deleted),
            "total": self.total,
        }


# ── Validation ─────────────────────────────────────────


class ValidationStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"


@dataclass(frozen=True)
class ValidationRule:
    """Result of a single validation rule check."""

    name: str
    status: ValidationStatus
    message: str
    details: list[str] = field(default_factory=list)


@dataclass
class ValidationReport:
    """Aggregated validation results."""

    rules: list[ValidationRule] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.status != ValidationStatus.FAIL for r in self.rules)

    @property
    def summary(self) -> dict[str, int]:
        return {
            "pass": sum(1 for r in self.rules if r.status == ValidationStatus.PASS),
            "fail": sum(1 for r in self.rules if r.status == ValidationStatus.FAIL),
            "warn": sum(1 for r in self.rules if r.status == ValidationStatus.WARN),
        }


# ── Explanations ───────────────────────────────────────


@dataclass(frozen=True)
class Explanation:
    """A human-readable explanation of a single change."""

    business_key_values: dict[str, Any]
    change_type: ChangeType
    text: str
    provider: str  # which LLM provider generated this


# ── Pipeline result ────────────────────────────────────


@dataclass
class LLMMetrics:
    """Detailed LLM token usage, cost, and latency metrics."""

    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost: float
    request_duration: float = 0.0
    num_changes_explained: int = 0
    avg_tokens_per_change: float = 0.0
    is_estimated: bool = False


@dataclass
class PipelineResult:
    """Full output of the SCD2 pipeline."""

    change_report: ChangeReport
    # scd2_output is a polars.DataFrame but we use Any to avoid
    # importing polars at the type level (keeps models lightweight)
    scd2_output: Any
    validation_report: ValidationReport
    explanations: list[Explanation] = field(default_factory=list)
    metrics: Optional[LLMMetrics] = None
