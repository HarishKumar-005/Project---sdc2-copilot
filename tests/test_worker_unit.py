"""Unit tests for the incremental ingestion worker (V2.2)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4
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
from src.scd2_copilot.worker.batch import MicroBatch, history_rows_to_target_df
from src.scd2_copilot.worker.exceptions import (
    WorkerError,
    WorkerShutdownException,
    WorkerValidationFailureError,
)
from src.scd2_copilot.worker.worker import IngestionWorker, WorkerCycleResult


@pytest.fixture
def mock_db() -> MagicMock:
    """Provide a mocked DatabaseManager with atomic transaction support."""
    db = MagicMock(spec=DatabaseManager)
    db.is_configured = True
    db.redacted_url = "postgresql://mock:***@localhost:5432/mockdb"

    # Transaction context manager yields a mock connection
    mock_conn = MagicMock()
    mock_tx = MagicMock()
    mock_conn.transaction.return_value.__enter__.return_value = mock_tx
    db.transaction.return_value.__enter__.return_value = mock_conn
    return db


@pytest.fixture
def sample_source_records() -> list[InventorySourceRow]:
    """Provide sample operational source rows."""
    t1 = datetime(2026, 9, 16, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 16, 10, 5, 0, tzinfo=timezone.utc)
    return [
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
            updated_at=t2,
        ),
    ]


class TestMicroBatchAndAdapters:
    """Test MicroBatch dataclass and DataFrame conversion helpers."""

    def test_micro_batch_properties(self, sample_source_records: list[InventorySourceRow]) -> None:
        batch = MicroBatch(
            source_records=sample_source_records,
            watermark_start=datetime(2026, 9, 16, 9, 0, 0, tzinfo=timezone.utc),
            watermark_end=datetime(2026, 9, 16, 10, 5, 0, tzinfo=timezone.utc),
        )
        assert batch.size == 2
        assert not batch.is_empty
        assert batch.business_keys == [("SKU-1", "WH-1"), ("SKU-2", "WH-1")]
        assert batch.max_updated_at == datetime(2026, 9, 16, 10, 5, 0, tzinfo=timezone.utc)

    def test_to_source_df_schema(self, sample_source_records: list[InventorySourceRow]) -> None:
        batch = MicroBatch(source_records=sample_source_records)
        df = batch.to_source_df()
        assert isinstance(df, pl.DataFrame)
        assert df.height == 2
        assert set(df.columns) == {"sku_id", "warehouse_id", "quantity_on_hand", "reorder_level", "status"}
        assert df.schema["quantity_on_hand"] == pl.Int64

    def test_history_rows_to_target_df_empty(self) -> None:
        df = history_rows_to_target_df([])
        assert isinstance(df, pl.DataFrame)
        assert df.is_empty()
        assert "effective_from" in df.columns
        assert "is_current" in df.columns

    def test_history_rows_to_target_df_populated(self) -> None:
        now = datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc)
        row = InventoryHistoryRow(
            sku_id="SKU-1",
            warehouse_id="WH-1",
            quantity_on_hand=80,
            reorder_level=20,
            status="ACTIVE",
            effective_from=now,
            effective_to=None,
            is_current=True,
        )
        df = history_rows_to_target_df([row])
        assert df.height == 1
        assert df["sku_id"][0] == "SKU-1"
        assert df["effective_from"][0] == date(2026, 9, 1)
        assert df["is_current"][0] is True


class TestWorkerLifecycleAndWatermark:
    """Test worker startup, watermark reads, empty polls, and processing."""

    def test_worker_starts_with_existing_checkpoint(self, mock_db: MagicMock) -> None:
        worker = IngestionWorker(db_manager=mock_db, source_name="inventory", table_name="inventory_source")
        watermark_ts = datetime(2026, 9, 16, 8, 0, 0, tzinfo=timezone.utc)

        worker.checkpoint_repo.get_checkpoint = MagicMock(
            return_value=ProcessingCheckpointRow(
                source_name="inventory",
                table_name="inventory_source",
                watermark_value=watermark_ts,
            )
        )
        worker.inventory_repo.fetch_inventory_updated_after = MagicMock(return_value=[])

        result = worker.run_once()
        assert result.status == "EMPTY"
        assert result.watermark_before == watermark_ts
        assert result.watermark_after == watermark_ts
        worker.inventory_repo.fetch_inventory_updated_after.assert_called_once_with(
            watermark=watermark_ts,
            limit=worker.batch_size,
        )

    def test_worker_starts_with_null_checkpoint_initial_load(self, mock_db: MagicMock) -> None:
        worker = IngestionWorker(db_manager=mock_db, source_name="inventory")
        worker.checkpoint_repo.get_checkpoint = MagicMock(return_value=None)
        worker.inventory_repo.fetch_inventory_updated_after = MagicMock(return_value=[])

        result = worker.run_once()
        assert result.status == "EMPTY"
        assert result.watermark_before is None
        worker.inventory_repo.fetch_inventory_updated_after.assert_called_once_with(
            watermark=None,
            limit=worker.batch_size,
        )

    def test_empty_poll_does_not_advance_checkpoint_or_write_history(self, mock_db: MagicMock) -> None:
        worker = IngestionWorker(db_manager=mock_db)
        worker.checkpoint_repo.get_checkpoint = MagicMock(return_value=None)
        worker.inventory_repo.fetch_inventory_updated_after = MagicMock(return_value=[])
        worker.checkpoint_repo.update_checkpoint = MagicMock()
        worker.history_repo.insert_history_rows = MagicMock()

        result = worker.run_once()
        assert result.status == "EMPTY"
        assert not worker.checkpoint_repo.update_checkpoint.called
        assert not worker.history_repo.insert_history_rows.called

    def test_respects_batch_size_limit(self, mock_db: MagicMock) -> None:
        worker = IngestionWorker(db_manager=mock_db, batch_size=25)
        worker.checkpoint_repo.get_checkpoint = MagicMock(return_value=None)
        worker.inventory_repo.fetch_inventory_updated_after = MagicMock(return_value=[])

        worker.run_once()
        worker.inventory_repo.fetch_inventory_updated_after.assert_called_once_with(
            watermark=None,
            limit=25,
        )


class TestSuccessfulBatchExecution:
    """Test full cycle execution: classification, history update, and checkpoint advancement."""

    def test_successful_batch_new_records(
        self,
        mock_db: MagicMock,
        sample_source_records: list[InventorySourceRow],
    ) -> None:
        worker = IngestionWorker(db_manager=mock_db, source_name="inventory")
        worker.checkpoint_repo.get_checkpoint = MagicMock(return_value=None)
        worker.inventory_repo.fetch_inventory_updated_after = MagicMock(
            return_value=sample_source_records
        )
        # Target history is empty -> both records are NEW
        worker.history_repo.fetch_current_history_for_keys = MagicMock(return_value=[])
        worker.history_repo.insert_history_rows = MagicMock(return_value=2)
        worker.history_repo.close_current_versions = MagicMock(return_value=0)
        worker.run_repo.create_run = MagicMock()
        worker.run_repo.mark_run_committed = MagicMock()
        worker.checkpoint_repo.update_checkpoint = MagicMock()

        result = worker.run_once()

        assert result.status == "COMMITTED"
        assert result.records_seen == 2
        assert result.records_changed == 0
        assert result.watermark_after == sample_source_records[-1].updated_at

        # Verify transaction boundary was invoked
        assert mock_db.transaction.called

        # Verify run marked committed
        worker.run_repo.mark_run_committed.assert_called_once()
        # Verify checkpoint updated with the max updated_at of the batch
        worker.checkpoint_repo.update_checkpoint.assert_called_once()
        args, kwargs = worker.checkpoint_repo.update_checkpoint.call_args
        assert kwargs["watermark"] == sample_source_records[-1].updated_at
        assert kwargs["run_id"] == result.run_id

    def test_successful_batch_changed_records(
        self,
        mock_db: MagicMock,
        sample_source_records: list[InventorySourceRow],
    ) -> None:
        worker = IngestionWorker(db_manager=mock_db, source_name="inventory")
        worker.checkpoint_repo.get_checkpoint = MagicMock(return_value=None)
        worker.inventory_repo.fetch_inventory_updated_after = MagicMock(
            return_value=sample_source_records
        )

        # Existing target has prior version with different quantity -> CHANGED!
        prior_row = InventoryHistoryRow(
            sku_id="SKU-1",
            warehouse_id="WH-1",
            quantity_on_hand=50,  # was 50, now 100 in source
            reorder_level=20,
            status="ACTIVE",
            effective_from=datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc),
            effective_to=None,
            is_current=True,
        )
        worker.history_repo.fetch_current_history_for_keys = MagicMock(return_value=[prior_row])
        worker.history_repo.close_current_versions = MagicMock(return_value=1)
        worker.history_repo.insert_history_rows = MagicMock(return_value=2)
        worker.run_repo.create_run = MagicMock()
        worker.run_repo.mark_run_committed = MagicMock()
        worker.checkpoint_repo.update_checkpoint = MagicMock()

        result = worker.run_once()
        assert result.status == "COMMITTED"
        assert result.records_seen == 2
        assert result.records_changed == 1  # 1 changed, 1 new

        # Verify close_current_versions was called for SKU-1
        worker.history_repo.close_current_versions.assert_called_once()
        close_keys = worker.history_repo.close_current_versions.call_args[1]["keys"]
        assert ("SKU-1", "WH-1") in close_keys


class TestFailureSafetyAndRollback:
    """Verify that failure guarantees checkpoint remains strictly unadvanced."""

    def test_processing_failure_does_not_advance_checkpoint(
        self,
        mock_db: MagicMock,
        sample_source_records: list[InventorySourceRow],
    ) -> None:
        worker = IngestionWorker(db_manager=mock_db, source_name="inventory")
        t_watermark = datetime(2026, 9, 16, 9, 0, 0, tzinfo=timezone.utc)
        worker.checkpoint_repo.get_checkpoint = MagicMock(
            return_value=ProcessingCheckpointRow(
                source_name="inventory",
                table_name="inventory_source",
                watermark_value=t_watermark,
            )
        )
        worker.inventory_repo.fetch_inventory_updated_after = MagicMock(
            return_value=sample_source_records
        )
        worker.history_repo.fetch_current_history_for_keys = MagicMock(return_value=[])
        worker.checkpoint_repo.update_checkpoint = MagicMock()

        # Simulate engine failure
        with patch("src.scd2_copilot.worker.worker.detect_changes", side_effect=ValueError("Simulated corrupt schema")):
            with pytest.raises(WorkerError, match="Simulated corrupt schema"):
                worker.run_once(raise_on_error=True)

        # Invariant: checkpoint must NOT advance
        assert not worker.checkpoint_repo.update_checkpoint.called

    def test_validation_failure_does_not_advance_checkpoint(
        self,
        mock_db: MagicMock,
        sample_source_records: list[InventorySourceRow],
    ) -> None:
        worker = IngestionWorker(db_manager=mock_db, source_name="inventory")
        t_watermark = datetime(2026, 9, 16, 9, 0, 0, tzinfo=timezone.utc)
        worker.checkpoint_repo.get_checkpoint = MagicMock(
            return_value=ProcessingCheckpointRow(
                source_name="inventory",
                table_name="inventory_source",
                watermark_value=t_watermark,
            )
        )
        worker.inventory_repo.fetch_inventory_updated_after = MagicMock(
            return_value=sample_source_records
        )
        worker.history_repo.fetch_current_history_for_keys = MagicMock(return_value=[])
        worker.checkpoint_repo.update_checkpoint = MagicMock()

        # Mock invalid validation report
        mock_val_report = MagicMock()
        mock_val_report.passed = False
        mock_rule = MagicMock()
        mock_rule.status.value = "fail"
        mock_rule.message = "Overlapping intervals detected"
        mock_val_report.rules = [mock_rule]

        with patch("src.scd2_copilot.worker.worker.validate_scd2", return_value=mock_val_report):
            with pytest.raises(WorkerError, match="SCD2 invariant validation failed"):
                worker.run_once(raise_on_error=True)

        # Invariant: checkpoint must NOT advance
        assert not worker.checkpoint_repo.update_checkpoint.called

    def test_persistence_failure_triggers_rollback_and_preserves_watermark(
        self,
        mock_db: MagicMock,
        sample_source_records: list[InventorySourceRow],
    ) -> None:
        worker = IngestionWorker(db_manager=mock_db, source_name="inventory")
        worker.checkpoint_repo.get_checkpoint = MagicMock(return_value=None)
        worker.inventory_repo.fetch_inventory_updated_after = MagicMock(
            return_value=sample_source_records
        )
        worker.history_repo.fetch_current_history_for_keys = MagicMock(return_value=[])
        worker.run_repo.create_run = MagicMock()
        worker.checkpoint_repo.update_checkpoint = MagicMock()

        # Simulate database constraint failure on insert_history_rows
        worker.history_repo.insert_history_rows = MagicMock(
            side_effect=RuntimeError("Database constraint error: duplicate active key")
        )

        with pytest.raises(WorkerError, match="duplicate active key"):
            worker.run_once(raise_on_error=True)

        # Invariant: checkpoint must NOT be updated
        assert not worker.checkpoint_repo.update_checkpoint.called


class TestContinuousWorkerControls:
    """Test continuous loop execution, retry behavior, and graceful shutdown."""

    def test_run_forever_stops_at_max_cycles(self, mock_db: MagicMock) -> None:
        worker = IngestionWorker(db_manager=mock_db, poll_interval_seconds=0.01)
        worker.checkpoint_repo.get_checkpoint = MagicMock(return_value=None)
        worker.inventory_repo.fetch_inventory_updated_after = MagicMock(return_value=[])

        results = worker.run_forever(max_cycles=3)
        assert len(results) == 3
        for r in results:
            assert r.status == "EMPTY"

    def test_graceful_shutdown_stops_loop(self, mock_db: MagicMock) -> None:
        worker = IngestionWorker(db_manager=mock_db, poll_interval_seconds=10.0)
        worker.checkpoint_repo.get_checkpoint = MagicMock(return_value=None)
        worker.inventory_repo.fetch_inventory_updated_after = MagicMock(return_value=[])

        # Signal stop immediately
        worker.stop()
        assert worker.is_stopped
        results = worker.run_forever(max_cycles=10)
        # Should exit without executing full max_cycles
        assert len(results) == 0

    def test_bounded_retries_on_transient_failure(self, mock_db: MagicMock) -> None:
        worker = IngestionWorker(
            db_manager=mock_db,
            poll_interval_seconds=0.01,
            retry_count=2,
            retry_backoff_seconds=0.01,
        )
        worker.checkpoint_repo.get_checkpoint = MagicMock(
            side_effect=ConnectionError("Temporary pool timeout")
        )

        with pytest.raises(WorkerError, match="Worker aborted after 2 consecutive failures"):
            worker.run_forever()


class TestWatermarkCorrectnessAndSanitization:
    """Test identical timestamps, retryability, monotonic advancement, and error sanitization."""

    def test_identical_timestamp_ordering(self) -> None:
        same_ts = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
        r1 = InventorySourceRow(sku_id="SKU-B", warehouse_id="WH-2", quantity_on_hand=10, reorder_level=5, status="ACTIVE", updated_at=same_ts)
        r2 = InventorySourceRow(sku_id="SKU-A", warehouse_id="WH-1", quantity_on_hand=20, reorder_level=5, status="ACTIVE", updated_at=same_ts)
        r3 = InventorySourceRow(sku_id="SKU-B", warehouse_id="WH-1", quantity_on_hand=30, reorder_level=5, status="ACTIVE", updated_at=same_ts)

        # MicroBatch business_keys should retain deterministic order
        batch = MicroBatch(source_records=[r2, r3, r1])
        assert batch.max_updated_at == same_ts
        assert batch.business_keys == [("SKU-A", "WH-1"), ("SKU-B", "WH-1"), ("SKU-B", "WH-2")]

    def test_failed_batch_is_retryable(self, mock_db: MagicMock, sample_source_records: list[InventorySourceRow]) -> None:
        worker = IngestionWorker(db_manager=mock_db, source_name="inventory")
        t_watermark = datetime(2026, 9, 16, 9, 0, 0, tzinfo=timezone.utc)
        worker.checkpoint_repo.get_checkpoint = MagicMock(
            return_value=ProcessingCheckpointRow(source_name="inventory", table_name="inventory_source", watermark_value=t_watermark)
        )
        worker.inventory_repo.fetch_inventory_updated_after = MagicMock(return_value=sample_source_records)
        worker.history_repo.fetch_current_history_for_keys = MagicMock(return_value=[])

        # Attempt 1 fails during compute
        with patch("src.scd2_copilot.worker.worker.detect_changes", side_effect=ValueError("Simulated intermittent crash")):
            result1 = worker.run_once(raise_on_error=False)
            assert result1.status == "FAILED"

        # Attempt 2 succeeds
        worker.history_repo.insert_history_rows = MagicMock(return_value=2)
        worker.checkpoint_repo.update_checkpoint = MagicMock()
        worker.run_repo.create_run = MagicMock()
        worker.run_repo.mark_run_committed = MagicMock()

        result2 = worker.run_once(raise_on_error=True)
        assert result2.status == "COMMITTED"
        assert result2.watermark_after == sample_source_records[-1].updated_at

    def test_sanitized_error_logging_on_failure(self, mock_db: MagicMock, sample_source_records: list[InventorySourceRow]) -> None:
        settings = Settings(database_url="postgresql://admin:super_secret_pw123@aws-0-pooler.supabase.com:5432/db")
        worker = IngestionWorker(db_manager=mock_db, settings=settings)
        worker.checkpoint_repo.get_checkpoint = MagicMock(return_value=None)
        worker.inventory_repo.fetch_inventory_updated_after = MagicMock(return_value=sample_source_records)
        worker.history_repo.fetch_current_history_for_keys = MagicMock(return_value=[])

        # Simulate exception containing connection string with password
        with patch(
            "src.scd2_copilot.worker.worker.detect_changes",
            side_effect=RuntimeError("Connection to postgresql://admin:super_secret_pw123@aws-0-pooler.supabase.com:5432/db failed"),
        ):
            with pytest.raises(WorkerError) as exc_info:
                worker.run_once(raise_on_error=True)

            assert "super_secret_pw123" not in str(exc_info.value)
            assert ":***@" in str(exc_info.value)

