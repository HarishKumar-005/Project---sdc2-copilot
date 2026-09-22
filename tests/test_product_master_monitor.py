"""Dedicated unit and integration tests for Product Master monitor configuration and runtime behavior."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from src.scd2_copilot.api.app import create_app
from src.scd2_copilot.api.client import ApiClient
from src.scd2_copilot.config import get_settings
from src.scd2_copilot.db.connection import DatabaseManager
from src.scd2_copilot.guardrail.engine import GuardrailEngine
from src.scd2_copilot.guardrail.models import GuardrailDecisionType, RuleId
from src.scd2_copilot.models import ChangeRecord, ChangeReport, ChangeType, FieldChange
from src.scd2_copilot.source import (
    MonitorConfig,
    get_default_inventory_monitor_config,
    get_default_product_master_monitor_config,
    get_monitor_registry,
)
from src.scd2_copilot.worker.batch import MicroBatch
from src.scd2_copilot.worker.worker import IngestionWorker


def test_product_master_config_structure():
    """get_default_product_master_monitor_config returns valid canonical Product Master configuration."""
    cfg = get_default_product_master_monitor_config()
    assert isinstance(cfg, MonitorConfig)
    assert cfg.name == "product_master"
    assert cfg.source.type == "postgresql"
    assert cfg.source.schema_name == "public"
    assert cfg.source.table_name == "product_master"
    assert cfg.business_keys == ["product_id"]
    assert cfg.change_timestamp.column == "updated_at"
    assert cfg.tracked_columns == ["product_name", "category", "supplier_id", "price", "status"]


def test_monitor_registry_active_and_default_routing():
    """MonitorRegistry routes 'active' to product_master and 'default' to warehouse_inventory."""
    registry = get_monitor_registry()

    # Active demo monitor
    active_mon = registry.get("active")
    assert active_mon is not None
    assert active_mon.name == "product_master"
    assert active_mon.business_keys == ["product_id"]
    assert active_mon.source.table_name == "product_master"

    # Specific name retrieval
    pm_mon = registry.get("product_master")
    assert pm_mon is not None
    assert pm_mon.name == "product_master"

    # Default canonical inventory monitor (backward compatibility)
    def_mon = registry.get("default")
    assert def_mon is not None
    assert def_mon.business_keys == ["sku_id", "warehouse_id"]
    assert def_mon.source.table_name == "inventory_source"

    # Inventory name retrieval
    inv_mon = registry.get("warehouse_inventory")
    assert inv_mon is not None
    assert inv_mon.business_keys == ["sku_id", "warehouse_id"]

    # list_all contains both with product_master first
    all_monitors = registry.list_all()
    assert len(all_monitors) >= 2
    assert all_monitors[0].name == "product_master"
    names = [m.name for m in all_monitors]
    assert "product_master" in names
    assert any("inventory" in n for n in names)


def test_worker_defaults_to_product_master():
    """IngestionWorker initializes with product_master by default."""
    mock_db = MagicMock()
    worker = IngestionWorker(db_manager=mock_db)

    assert worker.source_name == "product_master"
    assert worker.table_name == "product_master"
    assert worker.business_key == ["product_id"]
    assert worker.tracked_columns == ["product_name", "category", "supplier_id", "price", "status"]
    assert worker.timestamp_column == "updated_at"


def test_api_monitors_product_master_endpoints():
    """FastAPI routes serve product_master as active monitor."""
    app = create_app()
    client = TestClient(app)

    # 1. GET /api/v1/monitors/active
    resp_active = client.get("/api/v1/monitors/active")
    assert resp_active.status_code == 200
    data_active = resp_active.json()
    assert data_active["name"] == "product_master"
    assert data_active["keys"] == ["product_id"]
    assert data_active["source"]["table"] == "product_master"

    # 2. GET /api/v1/monitors/default
    resp_def = client.get("/api/v1/monitors/default")
    assert resp_def.status_code == 200
    data_def = resp_def.json()
    assert data_def["keys"] == ["sku_id", "warehouse_id"]
    assert data_def["source"]["table"] == "inventory_source"

    # 3. GET /api/v1/monitors list contains product_master
    resp_list = client.get("/api/v1/monitors")
    assert resp_list.status_code == 200
    data_list = resp_list.json()
    assert data_list["monitors"][0]["name"] == "product_master"


def test_guardrail_product_master_normal_price_update():
    """Normal price update on 1 product evaluates as NORMAL."""
    settings = get_settings()
    engine = GuardrailEngine(settings=settings)

    # Simulate 1 changed record: price increased
    batch_records = [
        {
            "product_id": "PRD-0001",
            "product_name": "Smart TV 55-inch 4K",
            "category": "Electronics",
            "supplier_id": "SUP-ELEC-01",
            "price": 1149.99,  # updated from 149.99
            "status": "ACTIVE",
            "updated_at": datetime.now(timezone.utc),
        }
    ]
    batch = MicroBatch(
        source_records=batch_records,
        source_name="product_master",
        key_columns=["product_id"],
        timestamp_column="updated_at",
    )

    changed_records = [
        ChangeRecord(
            business_key_values={"product_id": "PRD-0001"},
            change_type=ChangeType.CHANGED,
            field_changes=[FieldChange("price", 149.99, 160.00)],
        )
    ]
    report = ChangeReport(changed=changed_records)

    decision = engine.evaluate(batch=batch, change_report=report)
    assert decision.is_normal
    assert not decision.triggered_rules


def test_guardrail_product_master_mass_deactivation():
    """Bulk deactivation of 30 products triggers MASS_DEACTIVATION and HIGH_CHANGE_VOLUME."""
    settings = get_settings()
    engine = GuardrailEngine(settings=settings)

    now_utc = datetime.now(timezone.utc)
    batch_records = []
    changed_records = []

    for i in range(1, 31):
        pid = f"PRD-{i:04d}"
        batch_records.append(
            {
                "product_id": pid,
                "product_name": f"Electronics Item {i}",
                "category": "Electronics",
                "supplier_id": "SUP-ELEC-01",
                "price": 299.99,
                "status": "INACTIVE",
                "updated_at": now_utc,
            }
        )
        changed_records.append(
            ChangeRecord(
                business_key_values={"product_id": pid},
                change_type=ChangeType.CHANGED,
                field_changes=[FieldChange("status", "ACTIVE", "INACTIVE")],
            )
        )

    batch = MicroBatch(
        source_records=batch_records,
        source_name="product_master",
        key_columns=["product_id"],
        timestamp_column="updated_at",
    )
    report = ChangeReport(changed=changed_records)

    decision = engine.evaluate(batch=batch, change_report=report)
    assert decision.is_suspicious
    triggered_codes = [r.rule_id for r in decision.triggered_rules]
    # 30 > 5 triggers MASS_DEACTIVATION
    assert RuleId.MASS_DEACTIVATION.value in triggered_codes
    # 30 > 25 triggers HIGH_CHANGE_VOLUME
    assert RuleId.HIGH_CHANGE_VOLUME.value in triggered_codes
