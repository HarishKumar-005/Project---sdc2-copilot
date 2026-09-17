"""Deterministic Guardrail and Change Significance Engine.

Evaluates micro-batches and SCD2 change reports against transparent,
deterministic operational rules to categorize batches as NORMAL or SUSPICIOUS.

V3 Phase 3: extract_evidence() is now domain-agnostic.  It consumes the
MicroBatch's configured column lists (key_columns, tracked_columns,
timestamp_column) rather than hardcoded inventory field names.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import TYPE_CHECKING, Any, Optional
from uuid import UUID

from ..config import Settings, get_settings
from ..models import ChangeReport, ValidationReport

if TYPE_CHECKING:
    from ..source.models import MonitorConfig
    from ..worker.batch import MicroBatch
from .models import (
    GuardrailDecision,
    GuardrailDecisionType,
    GuardrailEvidence,
    GuardrailSeverity,
    RuleId,
    TriggeredRule,
)
from .rules import (
    check_change_velocity,
    check_change_volume,
    check_geographic_impact,
    check_mass_deactivation,
    check_population_impact,
    check_quantity_swing,
    check_validation_status,
)

logger = logging.getLogger("scd2_copilot.guardrail")


def _try_numeric(value: Any) -> Optional[float]:
    """Return float conversion of value, or None if not numeric."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class GuardrailEngine:
    """Deterministic, explainable change significance and guardrail engine."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        cfg = settings or get_settings()
        self.enabled: bool = cfg.guardrail_enabled
        self.max_changed_records: int = cfg.guardrail_max_changed_records
        self.max_affected_population_ratio: float = cfg.guardrail_max_affected_population_ratio
        self.min_evaluated_records_for_ratio: int = cfg.guardrail_min_evaluated_records_for_ratio
        self.max_quantity_relative_change: float = cfg.guardrail_max_quantity_relative_change
        self.min_absolute_quantity_change: int = cfg.guardrail_min_absolute_quantity_change
        self.max_deactivation_count: int = cfg.guardrail_max_deactivation_count
        self.max_changes_per_second: float = cfg.guardrail_max_changes_per_second
        self.min_velocity_records: int = cfg.guardrail_min_velocity_records
        self.max_warehouses_affected: int = cfg.guardrail_max_warehouses_affected

    def extract_evidence(
        self,
        batch: "MicroBatch",
        change_report: ChangeReport,
        validation_report: Optional[ValidationReport] = None,
        monitor_config: Optional["MonitorConfig"] = None,
    ) -> GuardrailEvidence:
        """Deterministically extract structured evidence and metrics from the batch and SCD2 output.

        Domain-agnostic implementation (V3 Phase 3):
        - Numeric delta analysis runs over ALL numeric-compatible tracked columns.
        - Categorical transition analysis runs over ALL non-numeric tracked columns.
        - Key dispersion uses the business key with highest cardinality across
          changed + new records (no hardcoded 'warehouse_id' or 'sku_id').
        - Velocity timestamp extraction uses batch.timestamp_column, not 'updated_at'.
        """
        evaluated_records = max(len(batch.source_records), 0)
        changed_records = len(change_report.changed)
        new_records = len(change_report.new)
        unchanged_records = len(change_report.unchanged)
        mutated_records = changed_records + new_records

        # Population impact ratio (denominator is the evaluated micro-batch population)
        affected_ratio = (
            round(mutated_records / evaluated_records, 4) if evaluated_records > 0 else 0.0
        )

        # ── Key Dispersion ────────────────────────────────────────────────────
        # Count distinct values per business key column across changed + new records.
        # Use the column with highest cardinality as the dispersion metric.
        # This is domain-agnostic: warehouse_id for inventory, region for logistics, etc.
        key_columns: list[str] = list(batch.key_columns or [])
        if not key_columns and monitor_config is not None:
            key_columns = list(monitor_config.business_keys)

        key_distinct: dict[str, set[str]] = {kc: set() for kc in key_columns}
        for rec in list(change_report.changed) + list(change_report.new):
            bk = rec.business_key_values
            for kc in key_columns:
                val = bk.get(kc)
                if val is not None:
                    key_distinct[kc].add(str(val))

        dispersion_key_column: Optional[str] = None
        key_value_dispersion: int = 0
        if key_distinct:
            dispersion_key_column = max(key_distinct, key=lambda k: len(key_distinct[k]))
            key_value_dispersion = len(key_distinct.get(dispersion_key_column, set()))

        # Backward-compat: populate warehouses_affected / skus_affected for inventory monitors
        warehouses_set = key_distinct.get("warehouse_id", set())
        skus_set = key_distinct.get("sku_id", set())
        warehouses_affected = len(warehouses_set)
        skus_affected = len(skus_set)

        # ── Numeric & Categorical Column Analysis ─────────────────────────────
        # Run over ALL FieldChange records across all changed rows.
        # Numeric: accumulate delta stats across all numeric-castable columns.
        # Categorical: count transitions for all non-numeric columns.
        per_column_change_counts: dict[str, int] = {}

        max_rel_change: float = 0.0
        max_abs_change: int = 0
        total_abs_change: int = 0
        numeric_col_worst_rel: dict[str, float] = {}

        categorical_col_transition_count: dict[str, int] = {}

        DEACTIVATION_TARGETS = {"INACTIVE", "DISCONTINUED", "DECOMMISSIONED", "CANCELLED", "CLOSED"}
        status_deactivations: int = 0

        for rec in change_report.changed:
            for fc in rec.field_changes:
                col_name = str(fc.column).strip()

                # Per-column change count
                per_column_change_counts[col_name] = per_column_change_counts.get(col_name, 0) + 1

                # Try numeric interpretation first
                old_f = _try_numeric(fc.old_value)
                new_f = _try_numeric(fc.new_value)

                if old_f is not None and new_f is not None:
                    abs_delta = abs(new_f - old_f)
                    abs_delta_int = int(abs_delta)
                    total_abs_change += abs_delta_int
                    if abs_delta_int > max_abs_change:
                        max_abs_change = abs_delta_int

                    if old_f > 0:
                        rel_delta = round(abs_delta / old_f, 4)
                    elif old_f == 0 and new_f != 0:
                        rel_delta = round(float(abs_delta), 4)
                    else:
                        rel_delta = 0.0

                    if rel_delta > max_rel_change:
                        max_rel_change = rel_delta
                    numeric_col_worst_rel[col_name] = max(
                        numeric_col_worst_rel.get(col_name, 0.0), rel_delta
                    )
                else:
                    # Categorical transition
                    old_st = str(fc.old_value).strip().upper() if fc.old_value is not None else ""
                    new_st = str(fc.new_value).strip().upper() if fc.new_value is not None else ""
                    if old_st and new_st and old_st != new_st:
                        categorical_col_transition_count[col_name] = (
                            categorical_col_transition_count.get(col_name, 0) + 1
                        )

                    # Inventory-compat: status deactivation counting
                    if col_name.lower() == "status":
                        if old_st == "ACTIVE" and new_st in DEACTIVATION_TARGETS:
                            status_deactivations += 1

        # Identify worst-case numeric column and categorical column
        numeric_column_analyzed: Optional[str] = None
        if numeric_col_worst_rel:
            numeric_column_analyzed = max(numeric_col_worst_rel, key=lambda k: numeric_col_worst_rel[k])

        categorical_column_analyzed: Optional[str] = None
        if categorical_col_transition_count:
            categorical_column_analyzed = max(
                categorical_col_transition_count, key=lambda k: categorical_col_transition_count[k]
            )

        # ── Event-time Velocity Analysis ──────────────────────────────────────
        # Use batch.timestamp_column — domain-agnostic; could be updated_at, last_modified, etc.
        ts_col = batch.timestamp_column
        timestamps = []
        for r in batch.source_records:
            val = r.get(ts_col) if isinstance(r, dict) else getattr(r, ts_col, None)
            if val is not None:
                timestamps.append(val)

        if len(timestamps) >= 2:
            min_ts = min(timestamps)
            max_ts = max(timestamps)
            event_window = max((max_ts - min_ts).total_seconds(), 0.0)
        else:
            event_window = 0.0

        event_window_seconds = round(event_window, 3)
        velocity = (
            round(changed_records / max(event_window_seconds, 1.0), 2)
            if changed_records > 0
            else 0.0
        )

        # ── Validation Status ─────────────────────────────────────────────────
        val_passed = validation_report.passed if validation_report is not None else True
        val_failures: list[str] = []
        if validation_report is not None and not validation_report.passed:
            val_failures = [r.message for r in validation_report.rules if r.status.value == "fail"]

        # ── Metrics dict (generic + inventory-compat) ─────────────────────────
        metrics: dict[str, Any] = {
            "mutated_records": mutated_records,
            "new_keys_count": new_records,
            "changed_keys_count": changed_records,
            "unchanged_keys_count": unchanged_records,
            "warehouses_list": sorted(warehouses_set),
            "skus_count": skus_affected,
            "key_value_dispersion": key_value_dispersion,
            "dispersion_key_column": dispersion_key_column,
            "numeric_column_analyzed": numeric_column_analyzed,
            "categorical_column_analyzed": categorical_column_analyzed,
            "per_column_change_counts": dict(per_column_change_counts),
        }

        return GuardrailEvidence(
            evaluated_records=evaluated_records,
            changed_records=changed_records,
            new_records=new_records,
            unchanged_records=unchanged_records,
            affected_population_ratio=affected_ratio,
            warehouses_affected=warehouses_affected,
            skus_affected=skus_affected,
            max_quantity_relative_change=max_rel_change,
            max_quantity_absolute_change=max_abs_change,
            total_quantity_absolute_change=total_abs_change,
            status_deactivations_count=status_deactivations,
            key_value_dispersion=key_value_dispersion,
            dispersion_key_column=dispersion_key_column,
            numeric_column_analyzed=numeric_column_analyzed,
            categorical_column_analyzed=categorical_column_analyzed,
            per_column_change_counts=per_column_change_counts,
            event_window_seconds=event_window_seconds,
            velocity_changes_per_second=velocity,
            validation_passed=val_passed,
            validation_failures=val_failures,
            metrics=metrics,
        )

    def evaluate(
        self,
        batch: "MicroBatch",
        change_report: ChangeReport,
        validation_report: Optional[ValidationReport] = None,
        run_id: Optional[UUID] = None,
        monitor_config: Optional["MonitorConfig"] = None,
    ) -> GuardrailDecision:
        """Evaluate a micro-batch and change report against deterministic guardrail rules.

        Returns:
            GuardrailDecision containing classification (NORMAL/SUSPICIOUS),
            deterministic severity, triggered rules, human-readable reasons,
            and machine-readable evidence.
        """
        now = datetime.now(timezone.utc)
        evidence = self.extract_evidence(
            batch=batch,
            change_report=change_report,
            validation_report=validation_report,
            monitor_config=monitor_config,
        )

        if not self.enabled:
            logger.debug("Guardrail evaluation is disabled via configuration.")
            return GuardrailDecision(
                decision=GuardrailDecisionType.NORMAL,
                severity=GuardrailSeverity.LOW,
                triggered_rules=[],
                reasons=["Guardrail evaluation is disabled by configuration."],
                evidence=evidence,
                evaluated_at=now,
                batch_id=batch.batch_id,
                run_id=run_id,
            )

        # Deterministic rule evaluation.
        # check_geographic_impact now receives key_value_dispersion (generic highest-cardinality
        # key count) which equals warehouses_affected for inventory when warehouse_id is highest.
        triggered_candidates: list[Optional[TriggeredRule]] = [
            check_change_volume(
                changed_records=evidence.changed_records,
                max_changed_records=self.max_changed_records,
            ),
            check_population_impact(
                evaluated_records=evidence.evaluated_records,
                affected_records=evidence.changed_records + evidence.new_records,
                max_affected_population_ratio=self.max_affected_population_ratio,
                min_evaluated_records=self.min_evaluated_records_for_ratio,
            ),
            check_quantity_swing(
                max_relative_change=evidence.max_quantity_relative_change,
                max_absolute_change=evidence.max_quantity_absolute_change,
                max_quantity_relative_change=self.max_quantity_relative_change,
                min_absolute_quantity_change=self.min_absolute_quantity_change,
            ),
            check_mass_deactivation(
                deactivation_count=evidence.status_deactivations_count,
                max_deactivation_count=self.max_deactivation_count,
            ),
            check_change_velocity(
                changed_records=evidence.changed_records,
                velocity_changes_per_second=evidence.velocity_changes_per_second,
                max_changes_per_second=self.max_changes_per_second,
                min_velocity_records=self.min_velocity_records,
            ),
            check_geographic_impact(
                warehouses_affected=evidence.key_value_dispersion,
                max_warehouses_affected=self.max_warehouses_affected,
            ),
            check_validation_status(
                validation_passed=evidence.validation_passed,
                validation_failures=evidence.validation_failures,
            ),
        ]

        # Deterministic rule sorting by rule_id
        active_triggered: list[TriggeredRule] = sorted(
            [r for r in triggered_candidates if r is not None],
            key=lambda x: x.rule_id,
        )

        # Deterministic Severity and Decision Derivation
        rule_ids = {r.rule_id for r in active_triggered}

        if RuleId.SCD2_VALIDATION_FAILURE.value in rule_ids:
            severity = GuardrailSeverity.CRITICAL
            decision = GuardrailDecisionType.SUSPICIOUS
        elif any(r.severity == GuardrailSeverity.CRITICAL for r in active_triggered):
            severity = GuardrailSeverity.CRITICAL
            decision = GuardrailDecisionType.SUSPICIOUS
        elif len(active_triggered) >= 2 or RuleId.MASS_DEACTIVATION.value in rule_ids:
            severity = GuardrailSeverity.HIGH
            decision = GuardrailDecisionType.SUSPICIOUS
        elif len(active_triggered) >= 1:
            severity = GuardrailSeverity.MEDIUM
            decision = GuardrailDecisionType.SUSPICIOUS
        else:
            severity = GuardrailSeverity.LOW
            decision = GuardrailDecisionType.NORMAL

        # Reasons construction
        if decision == GuardrailDecisionType.SUSPICIOUS:
            reasons = [r.message for r in active_triggered]
        else:
            reasons = ["All evaluated operational metrics within normal parameters."]

        logger.debug(
            "Guardrail outcome: decision=%s, severity=%s, rules_triggered=%d",
            decision.value,
            severity.value,
            len(active_triggered),
        )

        return GuardrailDecision(
            decision=decision,
            severity=severity,
            triggered_rules=active_triggered,
            reasons=reasons,
            evidence=evidence,
            evaluated_at=now,
            batch_id=batch.batch_id,
            run_id=run_id,
        )