"""Live integration tests for Evidence-Grounded AI Explanation against Supabase PostgreSQL (V2.5).

Verifies the full end-to-end evidence explanation loop:
1. Stream ingestion detects suspicious changes.
2. IngestionWorker contains suspicious batch in held_change_batch.
3. ExplanationService generates structured, evidence-grounded explanation.
4. Explanation is additively attached to held_change_batch.evidence["explanation"].
5. Baseline guardrail evidence and batch fingerprints are preserved intact.
6. Worker remains 100% resilient if explanation generation fails.
7. Cleans up all test entities from Supabase PostgreSQL.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from unittest.mock import MagicMock
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
from src.scd2_copilot.explanation import ExplanationService
from src.scd2_copilot.explanation.models import BatchExplanationResult, ExplanationContext
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


class TestLiveExplanationIntegration:
    """Test full live explanation generation and additive persistence in Supabase PostgreSQL."""

    def test_live_suspicious_batch_explanation_lifecycle(self, db_manager: DatabaseManager) -> None:
        stream_name = f"test_explain_stream_{uuid4().hex[:8]}"
        table_name = "inventory_source"
        test_sku = f"SKU-EXP-{uuid4().hex[:6]}"
        test_wh = "WH-EXP"

        checkpoint_repo = CheckpointRepository(db=db_manager)
        inventory_repo = InventorySourceRepository(db=db_manager)
        history_repo = InventoryHistoryRepository(db=db_manager)
        run_repo = ProcessingRunRepository(db=db_manager)
        hold_repo = HeldChangeBatchRepository(db=db_manager)

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
            # 4. Instantiate worker
            worker = IngestionWorker(
                db_manager=db_manager,
                source_name=stream_name,
                table_name=table_name,
                batch_size=10,
            )

            # 5. Run worker cycle: expect HELD with Explanation
            cycle_result = worker.run_once(raise_on_error=True)
            created_run_id = cycle_result.run_id

            assert cycle_result.status == "HELD"
            assert cycle_result.records_held == 1
            assert cycle_result.guardrail_decision is not None
            assert cycle_result.guardrail_decision.is_suspicious is True

            # Explanation checks on WorkerCycleResult
            assert cycle_result.explanation is not None
            exp = cycle_result.explanation
            assert exp.decision == "SUSPICIOUS"
            assert exp.severity == cycle_result.guardrail_decision.severity.value
            assert len(exp.summary) > 0
            assert len(exp.what_changed) > 0
            assert len(exp.why_flagged) > 0
            assert exp.grounding_passed is True

            # 6. Verify processing_checkpoint was NOT advanced
            cp = checkpoint_repo.get_checkpoint(source_name=stream_name, table_name=table_name)
            assert cp is not None
            assert cp.watermark_value == initial_watermark

            # 7. Verify inventory_history remains unmutated
            history_rows = history_repo.fetch_current_history_for_keys([(test_sku, test_wh)])
            assert len(history_rows) == 1
            assert history_rows[0].quantity_on_hand == 10
            assert history_rows[0].is_current is True

            # 8. Verify held_change_batch row in Supabase has explanation additively attached
            holds = hold_repo.get_recent_holds(
                limit=10, status=HoldStatus.HELD.value, source_name=stream_name
            )
            assert len(holds) >= 1
            held_batch = holds[0]
            created_hold_id = held_batch.hold_id

            # Verify additive evidence preservation
            assert "explanation" in held_batch.evidence
            persisted_exp = held_batch.evidence["explanation"]
            assert persisted_exp["decision"] == "SUSPICIOUS"
            assert persisted_exp["severity"] == cycle_result.guardrail_decision.severity.value
            assert len(persisted_exp["why_flagged"]) > 0
            assert "guardrail_evidence" in held_batch.evidence
            assert "batch_fingerprint" in held_batch.evidence

            # 9. Test direct ExplanationService.explain_held_batch on existing hold
            explanation_service = ExplanationService(db_manager=db_manager, hold_repo=hold_repo)
            re_explained = explanation_service.explain_held_batch(
                hold_id=held_batch.hold_id,
                persist=True,
            )
            assert re_explained.decision == "SUSPICIOUS"
            assert re_explained.grounding_passed is True

        finally:
            # 10. Clean up test records (respecting foreign key relationships)
            with db_manager.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM held_change_batch WHERE source_name = %s", (stream_name,))
                    if created_run_id:
                        cur.execute("DELETE FROM processing_run WHERE run_id = %s", (created_run_id,))
                    cur.execute(
                        "DELETE FROM processing_checkpoint WHERE source_name = %s AND table_name = %s",
                        (stream_name, table_name),
                    )
                    cur.execute(
                        "DELETE FROM inventory_history WHERE sku_id = %s AND warehouse_id = %s",
                        (test_sku, test_wh),
                    )
                    cur.execute(
                        "DELETE FROM inventory_source WHERE sku_id = %s AND warehouse_id = %s",
                        (test_sku, test_wh),
                    )
                conn.commit()

    def test_live_explanation_isolation_under_provider_failure(
        self, db_manager: DatabaseManager
    ) -> None:
        """Verify that if ExplanationService fails, containment is still 100% durable and unblocked."""
        stream_name = f"test_fail_stream_{uuid4().hex[:8]}"
        table_name = "inventory_source"
        test_sku = f"SKU-FAIL-{uuid4().hex[:6]}"
        test_wh = "WH-FAIL"

        checkpoint_repo = CheckpointRepository(db=db_manager)
        inventory_repo = InventorySourceRepository(db=db_manager)
        history_repo = InventoryHistoryRepository(db=db_manager)
        hold_repo = HeldChangeBatchRepository(db=db_manager)

        initial_watermark = datetime.now(timezone.utc)

        # 1. Initialize checkpoint
        checkpoint_repo.initialize_checkpoint_if_missing(
            source_name=stream_name,
            table_name=table_name,
            watermark=initial_watermark,
        )

        # 2. Seed baseline history
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

        # 3. Insert incoming source row with large swing
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
            # 4. Mock failing explanation service
            mock_explanation_service = MagicMock(spec=ExplanationService)
            mock_explanation_service.explain_held_batch.side_effect = RuntimeError("Simulated AI Failure")

            worker = IngestionWorker(
                db_manager=db_manager,
                source_name=stream_name,
                table_name=table_name,
                batch_size=10,
                explanation_service=mock_explanation_service,
            )

            # 5. Run worker cycle: must succeed with status HELD, explanation=None
            cycle_result = worker.run_once(raise_on_error=True)
            created_run_id = cycle_result.run_id

            assert cycle_result.status == "HELD"
            assert cycle_result.records_held == 1
            assert cycle_result.explanation is None

            # 6. Verify held_change_batch still exists in DB despite AI failure
            holds = hold_repo.get_recent_holds(
                limit=10, status=HoldStatus.HELD.value, source_name=stream_name
            )
            assert len(holds) >= 1
            created_hold_id = holds[0].hold_id
            assert holds[0].status == HoldStatus.HELD.value

            # 7. Verify checkpoint was preserved
            cp = checkpoint_repo.get_checkpoint(source_name=stream_name, table_name=table_name)
            assert cp is not None
            assert cp.watermark_value == initial_watermark

        finally:
            with db_manager.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM held_change_batch WHERE source_name = %s", (stream_name,))
                    if created_run_id:
                        cur.execute("DELETE FROM processing_run WHERE run_id = %s", (created_run_id,))
                    cur.execute(
                        "DELETE FROM processing_checkpoint WHERE source_name = %s AND table_name = %s",
                        (stream_name, table_name),
                    )
                    cur.execute(
                        "DELETE FROM inventory_history WHERE sku_id = %s AND warehouse_id = %s",
                        (test_sku, test_wh),
                    )
                    cur.execute(
                        "DELETE FROM inventory_source WHERE sku_id = %s AND warehouse_id = %s",
                        (test_sku, test_wh),
                    )
                conn.commit()
