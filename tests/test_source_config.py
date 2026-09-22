"""Comprehensive unit and integration tests for V3 Phase 1 — Configurable PostgreSQL Source."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import polars as pl
import pytest
from pydantic import ValidationError

from src.scd2_copilot.api.client import ApiClient
from src.scd2_copilot.detect_changes import detect_changes
from src.scd2_copilot.models import DeletePolicy, SnapshotMode
from src.scd2_copilot.source import (
    ChangeTimestampDefinition,
    MonitorConfig,
    NormalizedSourceRecord,
    PostgresSourceAdapter,
    PostgresSourceDefinition,
    SourceColumnMetadata,
    SourceColumnNotFoundError,
    SourceConfigurationError,
    SourceConnectionError,
    SourceSchemaNotFoundError,
    SourceTableMetadata,
    SourceTableNotFoundError,
    SourceTypeMismatchError,
    SourceValidationResult,
    get_default_inventory_monitor_config,
)
from src.scd2_copilot.transform_scd2 import apply_scd2
from src.scd2_copilot.validate import validate_scd2
from src.scd2_copilot.worker.batch import MicroBatch


# ── 1. MonitorConfig Validation Unit Tests ─────────────────────────────────


def test_valid_monitor_config_creation():
    """Valid MonitorConfig instantiates cleanly with alias support and serialization."""
    cfg = MonitorConfig(
        name="store_sales",
        source={"type": "postgresql", "schema": "sales_schema", "table": "orders"},
        keys=["order_id", "item_id"],
        change_timestamp="modified_at",
        tracked_columns=["quantity", "price", "status"],
    )

    assert cfg.name == "store_sales"
    assert cfg.source.type == "postgresql"
    assert cfg.source.schema_name == "sales_schema"
    assert cfg.source.table_name == "orders"
    assert cfg.business_keys == ["order_id", "item_id"]
    assert cfg.change_timestamp.column == "modified_at"
    assert cfg.tracked_columns == ["quantity", "price", "status"]

    # Verify JSON serialization round-trip
    dumped = cfg.model_dump(by_alias=True)
    assert dumped["keys"] == ["order_id", "item_id"]
    assert dumped["source"]["schema"] == "sales_schema"


def test_invalid_monitor_config_empty_name():
    """Empty or whitespace-only monitor name raises ValidationError."""
    with pytest.raises(ValidationError, match="Monitor name cannot be empty"):
        MonitorConfig(
            name="   ",
            source={"schema": "public", "table": "orders"},
            keys=["order_id"],
            change_timestamp="updated_at",
            tracked_columns=["status"],
        )


def test_invalid_sql_identifier_raises_validation_error():
    """SQL injection tokens or invalid identifier characters are rejected."""
    # Semicolon / drop table attempt
    with pytest.raises(ValidationError, match="not a valid SQL identifier"):
        MonitorConfig(
            name="valid_name",
            source={"schema": "public", "table": "orders; DROP TABLE orders;"},
            keys=["order_id"],
            change_timestamp="updated_at",
            tracked_columns=["status"],
        )

    # Spaces in column name
    with pytest.raises(ValidationError, match="not a valid SQL identifier"):
        MonitorConfig(
            name="valid_name",
            source={"schema": "public", "table": "orders"},
            keys=["order id with spaces"],
            change_timestamp="updated_at",
            tracked_columns=["status"],
        )


def test_empty_keys_or_tracked_columns_rejected():
    """Empty business_keys or empty tracked_columns raise ValidationError."""
    with pytest.raises(ValidationError, match="At least one business_key must be configured"):
        MonitorConfig(
            name="test",
            source={"schema": "public", "table": "orders"},
            keys=[],
            change_timestamp="updated_at",
            tracked_columns=["status"],
        )

    with pytest.raises(ValidationError, match="At least one tracked_column must be configured"):
        MonitorConfig(
            name="test",
            source={"schema": "public", "table": "orders"},
            keys=["order_id"],
            change_timestamp="updated_at",
            tracked_columns=[],
        )


def test_duplicate_keys_or_tracked_columns_rejected():
    """Duplicate keys or duplicate tracked columns raise ValidationError."""
    with pytest.raises(ValidationError, match="Duplicate business keys detected"):
        MonitorConfig(
            name="test",
            source={"schema": "public", "table": "orders"},
            keys=["order_id", "order_id"],
            change_timestamp="updated_at",
            tracked_columns=["status"],
        )

    with pytest.raises(ValidationError, match="Duplicate tracked columns detected"):
        MonitorConfig(
            name="test",
            source={"schema": "public", "table": "orders"},
            keys=["order_id"],
            change_timestamp="updated_at",
            tracked_columns=["status", "status"],
        )


def test_overlapping_keys_and_tracked_columns_rejected():
    """Columns cannot be both business keys and tracked columns."""
    with pytest.raises(ValidationError, match="Columns cannot be both business keys and tracked columns"):
        MonitorConfig(
            name="test",
            source={"schema": "public", "table": "orders"},
            keys=["order_id", "status"],
            change_timestamp="updated_at",
            tracked_columns=["status", "price"],
        )


def test_change_timestamp_column_conflict_rejected():
    """Change timestamp column cannot be part of business keys or tracked columns."""
    with pytest.raises(ValidationError, match="change_timestamp column 'updated_at' cannot be listed in business_keys"):
        MonitorConfig(
            name="test",
            source={"schema": "public", "table": "orders"},
            keys=["order_id", "updated_at"],
            change_timestamp="updated_at",
            tracked_columns=["price"],
        )

    with pytest.raises(ValidationError, match="change_timestamp column 'updated_at' cannot be listed in tracked_columns"):
        MonitorConfig(
            name="test",
            source={"schema": "public", "table": "orders"},
            keys=["order_id"],
            change_timestamp="updated_at",
            tracked_columns=["price", "updated_at"],
        )


def test_reserved_scd2_columns_rejected():
    """Reserved system columns (effective_from, effective_to, is_current) cannot be configured."""
    with pytest.raises(ValidationError, match="conflict with reserved SCD2 system columns"):
        MonitorConfig(
            name="test",
            source={"schema": "public", "table": "orders"},
            keys=["order_id"],
            change_timestamp="updated_at",
            tracked_columns=["is_current", "price"],
        )


def test_default_inventory_monitor_config():
    """get_default_inventory_monitor_config returns valid canonical inventory configuration."""
    cfg = get_default_inventory_monitor_config()
    assert cfg.source.schema_name == "public"
    assert "inventory" in cfg.source.table_name
    assert cfg.business_keys == ["sku_id", "warehouse_id"]
    assert cfg.change_timestamp.column == "updated_at"
    assert cfg.tracked_columns == ["quantity_on_hand", "reorder_level", "status"]


# ── 2. Source Adapter Metadata & Validation Tests ──────────────────────────


def _create_mock_cursor(schema_exists=True, table_exists=True, columns=None, pk_cols=None):
    """Helper creating a mock psycopg cursor with predictable responses."""
    mock_cur = MagicMock()

    def mock_execute(query, params=None):
        nonlocal mock_cur
        query_str = str(query).lower()
        if "information_schema.schemata" in query_str:
            mock_cur.fetchone.return_value = (1,) if schema_exists else None
        elif "information_schema.tables" in query_str:
            mock_cur.fetchone.return_value = (1,) if table_exists else None
        elif "information_schema.columns" in query_str:
            mock_cur.fetchall.return_value = columns or []
        elif "information_schema.table_constraints" in query_str:
            mock_cur.fetchall.return_value = [{"column_name": pk} for pk in (pk_cols or [])]
        elif "select 1" in query_str:
            mock_cur.fetchone.return_value = (1,)

    mock_cur.execute.side_effect = mock_execute
    return mock_cur


def test_adapter_missing_schema_error():
    """Adapter detects non-existent schema and reports typed error."""
    cfg = get_default_inventory_monitor_config()
    mock_db = MagicMock()
    mock_conn = MagicMock()
    mock_cur = _create_mock_cursor(schema_exists=False)
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_db.connection.return_value.__enter__.return_value = mock_conn

    adapter = PostgresSourceAdapter(config=cfg, db_manager=mock_db)

    # 1. Result mode
    res = adapter.validate_configuration(raise_on_error=False)
    assert not res.is_valid
    assert any("does not exist" in e for e in res.errors)

    # 2. Raise mode
    with pytest.raises(SourceSchemaNotFoundError):
        adapter.validate_configuration(raise_on_error=True)


def test_adapter_missing_table_error():
    """Adapter detects non-existent table and reports typed error."""
    cfg = get_default_inventory_monitor_config()
    mock_db = MagicMock()
    mock_conn = MagicMock()
    mock_cur = _create_mock_cursor(schema_exists=True, table_exists=False)
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_db.connection.return_value.__enter__.return_value = mock_conn

    adapter = PostgresSourceAdapter(config=cfg, db_manager=mock_db)

    res = adapter.validate_configuration(raise_on_error=False)
    assert not res.is_valid
    assert any("table 'public.inventory_source' does not exist" in e for e in res.errors)

    with pytest.raises(SourceTableNotFoundError):
        adapter.validate_configuration(raise_on_error=True)


def test_adapter_missing_business_key_column_error():
    """Adapter detects missing business key column in table schema."""
    cfg = get_default_inventory_monitor_config()
    # Missing 'warehouse_id'
    mock_columns = [
        {"column_name": "sku_id", "data_type": "text", "is_nullable": "NO", "ordinal_position": 1},
        {"column_name": "quantity_on_hand", "data_type": "integer", "is_nullable": "YES", "ordinal_position": 2},
        {"column_name": "reorder_level", "data_type": "integer", "is_nullable": "YES", "ordinal_position": 3},
        {"column_name": "status", "data_type": "text", "is_nullable": "YES", "ordinal_position": 4},
        {"column_name": "updated_at", "data_type": "timestamp with time zone", "is_nullable": "NO", "ordinal_position": 5},
    ]

    mock_db = MagicMock()
    mock_conn = MagicMock()
    mock_cur = _create_mock_cursor(schema_exists=True, table_exists=True, columns=mock_columns)
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_db.connection.return_value.__enter__.return_value = mock_conn

    adapter = PostgresSourceAdapter(config=cfg, db_manager=mock_db)
    res = adapter.validate_configuration(raise_on_error=False)

    assert not res.is_valid
    assert any("Business key column 'warehouse_id' not found" in e for e in res.errors)

    with pytest.raises(SourceColumnNotFoundError):
        adapter.validate_configuration(raise_on_error=True)


def test_adapter_missing_timestamp_column_error():
    """Adapter detects missing timestamp column in table schema."""
    cfg = get_default_inventory_monitor_config()
    # Missing 'updated_at'
    mock_columns = [
        {"column_name": "sku_id", "data_type": "text", "is_nullable": "NO", "ordinal_position": 1},
        {"column_name": "warehouse_id", "data_type": "text", "is_nullable": "NO", "ordinal_position": 2},
        {"column_name": "quantity_on_hand", "data_type": "integer", "is_nullable": "YES", "ordinal_position": 3},
        {"column_name": "reorder_level", "data_type": "integer", "is_nullable": "YES", "ordinal_position": 4},
        {"column_name": "status", "data_type": "text", "is_nullable": "YES", "ordinal_position": 5},
    ]

    mock_db = MagicMock()
    mock_conn = MagicMock()
    mock_cur = _create_mock_cursor(schema_exists=True, table_exists=True, columns=mock_columns)
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_db.connection.return_value.__enter__.return_value = mock_conn

    adapter = PostgresSourceAdapter(config=cfg, db_manager=mock_db)
    res = adapter.validate_configuration(raise_on_error=False)

    assert not res.is_valid
    assert any("Change timestamp column 'updated_at' not found" in e for e in res.errors)

    with pytest.raises(SourceColumnNotFoundError):
        adapter.validate_configuration(raise_on_error=True)


def test_adapter_invalid_timestamp_type_error():
    """Adapter detects non-temporal data type for change timestamp column."""
    cfg = get_default_inventory_monitor_config()
    # updated_at has data_type = 'character varying'
    mock_columns = [
        {"column_name": "sku_id", "data_type": "text", "is_nullable": "NO", "ordinal_position": 1},
        {"column_name": "warehouse_id", "data_type": "text", "is_nullable": "NO", "ordinal_position": 2},
        {"column_name": "quantity_on_hand", "data_type": "integer", "is_nullable": "YES", "ordinal_position": 3},
        {"column_name": "reorder_level", "data_type": "integer", "is_nullable": "YES", "ordinal_position": 4},
        {"column_name": "status", "data_type": "text", "is_nullable": "YES", "ordinal_position": 5},
        {"column_name": "updated_at", "data_type": "character varying", "is_nullable": "NO", "ordinal_position": 6},
    ]

    mock_db = MagicMock()
    mock_conn = MagicMock()
    mock_cur = _create_mock_cursor(schema_exists=True, table_exists=True, columns=mock_columns)
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_db.connection.return_value.__enter__.return_value = mock_conn

    adapter = PostgresSourceAdapter(config=cfg, db_manager=mock_db)
    res = adapter.validate_configuration(raise_on_error=False)

    assert not res.is_valid
    assert any("not a supported temporal type" in e for e in res.errors)

    with pytest.raises(SourceTypeMismatchError):
        adapter.validate_configuration(raise_on_error=True)


def test_adapter_missing_tracked_column_error():
    """Adapter detects missing tracked column in table schema."""
    cfg = get_default_inventory_monitor_config()
    # Missing 'reorder_level'
    mock_columns = [
        {"column_name": "sku_id", "data_type": "text", "is_nullable": "NO", "ordinal_position": 1},
        {"column_name": "warehouse_id", "data_type": "text", "is_nullable": "NO", "ordinal_position": 2},
        {"column_name": "quantity_on_hand", "data_type": "integer", "is_nullable": "YES", "ordinal_position": 3},
        {"column_name": "status", "data_type": "text", "is_nullable": "YES", "ordinal_position": 4},
        {"column_name": "updated_at", "data_type": "timestamp with time zone", "is_nullable": "NO", "ordinal_position": 5},
    ]

    mock_db = MagicMock()
    mock_conn = MagicMock()
    mock_cur = _create_mock_cursor(schema_exists=True, table_exists=True, columns=mock_columns)
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_db.connection.return_value.__enter__.return_value = mock_conn

    adapter = PostgresSourceAdapter(config=cfg, db_manager=mock_db)
    res = adapter.validate_configuration(raise_on_error=False)

    assert not res.is_valid
    assert any("Tracked column 'reorder_level' not found" in e for e in res.errors)

    with pytest.raises(SourceColumnNotFoundError):
        adapter.validate_configuration(raise_on_error=True)


def test_adapter_metadata_discovery_and_successful_validation():
    """Adapter successfully discovers schema metadata and validates complete configuration."""
    cfg = get_default_inventory_monitor_config()
    mock_columns = [
        {"column_name": "sku_id", "data_type": "character varying", "is_nullable": "NO", "ordinal_position": 1},
        {"column_name": "warehouse_id", "data_type": "character varying", "is_nullable": "NO", "ordinal_position": 2},
        {"column_name": "quantity_on_hand", "data_type": "integer", "is_nullable": "YES", "ordinal_position": 3},
        {"column_name": "reorder_level", "data_type": "integer", "is_nullable": "YES", "ordinal_position": 4},
        {"column_name": "status", "data_type": "text", "is_nullable": "YES", "ordinal_position": 5},
        {"column_name": "updated_at", "data_type": "timestamp with time zone", "is_nullable": "NO", "ordinal_position": 6},
    ]

    mock_db = MagicMock()
    mock_conn = MagicMock()
    mock_cur = _create_mock_cursor(
        schema_exists=True,
        table_exists=True,
        columns=mock_columns,
        pk_cols=["sku_id", "warehouse_id"],
    )
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_db.connection.return_value.__enter__.return_value = mock_conn

    adapter = PostgresSourceAdapter(config=cfg, db_manager=mock_db)
    meta = adapter.discover_schema()

    assert meta.schema_name == "public"
    assert meta.table_name == "inventory_source"
    assert set(meta.columns.keys()) == {
        "sku_id", "warehouse_id", "quantity_on_hand", "reorder_level", "status", "updated_at"
    }
    assert meta.primary_keys == ["sku_id", "warehouse_id"]

    val_res = adapter.validate_configuration(raise_on_error=False)
    assert val_res.is_valid
    assert len(val_res.errors) == 0
    assert val_res.metadata is not None


# ── 3. Source Adapter Reads & SCD2 Pipeline Integration ────────────────────


def test_adapter_read_incremental_and_initial():
    """Adapter formats and executes parameterized queries for initial and incremental reads."""
    cfg = get_default_inventory_monitor_config()
    mock_db = MagicMock()
    mock_conn = MagicMock()
    mock_cur = MagicMock()

    now = datetime.now(timezone.utc)
    mock_rows = [
        {
            "sku_id": "SKU-001",
            "warehouse_id": "WH-01",
            "quantity_on_hand": 100,
            "reorder_level": 20,
            "status": "ACTIVE",
            "updated_at": now,
        }
    ]
    mock_cur.fetchall.return_value = mock_rows
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_db.connection.return_value.__enter__.return_value = mock_conn

    adapter = PostgresSourceAdapter(config=cfg, db_manager=mock_db)

    # Initial snapshot read
    init_records = adapter.read_initial_snapshot(limit=10)
    assert len(init_records) == 1
    assert init_records[0]["sku_id"] == "SKU-001"

    # Incremental read with watermark
    inc_records = adapter.read_incremental_records(watermark=now, limit=50)
    assert len(inc_records) == 1
    assert inc_records[0]["quantity_on_hand"] == 100


def test_configured_source_to_scd2_pipeline():
    """Normalized records from configured source flow smoothly through SCD2 change detection and transformation."""
    now = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
    records = [
        {
            "sku_id": "SKU-100",
            "warehouse_id": "WH-01",
            "quantity_on_hand": 150,
            "reorder_level": 30,
            "status": "ACTIVE",
            "updated_at": now,
        },
        {
            "sku_id": "SKU-200",
            "warehouse_id": "WH-02",
            "quantity_on_hand": 75,
            "reorder_level": 15,
            "status": "ACTIVE",
            "updated_at": now,
        },
    ]

    # Formulate MicroBatch using configured keys and tracked columns
    batch = MicroBatch(
        source_records=records,
        watermark_start=None,
        watermark_end=now,
        key_columns=["sku_id", "warehouse_id"],
        tracked_columns=["quantity_on_hand", "reorder_level", "status"],
        timestamp_column="updated_at",
    )

    assert batch.size == 2
    assert set(batch.business_keys) == {("SKU-100", "WH-01"), ("SKU-200", "WH-02")}

    source_df = batch.to_source_df()
    assert isinstance(source_df, pl.DataFrame)
    assert set(source_df.columns) == {"sku_id", "warehouse_id", "quantity_on_hand", "reorder_level", "status"}

    # Target DF (empty initial target)
    target_df = pl.DataFrame(
        schema={
            "sku_id": pl.String,
            "warehouse_id": pl.String,
            "quantity_on_hand": pl.Int64,
            "reorder_level": pl.Int64,
            "status": pl.String,
            "effective_from": pl.Date,
            "effective_to": pl.Date,
            "is_current": pl.Boolean,
        }
    )

    # Process through core SCD2 engine
    change_report = detect_changes(
        source_df=source_df,
        target_df=target_df,
        business_key=["sku_id", "warehouse_id"],
        tracked_columns=["quantity_on_hand", "reorder_level", "status"],
        processing_date=now.date(),
        snapshot_mode=SnapshotMode.INCREMENTAL,
        delete_policy=DeletePolicy.IGNORE,
    )

    assert len(change_report.new) == 2
    assert len(change_report.changed) == 0

    scd2_df = apply_scd2(
        source_df=source_df,
        target_df=target_df,
        change_report=change_report,
        business_key=["sku_id", "warehouse_id"],
        tracked_columns=["quantity_on_hand", "reorder_level", "status"],
        processing_date=now.date(),
        delete_policy=DeletePolicy.IGNORE,
    )

    assert scd2_df.height == 2
    assert all(scd2_df["is_current"].to_list())

    validation_report = validate_scd2(scd2_df, business_key=["sku_id", "warehouse_id"])
    assert validation_report.passed


# ── 4. IngestionWorker Configuration Integration & Regression ──────────────


def test_worker_initialization_with_custom_monitor_config():
    """IngestionWorker correctly adopts MonitorConfig and initializes PostgresSourceAdapter."""
    from src.scd2_copilot.worker.worker import IngestionWorker

    custom_cfg = MonitorConfig(
        name="custom_feed",
        source={"schema": "analytics", "table": "store_stock"},
        keys=["store_id", "product_id"],
        change_timestamp="ts",
        tracked_columns=["inventory_count"],
    )

    mock_db = MagicMock()
    worker = IngestionWorker(
        db_manager=mock_db,
        monitor_config=custom_cfg,
    )

    assert worker.source_name == "custom_feed"
    assert worker.table_name == "store_stock"
    assert worker.business_key == ["store_id", "product_id"]
    assert worker.tracked_columns == ["inventory_count"]
    assert worker.timestamp_column == "ts"
    assert worker.source_adapter is not None
    assert worker.source_adapter.config.name == "custom_feed"


def test_worker_default_inventory_monitor_config_preservation():
    """IngestionWorker defaults to active product_master, but preserves inventory when requested."""
    from src.scd2_copilot.worker.worker import IngestionWorker

    mock_db = MagicMock()

    # 1. Default worker uses active product_master demo
    worker_default = IngestionWorker(db_manager=mock_db)
    assert worker_default.source_name == "product_master"
    assert worker_default.business_key == ["product_id"]
    assert worker_default.tracked_columns == ["product_name", "category", "supplier_id", "price", "status"]
    assert worker_default.timestamp_column == "updated_at"
    assert worker_default.source_adapter is not None

    # 2. Worker with source_name='inventory' preserves canonical warehouse inventory demo
    worker_inv = IngestionWorker(db_manager=mock_db, source_name="inventory")
    assert worker_inv.source_name == "inventory" or "inventory" in worker_inv.source_name
    assert worker_inv.business_key == ["sku_id", "warehouse_id"]
    assert worker_inv.tracked_columns == ["quantity_on_hand", "reorder_level", "status"]
    assert worker_inv.timestamp_column == "updated_at"
    assert worker_inv.source_adapter is not None


# ── 5. FastAPI Endpoints & ApiClient Unit Tests ────────────────────────────


def test_api_monitors_routes_and_client():
    """FastAPI /api/v1/monitors endpoints and ApiClient methods function correctly."""
    from fastapi.testclient import TestClient
    from src.scd2_copilot.api.app import create_app

    app = create_app()
    client = TestClient(app)

    # 1. GET /api/v1/monitors
    resp = client.get("/api/v1/monitors")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    inventory_mon = next((m for m in data["monitors"] if "inventory" in m["name"]), None)
    assert inventory_mon is not None
    assert inventory_mon["source"]["type"] == "postgresql"
    assert inventory_mon["keys"] == ["sku_id", "warehouse_id"]

    # 2. GET /api/v1/monitors/default
    resp_def = client.get("/api/v1/monitors/default")
    assert resp_def.status_code == 200
    assert resp_def.json()["keys"] == ["sku_id", "warehouse_id"]

    # 3. GET non-existent monitor
    resp_404 = client.get("/api/v1/monitors/non_existent_mon")
    assert resp_404.status_code == 404

    # 4. POST /api/v1/monitors/validate with invalid payload
    invalid_payload = {
        "name": "",
        "source": {"schema": "public", "table": "bad"},
        "keys": ["key1"],
        "change_timestamp": {"column": "ts"},
        "tracked_columns": ["val"],
    }
    resp_val_bad = client.post("/api/v1/monitors/validate", json=invalid_payload)
    assert resp_val_bad.status_code == 200
    assert not resp_val_bad.json()["is_valid"]
    assert len(resp_val_bad.json()["errors"]) > 0


def test_api_client_monitor_methods():
    """ApiClient helper methods call monitor endpoints correctly."""
    from unittest.mock import MagicMock
    from src.scd2_copilot.api.client import ApiClient

    mock_resp = MagicMock()
    mock_resp.is_success = True
    mock_resp.is_error = False
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "monitors": [
            {
                "name": "warehouse_inventory",
                "source": {"type": "postgresql", "schema": "public", "table": "inventory_source"},
                "keys": ["sku_id", "warehouse_id"],
                "change_timestamp": {"column": "updated_at"},
                "tracked_columns": ["quantity_on_hand", "reorder_level", "status"],
            }
        ],
        "total": 1,
    }

    client = ApiClient(base_url="http://testserver")
    client._client = MagicMock()
    client._client.request.return_value = mock_resp

    # 1. list_monitors
    res = client.list_monitors()
    assert res.total == 1
    assert res.monitors[0].name == "warehouse_inventory"

    # 2. get_monitor
    mock_resp.json.return_value = {
        "name": "warehouse_inventory",
        "source": {"type": "postgresql", "schema": "public", "table": "inventory_source"},
        "keys": ["sku_id", "warehouse_id"],
        "change_timestamp": {"column": "updated_at"},
        "tracked_columns": ["quantity_on_hand", "reorder_level", "status"],
    }
    mon = client.get_monitor("warehouse_inventory")
    assert mon.name == "warehouse_inventory"

    # 3. validate_monitor_config
    mock_resp.json.return_value = {
        "is_valid": True,
        "errors": [],
        "warnings": [],
        "discovered_columns": {
            "sku_id": {
                "column_name": "sku_id",
                "data_type": "text",
                "is_nullable": False,
                "ordinal_position": 1,
            }
        },
        "primary_keys": ["sku_id"],
    }
    val = client.validate_monitor_config()
    assert val.is_valid
    assert "sku_id" in val.discovered_columns
