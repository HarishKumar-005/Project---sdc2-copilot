"""Generic guardrail tests (V3 Phase 3).

Verifies that the guardrail engine is fully domain-agnostic:
- Works with arbitrary column names (price, stock_count, region, tier, ...)
- Numeric analysis runs on ANY numeric column, not just quantity_on_hand.
- Categorical analysis runs on ANY non-numeric column, not just status.
- Velocity uses batch.timestamp_column, not the hardcoded updated_at attribute.
- Key dispersion uses highest-cardinality business key, not hardcoded warehouse_id.
- Inventory demo scenarios still produce the same results as before.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from typing import Any, Optional
from uuid import uuid4

import pytest

from src.scd2_copilot.config import Settings
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


# ── Helpers ────────────────────────────────────────────────────────────────────


@dataclass
class GenericRow:
    """A generic source row that supports arbitrary column access via getattr."""

    _data: dict[str, Any] = field(default_factory=dict)
    # timestamp_column is accessed via getattr(r, ts_col) in the engine
    last_modified: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def __getattr__(self, name: str) -> Any:
        try:
            return object.__getattribute__(self, "_data")[name]
        except KeyError:
            raise AttributeError(name)


def generic_row(ts_col: str, ts: datetime, **cols: Any) -> GenericRow:
    """Build a GenericRow with arbitrary column values."""
    row = GenericRow(_data=cols)
    setattr(row, ts_col, ts)
    return row


def make_generic_batch(
    rows: list[Any],
    key_cols: list[str],
    tracked_cols: list[str],
    ts_col: str = "updated_at",
) -> MicroBatch:
    return MicroBatch(
        source_records=rows,
        key_columns=key_cols,
        tracked_columns=tracked_cols,
        timestamp_column=ts_col,
        source_name="test_monitor",
    )


def make_record(
    key_vals: dict[str, Any],
    change_type: ChangeType = ChangeType.CHANGED,
    field_changes: list[FieldChange] | None = None,
) -> ChangeRecord:
    return ChangeRecord(
        business_key_values=key_vals,
        change_type=change_type,
        field_changes=field_changes or [],
    )


@pytest.fixture
def engine() -> GuardrailEngine:
    return GuardrailEngine(
        settings=Settings(
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
    )


# ── Test 1: Generic normal change ─────────────────────────────────────────────


class TestGenericNormalChange:
    def test_normal_with_arbitrary_columns(self, engine: GuardrailEngine) -> None:
        """NORMAL result with columns named price, stock_count, region."""
        ts = datetime(2026, 9, 16, 10, 0, 0, tzinfo=timezone.utc)
        rows = [
            generic_row("updated_at", ts, price=99.99, stock_count=50, region="north"),
            generic_row("updated_at", ts, price=150.0, stock_count=30, region="south"),
        ]
        batch = make_generic_batch(
            rows, key_cols=["region"], tracked_cols=["price", "stock_count"]
        )
        changed = [
            make_record(
                {"region": "north"},
                field_changes=[FieldChange("price", 99.99, 101.0)],
            )
        ]
        report = ChangeReport(changed=changed, unchanged=[make_record({"region": "south"})])

        decision = engine.evaluate(batch=batch, change_report=report)

        assert decision.is_normal
        assert decision.severity == GuardrailSeverity.LOW
        assert len(decision.triggered_rules) == 0


# ── Test 2: Generic high population impact ─────────────────────────────────────


class TestGenericHighPopulationImpact:
    def test_triggers_on_non_inventory_monitor(self, engine: GuardrailEngine) -> None:
        """HIGH_POPULATION_IMPACT fires for non-inventory monitor at 90% mutation ratio."""
        ts = datetime(2026, 9, 16, 10, 0, 0, tzinfo=timezone.utc)
        rows = [generic_row("updated_at", ts, price=100.0) for _ in range(10)]
        batch = make_generic_batch(rows, key_cols=["product_id"], tracked_cols=["price"])
        changed = [
            make_record({"product_id": str(i)}, field_changes=[FieldChange("price", 100.0, 110.0)])
            for i in range(9)
        ]
        unchanged = [make_record({"product_id": "9"})]
        report = ChangeReport(changed=changed, unchanged=unchanged)

        decision = engine.evaluate(batch=batch, change_report=report)

        rule_ids = [r.rule_id for r in decision.triggered_rules]
        assert RuleId.HIGH_POPULATION_IMPACT.value in rule_ids


# ── Test 3: Generic numeric column ─────────────────────────────────────────────


class TestGenericNumericColumn:
    def test_price_column_triggers_large_numeric_change(self, engine: GuardrailEngine) -> None:
        """LARGE_QUANTITY_SWING fires on 'price' column with > 3x relative swing."""
        ts = datetime(2026, 9, 16, 10, 0, 0, tzinfo=timezone.utc)
        rows = [generic_row("updated_at", ts, price=100, tier="gold")]
        batch = make_generic_batch(rows, key_cols=["product_id"], tracked_cols=["price", "tier"])
        changed = [
            make_record(
                {"product_id": "P1"},
                field_changes=[FieldChange("price", 100, 500)],  # 400% > 300%, abs 400 >= 50
            )
        ]
        report = ChangeReport(changed=changed)

        decision = engine.evaluate(batch=batch, change_report=report)

        rule_ids = [r.rule_id for r in decision.triggered_rules]
        assert RuleId.LARGE_QUANTITY_SWING.value in rule_ids
        # Evidence should identify 'price' as the analyzed column
        assert decision.evidence.numeric_column_analyzed == "price"
        assert decision.evidence.max_quantity_relative_change == pytest.approx(4.0, abs=0.01)

    def test_stock_count_not_quantity_on_hand(self, engine: GuardrailEngine) -> None:
        """Numeric analysis works on 'stock_count', not just 'quantity_on_hand'."""
        ts = datetime(2026, 9, 16, 10, 0, 0, tzinfo=timezone.utc)
        rows = [generic_row("updated_at", ts, stock_count=100)]
        batch = make_generic_batch(rows, key_cols=["item_id"], tracked_cols=["stock_count"])
        changed = [
            make_record(
                {"item_id": "I1"},
                field_changes=[FieldChange("stock_count", 100, 600)],  # 500%, abs 500
            )
        ]
        report = ChangeReport(changed=changed)

        decision = engine.evaluate(batch=batch, change_report=report)

        assert RuleId.LARGE_QUANTITY_SWING.value in [r.rule_id for r in decision.triggered_rules]
        assert decision.evidence.numeric_column_analyzed == "stock_count"

    def test_absolute_noise_guard_still_works_on_generic_column(self, engine: GuardrailEngine) -> None:
        """Absolute noise guard (min_absolute_quantity_change) applies to generic columns too."""
        ts = datetime(2026, 9, 16, 10, 0, 0, tzinfo=timezone.utc)
        rows = [generic_row("updated_at", ts, price=1)]
        batch = make_generic_batch(rows, key_cols=["product_id"], tracked_cols=["price"])
        changed = [
            make_record(
                {"product_id": "P1"},
                field_changes=[FieldChange("price", 1, 5)],  # 400% but abs=4 < 50
            )
        ]
        report = ChangeReport(changed=changed)

        decision = engine.evaluate(batch=batch, change_report=report)

        assert RuleId.LARGE_QUANTITY_SWING.value not in [r.rule_id for r in decision.triggered_rules]


# ── Test 4: Generic categorical column ────────────────────────────────────────


class TestGenericCategoricalColumn:
    def test_tier_column_categorical_evidence_recorded_not_mass_deactivation(
        self, engine: GuardrailEngine
    ) -> None:
        """Categorical transitions in non-status columns are recorded in evidence but do NOT
        fire MASS_DEACTIVATION.  That rule is intentionally tied to the inventory 'status'
        column deactivation semantics (ACTIVE -> INACTIVE/DISCONTINUED etc.) to preserve
        backward compatibility with existing held batch evidence in PostgreSQL.
        """
        ts = datetime(2026, 9, 16, 10, 0, 0, tzinfo=timezone.utc)
        rows = [generic_row("updated_at", ts, tier="gold") for _ in range(6)]
        batch = make_generic_batch(rows, key_cols=["product_id"], tracked_cols=["tier"])
        changed = [
            make_record(
                {"product_id": str(i)},
                field_changes=[FieldChange("tier", "gold", "silver")],
            )
            for i in range(6)
        ]
        report = ChangeReport(changed=changed)

        decision = engine.evaluate(batch=batch, change_report=report)

        # 'tier' is categorical so transitions are tracked in evidence
        assert decision.evidence.categorical_column_analyzed == "tier"
        # status_deactivations_count is 0 — 'tier' is not the 'status' column
        assert decision.evidence.status_deactivations_count == 0
        # MASS_DEACTIVATION rule does NOT fire — it requires the 'status' column
        rule_ids = [r.rule_id for r in decision.triggered_rules]
        assert RuleId.MASS_DEACTIVATION.value not in rule_ids
        # per_column_change_counts captures the change on 'tier'
        assert decision.evidence.per_column_change_counts.get("tier") == 6

    def test_status_column_still_works_for_inventory_compat(self, engine: GuardrailEngine) -> None:
        """Inventory status deactivation still counts correctly."""
        ts = datetime(2026, 9, 16, 10, 0, 0, tzinfo=timezone.utc)
        rows = [generic_row("updated_at", ts, status="ACTIVE") for _ in range(6)]
        batch = make_generic_batch(
            rows, key_cols=["sku_id", "warehouse_id"], tracked_cols=["status"]
        )
        changed = [
            make_record(
                {"sku_id": str(i), "warehouse_id": "WH-1"},
                field_changes=[FieldChange("status", "ACTIVE", "INACTIVE")],
            )
            for i in range(6)
        ]
        report = ChangeReport(changed=changed)

        decision = engine.evaluate(batch=batch, change_report=report)

        assert RuleId.MASS_DEACTIVATION.value in [r.rule_id for r in decision.triggered_rules]
        assert decision.evidence.status_deactivations_count == 6


# ── Test 5: Inventory compatibility ───────────────────────────────────────────


class TestInventoryCompatibility:
    """All existing inventory scenarios must produce identical outcomes after refactor."""

    def test_quantity_on_hand_still_detected(self, engine: GuardrailEngine) -> None:
        """quantity_on_hand column still triggers LARGE_QUANTITY_SWING."""
        from src.scd2_copilot.db.models import InventorySourceRow

        ts = datetime(2026, 9, 16, 10, 0, 0, tzinfo=timezone.utc)
        row = InventorySourceRow(
            sku_id="SKU-1", warehouse_id="WH-1",
            quantity_on_hand=100, reorder_level=20, status="ACTIVE", updated_at=ts,
        )
        batch = MicroBatch(source_records=[row])
        changed = [
            make_record(
                {"sku_id": "SKU-1", "warehouse_id": "WH-1"},
                field_changes=[FieldChange("quantity_on_hand", 100, 500)],
            )
        ]
        report = ChangeReport(changed=changed)

        decision = engine.evaluate(batch=batch, change_report=report)

        assert RuleId.LARGE_QUANTITY_SWING.value in [r.rule_id for r in decision.triggered_rules]
        assert decision.evidence.numeric_column_analyzed == "quantity_on_hand"

    def test_warehouses_affected_still_populated(self, engine: GuardrailEngine) -> None:
        """warehouses_affected is still correct for inventory (backward compat)."""
        from src.scd2_copilot.db.models import InventorySourceRow

        ts = datetime(2026, 9, 16, 10, 0, 0, tzinfo=timezone.utc)
        rows = [
            InventorySourceRow(
                sku_id=f"SKU-{i}", warehouse_id=f"WH-{i}",
                quantity_on_hand=100, reorder_level=20, status="ACTIVE", updated_at=ts,
            )
            for i in range(3)
        ]
        batch = MicroBatch(source_records=rows)
        changed = [
            make_record({"sku_id": f"SKU-{i}", "warehouse_id": f"WH-{i}"})
            for i in range(3)
        ]
        report = ChangeReport(changed=changed)

        evidence = engine.extract_evidence(batch=batch, change_report=report)

        assert evidence.warehouses_affected == 3
        assert evidence.skus_affected == 3


# ── Test 6: Velocity with custom timestamp column ─────────────────────────────


class TestVelocityWithCustomTimestampColumn:
    def test_velocity_uses_batch_timestamp_column(self, engine: GuardrailEngine) -> None:
        """Velocity is computed from 'last_modified' timestamp column, not 'updated_at'."""
        t0 = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 9, 16, 12, 0, 0, 500000, tzinfo=timezone.utc)  # 0.5s window
        rows = [
            generic_row("last_modified", t0 if i % 2 == 0 else t1, price=100.0)
            for i in range(60)
        ]
        batch = make_generic_batch(
            rows,
            key_cols=["product_id"],
            tracked_cols=["price"],
            ts_col="last_modified",
        )
        changed = [
            make_record({"product_id": str(i)})
            for i in range(60)
        ]
        report = ChangeReport(changed=changed)

        decision = engine.evaluate(batch=batch, change_report=report)

        # 60 changes in ~0.5s window → velocity >> 50 threshold
        rule_ids = [r.rule_id for r in decision.triggered_rules]
        assert RuleId.HIGH_CHANGE_VELOCITY.value in rule_ids
        assert decision.evidence.velocity_changes_per_second > 50.0

    def test_velocity_zero_when_no_timestamp_attribute(self, engine: GuardrailEngine) -> None:
        """Velocity defaults to 0 when timestamp_column attribute not present on records."""
        ts = datetime(2026, 9, 16, 10, 0, 0, tzinfo=timezone.utc)
        # Rows have 'updated_at', but batch.timestamp_column = 'event_time' (not present)
        rows = [generic_row("updated_at", ts, price=100.0) for _ in range(5)]
        batch = make_generic_batch(
            rows, key_cols=["product_id"], tracked_cols=["price"], ts_col="event_time"
        )
        changed = [make_record({"product_id": str(i)}) for i in range(5)]
        report = ChangeReport(changed=changed)

        evidence = engine.extract_evidence(batch=batch, change_report=report)

        assert evidence.event_window_seconds == 0.0


# ── Test 7: Key dispersion with non-warehouse_id key ─────────────────────────


class TestKeyDispersionGeneric:
    def test_high_dispersion_on_region_code(self, engine: GuardrailEngine) -> None:
        """WIDE_GEOGRAPHIC_IMPACT fires when distinct 'region_code' values > threshold."""
        ts = datetime(2026, 9, 16, 10, 0, 0, tzinfo=timezone.utc)
        rows = [generic_row("updated_at", ts, price=100.0) for _ in range(6)]
        batch = make_generic_batch(
            rows, key_cols=["region_code"], tracked_cols=["price"]
        )
        # 6 distinct region_code values → key_value_dispersion=6 > threshold 5
        changed = [
            make_record({"region_code": f"R-{i}"})
            for i in range(6)
        ]
        report = ChangeReport(changed=changed)

        decision = engine.evaluate(batch=batch, change_report=report)

        rule_ids = [r.rule_id for r in decision.triggered_rules]
        assert RuleId.WIDE_GEOGRAPHIC_IMPACT.value in rule_ids
        assert decision.evidence.key_value_dispersion == 6
        assert decision.evidence.dispersion_key_column == "region_code"

    def test_highest_cardinality_key_selected(self, engine: GuardrailEngine) -> None:
        """When multiple keys exist, the highest-cardinality key drives dispersion."""
        ts = datetime(2026, 9, 16, 10, 0, 0, tzinfo=timezone.utc)
        rows = [generic_row("updated_at", ts, price=100.0) for _ in range(6)]
        batch = make_generic_batch(
            rows, key_cols=["category", "region_code"], tracked_cols=["price"]
        )
        # category has 2 distinct values, region_code has 6 → region_code is highest
        changed = [
            make_record({"category": f"CAT-{i % 2}", "region_code": f"R-{i}"})
            for i in range(6)
        ]
        report = ChangeReport(changed=changed)

        evidence = engine.extract_evidence(batch=batch, change_report=report)

        assert evidence.dispersion_key_column == "region_code"
        assert evidence.key_value_dispersion == 6


# ── Test 8: Evidence JSON serializable ────────────────────────────────────────


class TestEvidenceJsonSerializable:
    def test_to_dict_round_trips_for_generic_monitor(self, engine: GuardrailEngine) -> None:
        """GuardrailEvidence.to_dict() with new generic fields is JSON serializable."""
        ts = datetime(2026, 9, 16, 10, 0, 0, tzinfo=timezone.utc)
        rows = [generic_row("updated_at", ts, price=100)]
        batch = make_generic_batch(rows, key_cols=["product_id"], tracked_cols=["price"])
        changed = [
            make_record({"product_id": "P1"}, field_changes=[FieldChange("price", 100, 500)])
        ]
        report = ChangeReport(changed=changed)

        decision = engine.evaluate(batch=batch, change_report=report, run_id=uuid4())
        d = decision.to_dict()
        json_str = json.dumps(d)

        parsed = json.loads(json_str)
        evidence = parsed["evidence"]
        assert "key_value_dispersion" in evidence
        assert "dispersion_key_column" in evidence
        assert "numeric_column_analyzed" in evidence
        assert "categorical_column_analyzed" in evidence
        assert "per_column_change_counts" in evidence
        # All legacy fields preserved
        assert "warehouses_affected" in evidence
        assert "max_quantity_relative_change" in evidence
        assert "status_deactivations_count" in evidence


# ── Test 9: Per-column change counts ─────────────────────────────────────────


class TestPerColumnChangeCounts:
    def test_counts_all_changed_columns(self, engine: GuardrailEngine) -> None:
        """per_column_change_counts accumulates across all changed records."""
        ts = datetime(2026, 9, 16, 10, 0, 0, tzinfo=timezone.utc)
        rows = [generic_row("updated_at", ts, price=100, tier="gold") for _ in range(3)]
        batch = make_generic_batch(rows, key_cols=["product_id"], tracked_cols=["price", "tier"])
        changed = [
            make_record(
                {"product_id": "P1"},
                field_changes=[FieldChange("price", 100, 110), FieldChange("tier", "gold", "silver")],
            ),
            make_record(
                {"product_id": "P2"},
                field_changes=[FieldChange("price", 200, 220)],
            ),
        ]
        report = ChangeReport(changed=changed)

        evidence = engine.extract_evidence(batch=batch, change_report=report)

        assert evidence.per_column_change_counts.get("price") == 2
        assert evidence.per_column_change_counts.get("tier") == 1
