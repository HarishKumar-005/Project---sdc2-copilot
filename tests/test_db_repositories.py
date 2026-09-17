"""Unit tests for database domain models and repository implementations."""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
from unittest.mock import MagicMock
from uuid import UUID, uuid4
import pytest

from src.scd2_copilot.db.exceptions import (
    DatabaseError,
    DatabaseQueryError,
    EntityNotFoundError,
)
from src.scd2_copilot.db.models import (
    HeldChangeBatchRow,
    HoldSeverity,
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


class TestInventorySourceModel:
    """Test InventorySourceRow invariants and serialization."""

    def test_valid_inventory_source_creation(self) -> None:
        now = datetime.now(timezone.utc)
        row = InventorySourceRow(
            sku_id="SKU-100",
            warehouse_id="WH-EAST",
            quantity_on_hand=150,
            reorder_level=25,
            status="ACTIVE",
            updated_at=now,
        )
        assert row.sku_id == "SKU-100"
        assert row.warehouse_id == "WH-EAST"
        assert row.quantity_on_hand == 150
        assert row.reorder_level == 25
        assert row.status == "ACTIVE"

    def test_empty_sku_id_rejected(self) -> None:
        now = datetime.now(timezone.utc)
        with pytest.raises(ValueError, match="sku_id cannot be empty"):
            InventorySourceRow(
                sku_id="  ",
                warehouse_id="WH-EAST",
                quantity_on_hand=10,
                reorder_level=5,
                status="ACTIVE",
                updated_at=now,
            )

    def test_empty_warehouse_id_rejected(self) -> None:
        now = datetime.now(timezone.utc)
        with pytest.raises(ValueError, match="warehouse_id cannot be empty"):
            InventorySourceRow(
                sku_id="SKU-100",
                warehouse_id="",
                quantity_on_hand=10,
                reorder_level=5,
                status="ACTIVE",
                updated_at=now,
            )

    def test_negative_quantity_rejected(self) -> None:
        now = datetime.now(timezone.utc)
        with pytest.raises(ValueError, match="quantity_on_hand cannot be negative"):
            InventorySourceRow(
                sku_id="SKU-100",
                warehouse_id="WH-EAST",
                quantity_on_hand=-5,
                reorder_level=5,
                status="ACTIVE",
                updated_at=now,
            )

    def test_to_dict_and_from_dict(self) -> None:
        now = datetime.now(timezone.utc)
        row = InventorySourceRow(
            sku_id="SKU-200",
            warehouse_id="WH-WEST",
            quantity_on_hand=50,
            reorder_level=10,
            status="INACTIVE",
            updated_at=now,
        )
        d = row.to_dict()
        reconstructed = InventorySourceRow.from_dict(d)
        assert reconstructed == row


class TestInventoryHistoryModel:
    """Test InventoryHistoryRow temporal invariants and serialization."""

    def test_valid_current_history_row(self) -> None:
        row = InventoryHistoryRow(
            history_id=uuid4(),
            sku_id="SKU-1",
            warehouse_id="WH-1",
            quantity_on_hand=100,
            reorder_level=20,
            status="ACTIVE",
            effective_from=date(2026, 9, 1),
            effective_to=None,
            is_current=True,
        )
        assert row.is_current is True
        assert row.effective_to is None

    def test_valid_closed_history_row(self) -> None:
        row = InventoryHistoryRow(
            history_id=uuid4(),
            sku_id="SKU-1",
            warehouse_id="WH-1",
            quantity_on_hand=100,
            reorder_level=20,
            status="ACTIVE",
            effective_from=date(2026, 9, 1),
            effective_to=date(2026, 9, 5),
            is_current=False,
        )
        assert row.is_current is False
        assert row.effective_to == date(2026, 9, 5)

    def test_current_row_with_effective_to_rejected(self) -> None:
        with pytest.raises(ValueError, match="Active.*must have effective_to = None"):
            InventoryHistoryRow(
                history_id=uuid4(),
                sku_id="SKU-1",
                warehouse_id="WH-1",
                quantity_on_hand=100,
                reorder_level=20,
                status="ACTIVE",
                effective_from=date(2026, 9, 1),
                effective_to=date(2026, 9, 5),
                is_current=True,
            )

    def test_closed_row_with_null_effective_to_rejected(self) -> None:
        with pytest.raises(ValueError, match="Closed.*must have a non-null effective_to"):
            InventoryHistoryRow(
                history_id=uuid4(),
                sku_id="SKU-1",
                warehouse_id="WH-1",
                quantity_on_hand=100,
                reorder_level=20,
                status="ACTIVE",
                effective_from=date(2026, 9, 1),
                effective_to=None,
                is_current=False,
            )

    def test_reversed_dates_rejected(self) -> None:
        with pytest.raises(ValueError, match="effective_from must be before or equal to effective_to"):
            InventoryHistoryRow(
                history_id=uuid4(),
                sku_id="SKU-1",
                warehouse_id="WH-1",
                quantity_on_hand=100,
                reorder_level=20,
                status="ACTIVE",
                effective_from=date(2026, 9, 10),
                effective_to=date(2026, 9, 5),
                is_current=False,
            )

    def test_history_to_dict_and_from_dict(self) -> None:
        h_id = uuid4()
        now = datetime.now(timezone.utc)
        row = InventoryHistoryRow(
            history_id=h_id,
            sku_id="SKU-999",
            warehouse_id="WH-NORTH",
            quantity_on_hand=42,
            reorder_level=12,
            status="ACTIVE",
            effective_from=date(2026, 9, 1),
            effective_to=None,
            is_current=True,
            created_at=now,
        )
        d = row.to_dict()
        reconstructed = InventoryHistoryRow.from_dict(d)
        assert reconstructed.history_id == h_id
        assert reconstructed.sku_id == "SKU-999"
        assert reconstructed.is_current is True


class TestProcessingRunModel:
    """Test ProcessingRunRow and RunStatus enums."""

    def test_run_status_enum_values(self) -> None:
        assert RunStatus.RECEIVED.value == "RECEIVED"
        assert RunStatus.PROCESSING.value == "PROCESSING"
        assert RunStatus.COMMITTED.value == "COMMITTED"
        assert RunStatus.FAILED.value == "FAILED"
        assert RunStatus.HELD.value == "HELD"

    def test_processing_run_serialization(self) -> None:
        r_id = uuid4()
        now = datetime.now(timezone.utc)
        run = ProcessingRunRow(
            run_id=r_id,
            source_name="inventory_stream",
            status=RunStatus.COMMITTED.value,
            started_at=now,
            completed_at=now,
            records_seen=100,
            records_changed=15,
            records_held=2,
            error_message=None,
            created_at=now,
        )
        d = run.to_dict()
        reconstructed = ProcessingRunRow.from_dict(d)
        assert reconstructed.run_id == r_id
        assert reconstructed.records_seen == 100
        assert reconstructed.status == RunStatus.COMMITTED.value


class TestCheckpointAndHoldModels:
    """Test Checkpoint and HeldChangeBatch data classes."""

    def test_checkpoint_model(self) -> None:
        r_id = uuid4()
        now = datetime.now(timezone.utc)
        cp = ProcessingCheckpointRow(
            source_name="inventory_realtime",
            table_name="inventory_source",
            watermark_value=now,
            last_successful_run_id=r_id,
            updated_at=now,
        )
        d = cp.to_dict()
        reconstructed = ProcessingCheckpointRow.from_dict(d)
        assert reconstructed.source_name == "inventory_realtime"
        assert reconstructed.last_successful_run_id == r_id

    def test_held_batch_model_with_json_evidence(self) -> None:
        r_id = uuid4()
        h_id = uuid4()
        now = datetime.now(timezone.utc)
        hold = HeldChangeBatchRow(
            hold_id=h_id,
            run_id=r_id,
            source_name="inventory_stream",
            severity=HoldSeverity.HIGH.value,
            reason="Massive price shift detected",
            records_affected=25,
            evidence={"anomaly_score": 0.95, "flags": ["price_spike"]},
            status=HoldStatus.HELD.value,
            created_at=now,
        )
        d = hold.to_dict()
        reconstructed = HeldChangeBatchRow.from_dict(d)
        assert reconstructed.hold_id == h_id
        assert reconstructed.severity == "HIGH"
        assert reconstructed.evidence["anomaly_score"] == 0.95

    def test_held_batch_model_with_string_evidence(self) -> None:
        raw_dict = {
            "hold_id": str(uuid4()),
            "run_id": str(uuid4()),
            "source_name": "inventory_stream",
            "severity": "CRITICAL",
            "reason": "Contract violation",
            "records_affected": 5,
            "evidence": json.dumps({"schema_error": "missing column"}),
            "status": "HELD",
        }
        reconstructed = HeldChangeBatchRow.from_dict(raw_dict)
        assert reconstructed.evidence == {"schema_error": "missing column"}


class TestInventorySourceRepository:
    """Test InventorySourceRepository query construction and error wrapping."""

    def test_fetch_inventory_for_keys_empty_returns_early(self) -> None:
        mock_conn = MagicMock()
        repo = InventorySourceRepository()
        result = repo.fetch_inventory_for_keys([], conn=mock_conn)
        assert result == []
        assert not mock_conn.cursor.called

    def test_fetch_all_inventory_executes_query(self) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        now = datetime.now(timezone.utc)
        mock_cur.fetchall.return_value = [
            {
                "sku_id": "SKU-1",
                "warehouse_id": "WH-1",
                "quantity_on_hand": 10,
                "reorder_level": 5,
                "status": "ACTIVE",
                "updated_at": now,
            }
        ]

        repo = InventorySourceRepository()
        items = repo.fetch_all_inventory(conn=mock_conn)
        assert len(items) == 1
        assert items[0].sku_id == "SKU-1"
        assert mock_cur.execute.called

    def test_fetch_inventory_updated_after_has_deterministic_ordering(self) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_cur.fetchall.return_value = []

        repo = InventorySourceRepository()
        now = datetime.now(timezone.utc)
        repo.fetch_inventory_updated_after(watermark=now, conn=mock_conn)

        query = mock_cur.execute.call_args[0][0]
        # Invariant: Must enforce deterministic ordering for stream replay
        assert "ORDER BY updated_at ASC, sku_id ASC, warehouse_id ASC" in query


class TestProcessingRunRepository:
    """Test ProcessingRunRepository lifecycle methods."""

    def test_create_run_and_fetch(self) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        r_id = uuid4()
        now = datetime.now(timezone.utc)
        mock_cur.fetchone.return_value = {
            "run_id": r_id,
            "source_name": "inventory_stream",
            "status": "STARTED",
            "started_at": now,
            "completed_at": None,
            "records_seen": 0,
            "records_changed": 0,
            "records_held": 0,
            "error_message": None,
            "created_at": now,
        }

        repo = ProcessingRunRepository()
        run = repo.create_run(source_name="inventory_stream", run_id=r_id, conn=mock_conn)
        assert run.run_id == r_id
        assert run.status == "STARTED"

    def test_mark_run_completed(self) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        r_id = uuid4()
        now = datetime.now(timezone.utc)
        mock_cur.fetchone.return_value = {
            "run_id": r_id,
            "source_name": "inventory_stream",
            "status": "COMPLETED",
            "started_at": now,
            "completed_at": now,
            "records_seen": 50,
            "records_changed": 10,
            "records_held": 0,
            "error_message": None,
            "created_at": now,
        }

        repo = ProcessingRunRepository()
        run = repo.mark_run_completed(run_id=r_id, records_seen=50, records_changed=10, records_held=0, conn=mock_conn)
        assert run.status == "COMPLETED"
        assert run.records_seen == 50

    def test_missing_run_raises_entity_not_found(self) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_cur.fetchone.return_value = None

        repo = ProcessingRunRepository()
        with pytest.raises(EntityNotFoundError):
            repo.mark_run_started(run_id=uuid4(), conn=mock_conn)


class TestHeldChangeBatchRepository:
    """Test HeldChangeBatchRepository operations."""

    def test_create_hold(self) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        h_id = uuid4()
        r_id = uuid4()
        now = datetime.now(timezone.utc)
        mock_cur.fetchone.return_value = {
            "hold_id": h_id,
            "run_id": r_id,
            "source_name": "stream_1",
            "severity": "CRITICAL",
            "reason": "Breach of contract",
            "records_affected": 10,
            "evidence": {"detail": "violation"},
            "status": "HELD",
            "created_at": now,
            "resolved_at": None,
        }

        repo = HeldChangeBatchRepository()
        hold = repo.create_hold(
            run_id=r_id,
            source_name="stream_1",
            severity="CRITICAL",
            reason="Breach of contract",
            records_affected=10,
            evidence={"detail": "violation"},
            conn=mock_conn,
        )
        assert hold.hold_id == h_id
        assert hold.status == "HELD"

    def test_approve_hold(self) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        h_id = uuid4()
        r_id = uuid4()
        now = datetime.now(timezone.utc)
        mock_cur.fetchone.return_value = {
            "hold_id": h_id,
            "run_id": r_id,
            "source_name": "stream_1",
            "severity": "HIGH",
            "reason": "Suspicious update",
            "records_affected": 2,
            "evidence": {},
            "status": "APPROVED",
            "created_at": now,
            "resolved_at": now,
        }

        repo = HeldChangeBatchRepository()
        hold = repo.approve_hold(hold_id=h_id, conn=mock_conn)
        assert hold.status == "APPROVED"
        assert hold.resolved_at is not None
