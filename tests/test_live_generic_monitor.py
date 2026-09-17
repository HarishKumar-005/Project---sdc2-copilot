"""Live PostgreSQL integration test for V3 Phase 4 End-to-End Configured Monitoring.

Verifies end-to-end against live Supabase PostgreSQL:
1. Setting up a non-inventory product table with custom keys and columns.
2. Initial generic entity ingestion creating current versions in monitored_entity_history.
3. Update version transition: closing prior version with effective_to, inserting new active version.
4. History query API endpoint returning chronological versions for generic entity.
5. Suspicious batch containment: holding batch, preserving checkpoint, zero history writes.
6. Recovery release: replaying generic batch, updating monitored_entity_history, advancing checkpoint.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import psycopg
import pytest

from src.scd2_copilot.api.app import app
from src.scd2_copilot.config import Settings
from src.scd2_copilot.db.connection import DatabaseManager
from src.scd2_copilot.db.models import HoldStatus, MonitoredEntityHistoryRow
from src.scd2_copilot.db.repositories import (
    CheckpointRepository,
    HeldChangeBatchRepository,
    MonitoredEntityHistoryRepository,
    ProcessingRunRepository,
)
from src.scd2_copilot.source.models import (
    ChangeTimestampDefinition,
    MonitorConfig,
    PostgresSourceDefinition,
)
from src.scd2_copilot.source.registry import get_monitor_registry
from src.scd2_copilot.worker.worker import IngestionWorker


def _db_is_reachable() -> bool:
    """Return True only if a live database connection can be established."""
    try:
        mgr = DatabaseManager()
        return mgr.ping()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_is_reachable(),
    reason="Live PostgreSQL/Supabase database is not configured or not reachable",
)


@pytest.fixture(scope="module")
def db_manager() -> DatabaseManager:
    return DatabaseManager()


@pytest.fixture(scope="module")
def setup_product_table(db_manager: DatabaseManager):
    """Create a temporary operational source table test_product_source."""
    with db_manager.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS test_product_source (
                    product_id TEXT PRIMARY KEY,
                    price INTEGER NOT NULL,
                    tier TEXT NOT NULL,
                    last_modified TIMESTAMPTZ NOT NULL
                );
            """)
        conn.commit()

    yield

    with db_manager.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS test_product_source;")
        conn.commit()


@pytest.fixture(autouse=True)
def clean_state(db_manager: DatabaseManager):
    """Clean test data before and after each test."""
    source_name = "product_price_monitor"

    def _cleanup():
        with db_manager.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM test_product_source;")
                cur.execute("DELETE FROM monitored_entity_history WHERE source_name = %s;", [source_name])
                cur.execute("DELETE FROM held_change_batch WHERE source_name = %s;", [source_name])
                cur.execute("DELETE FROM processing_checkpoint WHERE source_name = %s;", [source_name])
                cur.execute("DELETE FROM processing_run WHERE source_name = %s;", [source_name])
            conn.commit()

    _cleanup()
    yield
    _cleanup()


