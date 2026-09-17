"""Unit tests for the Deterministic Guardrail and Change Significance Engine (V2.3)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from uuid import uuid4
import pytest

from src.scd2_copilot.config import Settings
from src.scd2_copilot.db.models import InventorySourceRow
from src.scd2_copilot.guardrail.engine import GuardrailEngine
from src.scd2_copilot.guardrail.models import (
    GuardrailDecisionType,
    GuardrailSeverity,
    RuleId,
)
from src.scd2_copilot.models import (
    ChangeRecord,
    ChangeReport,
    ChangeType,
    FieldChange,
    ValidationReport,
    ValidationRule,
    ValidationStatus,
)
from src.scd2_copilot.worker.batch import MicroBatch


def make_source_row(
    sku_id: str,
    warehouse_id: str = "WH-1",
    quantity: int = 100,
    reorder: int = 20,
    status: str = "ACTIVE",
    updated_at: datetime | None = None,
) -> InventorySourceRow:
    """Helper to construct an InventorySourceRow."""
    return InventorySourceRow(
        sku_id=sku_id,
        warehouse_id=warehouse_id,
        quantity_on_hand=quantity,
        reorder_level=reorder,
        status=status,
        updated_at=updated_at or datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc),
    )


def make_change_record(
    sku_id: str,
    warehouse_id: str = "WH-1",
    change_type: ChangeType = ChangeType.CHANGED,
    field_changes: list[FieldChange] | None = None,
) -> ChangeRecord:
    """Helper to construct a ChangeRecord."""
    return ChangeRecord(
        business_key_values={"sku_id": sku_id, "warehouse_id": warehouse_id},
        change_type=change_type,
        field_changes=field_changes or [],
    )


class TestGuardrailRulesUnit:
    """Unit tests for individual guardrail rules and thresholds."""

    @pytest.fixture
    def engine(self) -> GuardrailEngine:
        settings = Settings(
            guardrail_enabled=True,
            guardrail_max_changed_records=25,
            guardrail_max_affected_population_ratio=0.80,
            guardrail_min_evaluated_records_for_ratio=10,
            guardrail_max_quantity_relative_change=3.0,
            guardrail_min_absolute_quantity_change=50,
            guardrail_max_deactivation_count=5,
            guardrail_max_changes_per_second=50.0,
            guardrail_min_velocity_records=10,
            guardrail_max_warehouses_affected=5,
        )
        return GuardrailEngine(settings=settings)

    def test_normal_batch_evaluation(self, engine: GuardrailEngine) -> None:
        """Verify that a modest, typical batch is classified as NORMAL with LOW severity."""
        t0 = datetime(2026, 9, 16, 10, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 9, 16, 10, 1, 0, tzinfo=timezone.utc)
        rows = [
            make_source_row("SKU-1", "WH-1", 100, updated_at=t0),
            make_source_row("SKU-2", "WH-1", 120, updated_at=t1),
            make_source_row("SKU-3", "WH-1", 80, updated_at=t1),
        ]
        batch = MicroBatch(source_records=rows)
        changed = [
            make_change_record(
                "SKU-1",
                "WH-1",
                field_changes=[FieldChange("quantity_on_hand", 90, 100)],
            )
        ]
        report = ChangeReport(
            changed=changed,
            unchanged=[make_change_record("SKU-2"), make_change_record("SKU-3")],
        )
        val_report = ValidationReport(
            rules=[ValidationRule("rule1", ValidationStatus.PASS, "ok")]
        )

        decision = engine.evaluate(batch=batch, change_report=report, validation_report=val_report)

        assert decision.is_normal
        assert not decision.is_suspicious
        assert decision.decision == GuardrailDecisionType.NORMAL
        assert decision.severity == GuardrailSeverity.LOW
        assert len(decision.triggered_rules) == 0
        assert "normal" in decision.reasons[0].lower()
        assert decision.evidence.evaluated_records == 3
        assert decision.evidence.changed_records == 1

    def test_high_change_volume_rule_triggered(self, engine: GuardrailEngine) -> None:
        """Verify Rule 1: HIGH_CHANGE_VOLUME triggers when changed count exceeds threshold."""
        rows = [make_source_row(f"SKU-{i}") for i in range(30)]
        batch = MicroBatch(source_records=rows)
        changed = [
            make_change_record(f"SKU-{i}", field_changes=[FieldChange("quantity_on_hand", 10, 20)])
            for i in range(30)
        ]
        report = ChangeReport(changed=changed)

        decision = engine.evaluate(batch=batch, change_report=report)

        assert decision.is_suspicious
        rule_ids = [r.rule_id for r in decision.triggered_rules]
        assert RuleId.HIGH_CHANGE_VOLUME.value in rule_ids
        rule = next(r for r in decision.triggered_rules if r.rule_id == RuleId.HIGH_CHANGE_VOLUME.value)
        assert rule.observed_value == 30
        assert rule.threshold == 25

    def test_high_population_impact_rule(self, engine: GuardrailEngine) -> None:
        """Verify Rule 2: HIGH_POPULATION_IMPACT triggers when ratio exceeds threshold."""
        # 10 evaluated, 9 changed = 90% > 80%
        rows = [make_source_row(f"SKU-{i}") for i in range(10)]
        batch = MicroBatch(source_records=rows)
        changed = [
            make_change_record(f"SKU-{i}", field_changes=[FieldChange("reorder_level", 10, 15)])
            for i in range(9)
        ]
        unchanged = [make_change_record("SKU-9")]
        report = ChangeReport(changed=changed, unchanged=unchanged)

        decision = engine.evaluate(batch=batch, change_report=report)

        rule_ids = [r.rule_id for r in decision.triggered_rules]
        assert RuleId.HIGH_POPULATION_IMPACT.value in rule_ids
        rule = next(r for r in decision.triggered_rules if r.rule_id == RuleId.HIGH_POPULATION_IMPACT.value)
        assert rule.observed_value == 0.9

    def test_population_impact_noise_guard_on_small_batch(self, engine: GuardrailEngine) -> None:
        """Verify Rule 2 does not trigger on small batches below min_evaluated_records."""
        # 3 evaluated, all 3 changed (100%), but evaluated < 10
        rows = [make_source_row(f"SKU-{i}") for i in range(3)]
        batch = MicroBatch(source_records=rows)
        changed = [make_change_record(f"SKU-{i}") for i in range(3)]
        report = ChangeReport(changed=changed)

        decision = engine.evaluate(batch=batch, change_report=report)

        rule_ids = [r.rule_id for r in decision.triggered_rules]
        assert RuleId.HIGH_POPULATION_IMPACT.value not in rule_ids

    def test_large_quantity_swing_rule(self, engine: GuardrailEngine) -> None:
        """Verify Rule 3: LARGE_QUANTITY_SWING triggers on large relative AND absolute swings."""
        # Old = 100, New = 500 (rel delta = 4.0 = 400% > 3.0, abs delta = 400 >= 50)
        rows = [make_source_row("SKU-1")]
        batch = MicroBatch(source_records=rows)
        changed = [
            make_change_record(
                "SKU-1",
                field_changes=[FieldChange("quantity_on_hand", 100, 500)],
            )
        ]
        report = ChangeReport(changed=changed)

        decision = engine.evaluate(batch=batch, change_report=report)

        rule_ids = [r.rule_id for r in decision.triggered_rules]
        assert RuleId.LARGE_QUANTITY_SWING.value in rule_ids

    def test_quantity_swing_absolute_noise_guard(self, engine: GuardrailEngine) -> None:
        """Verify Rule 3 does not trigger on high percentage swings with tiny absolute numbers."""
        # Old = 1, New = 5 (rel delta = 4.0 = 400% > 3.0, but abs delta = 4 < 50)
        rows = [make_source_row("SKU-1")]
        batch = MicroBatch(source_records=rows)
        changed = [
            make_change_record(
                "SKU-1",
                field_changes=[FieldChange("quantity_on_hand", 1, 5)],
            )
        ]
        report = ChangeReport(changed=changed)

        decision = engine.evaluate(batch=batch, change_report=report)

        rule_ids = [r.rule_id for r in decision.triggered_rules]
        assert RuleId.LARGE_QUANTITY_SWING.value not in rule_ids

    def test_zero_baseline_quantity_handling(self, engine: GuardrailEngine) -> None:
        """Verify zero baseline quantity doesn't raise ZeroDivisionError."""
        rows = [make_source_row("SKU-1")]
        batch = MicroBatch(source_records=rows)
        changed = [
            make_change_record(
                "SKU-1",
                field_changes=[FieldChange("quantity_on_hand", 0, 100)],
            )
        ]
        report = ChangeReport(changed=changed)

        decision = engine.evaluate(batch=batch, change_report=report)
        assert decision.evidence.max_quantity_relative_change == 100.0
        assert decision.evidence.max_quantity_absolute_change == 100
        # Triggers because 100.0 > 3.0 and abs 100 >= 50
        assert RuleId.LARGE_QUANTITY_SWING.value in [r.rule_id for r in decision.triggered_rules]

    def test_mass_deactivation_rule(self, engine: GuardrailEngine) -> None:
        """Verify Rule 4: MASS_DEACTIVATION triggers on multiple ACTIVE -> INACTIVE/DISCONTINUED."""
        # 6 deactivations > threshold 5
        rows = [make_source_row(f"SKU-{i}") for i in range(6)]
        batch = MicroBatch(source_records=rows)
        changed = [
            make_change_record(
                f"SKU-{i}",
                field_changes=[FieldChange("status", "ACTIVE", "INACTIVE")],
            )
            for i in range(6)
        ]
        report = ChangeReport(changed=changed)

        decision = engine.evaluate(batch=batch, change_report=report)

        rule_ids = [r.rule_id for r in decision.triggered_rules]
        assert RuleId.MASS_DEACTIVATION.value in rule_ids
        assert decision.severity == GuardrailSeverity.HIGH

    def test_high_change_velocity_rule(self, engine: GuardrailEngine) -> None:
        """Verify Rule 5: HIGH_CHANGE_VELOCITY triggers on burst changes across small time window."""
        t0 = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 9, 16, 12, 0, 0, 500000, tzinfo=timezone.utc)  # 0.5s window
        # 60 records changed in 0.5s window -> velocity = 60 / 1.0 = 60 changes/sec > 50
        rows = [make_source_row(f"SKU-{i}", updated_at=(t0 if i % 2 == 0 else t1)) for i in range(60)]
        batch = MicroBatch(source_records=rows)
        changed = [make_change_record(f"SKU-{i}") for i in range(60)]
        report = ChangeReport(changed=changed)

        decision = engine.evaluate(batch=batch, change_report=report)

        rule_ids = [r.rule_id for r in decision.triggered_rules]
        assert RuleId.HIGH_CHANGE_VELOCITY.value in rule_ids

    def test_wide_geographic_impact_rule(self, engine: GuardrailEngine) -> None:
        """Verify Rule 6: WIDE_GEOGRAPHIC_IMPACT triggers when affected warehouses > threshold."""
        # 6 distinct warehouses > threshold 5
        rows = [make_source_row(f"SKU-{i}", warehouse_id=f"WH-{i}") for i in range(6)]
        batch = MicroBatch(source_records=rows)
        changed = [make_change_record(f"SKU-{i}", warehouse_id=f"WH-{i}") for i in range(6)]
        report = ChangeReport(changed=changed)

        decision = engine.evaluate(batch=batch, change_report=report)

        rule_ids = [r.rule_id for r in decision.triggered_rules]
        assert RuleId.WIDE_GEOGRAPHIC_IMPACT.value in rule_ids

    def test_scd2_validation_failure_rule(self, engine: GuardrailEngine) -> None:
        """Verify Rule 7: SCD2_VALIDATION_FAILURE triggers CRITICAL severity."""
        rows = [make_source_row("SKU-1")]
        batch = MicroBatch(source_records=rows)
        report = ChangeReport(changed=[make_change_record("SKU-1")])
        failed_val = ValidationReport(
            rules=[ValidationRule("Rule 2: Temporal Interval Consistency", ValidationStatus.FAIL, "Interval reversed")]
        )

        decision = engine.evaluate(batch=batch, change_report=report, validation_report=failed_val)

        assert decision.is_suspicious
        assert decision.severity == GuardrailSeverity.CRITICAL
        rule_ids = [r.rule_id for r in decision.triggered_rules]
        assert RuleId.SCD2_VALIDATION_FAILURE.value in rule_ids

    def test_multiple_rules_escalate_severity_to_high(self, engine: GuardrailEngine) -> None:
        """Verify that multiple medium-severity rules escalate decision severity to HIGH."""
        # Triggers HIGH_CHANGE_VOLUME (> 25) AND WIDE_GEOGRAPHIC_IMPACT (> 5 warehouses)
        rows = [make_source_row(f"SKU-{i}", warehouse_id=f"WH-{i % 7}") for i in range(28)]
        batch = MicroBatch(source_records=rows)
        changed = [make_change_record(f"SKU-{i}", warehouse_id=f"WH-{i % 7}") for i in range(28)]
        report = ChangeReport(changed=changed)

        decision = engine.evaluate(batch=batch, change_report=report)

        assert decision.is_suspicious
        assert len(decision.triggered_rules) >= 2
        assert decision.severity == GuardrailSeverity.HIGH

    def test_deterministic_rule_ordering(self, engine: GuardrailEngine) -> None:
        """Verify triggered rules are always sorted deterministically by rule_id."""
        rows = [make_source_row(f"SKU-{i}", warehouse_id=f"WH-{i % 8}") for i in range(30)]
        batch = MicroBatch(source_records=rows)
        changed = [
            make_change_record(
                f"SKU-{i}",
                warehouse_id=f"WH-{i % 8}",
                field_changes=[FieldChange("quantity_on_hand", 10, 500)],
            )
            for i in range(30)
        ]
        report = ChangeReport(changed=changed)

        decision = engine.evaluate(batch=batch, change_report=report)
        rule_ids = [r.rule_id for r in decision.triggered_rules]
        assert rule_ids == sorted(rule_ids)

    def test_repeated_evaluation_is_idempotent_and_identical(self, engine: GuardrailEngine) -> None:
        """Verify repeated evaluation of identical inputs yields identical decisions and evidence."""
        rows = [make_source_row(f"SKU-{i}") for i in range(5)]
        batch = MicroBatch(source_records=rows)
        changed = [make_change_record(f"SKU-{i}") for i in range(5)]
        report = ChangeReport(changed=changed)

        d1 = engine.evaluate(batch=batch, change_report=report)
        d2 = engine.evaluate(batch=batch, change_report=report)

        assert d1.decision == d2.decision
        assert d1.severity == d2.severity
        assert len(d1.triggered_rules) == len(d2.triggered_rules)
        assert d1.evidence.to_dict() == d2.evidence.to_dict()

    def test_disabled_guardrail_returns_normal(self) -> None:
        """Verify that disabling guardrail in config returns NORMAL regardless of metrics."""
        disabled_engine = GuardrailEngine(settings=Settings(guardrail_enabled=False))
        rows = [make_source_row(f"SKU-{i}") for i in range(100)]
        batch = MicroBatch(source_records=rows)
        changed = [make_change_record(f"SKU-{i}") for i in range(100)]
        report = ChangeReport(changed=changed)

        decision = disabled_engine.evaluate(batch=batch, change_report=report)

        assert decision.is_normal
        assert decision.severity == GuardrailSeverity.LOW
        assert len(decision.triggered_rules) == 0

    def test_decision_to_dict_json_serializable(self, engine: GuardrailEngine) -> None:
        """Verify GuardrailDecision.to_dict() can be serialized to valid JSON without error."""
        run_id = uuid4()
        rows = [make_source_row("SKU-1")]
        batch = MicroBatch(source_records=rows)
        report = ChangeReport(
            changed=[
                make_change_record(
                    "SKU-1",
                    field_changes=[FieldChange("quantity_on_hand", 10, 200)],
                )
            ]
        )

        decision = engine.evaluate(batch=batch, change_report=report, run_id=run_id)
        d_dict = decision.to_dict()

        json_str = json.dumps(d_dict)
        assert json_str is not None
        assert d_dict["run_id"] == str(run_id)
        assert "evidence" in d_dict
        assert "triggered_rules" in d_dict


class TestGuardrailConfigValidation:
    """Test configuration validation bounds for guardrail settings."""

    def test_rejects_negative_thresholds(self) -> None:
        with pytest.raises(ValueError, match="cannot be negative"):
            Settings(guardrail_max_changed_records=-5)

        with pytest.raises(ValueError, match="cannot be negative"):
            Settings(guardrail_max_quantity_relative_change=-1.0)

    def test_rejects_invalid_population_ratio(self) -> None:
        with pytest.raises(ValueError, match="between 0.0 and 1.0"):
            Settings(guardrail_max_affected_population_ratio=1.5)

        with pytest.raises(ValueError, match="between 0.0 and 1.0"):
            Settings(guardrail_max_affected_population_ratio=-0.1)
