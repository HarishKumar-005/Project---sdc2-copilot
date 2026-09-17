"""Mandatory live integration tests for V3 Phase 2 Live Monitoring Pipeline.

Verifies end-to-end against live Supabase PostgreSQL:
1. Scenario 1: Same timestamp (T), small batch_size=2 across 3 records.
   Verifies composite cursor (timestamp, keys) ordering, split boundaries, zero skips, zero duplicates.
2. Scenario 2: Normal update advances checkpoint, closes prior SCD2 version, creates new active version.
3. Scenario 3: Suspicious swing triggers guardrail HOLD, preserves checkpoint unadvanced, zero history mutations.
4. Scenario 4: Concurrency mutual exclusion via PostgreSQL advisory locks (pg_try_advisory_lock).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time
from uuid import uuid4

import pytest

from src.scd2_copilot.config import Settings
from src.scd2_copilot.db.connection import DatabaseManager
from src.scd2_copilot.db.models import (
    HeldChangeBatchRow,
    HoldStatus,
    InventoryHistoryRow,
    InventorySourceRow,
    ProcessingCheckpointRow,
)
from src.scd2_copilot.db.repositories import (
    CheckpointRepository,
    HeldChangeBatchRepository,
    InventoryHistoryRepository,
    InventorySourceRepository,
    ProcessingRunRepository,
)
from src.scd2_copilot.source.models import MonitorConfig, PostgresSourceDefinition
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


class TestLiveMonitoringPipeline:
    """Mandatory live verification of the composite cursor and monitoring pipeline."""

    def test_scenario_1_same_timestamp_composite_cursor_microbatching(
        self, db_manager: DatabaseManager
    ) -> None:
        """Scenario 1: 3 records sharing the exact same timestamp T with batch_size=2.

        Proves:
        - Cycle 1 reads records 1 & 2, commits, advances cursor to (T, SKU-B).
        - Cycle 2 reads record 3, commits, advances cursor to (T, SKU-C).
        - Cycle 3 returns EMPTY.
        - Zero records skipped, zero duplicates processed.
        - Exactly 3 SCD2 current versions created in inventory_history.
        """
        prefix = uuid4().hex[:6]
        stream_name = f"live_stream_{prefix}"
        table_name = "inventory_source"

        sku_a = f"SKU-A-{prefix}"
        sku_b = f"SKU-B-{prefix}"
        sku_c = f"SKU-C-{prefix}"
        wh = "WH-1"

        checkpoint_repo = CheckpointRepository(db=db_manager)
        inventory_repo = InventorySourceRepository(db=db_manager)
        history_repo = InventoryHistoryRepository(db=db_manager)

        # Baseline timestamp T (truncated to microseconds to match postgres timestamptz)
        now_utc = datetime.now(timezone.utc)
        t_baseline = now_utc.replace(microsecond=(now_utc.microsecond // 1000) * 1000)
        t_init = t_baseline - timedelta(seconds=10)

        # Initialize checkpoint at T_init < T_baseline
        checkpoint_repo.initialize_checkpoint_if_missing(
            source_name=stream_name,
            table_name=table_name,
            watermark=t_init,
        )

        # Insert 3 records with identical timestamp T_baseline
        records = [
            InventorySourceRow(sku_id=sku_a, warehouse_id=wh, quantity_on_hand=10, reorder_level=5, status="ACTIVE", updated_at=t_baseline),
            InventorySourceRow(sku_id=sku_b, warehouse_id=wh, quantity_on_hand=20, reorder_level=5, status="ACTIVE", updated_at=t_baseline),
            InventorySourceRow(sku_id=sku_c, warehouse_id=wh, quantity_on_hand=30, reorder_level=5, status="ACTIVE", updated_at=t_baseline),
        ]

        worker = IngestionWorker(
            db_manager=db_manager,
            source_name=stream_name,
            table_name=table_name,
            batch_size=2,
        )

        try:
            inventory_repo.upsert_inventory_rows(records)

            # ── CYCLE 1: Expect SKU_A and SKU_B ──
            res1 = worker.run_once(raise_on_error=True)
            assert res1.status == "COMMITTED"
            assert res1.records_seen == 2
            assert res1.last_cursor is not None
            assert res1.last_cursor.keys["sku_id"] == sku_b
            assert res1.last_cursor.timestamp == t_baseline

            cp1 = checkpoint_repo.get_checkpoint(source_name=stream_name, table_name=table_name)
            assert cp1 is not None
            assert cp1.watermark_value == t_baseline
            assert cp1.cursor_keys is not None
            assert cp1.cursor_keys["sku_id"] == sku_b

            # ── CYCLE 2: Expect SKU_C only ──
            res2 = worker.run_once(raise_on_error=True)
            assert res2.status == "COMMITTED"
            assert res2.records_seen == 1
            assert res2.last_cursor is not None
            assert res2.last_cursor.keys["sku_id"] == sku_c
            assert res2.last_cursor.timestamp == t_baseline

            cp2 = checkpoint_repo.get_checkpoint(source_name=stream_name, table_name=table_name)
            assert cp2 is not None
            assert cp2.watermark_value == t_baseline
            assert cp2.cursor_keys is not None
            assert cp2.cursor_keys["sku_id"] == sku_c

            # ── CYCLE 3: Stream is up to date (EMPTY) ──
            res3 = worker.run_once(raise_on_error=True)
            assert res3.status == "EMPTY"
            assert res3.records_seen == 0

            # ── VERIFY INVENTORY HISTORY INVARIANTS ──
            test_keys = [(sku_a, wh), (sku_b, wh), (sku_c, wh)]
            history = history_repo.fetch_current_history_for_keys(test_keys)
            assert len(history) == 3
            assert all(h.is_current for h in history)
            assert {h.sku_id for h in history} == {sku_a, sku_b, sku_c}

        finally:
            # Clean up test rows
            with db_manager.transaction() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM inventory_source WHERE sku_id IN (%s, %s, %s);",
                        (sku_a, sku_b, sku_c),
                    )
                    cur.execute(
                        "DELETE FROM inventory_history WHERE sku_id IN (%s, %s, %s);",
                        (sku_a, sku_b, sku_c),
                    )
                    cur.execute(
                        "DELETE FROM processing_checkpoint WHERE source_name = %s;",
                        (stream_name,),
                    )
                    cur.execute(
                        "DELETE FROM processing_run WHERE source_name = %s;",
                        (stream_name,),
                    )

    def test_scenario_2_normal_update_advances_checkpoint_and_version(
        self, db_manager: DatabaseManager
    ) -> None:
        """Scenario 2: Normal update commits new version and advances checkpoint."""
        prefix = uuid4().hex[:6]
        stream_name = f"live_stream_{prefix}"
        table_name = "inventory_source"
        sku = f"SKU-U-{prefix}"
        wh = "WH-1"

        checkpoint_repo = CheckpointRepository(db=db_manager)
        inventory_repo = InventorySourceRepository(db=db_manager)
        history_repo = InventoryHistoryRepository(db=db_manager)

        now = datetime.now(timezone.utc)
        t_init = now
        t1 = t_init + timedelta(milliseconds=10)
        t2 = t_init + timedelta(milliseconds=20)

        checkpoint_repo.initialize_checkpoint_if_missing(
            source_name=stream_name,
            table_name=table_name,
            watermark=t_init,
        )

        worker = IngestionWorker(
            db_manager=db_manager,
            source_name=stream_name,
            table_name=table_name,
            batch_size=10,
        )

        try:
            # 1. Insert initial record at T1
            inventory_repo.upsert_inventory_rows([
                InventorySourceRow(sku_id=sku, warehouse_id=wh, quantity_on_hand=50, reorder_level=10, status="ACTIVE", updated_at=t1)
            ])
            r1 = worker.run_once(raise_on_error=True)
            assert r1.status == "COMMITTED"

            # Backdate initial history row by 1 day so date-based SCD2 interval [effective_from, effective_to) is strictly positive
            with db_manager.transaction() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE inventory_history SET effective_from = effective_from - INTERVAL '1 day' WHERE sku_id = %s;",
                        (sku,),
                    )

            # 2. Update record to 60 at T2 (normal update)
            inventory_repo.upsert_inventory_rows([
                InventorySourceRow(sku_id=sku, warehouse_id=wh, quantity_on_hand=60, reorder_level=10, status="ACTIVE", updated_at=t2)
            ])
            r2 = worker.run_once(raise_on_error=True)
            assert r2.status == "COMMITTED"
            assert r2.records_seen == 1
            assert r2.records_changed == 1

            # 3. Checkpoint advanced to the updated record's timestamp
            updated_record = inventory_repo.fetch_inventory_for_keys([(sku, wh)])[0]
            cp = checkpoint_repo.get_checkpoint(source_name=stream_name, table_name=table_name)
            assert cp is not None
            assert cp.watermark_value == updated_record.updated_at
            assert cp.watermark_value > t1
            assert cp.cursor_keys == {"sku_id": sku, "warehouse_id": wh}

            # 4. Check SCD2 history: prior closed, current active
            with db_manager.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT quantity_on_hand, is_current, effective_to FROM inventory_history WHERE sku_id = %s ORDER BY effective_from ASC;",
                        (sku,),
                    )
                    rows = cur.fetchall()
            assert len(rows) == 2
            # Prior version closed
            row0_qty = rows[0]["quantity_on_hand"] if isinstance(rows[0], dict) else rows[0][0]
            row0_cur = rows[0]["is_current"] if isinstance(rows[0], dict) else rows[0][1]
            row0_eff = rows[0]["effective_to"] if isinstance(rows[0], dict) else rows[0][2]
            assert row0_qty == 50
            assert row0_cur is False
            assert row0_eff is not None

            # New active version
            row1_qty = rows[1]["quantity_on_hand"] if isinstance(rows[1], dict) else rows[1][0]
            row1_cur = rows[1]["is_current"] if isinstance(rows[1], dict) else rows[1][1]
            row1_eff = rows[1]["effective_to"] if isinstance(rows[1], dict) else rows[1][2]
            assert row1_qty == 60
            assert row1_cur is True
            assert row1_eff is None

        finally:
            with db_manager.transaction() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM held_change_batch WHERE source_name = %s;", (stream_name,))
                    cur.execute("DELETE FROM processing_checkpoint WHERE source_name = %s;", (stream_name,))
                    cur.execute("DELETE FROM processing_run WHERE source_name = %s;", (stream_name,))
                    cur.execute("DELETE FROM inventory_history WHERE sku_id = %s;", (sku,))
                    cur.execute("DELETE FROM inventory_source WHERE sku_id = %s;", (sku,))

    def test_scenario_3_suspicious_swing_holds_batch_and_preserves_checkpoint(
        self, db_manager: DatabaseManager
    ) -> None:
        """Scenario 3: Suspicious swing triggers HOLD, keeps checkpoint unchanged, no history written."""
        prefix = uuid4().hex[:6]
        stream_name = f"live_stream_{prefix}"
        table_name = "inventory_source"
        sku = f"SKU-H-{prefix}"
        wh = "WH-1"

        checkpoint_repo = CheckpointRepository(db=db_manager)
        inventory_repo = InventorySourceRepository(db=db_manager)
        history_repo = InventoryHistoryRepository(db=db_manager)
        hold_repo = HeldChangeBatchRepository(db=db_manager)

        now = datetime.now(timezone.utc)
        t_init = now
        t1 = t_init + timedelta(milliseconds=10)
        t2 = t_init + timedelta(milliseconds=20)

        checkpoint_repo.initialize_checkpoint_if_missing(
            source_name=stream_name,
            table_name=table_name,
            watermark=t_init,
        )

        settings = Settings(
            guardrail_enabled=True,
            guardrail_max_quantity_relative_change=3.0,
            guardrail_min_absolute_quantity_change=50,
            ingestion_batch_size=10,
        )

        worker = IngestionWorker(
            db_manager=db_manager,
            settings=settings,
            source_name=stream_name,
            table_name=table_name,
        )

        try:
            # 1. Establish baseline at T1 with quantity 100
            inventory_repo.upsert_inventory_rows([
                InventorySourceRow(sku_id=sku, warehouse_id=wh, quantity_on_hand=100, reorder_level=20, status="ACTIVE", updated_at=t1)
            ])
            r1 = worker.run_once(raise_on_error=True)
            assert r1.status == "COMMITTED"

            # Backdate initial history row by 1 day so SCD2 interval [effective_from, effective_to) is strictly positive
            with db_manager.transaction() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE inventory_history SET effective_from = effective_from - INTERVAL '1 day' WHERE sku_id = %s;",
                        (sku,),
                    )

            cp_before = checkpoint_repo.get_checkpoint(source_name=stream_name, table_name=table_name)
            assert cp_before is not None
            assert cp_before.watermark_value == t1

            # 2. Update to 1500 (1400% jump) at T2 -> suspicious
            inventory_repo.upsert_inventory_rows([
                InventorySourceRow(sku_id=sku, warehouse_id=wh, quantity_on_hand=1500, reorder_level=20, status="ACTIVE", updated_at=t2)
            ])
            r2 = worker.run_once(raise_on_error=True)

            # Invariant: Guardrail holds the batch
            assert r2.status == "HELD"
            assert r2.records_held == 1
            assert r2.guardrail_decision is not None
            assert r2.guardrail_decision.is_suspicious

            # Invariant: Checkpoint MUST NOT advance
            cp_after = checkpoint_repo.get_checkpoint(source_name=stream_name, table_name=table_name)
            assert cp_after is not None
            assert cp_after.watermark_value == cp_before.watermark_value
            assert cp_after.cursor_keys == cp_before.cursor_keys

            # Invariant: inventory_history must NOT contain quantity 1500
            with db_manager.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT quantity_on_hand, is_current FROM inventory_history WHERE sku_id = %s;",
                        (sku,),
                    )
                    rows = cur.fetchall()
            assert len(rows) == 1
            row_qty = rows[0]["quantity_on_hand"] if isinstance(rows[0], dict) else rows[0][0]
            row_cur = rows[0]["is_current"] if isinstance(rows[0], dict) else rows[0][1]
            assert row_qty == 100
            assert row_cur is True

            # Invariant: Hold record exists in held_change_batch
            holds = hold_repo.get_recent_holds(limit=5, source_name=stream_name)
            assert len(holds) >= 1
            latest_hold = holds[0]
            assert latest_hold.status == HoldStatus.HELD.value
            assert latest_hold.evidence is not None
            assert "first_cursor" in latest_hold.evidence

        finally:
            with db_manager.transaction() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM held_change_batch WHERE source_name = %s;", (stream_name,))
                    cur.execute("DELETE FROM processing_checkpoint WHERE source_name = %s;", (stream_name,))
                    cur.execute("DELETE FROM processing_run WHERE source_name = %s;", (stream_name,))
                    cur.execute("DELETE FROM inventory_history WHERE sku_id = %s;", (sku,))
                    cur.execute("DELETE FROM inventory_source WHERE sku_id = %s;", (sku,))

    def test_scenario_4_advisory_lock_mutual_exclusion(
        self, db_manager: DatabaseManager
    ) -> None:
        """Scenario 4: PostgreSQL advisory lock blocks concurrent worker cycles for same source."""
        source_name = f"lock_test_{uuid4().hex[:8]}"
        lock_name = f"scd2_worker_{source_name}"

        # Initialize checkpoint at now so worker doesn't read historical demo data
        now = datetime.now(timezone.utc)
        checkpoint_repo = CheckpointRepository(db=db_manager)
        checkpoint_repo.initialize_checkpoint_if_missing(
            source_name=source_name,
            table_name="inventory_source",
            watermark=now,
        )

        try:
            # Acquire advisory lock externally using a dedicated connection
            with db_manager.get_connection() as lock_conn:
                with lock_conn.cursor() as cur:
                    cur.execute("SELECT pg_try_advisory_lock(hashtext(%s));", (lock_name,))
                    row = cur.fetchone()
                    acquired = list(row.values())[0] if isinstance(row, dict) else row[0]
                    assert acquired is True, "External lock acquisition should succeed"

                # Instantiate worker for same source and run with acquire_lock=True
                worker = IngestionWorker(
                    db_manager=db_manager,
                    source_name=source_name,
                )

                result = worker.run_once(raise_on_error=True, acquire_lock=True)
                # Must detect conflicting lock and return LOCKED status
                assert result.status == "LOCKED"
                assert result.records_seen == 0

                # Release advisory lock
                with lock_conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(hashtext(%s));", (lock_name,))

            # Now that lock is released, worker should acquire lock cleanly
            res_after = worker.run_once(raise_on_error=True, acquire_lock=True)
            assert res_after.status != "LOCKED"
            assert res_after.status in ("EMPTY", "COMMITTED")

        finally:
            with db_manager.transaction() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM processing_checkpoint WHERE source_name = %s;", (source_name,))
                    cur.execute("DELETE FROM processing_run WHERE source_name = %s;", (source_name,))