class TestLiveGenericMonitorPipeline:
    """End-to-end verification of configured non-inventory monitoring on live PostgreSQL."""

    def test_full_generic_monitor_lifecycle(
        self,
        db_manager: DatabaseManager,
        setup_product_table: None,
    ) -> None:
        source_name = "product_price_monitor"
        table_name = "test_product_source"

        monitor_config = MonitorConfig(
            name=source_name,
            source=PostgresSourceDefinition(type="postgresql", schema_name="public", table_name=table_name),
            keys=["product_id"],
            change_timestamp=ChangeTimestampDefinition(column="last_modified"),
            tracked_columns=["price", "tier"],
        )

        worker = IngestionWorker(
            monitor_config=monitor_config,
            db_manager=db_manager,
            batch_size=10,
            poll_interval_seconds=0.1,
        )

        # 1. Verify monitor registration in neutral registry
        reg = get_monitor_registry()
        assert reg.get(source_name) is not None
        assert reg.get(source_name).name == source_name

        # 2. Seed initial product rows
        t0 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
        with db_manager.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO test_product_source (product_id, price, tier, last_modified)
                    VALUES (%s, %s, %s, %s), (%s, %s, %s, %s);
                    """,
                    ("P-101", 50, "standard", t0, "P-102", 150, "premium", t0 + timedelta(minutes=5)),
                )
            conn.commit()

        # 3. Execute Worker Cycle 1: Initial Ingestion
        res1 = worker.run_once(raise_on_error=True)
        assert res1.status == "COMMITTED"
        assert res1.records_seen == 2
        assert res1.records_changed == 0

        # Verify monitored_entity_history has 2 active records
        entity_repo = MonitoredEntityHistoryRepository(db=db_manager)
        current_rows = entity_repo.fetch_current_history_for_keys(
            source_name=source_name,
            entity_keys=[{"product_id": "P-101"}, {"product_id": "P-102"}],
        )
        assert len(current_rows) == 2
        for r in current_rows:
            assert r.is_current is True
            assert r.effective_to is None
            assert r.source_name == source_name
            if r.entity_key == {"product_id": "P-101"}:
                assert r.attributes["price"] == 50
                assert r.attributes["tier"] == "standard"
            elif r.entity_key == {"product_id": "P-102"}:
                assert r.attributes["price"] == 150
                assert r.attributes["tier"] == "premium"

        # Verify checkpoint advanced
        chk_repo = CheckpointRepository(db=db_manager)
        chk = chk_repo.get_checkpoint(source_name=source_name, table_name=table_name)
        assert chk is not None
        assert chk.watermark_value == t0 + timedelta(minutes=5)

        # 4. Update Product P-101 (price change: 50 -> 60)
        t1 = t0 + timedelta(days=1)
        with db_manager.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE test_product_source
                    SET price = 60, last_modified = %s
                    WHERE product_id = 'P-101';
                    """,
                    [t1],
                )
            conn.commit()

        # Execute Worker Cycle 2: Changed Record Ingestion
        res2 = worker.run_once(raise_on_error=True)
        assert res2.status == "COMMITTED"
        assert res2.records_seen == 1
        assert res2.records_changed == 1

        # Verify SCD2 history for P-101
        p101_history = entity_repo.fetch_history_for_key(
            source_name=source_name,
            entity_key={"product_id": "P-101"},
        )
        assert len(p101_history) == 2
        v1, v2 = p101_history[0], p101_history[1]
        # v1: Closed
        assert v1.is_current is False
        assert v1.attributes["price"] == 50
        assert v1.effective_to is not None
        # v2: Active
        assert v2.is_current is True
        assert v2.attributes["price"] == 60
        assert v2.effective_to is None
        assert v2.effective_from == v1.effective_to

        # 5. Query Generic History via FastAPI API Endpoint
        from src.scd2_copilot.api.dependencies import get_db, set_db
        set_db(db_manager)
        app.dependency_overrides[get_db] = lambda: db_manager
        try:
            with TestClient(app) as client:
                api_resp = client.get(
                    f"/api/v1/history/{source_name}/entity",
                    params={"key": json.dumps({"product_id": "P-101"})},
                )
                assert api_resp.status_code == 200
                data = api_resp.json()
                assert data["source_name"] == source_name
                assert data["entity_key"] == {"product_id": "P-101"}
                assert data["total_versions"] == 2
                assert len(data["versions"]) == 2
                assert data["versions"][0]["is_current"] is False
                assert data["versions"][0]["attributes"]["price"] == 50
                assert data["versions"][1]["is_current"] is True
                assert data["versions"][1]["attributes"]["price"] == 60

                # Invalid key returns 400
                bad_resp = client.get(f"/api/v1/history/{source_name}/entity?key=not-json")
                assert bad_resp.status_code == 400
        finally:
            app.dependency_overrides.pop(get_db, None)

        # 6. Suspicious Swing (P-102 price 150 -> 1500) triggers guardrail HOLD
        t2 = t1 + timedelta(days=1)
        with db_manager.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE test_product_source
                    SET price = 1500, last_modified = %s
                    WHERE product_id = 'P-102';
                    """,
                    [t2],
                )
            conn.commit()

        # Execute Worker Cycle 3: Suspicious swing held
        res3 = worker.run_once(raise_on_error=True)
        assert res3.status == "HELD"
        assert res3.records_held == 1

        # Verify history was NOT mutated (P-102 price remains 150)
        p102_history = entity_repo.fetch_history_for_key(
            source_name=source_name,
            entity_key={"product_id": "P-102"},
        )
        assert len(p102_history) == 1
        assert p102_history[0].is_current is True
        assert p102_history[0].attributes["price"] == 150

        # Verify checkpoint was NOT advanced (still t1)
        chk_after_hold = chk_repo.get_checkpoint(source_name=source_name, table_name=table_name)
        assert chk_after_hold.watermark_value == t1

        # 7. Release the Held Batch
        hold_repo = HeldChangeBatchRepository(db=db_manager)
        recent_holds = hold_repo.get_recent_holds(source_name=source_name, limit=1)
        assert len(recent_holds) == 1
        hold_id = recent_holds[0].hold_id

        release_res = worker.containment.release_held_batch(
            hold_id=hold_id,
            operator_reason="Verified valid price surge for luxury tier",
        )
        assert release_res.success is True
        assert release_res.status == HoldStatus.RELEASED.value

        # Verify P-102 history now has 2 versions
        p102_history_after = entity_repo.fetch_history_for_key(
            source_name=source_name,
            entity_key={"product_id": "P-102"},
        )
        assert len(p102_history_after) == 2
        assert p102_history_after[0].is_current is False
        assert p102_history_after[1].is_current is True
        assert p102_history_after[1].attributes["price"] == 1500

        # Verify checkpoint advanced to t2
        chk_after_release = chk_repo.get_checkpoint(source_name=source_name, table_name=table_name)
        assert chk_after_release.watermark_value == t2
