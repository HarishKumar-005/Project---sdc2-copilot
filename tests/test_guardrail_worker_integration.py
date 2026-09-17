"""Integration tests for IngestionWorker with Guardrail Engine (V2.3).

Verifies end-to-end integration between worker orchestration, Polars SCD2 engine,
guardrail decision evaluation, atomic transaction handling, and checkpoint safety.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import uuid4
import pytest

import polars as pl

from src.scd2_copilot.config import Settings
from src.scd2_copilot.db.connection import DatabaseManager
from src.scd2_copilot.db.models import (
    InventoryHistoryRow,
    InventorySourceRow,
    ProcessingCheckpointRow,
    ProcessingRunRow,
    RunStatus,
)
from src.scd2_copilot.guardrail import (
    GuardrailDecisionType,
    GuardrailEngine,
    GuardrailSeverity,
    RuleId,
)
from src.scd2_copilot.worker.worker import IngestionWorker


@pytest.fixture
def mock_db() -> MagicMock:
    """Provide a mock DatabaseManager with transaction context support."""
    db = MagicMock(spec=DatabaseManager)
    db.is_configured = True
    db.redacted_url = "postgresql://mock:***@localhost:5432/mockdb"

    mock_conn = MagicMock()
    mock_tx = MagicMock()
    mock_conn.transaction.return_value.__enter__.return_value = mock_tx
    db.transaction.return_value.__enter__.return_value = mock_conn
    return db


class TestGuardrailWorkerIntegration:
    """Integration of GuardrailEngine inside IngestionWorker cycles."""

    def test_worker_commits_normal_batch(self, mock_db: MagicMock) -> None:
        """Verify worker executes SCD2, evaluates NORMAL guardrail decision, and commits."""
        settings = Settings(
            guardrail_enabled=True,
            guardrail_max_changed_records=25,
            ingestion_batch_size=10,
        )
        worker = IngestionWorker(db_manager=mock_db, settings=settings)

        t1 = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
        # Mock initial checkpoint
        worker.checkpoint_repo.get_checkpoint = MagicMock(return_value=None)
        # Mock source records (2 normal rows)
        source_records = [
            InventorySourceRow(
                sku_id="SKU-1",
                warehouse_id="WH-1",
                quantity_on_hand=100,
                reorder_level=20,
                status="ACTIVE",
                updated_at=t1,
            ),
            InventorySourceRow(
                sku_id="SKU-2",
                warehouse_id="WH-1",
                quantity_on_hand=50,
                reorder_level=10,
                status="ACTIVE",
                updated_at=t1,
            ),
        ]
        worker.inventory_repo.fetch_inventory_updated_after = MagicMock(return_value=source_records)
        # No existing history -> NEW records
        worker.history_repo.fetch_current_history_for_keys = MagicMock(return_value=[])

        # Spy on repository calls
        worker.history_repo.insert_history_rows = MagicMock()
        worker.run_repo.create_run = MagicMock()
        worker.run_repo.mark_run_committed = MagicMock()
        worker.checkpoint_repo.update_checkpoint = MagicMock()

        result = worker.run_once()

        assert result.status == "COMMITTED"
        assert result.records_seen == 2
        assert result.records_held == 0
        assert result.guardrail_decision is not None
        assert result.guardrail_decision.is_normal
        assert result.guardrail_decision.severity == GuardrailSeverity.LOW
        assert result.watermark_after == t1

        # Checkpoint advanced and history rows inserted
        assert worker.checkpoint_repo.update_checkpoint.called
        assert worker.history_repo.insert_history_rows.called
        assert worker.run_repo.mark_run_committed.called

    def test_worker_holds_suspicious_batch_without_advancing_checkpoint(
        self, mock_db: MagicMock
    ) -> None:
        """Verify worker evaluates SUSPICIOUS guardrail decision, marks run as HELD,
        does NOT write history rows, and does NOT advance checkpoint.
        """
        # Set max_changed_records low to trigger HIGH_CHANGE_VOLUME
        settings = Settings(
            guardrail_enabled=True,
            guardrail_max_changed_records=2,
            ingestion_batch_size=10,
        )
        worker = IngestionWorker(db_manager=mock_db, settings=settings)

        t_prev = datetime(2026, 9, 15, 11, 0, 0, tzinfo=timezone.utc)
        t_batch = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)

        # Checkpoint exists at t_prev
        checkpoint = ProcessingCheckpointRow(
            source_name="inventory",
            table_name="inventory_source",
            watermark_value=t_prev,
        )
        worker.checkpoint_repo.get_checkpoint = MagicMock(return_value=checkpoint)

        # 4 records changed (exceeds threshold of 2)
        source_records = [
            InventorySourceRow("SKU-1", "WH-1", 150, 20, "ACTIVE", t_batch),
            InventorySourceRow("SKU-2", "WH-1", 250, 20, "ACTIVE", t_batch),
            InventorySourceRow("SKU-3", "WH-1", 350, 20, "ACTIVE", t_batch),
            InventorySourceRow("SKU-4", "WH-1", 450, 20, "ACTIVE", t_batch),
        ]
        worker.inventory_repo.fetch_inventory_updated_after = MagicMock(return_value=source_records)

        # Target history has old values
        old_history = [
            InventoryHistoryRow("SKU-1", "WH-1", 100, 20, "ACTIVE", t_prev, True),
            InventoryHistoryRow("SKU-2", "WH-1", 200, 20, "ACTIVE", t_prev, True),
            InventoryHistoryRow("SKU-3", "WH-1", 300, 20, "ACTIVE", t_prev, True),
            InventoryHistoryRow("SKU-4", "WH-1", 400, 20, "ACTIVE", t_prev, True),
        ]
        worker.history_repo.fetch_current_history_for_keys = MagicMock(return_value=old_history)

        # Spies
        worker.history_repo.insert_history_rows = MagicMock()
        worker.history_repo.close_current_versions = MagicMock()
        worker.checkpoint_repo.update_checkpoint = MagicMock()
        worker.run_repo.create_run = MagicMock()
        worker.run_repo.mark_run_held = MagicMock()

        result = worker.run_once()

        assert result.status == "HELD"
        assert result.records_seen == 4
        assert result.records_changed == 0
        assert result.records_held == 4
        assert result.watermark_before == t_prev
        assert result.watermark_after == t_prev  # Crucial: Checkpoint strictly preserved!
        assert result.guardrail_decision is not None
        assert result.guardrail_decision.is_suspicious
        assert RuleId.HIGH_CHANGE_VOLUME.value in [
            r.rule_id for r in result.guardrail_decision.triggered_rules
        ]

        # Invariants: NO history row mutations, NO checkpoint advancement
        assert not worker.history_repo.insert_history_rows.called
        assert not worker.history_repo.close_current_versions.called
        assert not worker.checkpoint_repo.update_checkpoint.called

        # Invariant: Run was marked HELD in database
        assert worker.run_repo.mark_run_held.called
        call_args = worker.run_repo.mark_run_held.call_args[1]
        assert call_args["records_seen"] == 4
        assert call_args["records_held"] == 4

    def test_guardrail_does_not_mutate_source_records(self, mock_db: MagicMock) -> None:
        """Verify guardrail evaluation strictly preserves source record fields and types."""
        worker = IngestionWorker(db_manager=mock_db)
        t = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
        record = InventorySourceRow("SKU-1", "WH-1", 100, 20, "ACTIVE", t)
        original_dict = record.to_dict()

        worker.checkpoint_repo.get_checkpoint = MagicMock(return_value=None)
        worker.inventory_repo.fetch_inventory_updated_after = MagicMock(return_value=[record])
        worker.history_repo.fetch_current_history_for_keys = MagicMock(return_value=[])
        worker.history_repo.insert_history_rows = MagicMock()
        worker.checkpoint_repo.update_checkpoint = MagicMock()
        worker.run_repo.create_run = MagicMock()
        worker.run_repo.mark_run_committed = MagicMock()

        worker.run_once()

        assert record.to_dict() == original_dict

    def test_worker_validation_failure_rolls_back_and_preserves_checkpoint(
        self, mock_db: MagicMock
    ) -> None:
        """Verify that SCD2 invariant failure raises error, marks run FAILED, and preserves checkpoint."""
        worker = IngestionWorker(db_manager=mock_db)
        t_prev = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
        checkpoint = ProcessingCheckpointRow("inventory", "inventory_source", t_prev)
        worker.checkpoint_repo.get_checkpoint = MagicMock(return_value=checkpoint)

        # Record with duplicate/invalid date causing validation failure
        record = InventorySourceRow("SKU-1", "WH-1", 100, 20, "ACTIVE", t_prev)
        worker.inventory_repo.fetch_inventory_updated_after = MagicMock(return_value=[record])
        # Force a history row with identical effective date so effective_from >= effective_to
        history = [InventoryHistoryRow("SKU-1", "WH-1", 90, 20, "ACTIVE", t_prev, True)]
        worker.history_repo.fetch_current_history_for_keys = MagicMock(return_value=history)

        worker.history_repo.insert_history_rows = MagicMock()
        worker.checkpoint_repo.update_checkpoint = MagicMock()
        worker.run_repo.mark_run_failed = MagicMock()

        with pytest.raises(Exception):
            worker.run_once(raise_on_error=True)

        assert not worker.history_repo.insert_history_rows.called
        assert not worker.checkpoint_repo.update_checkpoint.called
