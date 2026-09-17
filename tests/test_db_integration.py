"""Live integration tests against PostgreSQL/Supabase database.

These tests run against the configured Supabase database when reachable.
If DATABASE_URL is unconfigured or the remote database is unreachable (e.g. offline CI),
these tests are gracefully skipped without failing the test suite.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from uuid import uuid4
import pytest

from src.scd2_copilot.config import get_settings
from src.scd2_copilot.db.connection import DatabaseManager
from src.scd2_copilot.db.exceptions import DatabaseTransactionError
from src.scd2_copilot.db.models import HoldSeverity, HoldStatus, RunStatus
from src.scd2_copilot.db.repositories import (
    CheckpointRepository,
    HeldChangeBatchRepository,
    InventoryHistoryRepository,
    InventorySourceRepository,
    ProcessingRunRepository,
)


def _db_is_reachable() -> bool:
    """Return True only if a live database connection can be established."""
    try:
        mgr = DatabaseManager()
        return mgr.ping()
    except Exception:
        return False


# Skip whole module if database is unreachable
pytestmark = pytest.mark.skipif(
    not _db_is_reachable(),
    reason="Live PostgreSQL/Supabase database is not configured or not reachable",
)


@pytest.fixture(scope="module")
def db_manager() -> DatabaseManager:
    return DatabaseManager()


class TestLiveDatabaseConnection:
    """Verify live connectivity and latency against the remote pooler."""

    def test_live_health_check(self, db_manager: DatabaseManager) -> None:
        result = db_manager.health_check()
        assert result["status"] == "healthy"
        assert result["latency_ms"] > 0
        assert "url" in result
        # Check credentials are not in the reported url
        assert ":***@" in result["url"] or "***" in result["url"]


class TestLiveInventorySourceRepository:
    """Verify reading operational inventory from Supabase."""

    def test_fetch_all_inventory(self, db_manager: DatabaseManager) -> None:
        repo = InventorySourceRepository(db=db_manager)
        items = repo.fetch_all_inventory()
        # The database contains seeded test inventory items
        assert len(items) >= 10
        first = items[0]
        assert first.sku_id.startswith("SKU-")
        assert first.warehouse_id.startswith("WH-")
        assert first.quantity_on_hand >= 0
        assert first.updated_at.tzinfo is not None

    def test_fetch_inventory_updated_after_ordering(self, db_manager: DatabaseManager) -> None:
        repo = InventorySourceRepository(db=db_manager)
        # Fetch with early watermark (epoch)
        early = datetime(2020, 1, 1, tzinfo=timezone.utc)
        items = repo.fetch_inventory_updated_after(watermark=early, limit=5)
        assert len(items) > 0
        # Verify deterministic ordering: updated_at ASC, sku_id ASC, warehouse_id ASC
        for i in range(len(items) - 1):
            curr, nxt = items[i], items[i + 1]
            assert curr.updated_at <= nxt.updated_at
            if curr.updated_at == nxt.updated_at:
                assert (curr.sku_id, curr.warehouse_id) <= (nxt.sku_id, nxt.warehouse_id)

    def test_fetch_inventory_for_specific_keys(self, db_manager: DatabaseManager) -> None:
        repo = InventorySourceRepository(db=db_manager)
        keys = [("SKU-1001", "WH-01"), ("SKU-1002", "WH-01")]
        items = repo.fetch_inventory_for_keys(keys)
        assert len(items) == 2
        returned_keys = {item.business_key for item in items}
        assert ("SKU-1001", "WH-01") in returned_keys
        assert ("SKU-1002", "WH-01") in returned_keys


class TestLiveCheckpointRepository:
    """Verify stream checkpoint operations in Supabase."""

    def test_get_existing_checkpoint(self, db_manager: DatabaseManager) -> None:
        repo = CheckpointRepository(db=db_manager)
        cp = repo.get_checkpoint(source_name="inventory", table_name="inventory_source")
        assert cp is not None
        assert cp.source_name == "inventory"
        assert cp.table_name == "inventory_source"


class TestLiveTransactionalAtomicity:
    """Verify that multi-table transactional boundaries strictly enforce commit and rollback."""

    def test_transaction_rollback_guarantee(self, db_manager: DatabaseManager) -> None:
        run_repo = ProcessingRunRepository(db=db_manager)
        test_run_id = uuid4()
        source_name = "test_rollback_stream"

        with pytest.raises(DatabaseTransactionError):
            with db_manager.transaction() as conn:
                # 1. Create run inside transaction
                run_repo.create_run(source_name=source_name, run_id=test_run_id, conn=conn)

                # 2. Simulate pipeline crash
                raise ValueError("Simulated unexpected processing failure")

        # In a separate connection, verify the run was NOT persisted (rolled back!)
        persisted_run = run_repo.get_run(test_run_id)
        assert persisted_run is None, "Rollback failed: run record should not exist in database"

    def test_transaction_commit_and_cleanup(self, db_manager: DatabaseManager) -> None:
        run_repo = ProcessingRunRepository(db=db_manager)
        hold_repo = HeldChangeBatchRepository(db=db_manager)
        test_run_id = uuid4()
        test_hold_id = uuid4()
        source_name = "test_commit_stream"

        try:
            # 1. Execute atomic multi-table commit
            with db_manager.transaction() as conn:
                created_run = run_repo.create_run(
                    source_name=source_name,
                    run_id=test_run_id,
                    conn=conn,
                )
                assert created_run.status in (RunStatus.PROCESSING.value, RunStatus.STARTED.value)

                created_hold = hold_repo.create_hold(
                    run_id=test_run_id,
                    source_name=source_name,
                    severity=HoldSeverity.HIGH.value,
                    reason="Test containment hold",
                    records_affected=3,
                    evidence={"test_metric": 42},
                    hold_id=test_hold_id,
                    conn=conn,
                )
                assert created_hold.status == HoldStatus.HELD.value

                # Mark run completed / committed
                run_repo.mark_run_committed(
                    run_id=test_run_id,
                    records_seen=10,
                    records_changed=3,
                    records_held=3,
                    conn=conn,
                )

            # 2. Verify committed state in fresh connection
            fetched_run = run_repo.get_run(test_run_id)
            assert fetched_run is not None
            assert fetched_run.status == RunStatus.COMMITTED.value
            assert fetched_run.records_held == 3

            fetched_hold = hold_repo.get_hold(test_hold_id)
            assert fetched_hold is not None
            assert fetched_hold.severity == "HIGH"
            assert fetched_hold.evidence["test_metric"] == 42

            # 3. Test hold release/approval lifecycle
            approved_hold = hold_repo.release_hold(test_hold_id)
            assert approved_hold.status == HoldStatus.RELEASED.value
            assert approved_hold.resolved_at is not None

        finally:
            # Clean up test rows
            try:
                with db_manager.get_connection() as cleanup_conn:
                    with cleanup_conn.cursor() as cur:
                        cur.execute("DELETE FROM held_change_batch WHERE hold_id = %s;", (test_hold_id,))
                        cur.execute("DELETE FROM processing_run WHERE run_id = %s;", (test_run_id,))
                    cleanup_conn.commit()
            except Exception:
                pass
