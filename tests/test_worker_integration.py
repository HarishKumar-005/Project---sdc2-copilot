"""Live integration tests for the incremental ingestion worker against Supabase PostgreSQL.

Tests are gracefully skipped if the database is unconfigured or unreachable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4
import pytest

from src.scd2_copilot.db.connection import DatabaseManager
from src.scd2_copilot.db.models import InventorySourceRow, RunStatus
from src.scd2_copilot.db.repositories import (
    CheckpointRepository,
    InventoryHistoryRepository,
    InventorySourceRepository,
    ProcessingRunRepository,
)
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


class TestLiveWorkerIngestion:
    """Verify live incremental ingestion flow against Supabase PostgreSQL."""

    def test_live_worker_single_cycle_end_to_end(self, db_manager: DatabaseManager) -> None:
        stream_name = f"test_stream_{uuid4().hex[:8]}"
        table_name = "inventory_source"
        test_sku = f"SKU-TEST-{uuid4().hex[:6]}"
        test_wh = "WH-TEST"

        checkpoint_repo = CheckpointRepository(db=db_manager)
        inventory_repo = InventorySourceRepository(db=db_manager)
        history_repo = InventoryHistoryRepository(db=db_manager)
        run_repo = ProcessingRunRepository(db=db_manager)

        now = datetime.now(timezone.utc)

        # 1. Initialize checkpoint for isolated test stream
        checkpoint_repo.initialize_checkpoint_if_missing(
            source_name=stream_name,
            table_name=table_name,
            watermark=now,
        )

        # 2. Insert test source row with timestamp > watermark
        record_ts = datetime.now(timezone.utc)
        test_source_row = InventorySourceRow(
            sku_id=test_sku,
            warehouse_id=test_wh,
            quantity_on_hand=77,
            reorder_level=15,
            status="ACTIVE",
            updated_at=record_ts,
        )

        created_run_id = None
        try:
            inventory_repo.upsert_inventory_rows([test_source_row])

            # 3. Instantiate worker and execute exactly one cycle
            worker = IngestionWorker(
                db_manager=db_manager,
                source_name=stream_name,
                table_name=table_name,
                batch_size=10,
            )

            result = worker.run_once(raise_on_error=True)
            created_run_id = result.run_id

            # 4. Verify cycle committed
            assert result.status == "COMMITTED"
            assert result.records_seen >= 1
            assert result.run_id is not None

            # 5. Verify checkpoint advanced
            updated_cp = checkpoint_repo.get_checkpoint(source_name=stream_name, table_name=table_name)
            assert updated_cp is not None
            assert updated_cp.watermark_value is not None
            assert updated_cp.watermark_value >= record_ts
            assert updated_cp.last_successful_run_id == result.run_id

            # 6. Verify processing run record
            run_row = run_repo.get_run(result.run_id)
            assert run_row is not None
            assert run_row.status == RunStatus.COMMITTED.value

            # 7. Verify history row was inserted
            history_rows = history_repo.fetch_current_history_for_keys([(test_sku, test_wh)])
            assert len(history_rows) == 1
            assert history_rows[0].quantity_on_hand == 77
            assert history_rows[0].is_current is True

        finally:
            # Clean up test rows
            try:
                with db_manager.get_connection() as cleanup_conn:
                    with cleanup_conn.cursor() as cur:
                        cur.execute("DELETE FROM inventory_history WHERE sku_id = %s;", (test_sku,))
                        cur.execute("DELETE FROM inventory_source WHERE sku_id = %s;", (test_sku,))
                        cur.execute("DELETE FROM processing_checkpoint WHERE source_name = %s;", (stream_name,))
                        if created_run_id:
                            cur.execute("DELETE FROM processing_run WHERE run_id = %s;", (created_run_id,))
                    cleanup_conn.commit()
            except Exception:
                pass

    def test_live_worker_failure_preserves_checkpoint(self, db_manager: DatabaseManager) -> None:
        stream_name = f"test_fail_stream_{uuid4().hex[:8]}"
        table_name = "inventory_source"
        test_sku = f"SKU-FAIL-{uuid4().hex[:6]}"
        test_wh = "WH-FAIL"

        checkpoint_repo = CheckpointRepository(db=db_manager)
        inventory_repo = InventorySourceRepository(db=db_manager)

        initial_ts = datetime.now(timezone.utc)

        # 1. Initialize checkpoint
        checkpoint_repo.initialize_checkpoint_if_missing(
            source_name=stream_name,
            table_name=table_name,
            watermark=initial_ts,
        )

        # 2. Insert test source row
        record_ts = datetime.now(timezone.utc)
        test_row = InventorySourceRow(
            sku_id=test_sku,
            warehouse_id=test_wh,
            quantity_on_hand=99,
            reorder_level=10,
            status="ACTIVE",
            updated_at=record_ts,
        )

        failed_run_id = None
        try:
            inventory_repo.upsert_inventory_rows([test_row])

            worker = IngestionWorker(
                db_manager=db_manager,
                source_name=stream_name,
                table_name=table_name,
            )

            # Force failure by patching detect_changes
            from unittest.mock import patch

            with patch("src.scd2_copilot.worker.worker.detect_changes", side_effect=RuntimeError("Simulated pipeline crash")):
                result = worker.run_once(raise_on_error=False)
                failed_run_id = result.run_id

            assert result.status == "FAILED"

            # 3. Checkpoint must remain unchanged!
            cp = checkpoint_repo.get_checkpoint(source_name=stream_name, table_name=table_name)
            assert cp is not None
            assert cp.watermark_value == initial_ts

        finally:
            try:
                with db_manager.get_connection() as cleanup_conn:
                    with cleanup_conn.cursor() as cur:
                        cur.execute("DELETE FROM inventory_source WHERE sku_id = %s;", (test_sku,))
                        cur.execute("DELETE FROM processing_checkpoint WHERE source_name = %s;", (stream_name,))
                        if failed_run_id:
                            cur.execute("DELETE FROM processing_run WHERE run_id = %s;", (failed_run_id,))
                    cleanup_conn.commit()
            except Exception:
                pass
