"""Pure, deterministic rule evaluators for the guardrail engine.

Each rule evaluator takes extracted batch metrics and threshold configurations,
and returns an Optional[TriggeredRule]. No database I/O, no network calls, no randomness.
"""

from __future__ import annotations

from typing import Optional

from .models import GuardrailSeverity, RuleId, TriggeredRule


def check_change_volume(
    changed_records: int,
    max_changed_records: int,
) -> Optional[TriggeredRule]:
    """Rule 1: Detect when changed record count exceeds operational limit."""
    if changed_records > max_changed_records:
        return TriggeredRule(
            rule_id=RuleId.HIGH_CHANGE_VOLUME.value,
            rule_name="High Change Volume",
            description="Number of mutated records in micro-batch exceeds maximum allowed threshold.",
            threshold=max_changed_records,
            observed_value=changed_records,
            severity=GuardrailSeverity.MEDIUM,
            message=(
                f"Batch contains {changed_records} changed records, exceeding threshold of {max_changed_records}."
            ),
        )
    return None


def check_population_impact(
    evaluated_records: int,
    affected_records: int,
    max_affected_population_ratio: float,
    min_evaluated_records: int,
) -> Optional[TriggeredRule]:
    """Rule 2: Detect when an unusually high percentage of evaluated records mutate."""
    if evaluated_records < min_evaluated_records:
        return None

    ratio = round(affected_records / max(evaluated_records, 1), 4)
    if ratio > max_affected_population_ratio:
        pct_observed = round(ratio * 100, 1)
        pct_threshold = round(max_affected_population_ratio * 100, 1)
        return TriggeredRule(
            rule_id=RuleId.HIGH_POPULATION_IMPACT.value,
            rule_name="High Population Impact",
            description="Proportion of evaluated batch records resulting in state mutations exceeds threshold.",
            threshold=max_affected_population_ratio,
            observed_value=ratio,
            severity=GuardrailSeverity.MEDIUM,
            message=(
                f"Batch mutation ratio of {pct_observed}% ({affected_records}/{evaluated_records}) "
                f"exceeds threshold of {pct_threshold}%."
            ),
        )
    return None


def check_quantity_swing(
    max_relative_change: float,
    max_absolute_change: int,
    max_quantity_relative_change: float,
    min_absolute_quantity_change: int,
) -> Optional[TriggeredRule]:
    """Rule 3: Detect large numeric quantity swings beyond relative and absolute bounds."""
    if (
        max_relative_change > max_quantity_relative_change
        and max_absolute_change >= min_absolute_quantity_change
    ):
        pct_observed = round(max_relative_change * 100, 1)
        pct_threshold = round(max_quantity_relative_change * 100, 1)
        return TriggeredRule(
            rule_id=RuleId.LARGE_QUANTITY_SWING.value,
            rule_name="Large Quantity Swing",
            description="Numeric quantity delta exceeds relative change and absolute noise thresholds.",
            threshold={
                "max_relative_change": max_quantity_relative_change,
                "min_absolute_change": min_absolute_quantity_change,
            },
            observed_value={
                "max_relative_change": max_relative_change,
                "max_absolute_change": max_absolute_change,
            },
            severity=GuardrailSeverity.MEDIUM,
            message=(
                f"Maximum quantity swing of {pct_observed}% (abs: {max_absolute_change}) "
                f"exceeds relative threshold of {pct_threshold}% with absolute >= {min_absolute_quantity_change}."
            ),
        )
    return None


def check_mass_deactivation(
    deactivation_count: int,
    max_deactivation_count: int,
) -> Optional[TriggeredRule]:
    """Rule 4: Detect mass transitions from ACTIVE to INACTIVE/DISCONTINUED status."""
    if deactivation_count > max_deactivation_count:
        return TriggeredRule(
            rule_id=RuleId.MASS_DEACTIVATION.value,
            rule_name="Mass Deactivation",
            description="Count of SKUs transitioning to inactive or discontinued status exceeds safe limit.",
            threshold=max_deactivation_count,
            observed_value=deactivation_count,
            severity=GuardrailSeverity.HIGH,
            message=(
                f"Batch contains {deactivation_count} deactivations, exceeding safe limit of {max_deactivation_count}."
            ),
        )
    return None


def check_change_velocity(
    changed_records: int,
    velocity_changes_per_second: float,
    max_changes_per_second: float,
    min_velocity_records: int,
) -> Optional[TriggeredRule]:
    """Rule 5: Detect sudden bursts of changes across a narrow event time window."""
    if (
        changed_records >= min_velocity_records
        and velocity_changes_per_second > max_changes_per_second
    ):
        return TriggeredRule(
            rule_id=RuleId.HIGH_CHANGE_VELOCITY.value,
            rule_name="High Change Velocity",
            description="Rate of change per event second indicates an unexpected burst or runaway process.",
            threshold=max_changes_per_second,
            observed_value=velocity_changes_per_second,
            severity=GuardrailSeverity.MEDIUM,
            message=(
                f"Change velocity of {velocity_changes_per_second:.1f} changes/sec "
                f"exceeds threshold of {max_changes_per_second:.1f} changes/sec."
            ),
        )
    return None


def check_geographic_impact(
    warehouses_affected: int,
    max_warehouses_affected: int,
) -> Optional[TriggeredRule]:
    """Rule 6: Detect changes distributed across an unusually large number of warehouses."""
    if warehouses_affected > max_warehouses_affected:
        return TriggeredRule(
            rule_id=RuleId.WIDE_GEOGRAPHIC_IMPACT.value,
            rule_name="Wide Geographic Impact",
            description="Number of distinct warehouses affected in a single batch exceeds expected threshold.",
            threshold=max_warehouses_affected,
            observed_value=warehouses_affected,
            severity=GuardrailSeverity.MEDIUM,
            message=(
                f"Changes span {warehouses_affected} distinct warehouses, exceeding threshold of {max_warehouses_affected}."
            ),
        )
    return None


def check_validation_status(
    validation_passed: bool,
    validation_failures: list[str],
) -> Optional[TriggeredRule]:
    """Rule 7: Detect underlying SCD2 invariant validation failures."""
    if not validation_passed:
        failure_summary = "; ".join(validation_failures) if validation_failures else "SCD2 invariant check failed"
        return TriggeredRule(
            rule_id=RuleId.SCD2_VALIDATION_FAILURE.value,
            rule_name="SCD2 Invariant Validation Failure",
            description="Deterministic SCD2 temporal or uniqueness invariants were violated.",
            threshold="100% invariant compliance",
            observed_value=f"{len(validation_failures)} rule failures",
            severity=GuardrailSeverity.CRITICAL,
            message=f"SCD2 validation failed: {failure_summary}",
        )
    return None
