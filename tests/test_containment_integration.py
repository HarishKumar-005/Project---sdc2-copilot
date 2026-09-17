"""Live integration tests for suspicious batch containment and recovery against Supabase PostgreSQL (V2.4).

Verifies the full end-to-end operational containment loop:
1. Stream ingestion detects suspicious changes.
2. IngestionWorker delegates to ContainmentService.
3. held_change_batch & processing_run are atomically persisted in HELD status.
4. Checkpoint is preserved (no advance) and target inventory_history is not mutated.
5. ContainmentService.release_held_batch / discard_held_batch resolves the hold,
   advances the checkpoint, and commits history only on release.
6. Cleans up all test entities from Supabase PostgreSQL.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID, uuid4
import pytest

from src.scd2_copilot.config import Settings
from src.scd2_copilot.containment import ContainmentService
from src.scd2_copilot.db.connection import DatabaseManager
from src.scd2_copilot.db.models import (
    HeldChangeBatchRow,
    HoldStatus,
    InventoryHistoryRow,
    InventorySourceRow,
    ProcessingCheckpointRow,
    ProcessingRunRow,
    RunStatus,
)
from src.scd2_copilot.db.repositories import (
    CheckpointRepository,
    HeldChangeBatchRepository,
    InventoryHistoryRepository,
    InventorySourceRepository,
    ProcessingRunRepository,
)
from src.scd2_copilot.guardrail.engine import GuardrailEngine
from src.scd2_copilot.guardrail.models import RuleId
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


class TestLiveContainmentAndRecovery:
    """Test full live containment, hold deduplication, and recovery lifecycle against Supabase."""

    def test_live_containment_and_release_lifecycle(self, db_manager: DatabaseManager) -> None:
        stream_name = f"test_contain_stream_{uuid4().hex[:8]}"
        table_name = "inventory_source"
        test_sku = f"SKU-HELD-{uuid4().hex[:6]}"
        test_wh = "WH-HELD"

        checkpoint_repo = CheckpointRepository(db=db_manager)
        inventory_repo = InventorySourceRepository(db=db_manager)
        history_repo = InventoryHistoryRepository(db=db_manager)
        run_repo = ProcessingRunRepository(db=db_manager)
        hold_repo = HeldChangeBatchRepository(db=db_manager)

        containment_service = ContainmentService(
            db_manager=db_manager,
            source_name=stream_name,
            table_name=table_name,
        )

        initial_watermark = datetime.now(timezone.utc)

        # 1. Initialize checkpoint
        checkpoint_repo.initialize_checkpoint_if_missing(
            source_name=stream_name,
            table_name=table_name,
            watermark=initial_watermark,
        )

        # 2. Seed baseline active history row: 10 units
        eff_from = datetime.combine(initial_watermark.date() - timedelta(days=7), time.min, tzinfo=timezone.utc)
        baseline_history = InventoryHistoryRow(
            sku_id=test_sku,
            warehouse_id=test_wh,
            quantity_on_hand=10,
            reorder_level=20,
            status="ACTIVE",
            effective_from=eff_from,
            effective_to=None,
            is_current=True,
        )
        history_repo.insert_history_rows([baseline_history])

        # 3. Insert incoming source row with a 900% quantity swing (100 units), triggering LARGE_QUANTITY_SWING
        source_ts = datetime.now(timezone.utc)
        source_row = InventorySourceRow(
            sku_id=test_sku,
            warehouse_id=test_wh,
            quantity_on_hand=100,
            reorder_level=20,
            status="ACTIVE",
            updated_at=source_ts,
        )
        inventory_repo.upsert_inventory_rows([source_row])

        created_run_id: UUID | None = None
        created_hold_id: UUID | None = None

        try:
            # 4. Instantiate worker with strict guardrail threshold
            worker = IngestionWorker(
                db_manager=db_manager,
                source_name=stream_name,
                table_name=table_name,
                batch_size=10,
            )

            # 5. Run worker cycle: expect HELD
            cycle_result = worker.run_once(raise_on_error=True)
            created_run_id = cycle_result.run_id

            assert cycle_result.status == "HELD"
            assert cycle_result.records_held == 1
            assert cycle_result.guardrail_decision is not None
            assert cycle_result.guardrail_decision.is_suspicious is True
            assert any(
                r.rule_id == RuleId.LARGE_QUANTITY_SWING.value
                for r in cycle_result.guardrail_decision.triggered_rules
            )

            # 6. Verify processing_checkpoint was NOT advanced
            cp = checkpoint_repo.get_checkpoint(source_name=stream_name, table_name=table_name)
            assert cp is not None
            assert cp.watermark_value == initial_watermark

            # 7. Verify inventory_history remains unmutated (only 1 baseline row, still active)
            history_rows = history_repo.fetch_current_history_for_keys([(test_sku, test_wh)])
            assert len(history_rows) == 1
            assert history_rows[0].quantity_on_hand == 10
            assert history_rows[0].is_current is True

            # 8. Verify held_change_batch row exists in Supabase
            holds = hold_repo.get_recent_holds(
                limit=10, status=HoldStatus.HELD.value, source_name=stream_name
            )
            assert len(holds) >= 1
            held_batch = holds[0]
            created_hold_id = held_batch.hold_id
            assert held_batch.run_id == created_run_id
            assert held_batch.status == "HELD"
            assert "batch_fingerprint" in held_batch.evidence

            # 9. Verify duplicate polling suppression:
            # Running worker again on unadvanced checkpoint must NOT create a 2nd hold
            second_cycle = worker.run_once(raise_on_error=True)
            assert second_cycle.status == "HELD"
            holds_after = hold_repo.get_recent_holds(
                limit=10, status=HoldStatus.HELD.value, source_name=stream_name
            )
            assert len(holds_after) == len(holds)  # No duplicate hold row created!

            # 10. Release the held batch via ContainmentService
            res = containment_service.release_held_batch(
                hold_id=created_hold_id,
                operator_reason="Verified legitimate large inventory reduction",
            )
            assert res.success is True
            assert res.status == "RELEASED"
            assert res.is_idempotent is False

            # 11. Verify hold status updated to RELEASED in Supabase
            updated_hold = hold_repo.get_hold(created_hold_id)
            assert updated_hold is not None
            assert updated_hold.status == "RELEASED"
            assert updated_hold.resolved_at is not None

            # 12. Verify inventory_history is now updated:
            # New active row with quantity 10, prior baseline row closed
            current_hist = history_repo.fetch_current_history_for_keys([(test_sku, test_wh)])
            assert len(current_hist) == 1
            assert current_hist[0].quantity_on_hand == 100
            assert current_hist[0].is_current is True

            # 13. Verify processing_checkpoint has now advanced to the batch timestamp
            updated_cp = checkpoint_repo.get_checkpoint(
                source_name=stream_name, table_name=table_name
            )
            assert updated_cp is not None
            assert updated_cp.watermark_value == source_ts

            # 14. Verify idempotent release: calling release again returns success without re-inserting
            idempotent_res = containment_service.release_held_batch(created_hold_id)
            assert idempotent_res.success is True
            assert idempotent_res.is_idempotent is True

        finally:
            # Clean up all created test records
            try:
                with db_manager.get_connection() as conn:
                    with conn.cursor() as cur:
                        if created_hold_id:
                            cur.execute(
                                "DELETE FROM held_change_batch WHERE hold_id = %s;",
                                (created_hold_id,),
                            )
                        if created_run_id:
                            cur.execute(
                                "DELETE FROM held_change_batch WHERE run_id = %s;",
                                (created_run_id,),
                            )
                            cur.execute(
                                "DELETE FROM processing_run WHERE run_id = %s;",
                                (created_run_id,),
                            )
                        cur.execute(
                            "DELETE FROM inventory_history WHERE sku_id = %s;",
                            (test_sku,),
                        )
                        cur.execute(
                            "DELETE FROM inventory_source WHERE sku_id = %s;",
                            (test_sku,),
                        )
                        cur.execute(
                            "DELETE FROM processing_checkpoint WHERE source_name = %s;",
                            (stream_name,),
                        )
                    conn.commit()
            except Exception:
                pass

    def test_live_containment_and_discard_lifecycle(self, db_manager: DatabaseManager) -> None:
        stream_name = f"test_discard_stream_{uuid4().hex[:8]}"
        table_name = "inventory_source"
        test_sku = f"SKU-DISCARD-{uuid4().hex[:6]}"
        test_wh = "WH-DISCARD"

        checkpoint_repo = CheckpointRepository(db=db_manager)
        inventory_repo = InventorySourceRepository(db=db_manager)
        history_repo = InventoryHistoryRepository(db=db_manager)
        run_repo = ProcessingRunRepository(db=db_manager)
        hold_repo = HeldChangeBatchRepository(db=db_manager)

        containment_service = ContainmentService(
            db_manager=db_manager,
            source_name=stream_name,
            table_name=table_name,
        )

        initial_watermark = datetime.now(timezone.utc)

        # 1. Initialize checkpoint
        checkpoint_repo.initialize_checkpoint_if_missing(
            source_name=stream_name,
            table_name=table_name,
            watermark=initial_watermark,
        )

        # 2. Seed baseline active history row: 10 units
        eff_from = datetime.combine(initial_watermark.date() - timedelta(days=7), time.min, tzinfo=timezone.utc)
        baseline_history = InventoryHistoryRow(
            sku_id=test_sku,
            warehouse_id=test_wh,
            quantity_on_hand=10,
            reorder_level=20,
            status="ACTIVE",
            effective_from=eff_from,
            effective_to=None,
            is_current=True,
        )
        history_repo.insert_history_rows([baseline_history])

        # 3. Insert incoming source row with a 900% quantity swing
        source_ts = datetime.now(timezone.utc)
        source_row = InventorySourceRow(
            sku_id=test_sku,
            warehouse_id=test_wh,
            quantity_on_hand=100,
            reorder_level=20,
            status="ACTIVE",
            updated_at=source_ts,
        )
        inventory_repo.upsert_inventory_rows([source_row])

        created_run_id: UUID | None = None
        created_hold_id: UUID | None = None

        try:
            worker = IngestionWorker(
                db_manager=db_manager,
                source_name=stream_name,
                table_name=table_name,
                batch_size=10,
            )

            cycle_result = worker.run_once(raise_on_error=True)
            created_run_id = cycle_result.run_id
            assert cycle_result.status == "HELD"

            holds = hold_repo.get_recent_holds(
                limit=10, status=HoldStatus.HELD.value, source_name=stream_name
            )
            assert len(holds) >= 1
            created_hold_id = holds[0].hold_id

            # Discard the batch and advance checkpoint to unblock stream
            discard_res = containment_service.discard_held_batch(
                hold_id=created_hold_id,
                operator_reason="Rejected corrupt source batch",
                advance_checkpoint=True,
            )
            assert discard_res.success is True
            assert discard_res.status == "DISCARDED"
            assert discard_res.checkpoint_advanced_to == source_ts

            # Invariant: inventory_history remains unmutated (quantity remains 10)
            hist = history_repo.fetch_current_history_for_keys([(test_sku, test_wh)])
            assert len(hist) == 1
            assert hist[0].quantity_on_hand == 10

            # Invariant: checkpoint advanced to source_ts to unblock stream
            cp = checkpoint_repo.get_checkpoint(source_name=stream_name, table_name=table_name)
            assert cp is not None
            assert cp.watermark_value == source_ts

            # Subsequent worker poll finds NO new records (empty cycle)
            next_cycle = worker.run_once(raise_on_error=True)
            assert next_cycle.status == "EMPTY"

        finally:
            try:
                with db_manager.get_connection() as conn:
                    with conn.cursor() as cur:
                        if created_hold_id:
                            cur.execute(
                                "DELETE FROM held_change_batch WHERE hold_id = %s;",
                                (created_hold_id,),
                            )
                        if created_run_id:
                            cur.execute(
                                "DELETE FROM held_change_batch WHERE run_id = %s;",
                                (created_run_id,),
                            )
                            cur.execute(
                                "DELETE FROM processing_run WHERE run_id = %s;",
                                (created_run_id,),
                            )
                        cur.execute(
                            "DELETE FROM inventory_history WHERE sku_id = %s;",
                            (test_sku,),
                        )
                        cur.execute(
                            "DELETE FROM inventory_source WHERE sku_id = %s;",
                            (test_sku,),
                        )
                        cur.execute(
                            "DELETE FROM processing_checkpoint WHERE source_name = %s;",
                            (stream_name,),
                        )
                    conn.commit()
            except Exception:
                pass
