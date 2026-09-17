"""Unit tests for suspicious batch containment, deduplication, and recovery (V2.4)."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4
import pytest

import polars as pl

from src.scd2_copilot.config import Settings
from src.scd2_copilot.containment import (
    ContainmentError,
    ContainmentService,
    DuplicateHoldError,
    HoldNotFoundError,
    HoldReplayError,
    HoldResolutionResult,
    InvalidHoldTransitionError,
    compute_batch_fingerprint,
)
from src.scd2_copilot.db.connection import DatabaseManager
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
from src.scd2_copilot.guardrail.engine import GuardrailEngine
from src.scd2_copilot.guardrail.models import (
    GuardrailDecision,
    GuardrailDecisionType,
    GuardrailEvidence,
    GuardrailSeverity,
    TriggeredRule,
)
from src.scd2_copilot.worker.batch import MicroBatch


@pytest.fixture
def mock_db() -> MagicMock:
    """Provide a mocked DatabaseManager with atomic transaction context manager."""
    db = MagicMock(spec=DatabaseManager)
    db.is_configured = True
    db.redacted_url = "postgresql://mock:***@localhost:5432/mockdb"

    mock_conn = MagicMock()
    mock_tx = MagicMock()
    mock_conn.transaction.return_value.__enter__.return_value = mock_tx
    db.transaction.return_value.__enter__.return_value = mock_conn
    return db


@pytest.fixture
def sample_source_records() -> list[InventorySourceRow]:
    """Sample operational rows."""
    t1 = datetime(2026, 9, 16, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 16, 10, 5, 0, tzinfo=timezone.utc)
    return [
        InventorySourceRow(
            sku_id="SKU-101",
            warehouse_id="WH-1",
            quantity_on_hand=50,
            reorder_level=20,
            status="ACTIVE",
            updated_at=t1,
        ),
        InventorySourceRow(
            sku_id="SKU-102",
            warehouse_id="WH-2",
            quantity_on_hand=0,
            reorder_level=10,
            status="INACTIVE",
            updated_at=t2,
        ),
    ]


@pytest.fixture
def sample_suspicious_guardrail_decision() -> GuardrailDecision:
    """Sample guardrail decision flagging a batch as SUSPICIOUS."""
    rule = TriggeredRule(
        rule_id="LARGE_QUANTITY_SWING",
        rule_name="Large Quantity Swing",
        description="Single SKU quantity swing >= 50.0%",
        threshold=0.5,
        observed_value=0.9,
        severity=GuardrailSeverity.HIGH,
        message="SKU SKU-101 swung by 90.0%",
    )
    evidence = GuardrailEvidence(
        evaluated_records=2,
        changed_records=2,
        new_records=0,
        unchanged_records=0,
        affected_population_ratio=1.0,
        warehouses_affected=2,
        skus_affected=2,
        max_quantity_relative_change=0.9,
        max_quantity_absolute_change=90,
        total_quantity_absolute_change=90,
        status_deactivations_count=1,
        event_window_seconds=300.0,
        velocity_changes_per_second=0.0067,
        validation_passed=True,
    )
    return GuardrailDecision(
        decision=GuardrailDecisionType.SUSPICIOUS,
        severity=GuardrailSeverity.HIGH,
        triggered_rules=[rule],
        reasons=["SKU SKU-101 swung by 90.0%"],
        evidence=evidence,
    )


@pytest.fixture
def sample_normal_guardrail_decision() -> GuardrailDecision:
    """Sample guardrail decision classifying a batch as NORMAL."""
    evidence = GuardrailEvidence(
        evaluated_records=2,
        changed_records=1,
        new_records=0,
        unchanged_records=1,
        affected_population_ratio=0.1,
        warehouses_affected=1,
        skus_affected=1,
        max_quantity_relative_change=0.05,
        max_quantity_absolute_change=5,
        total_quantity_absolute_change=5,
        status_deactivations_count=0,
        event_window_seconds=300.0,
        velocity_changes_per_second=0.0033,
        validation_passed=True,
    )
    return GuardrailDecision(
        decision=GuardrailDecisionType.NORMAL,
        severity=GuardrailSeverity.LOW,
        triggered_rules=[],
        reasons=[],
        evidence=evidence,
    )


class TestBatchFingerprinting:
    """Test deterministic batch fingerprint generation and deduplication hashing."""

    def test_fingerprint_deterministic_same_inputs(
        self, sample_source_records: list[InventorySourceRow]
    ) -> None:
        batch1 = MicroBatch(
            source_records=sample_source_records,
            watermark_start=datetime(2026, 9, 16, 9, 50, 0, tzinfo=timezone.utc),
            watermark_end=datetime(2026, 9, 16, 10, 5, 0, tzinfo=timezone.utc),
        )
        batch2 = MicroBatch(
            source_records=list(sample_source_records),
            watermark_start=datetime(2026, 9, 16, 9, 50, 0, tzinfo=timezone.utc),
            watermark_end=datetime(2026, 9, 16, 10, 5, 0, tzinfo=timezone.utc),
        )

        fp1 = compute_batch_fingerprint(batch1, "test_stream")
        fp2 = compute_batch_fingerprint(batch2, "test_stream")
        assert fp1 == fp2
        assert len(fp1) == 64  # SHA-256

    def test_fingerprint_order_invariant(
        self, sample_source_records: list[InventorySourceRow]
    ) -> None:
        # Records in reverse order should yield the same fingerprint
        batch_normal = MicroBatch(
            source_records=sample_source_records,
            watermark_start=datetime(2026, 9, 16, 9, 50, 0, tzinfo=timezone.utc),
            watermark_end=datetime(2026, 9, 16, 10, 5, 0, tzinfo=timezone.utc),
        )
        batch_reversed = MicroBatch(
            source_records=list(reversed(sample_source_records)),
            watermark_start=datetime(2026, 9, 16, 9, 50, 0, tzinfo=timezone.utc),
            watermark_end=datetime(2026, 9, 16, 10, 5, 0, tzinfo=timezone.utc),
        )

        fp_normal = compute_batch_fingerprint(batch_normal, "test_stream")
        fp_reversed = compute_batch_fingerprint(batch_reversed, "test_stream")
        assert fp_normal == fp_reversed

    def test_fingerprint_sensitive_to_different_data(
        self, sample_source_records: list[InventorySourceRow]
    ) -> None:
        batch1 = MicroBatch(source_records=sample_source_records)
        diff_record = InventorySourceRow(
            sku_id="SKU-999",
            warehouse_id="WH-1",
            quantity_on_hand=100,
            reorder_level=10,
            status="ACTIVE",
            updated_at=datetime(2026, 9, 16, 10, 10, 0, tzinfo=timezone.utc),
        )
        batch2 = MicroBatch(source_records=[diff_record])

        fp1 = compute_batch_fingerprint(batch1, "test_stream")
        fp2 = compute_batch_fingerprint(batch2, "test_stream")
        assert fp1 != fp2


class TestContainSuspiciousBatch:
    """Test quarantine containment, duplicate hold suppression, and frozen snapshots."""

    def test_contain_creates_held_run_and_batch(
        self,
        mock_db: MagicMock,
        sample_source_records: list[InventorySourceRow],
        sample_suspicious_guardrail_decision: GuardrailDecision,
    ) -> None:
        mock_hold_repo = MagicMock(spec=HeldChangeBatchRepository)
        mock_run_repo = MagicMock(spec=ProcessingRunRepository)
        mock_hold_repo.get_recent_holds.return_value = []

        expected_hold_id = uuid4()
        run_id = uuid4()
        mock_hold_repo.create_hold.return_value = HeldChangeBatchRow(
            hold_id=expected_hold_id,
            run_id=run_id,
            source_name="test_source",
            severity="HIGH",
            reason="SKU SKU-101 swung by 90.0%",
            records_affected=2,
            evidence={"batch_records": [r.to_dict() for r in sample_source_records]},
            status="HELD",
        )
        mock_run_repo.get_run.return_value = None

        service = ContainmentService(
            db_manager=mock_db,
            hold_repo=mock_hold_repo,
            run_repo=mock_run_repo,
            source_name="test_source",
        )

        batch = MicroBatch(source_records=sample_source_records)
        hold_row, is_new = service.contain_suspicious_batch(
            batch=batch,
            guardrail_decision=sample_suspicious_guardrail_decision,
            run_id=run_id,
        )

        assert is_new is True
        assert hold_row.hold_id == expected_hold_id
        assert hold_row.status == "HELD"

        # Verify hold_repo.create_hold was called with correct arguments
        mock_hold_repo.create_hold.assert_called_once()
        call_kwargs = mock_hold_repo.create_hold.call_args[1]
        assert call_kwargs["severity"] == "HIGH"
        assert call_kwargs["status"] == "HELD"
        assert call_kwargs["records_affected"] == 2
        assert "batch_fingerprint" in call_kwargs["evidence"]
        assert len(call_kwargs["evidence"]["batch_records"]) == 2

    def test_contain_suppresses_duplicate_active_holds(
        self,
        mock_db: MagicMock,
        sample_source_records: list[InventorySourceRow],
        sample_suspicious_guardrail_decision: GuardrailDecision,
    ) -> None:
        batch = MicroBatch(source_records=sample_source_records)
        fingerprint = compute_batch_fingerprint(batch, "test_source")

        existing_hold_id = uuid4()
        existing_hold = HeldChangeBatchRow(
            hold_id=existing_hold_id,
            run_id=uuid4(),
            source_name="test_source",
            severity="HIGH",
            reason="Existing reason",
            records_affected=2,
            evidence={"batch_fingerprint": fingerprint, "batch_records": []},
            status="HELD",
        )

        mock_hold_repo = MagicMock(spec=HeldChangeBatchRepository)
        mock_run_repo = MagicMock(spec=ProcessingRunRepository)
        # Return existing active hold with matching fingerprint
        mock_hold_repo.get_recent_holds.return_value = [existing_hold]

        service = ContainmentService(
            db_manager=mock_db,
            hold_repo=mock_hold_repo,
            run_repo=mock_run_repo,
            source_name="test_source",
        )

        hold_row, is_new = service.contain_suspicious_batch(
            batch=batch,
            guardrail_decision=sample_suspicious_guardrail_decision,
        )

        # Must return the existing hold and NOT call create_hold
        assert is_new is False
        assert hold_row.hold_id == existing_hold_id
        mock_hold_repo.create_hold.assert_not_called()
        mock_run_repo.create_run.assert_not_called()

    def test_frozen_source_records_exact_serialization(
        self,
        mock_db: MagicMock,
        sample_source_records: list[InventorySourceRow],
        sample_suspicious_guardrail_decision: GuardrailDecision,
    ) -> None:
        mock_hold_repo = MagicMock(spec=HeldChangeBatchRepository)
        mock_run_repo = MagicMock(spec=ProcessingRunRepository)
        mock_hold_repo.get_recent_holds.return_value = []
        mock_run_repo.get_run.return_value = None

        service = ContainmentService(
            db_manager=mock_db,
            hold_repo=mock_hold_repo,
            run_repo=mock_run_repo,
            source_name="test_source",
        )

        batch = MicroBatch(source_records=sample_source_records)
        service.contain_suspicious_batch(
            batch=batch,
            guardrail_decision=sample_suspicious_guardrail_decision,
        )

        call_kwargs = mock_hold_repo.create_hold.call_args[1]
        frozen = call_kwargs["evidence"]["batch_records"]
        assert len(frozen) == 2
        assert frozen[0]["sku_id"] == "SKU-101"
        assert frozen[0]["warehouse_id"] == "WH-1"
        assert frozen[0]["quantity_on_hand"] == 50
        assert isinstance(frozen[0]["updated_at"], str)


class TestHoldStateTransitionsAndValidation:
    """Test state machine boundaries and exception enforcement."""

    def test_get_nonexistent_hold_raises_not_found(self, mock_db: MagicMock) -> None:
        mock_hold_repo = MagicMock(spec=HeldChangeBatchRepository)
        mock_hold_repo.get_hold.return_value = None

        service = ContainmentService(db_manager=mock_db, hold_repo=mock_hold_repo)
        missing_id = uuid4()
        with pytest.raises(HoldNotFoundError) as exc_info:
            service.get_held_batch(missing_id)
        assert str(missing_id) in str(exc_info.value)

    def test_invalid_transition_from_released(self, mock_db: MagicMock) -> None:
        hold_id = uuid4()
        released_hold = HeldChangeBatchRow(
            hold_id=hold_id,
            run_id=uuid4(),
            source_name="test_source",
            severity="LOW",
            reason="ok",
            records_affected=1,
            evidence={"batch_records": []},
            status=HoldStatus.RELEASED.value,
        )
        mock_hold_repo = MagicMock(spec=HeldChangeBatchRepository)
        mock_hold_repo.get_hold.return_value = released_hold

        service = ContainmentService(db_manager=mock_db, hold_repo=mock_hold_repo)

        # Attempting to discard an already released hold must fail
        with pytest.raises(InvalidHoldTransitionError) as exc_info:
            service.discard_held_batch(hold_id)
        assert exc_info.value.current_status == "RELEASED"
        assert exc_info.value.target_status == "DISCARDED"

        # Attempting to reprocess an already released hold must fail
        with pytest.raises(InvalidHoldTransitionError) as exc_info2:
            service.reprocess_held_batch(hold_id)
        assert exc_info2.value.current_status == "RELEASED"
        assert exc_info2.value.target_status == "REPROCESSED"

    def test_invalid_transition_from_discarded(self, mock_db: MagicMock) -> None:
        hold_id = uuid4()
        discarded_hold = HeldChangeBatchRow(
            hold_id=hold_id,
            run_id=uuid4(),
            source_name="test_source",
            severity="HIGH",
            reason="discarded",
            records_affected=1,
            evidence={"batch_records": []},
            status=HoldStatus.DISCARDED.value,
        )
        mock_hold_repo = MagicMock(spec=HeldChangeBatchRepository)
        mock_hold_repo.get_hold.return_value = discarded_hold

        service = ContainmentService(db_manager=mock_db, hold_repo=mock_hold_repo)

        # Attempting to release a discarded hold must fail
        with pytest.raises(InvalidHoldTransitionError) as exc_info:
            service.release_held_batch(hold_id)
        assert exc_info.value.current_status == "DISCARDED"
        assert exc_info.value.target_status == "RELEASED"


class TestHoldResolutionOperations:
    """Test RELEASE, REPROCESS, and DISCARD operations, including idempotency and replay."""

    def test_release_replays_frozen_batch_commits_history_and_advances_watermark(
        self,
        mock_db: MagicMock,
        sample_source_records: list[InventorySourceRow],
    ) -> None:
        hold_id = uuid4()
        run_id = uuid4()
        t_max = datetime(2026, 9, 16, 10, 5, 0, tzinfo=timezone.utc)
        frozen_data = [
            {
                "sku_id": r.sku_id,
                "warehouse_id": r.warehouse_id,
                "quantity_on_hand": r.quantity_on_hand,
                "reorder_level": r.reorder_level,
                "status": r.status,
                "updated_at": r.updated_at.isoformat(),
            }
            for r in sample_source_records
        ]
        held_row = HeldChangeBatchRow(
            hold_id=hold_id,
            run_id=run_id,
            source_name="test_source",
            severity="HIGH",
            reason="test reason",
            records_affected=2,
            evidence={
                "batch_records": frozen_data,
                "watermark_start": "2026-09-16T09:50:00+00:00",
                "watermark_end": t_max.isoformat(),
            },
            status=HoldStatus.HELD.value,
        )

        mock_hold_repo = MagicMock(spec=HeldChangeBatchRepository)
        mock_run_repo = MagicMock(spec=ProcessingRunRepository)
        mock_checkpoint_repo = MagicMock(spec=CheckpointRepository)
        mock_history_repo = MagicMock(spec=InventoryHistoryRepository)

        mock_hold_repo.get_hold.return_value = held_row
        mock_history_repo.fetch_current_history_for_keys.return_value = []

        service = ContainmentService(
            db_manager=mock_db,
            hold_repo=mock_hold_repo,
            run_repo=mock_run_repo,
            checkpoint_repo=mock_checkpoint_repo,
            history_repo=mock_history_repo,
            source_name="test_source",
        )

        res = service.release_held_batch(hold_id, operator_reason="Approved after manual audit")

        assert res.success is True
        assert res.status == "RELEASED"
        assert res.records_affected == 2
        assert res.checkpoint_advanced_to == t_max
        assert res.is_idempotent is False

        # Verify downstream commits
        mock_history_repo.insert_history_rows.assert_called_once()
        inserted_rows = mock_history_repo.insert_history_rows.call_args[0][0]
        assert len(inserted_rows) == 2
        assert inserted_rows[0].is_current is True

        mock_checkpoint_repo.update_checkpoint.assert_called_once_with(
            source_name="test_source",
            watermark=t_max,
            cursor_keys={"sku_id": "SKU-102", "warehouse_id": "WH-2"},
            run_id=run_id,
            table_name=service.table_name,
            conn=mock_db.transaction.return_value.__enter__.return_value,
        )
        mock_hold_repo.release_hold.assert_called_once()

    def test_release_idempotent_when_already_released(self, mock_db: MagicMock) -> None:
        hold_id = uuid4()
        now = datetime.now(timezone.utc)
        released_row = HeldChangeBatchRow(
            hold_id=hold_id,
            run_id=uuid4(),
            source_name="test_source",
            severity="HIGH",
            reason="test",
            records_affected=2,
            evidence={"batch_records": []},
            status=HoldStatus.RELEASED.value,
            resolved_at=now,
        )

        mock_hold_repo = MagicMock(spec=HeldChangeBatchRepository)
        mock_history_repo = MagicMock(spec=InventoryHistoryRepository)
        mock_hold_repo.get_hold.return_value = released_row

        service = ContainmentService(
            db_manager=mock_db,
            hold_repo=mock_hold_repo,
            history_repo=mock_history_repo,
        )

        res = service.release_held_batch(hold_id)
        assert res.success is True
        assert res.is_idempotent is True
        assert res.status == "RELEASED"
        mock_history_repo.insert_history_rows.assert_not_called()

    def test_release_fails_if_scd2_invariants_violated(self, mock_db: MagicMock) -> None:
        hold_id = uuid4()
        # Invalid record: negative quantity
        invalid_frozen = [
            {
                "sku_id": "SKU-1",
                "warehouse_id": "WH-1",
                "quantity_on_hand": -50,
                "reorder_level": 10,
                "status": "ACTIVE",
                "updated_at": "2026-09-16T10:00:00+00:00",
            }
        ]
        held_row = HeldChangeBatchRow(
            hold_id=hold_id,
            run_id=uuid4(),
            source_name="test_source",
            severity="HIGH",
            reason="test",
            records_affected=1,
            evidence={"batch_records": invalid_frozen},
            status=HoldStatus.HELD.value,
        )

        mock_hold_repo = MagicMock(spec=HeldChangeBatchRepository)
        mock_hold_repo.get_hold.return_value = held_row

        service = ContainmentService(db_manager=mock_db, hold_repo=mock_hold_repo)

        # Negative quantity raises HoldReplayError during deserialization
        with pytest.raises(HoldReplayError):
            service.release_held_batch(hold_id)

    def test_reprocess_retains_held_when_still_suspicious(
        self,
        mock_db: MagicMock,
        sample_source_records: list[InventorySourceRow],
        sample_suspicious_guardrail_decision: GuardrailDecision,
    ) -> None:
        hold_id = uuid4()
        frozen_data = [
            {
                "sku_id": r.sku_id,
                "warehouse_id": r.warehouse_id,
                "quantity_on_hand": r.quantity_on_hand,
                "reorder_level": r.reorder_level,
                "status": r.status,
                "updated_at": r.updated_at.isoformat(),
            }
            for r in sample_source_records
        ]
        held_row = HeldChangeBatchRow(
            hold_id=hold_id,
            run_id=uuid4(),
            source_name="test_source",
            severity="HIGH",
            reason="test",
            records_affected=2,
            evidence={"batch_records": frozen_data},
            status=HoldStatus.HELD.value,
        )

        mock_hold_repo = MagicMock(spec=HeldChangeBatchRepository)
        mock_history_repo = MagicMock(spec=InventoryHistoryRepository)
        mock_checkpoint_repo = MagicMock(spec=CheckpointRepository)
        mock_guardrail = MagicMock(spec=GuardrailEngine)

        mock_hold_repo.get_hold.return_value = held_row
        mock_history_repo.fetch_current_history_for_keys.return_value = []
        mock_guardrail.evaluate.return_value = sample_suspicious_guardrail_decision

        service = ContainmentService(
            db_manager=mock_db,
            hold_repo=mock_hold_repo,
            history_repo=mock_history_repo,
            checkpoint_repo=mock_checkpoint_repo,
            guardrail=mock_guardrail,
        )

        res = service.reprocess_held_batch(hold_id, force_normal=False)

        assert res.success is False
        assert res.status == "HELD"
        mock_history_repo.insert_history_rows.assert_not_called()
        mock_checkpoint_repo.update_checkpoint.assert_not_called()
        mock_hold_repo.reprocess_hold.assert_not_called()

    def test_reprocess_commits_when_normal_or_forced(
        self,
        mock_db: MagicMock,
        sample_source_records: list[InventorySourceRow],
        sample_normal_guardrail_decision: GuardrailDecision,
    ) -> None:
        hold_id = uuid4()
        run_id = uuid4()
        frozen_data = [
            {
                "sku_id": r.sku_id,
                "warehouse_id": r.warehouse_id,
                "quantity_on_hand": r.quantity_on_hand,
                "reorder_level": r.reorder_level,
                "status": r.status,
                "updated_at": r.updated_at.isoformat(),
            }
            for r in sample_source_records
        ]
        held_row = HeldChangeBatchRow(
            hold_id=hold_id,
            run_id=run_id,
            source_name="test_source",
            severity="HIGH",
            reason="test",
            records_affected=2,
            evidence={"batch_records": frozen_data},
            status=HoldStatus.HELD.value,
        )

        mock_hold_repo = MagicMock(spec=HeldChangeBatchRepository)
        mock_run_repo = MagicMock(spec=ProcessingRunRepository)
        mock_history_repo = MagicMock(spec=InventoryHistoryRepository)
        mock_checkpoint_repo = MagicMock(spec=CheckpointRepository)
        mock_guardrail = MagicMock(spec=GuardrailEngine)

        mock_hold_repo.get_hold.return_value = held_row
        mock_history_repo.fetch_current_history_for_keys.return_value = []
        mock_guardrail.evaluate.return_value = sample_normal_guardrail_decision

        service = ContainmentService(
            db_manager=mock_db,
            hold_repo=mock_hold_repo,
            run_repo=mock_run_repo,
            history_repo=mock_history_repo,
            checkpoint_repo=mock_checkpoint_repo,
            guardrail=mock_guardrail,
            source_name="test_source",
        )

        res = service.reprocess_held_batch(hold_id, force_normal=False)

        assert res.success is True
        assert res.status == "REPROCESSED"
        mock_history_repo.insert_history_rows.assert_called_once()
        mock_checkpoint_repo.update_checkpoint.assert_called_once()
        mock_hold_repo.reprocess_hold.assert_called_once()

    def test_discard_advances_checkpoint_without_history_writes(
        self,
        mock_db: MagicMock,
    ) -> None:
        hold_id = uuid4()
        run_id = uuid4()
        t_end = datetime(2026, 9, 16, 10, 5, 0, tzinfo=timezone.utc)
        held_row = HeldChangeBatchRow(
            hold_id=hold_id,
            run_id=run_id,
            source_name="test_source",
            severity="HIGH",
            reason="Corrupted feed batch",
            records_affected=10,
            evidence={
                "batch_records": [],
                "watermark_end": t_end.isoformat(),
            },
            status=HoldStatus.HELD.value,
        )

        mock_hold_repo = MagicMock(spec=HeldChangeBatchRepository)
        mock_history_repo = MagicMock(spec=InventoryHistoryRepository)
        mock_checkpoint_repo = MagicMock(spec=CheckpointRepository)
        mock_hold_repo.get_hold.return_value = held_row

        service = ContainmentService(
            db_manager=mock_db,
            hold_repo=mock_hold_repo,
            history_repo=mock_history_repo,
            checkpoint_repo=mock_checkpoint_repo,
            source_name="test_source",
        )

        res = service.discard_held_batch(
            hold_id,
            operator_reason="Rejected corrupted batch",
            advance_checkpoint=True,
        )

        assert res.success is True
        assert res.status == "DISCARDED"
        assert res.checkpoint_advanced_to == t_end

        # Invariant: inventory_history must NOT receive any rows
        mock_history_repo.insert_history_rows.assert_not_called()
        mock_history_repo.close_current_versions.assert_not_called()

        # Invariant: checkpoint advances to unblock stream
        mock_checkpoint_repo.update_checkpoint.assert_called_once_with(
            source_name="test_source",
            watermark=t_end,
            cursor_keys=None,
            run_id=run_id,
            table_name=service.table_name,
            conn=mock_db.transaction.return_value.__enter__.return_value,
        )
        mock_hold_repo.discard_hold.assert_called_once()

    def test_discard_without_checkpoint_advancement(self, mock_db: MagicMock) -> None:
        hold_id = uuid4()
        held_row = HeldChangeBatchRow(
            hold_id=hold_id,
            run_id=uuid4(),
            source_name="test_source",
            severity="HIGH",
            reason="Corrupted feed batch",
            records_affected=10,
            evidence={"batch_records": []},
            status=HoldStatus.HELD.value,
        )

        mock_hold_repo = MagicMock(spec=HeldChangeBatchRepository)
        mock_checkpoint_repo = MagicMock(spec=CheckpointRepository)
        mock_hold_repo.get_hold.return_value = held_row

        service = ContainmentService(
            db_manager=mock_db,
            hold_repo=mock_hold_repo,
            checkpoint_repo=mock_checkpoint_repo,
        )

        res = service.discard_held_batch(hold_id, advance_checkpoint=False)
        assert res.success is True
        assert res.status == "DISCARDED"
        mock_checkpoint_repo.update_checkpoint.assert_not_called()

    def test_discard_idempotent_when_already_discarded(self, mock_db: MagicMock) -> None:
        hold_id = uuid4()
        now = datetime.now(timezone.utc)
        discarded_row = HeldChangeBatchRow(
            hold_id=hold_id,
            run_id=uuid4(),
            source_name="test_source",
            severity="HIGH",
            reason="already discarded",
            records_affected=10,
            evidence={"batch_records": []},
            status=HoldStatus.DISCARDED.value,
            resolved_at=now,
        )

        mock_hold_repo = MagicMock(spec=HeldChangeBatchRepository)
        mock_hold_repo.get_hold.return_value = discarded_row

        service = ContainmentService(db_manager=mock_db, hold_repo=mock_hold_repo)

        res = service.discard_held_batch(hold_id)
        assert res.success is True
        assert res.is_idempotent is True
        assert res.status == "DISCARDED"
        mock_hold_repo.discard_hold.assert_not_called()


class TestSourceDataImmutability:
    """Verify that under no circumstance are records in inventory_source mutated or deleted."""

    def test_inventory_source_never_mutated_during_containment_or_recovery(
        self,
        mock_db: MagicMock,
        sample_source_records: list[InventorySourceRow],
        sample_suspicious_guardrail_decision: GuardrailDecision,
    ) -> None:
        mock_inventory_repo = MagicMock(spec=InventorySourceRepository)
        mock_hold_repo = MagicMock(spec=HeldChangeBatchRepository)
        mock_run_repo = MagicMock(spec=ProcessingRunRepository)
        mock_hold_repo.get_recent_holds.return_value = []
        mock_run_repo.get_run.return_value = None

        service = ContainmentService(
            db_manager=mock_db,
            inventory_repo=mock_inventory_repo,
            hold_repo=mock_hold_repo,
            run_repo=mock_run_repo,
            source_name="test_source",
        )

        batch = MicroBatch(source_records=sample_source_records)
        service.contain_suspicious_batch(
            batch=batch,
            guardrail_decision=sample_suspicious_guardrail_decision,
        )

        # Invariant: inventory_source repository has zero mutation calls
        mock_inventory_repo.upsert_inventory_rows.assert_not_called()
