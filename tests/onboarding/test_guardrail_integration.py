"""Comprehensive test suite for M9 — Historical Guardrail Integration for Customer Onboarding."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
import json
from pathlib import Path
import tempfile
from unittest.mock import MagicMock
from uuid import UUID, uuid4
import pytest

import polars as pl

from src.scd2_copilot.containment.models import HoldResolutionResult
from src.scd2_copilot.db.models import HeldChangeBatchRow, HoldStatus, MonitoredEntityHistoryRow
from src.scd2_copilot.db.repositories import (
    HeldChangeBatchRepository,
    MonitoredEntityHistoryRepository,
    ProcessingRunRepository,
)
from src.scd2_copilot.explanation.models import BatchExplanationResult, ExplanationContext
from src.scd2_copilot.explanation.service import ExplanationService
from src.scd2_copilot.guardrail.models import (
    GuardrailDecisionType,
    GuardrailSeverity,
    RuleId,
)
from src.scd2_copilot.onboarding.canonical import get_canonical_customer_v1
from src.scd2_copilot.onboarding.exceptions import (
    SCD2InvariantValidationError,
    SchemaCompatibilityError,
)
from src.scd2_copilot.onboarding.guardrail import (
    CustomerGuardrailAdapter,
    CustomerGuardrailConfig,
    CustomerGuardrailEvaluationResult,
    CustomerHistoricalGuardrailService,
)
from src.scd2_copilot.onboarding.models.approval import (
    ApprovedMappingDefinition,
    ApprovedMappingVersion,
    ReviewDecisionType,
)
from src.scd2_copilot.onboarding.models.drift import (
    DriftType,
    FieldMappingImpact,
    MappingCompatibilityState,
    SchemaDiffResult,
    SchemaDriftEvent,
    SchemaDriftReport,
)
from src.scd2_copilot.onboarding.models.mapping import (
    CandidateMapping,
    MappingType,
    TransformationOpType,
    TransformationStep,
)
from src.scd2_copilot.onboarding.models.run import RunCreateRequest, RunStatus
from src.scd2_copilot.onboarding.models.transformation import CanonicalCustomerRecord
from src.scd2_copilot.onboarding.runs.service import OnboardingRunService
from src.scd2_copilot.onboarding.scd2.models import CustomerSCD2Config
from src.scd2_copilot.onboarding.scd2.service import CustomerSCD2Service


def _create_customer(
    customer_id: str,
    first_name: str = "Jane",
    last_name: str = "Doe",
    email: str = "jane.doe@example.com",
    date_of_birth: date = date(1990, 5, 15),
    status: str = "ACTIVE",
    created_at: datetime = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
) -> CanonicalCustomerRecord:
    return CanonicalCustomerRecord(
        customer_id=customer_id,
        first_name=first_name,
        last_name=last_name,
        email=email,
        date_of_birth=date_of_birth,
        status=status,
        created_at=created_at,
    )


# ── 1. Normal Batch Passes Guardrail and Commits ──────────────


def test_normal_customer_batch_passes_and_commits() -> None:
    history_repo = MonitoredEntityHistoryRepository(in_memory=True)
    scd2_service = CustomerSCD2Service(repository=history_repo)
    service = CustomerHistoricalGuardrailService(scd2_service=scd2_service)

    records = [
        _create_customer("CUST-101", first_name="Alice", status="ACTIVE"),
        _create_customer("CUST-102", first_name="Bob", status="ACTIVE"),
        _create_customer("CUST-103", first_name="Charlie", status="ACTIVE"),
    ]

    proc_date = date(2026, 4, 1)
    result = service.evaluate_and_process(
        records=records,
        processing_date=proc_date,
        run_id="run-norm-001",
        batch_id="batch-001",
    )

    assert result.decision == GuardrailDecisionType.NORMAL
    assert result.is_held is False
    assert result.hold_id is None
    assert result.persisted is True
    assert result.candidate_execution_result is not None
    assert result.candidate_execution_result.new_count == 3
    assert result.candidate_execution_result.persisted_history_rows_count == 3

    # Verify history repository actually persisted the active records
    active_rows = history_repo.fetch_current_history_for_keys(
        source_name="customer",
        entity_keys=[{"customer_id": "CUST-101"}, {"customer_id": "CUST-102"}, {"customer_id": "CUST-103"}],
    )
    assert len(active_rows) == 3
    for row in active_rows:
        assert row.is_current is True
        assert row.effective_to is None
        assert row.effective_from == datetime(2026, 4, 1, tzinfo=timezone.utc)


def test_normal_incremental_change_batch_passes_and_commits() -> None:
    history_repo = MonitoredEntityHistoryRepository(in_memory=True)
    scd2_service = CustomerSCD2Service(repository=history_repo)
    service = CustomerHistoricalGuardrailService(scd2_service=scd2_service)

    # Initial batch
    initial = [
        _create_customer("CUST-201", first_name="Dave", status="ACTIVE"),
        _create_customer("CUST-202", first_name="Emma", status="ACTIVE"),
    ]
    service.evaluate_and_process(records=initial, processing_date=date(2026, 4, 1))

    # Second batch: 1 changed, 1 unchanged (well below thresholds)
    second_batch = [
        _create_customer("CUST-201", first_name="David", status="ACTIVE"),  # changed name
        _create_customer("CUST-202", first_name="Emma", status="ACTIVE"),   # unchanged
    ]
    result2 = service.evaluate_and_process(
        records=second_batch,
        processing_date=date(2026, 4, 10),
    )

    assert result2.decision == GuardrailDecisionType.NORMAL
    assert result2.is_held is False
    assert result2.persisted is True
    assert result2.candidate_execution_result.changed_count == 1
    assert result2.candidate_execution_result.unchanged_count == 1

    # Check history state for CUST-201
    all_dave_rows = scd2_service.get_customer_history("CUST-201")
    assert len(all_dave_rows) == 2
    # Closed version:
    v1 = next(r for r in all_dave_rows if not r.is_current)
    assert v1.effective_to == datetime(2026, 4, 10, tzinfo=timezone.utc)
    assert v1.attributes["first_name"] == "Dave"
    # Current version:
    v2 = next(r for r in all_dave_rows if r.is_current)
    assert v2.effective_to is None
    assert v2.effective_from == datetime(2026, 4, 10, tzinfo=timezone.utc)
    assert v2.attributes["first_name"] == "David"


# ── 2. Guardrail Triggers Hold and Blocks Commit ─────────────


def test_high_change_volume_triggers_hold_and_blocks_commit() -> None:
    history_repo = MonitoredEntityHistoryRepository(in_memory=True)
    hold_repo = HeldChangeBatchRepository(in_memory=True)
    scd2_service = CustomerSCD2Service(repository=history_repo)
    # Threshold: max 2 changes allowed before hold
    cfg = CustomerGuardrailConfig(max_change_volume=2)
    service = CustomerHistoricalGuardrailService(
        config=cfg,
        scd2_service=scd2_service,
        hold_repo=hold_repo,
    )

    # Initial load of 5 entities
    initial = [_create_customer(f"CUST-{i}", status="ACTIVE") for i in range(1, 6)]
    res1 = service.evaluate_and_process(records=initial, processing_date=date(2026, 4, 1))
    assert res1.is_held is False

    # Pre-batch history verification: 5 rows
    assert len(history_repo.get_all_rows()) == 5

    # Second batch updates 4 entities (exceeds max_change_volume=2)
    second_batch = [
        _create_customer(f"CUST-{i}", first_name=f"Updated_{i}", status="ACTIVE")
        for i in range(1, 5)
    ] + [_create_customer("CUST-5", status="ACTIVE")]

    res2 = service.evaluate_and_process(
        records=second_batch,
        processing_date=date(2026, 4, 10),
        run_id="run-vol-002",
        source_id="crm_feed",
    )

    # 1. Evaluates to SUSPICIOUS and held
    assert res2.decision == GuardrailDecisionType.SUSPICIOUS
    assert res2.is_held is True
    assert res2.hold_id is not None
    assert res2.persisted is False
    assert any("HIGH_CHANGE_VOLUME" in r.rule_id for r in res2.triggered_rules)

    # 2. Candidate result is populated but indicates 0 persisted rows
    assert res2.candidate_execution_result is not None
    assert res2.candidate_execution_result.changed_count == 4
    assert res2.candidate_execution_result.persisted_history_rows_count == 0
    assert res2.candidate_execution_result.closed_versions_count == 0
    assert res2.candidate_execution_result.new_versions_count == 0

    # 3. CRITICAL INVARIANT: History state remains 100% untouched!
    history_after = history_repo.get_all_rows()
    assert len(history_after) == 5
    for row in history_after:
        assert row.is_current is True
        assert row.effective_to is None
        assert not row.attributes["first_name"].startswith("Updated_")

    # 4. Hold record is durably persisted
    hold_entry = service.get_held_batch(res2.hold_id)
    assert hold_entry is not None
    assert hold_entry.status == HoldStatus.HELD
    assert hold_entry.source_name == "crm_feed"


def test_high_population_impact_triggers_hold_and_blocks_commit() -> None:
    history_repo = MonitoredEntityHistoryRepository(in_memory=True)
    hold_repo = HeldChangeBatchRepository(in_memory=True)
    scd2_service = CustomerSCD2Service(repository=history_repo)
    # Threshold: max 30% population impact allowed
    cfg = CustomerGuardrailConfig(
        max_population_impact_ratio=0.30,
        max_change_volume=100,  # high so volume doesn't trigger first
    )
    service = CustomerHistoricalGuardrailService(
        config=cfg,
        scd2_service=scd2_service,
        hold_repo=hold_repo,
    )

    # Initial load of 10 entities
    initial = [_create_customer(f"CUST-{i}", status="ACTIVE") for i in range(1, 11)]
    service.evaluate_and_process(records=initial, processing_date=date(2026, 4, 1))
    assert len(history_repo.get_all_rows()) == 10

    # Second batch changes 5 of 10 entities (50% impact > 30% threshold)
    second_batch = [
        _create_customer(f"CUST-{i}", first_name=f"NewName_{i}", status="ACTIVE")
        for i in range(1, 6)
    ] + [_create_customer(f"CUST-{i}", status="ACTIVE") for i in range(6, 11)]

    res2 = service.evaluate_and_process(
        records=second_batch,
        processing_date=date(2026, 4, 10),
    )

    assert res2.is_held is True
    assert res2.decision == GuardrailDecisionType.SUSPICIOUS
    assert any("HIGH_POPULATION_IMPACT" in r.rule_id for r in res2.triggered_rules)
    assert res2.persisted is False

    # History untouched
    assert len(history_repo.get_all_rows()) == 10


def test_mass_deactivation_triggers_hold_and_blocks_commit() -> None:
    history_repo = MonitoredEntityHistoryRepository(in_memory=True)
    hold_repo = HeldChangeBatchRepository(in_memory=True)
    scd2_service = CustomerSCD2Service(repository=history_repo)
    # Threshold: max 2 deactivations allowed
    cfg = CustomerGuardrailConfig(
        max_deactivation_count=2,
        max_change_volume=100,
        max_population_impact_ratio=1.0,
    )
    service = CustomerHistoricalGuardrailService(
        config=cfg,
        scd2_service=scd2_service,
        hold_repo=hold_repo,
    )

    # Initial load of 8 active entities
    initial = [_create_customer(f"CUST-{i}", status="ACTIVE") for i in range(1, 9)]
    service.evaluate_and_process(records=initial, processing_date=date(2026, 4, 1))

    # Second batch deactivates 4 of 8 entities (4 > 2 threshold)
    second_batch = [
        _create_customer(f"CUST-{i}", status="INACTIVE") for i in range(1, 5)
    ] + [_create_customer(f"CUST-{i}", status="ACTIVE") for i in range(5, 9)]

    res2 = service.evaluate_and_process(
        records=second_batch,
        processing_date=date(2026, 4, 10),
    )

    assert res2.is_held is True
    assert res2.decision == GuardrailDecisionType.SUSPICIOUS
    assert any("MASS_DEACTIVATION" in r.rule_id for r in res2.triggered_rules)
    assert res2.persisted is False

    # History untouched: all 8 entities remain ACTIVE and is_current=True
    current_active = history_repo.fetch_current_history_for_keys(
        source_name="customer",
        entity_keys=[{"customer_id": f"CUST-{i}"} for i in range(1, 9)],
    )
    assert len(current_active) == 8
    for r in current_active:
        assert r.is_current is True
        assert r.attributes["status"] == "ACTIVE"


def test_high_change_velocity_triggers_hold_and_blocks_commit() -> None:
    history_repo = MonitoredEntityHistoryRepository(in_memory=True)
    hold_repo = HeldChangeBatchRepository(in_memory=True)
    scd2_service = CustomerSCD2Service(repository=history_repo)
    # Threshold: max velocity 2.0 changes/second
    cfg = CustomerGuardrailConfig(
        max_change_velocity=2.0,
        max_change_volume=100,
        max_population_impact_ratio=1.0,
        min_velocity_records=3,
    )
    service = CustomerHistoricalGuardrailService(
        config=cfg,
        scd2_service=scd2_service,
        hold_repo=hold_repo,
    )

    # Pre-populate 5 entities
    initial = [_create_customer(f"CUST-{i}") for i in range(1, 6)]
    service.evaluate_and_process(records=initial, processing_date=date(2026, 4, 1))

    # Burst of 4 updates occurring across a 0.5s span -> velocity = 4 / 0.5 = 8.0 changes/sec > 2.0
    t0 = datetime(2026, 4, 10, 12, 0, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 4, 10, 12, 0, 0, 500000, tzinfo=timezone.utc)
    burst_batch = [
        _create_customer("CUST-1", first_name="Burst_1", created_at=t0),
        _create_customer("CUST-2", first_name="Burst_2", created_at=t0),
        _create_customer("CUST-3", first_name="Burst_3", created_at=t1),
        _create_customer("CUST-4", first_name="Burst_4", created_at=t1),
        _create_customer("CUST-5", created_at=t1),
    ]

    res = service.evaluate_and_process(
        records=burst_batch,
        processing_date=date(2026, 4, 10),
    )

    assert res.is_held is True
    assert any("HIGH_CHANGE_VELOCITY" in r.rule_id for r in res.triggered_rules)
    assert res.persisted is False


# ── 3. SCD2 Invariant Failure & Schema Compatibility Gating ──


def test_schema_compatibility_drift_blocks_before_guardrail() -> None:
    history_repo = MonitoredEntityHistoryRepository(in_memory=True)
    scd2_service = CustomerSCD2Service(repository=history_repo)
    service = CustomerHistoricalGuardrailService(scd2_service=scd2_service)

    # Incompatible drift report with breaking change
    drift_report = SchemaDriftReport(
        source_id="crm_system",
        prior_schema_version=1,
        current_schema_version=2,
        prior_fingerprint="fp1",
        current_fingerprint="fp2",
        mapping_version_id="map_v1",
        drift_events=[
            SchemaDriftEvent(
                drift_type=DriftType.REMOVED_COLUMN,
                field_name="email",
                details="Required customer identity column deleted",
            )
        ],
        impacted_mappings=[
            FieldMappingImpact(
                source_field="email",
                target_field="email",
                mapping_version_id="map_v1",
                impact_category=DriftType.REMOVED_COLUMN,
                compatibility=MappingCompatibilityState.BROKEN,
                reason="Breaking: required field missing",
            )
        ],
        overall_compatibility=MappingCompatibilityState.BROKEN,
        summary_reason="Incompatible drift detected: required field missing",
    )

    records = [_create_customer("CUST-999")]

    with pytest.raises(SchemaCompatibilityError) as exc_info:
        service.evaluate_and_process(
            records=records,
            processing_date=date(2026, 4, 1),
            drift_report=drift_report,
        )

    assert "BROKEN" in str(exc_info.value)
    # History untouched
    assert len(history_repo.get_all_rows()) == 0


# ── 4. Historical State Immutability Guarantee ───────────────


def test_historical_state_unmodified_after_hold() -> None:
    history_repo = MonitoredEntityHistoryRepository(in_memory=True)
    scd2_service = CustomerSCD2Service(repository=history_repo)
    cfg = CustomerGuardrailConfig(max_change_volume=1)
    service = CustomerHistoricalGuardrailService(config=cfg, scd2_service=scd2_service)

    # Setup 3 records
    initial = [_create_customer(f"CUST-{i}", first_name=f"Orig_{i}") for i in range(1, 4)]
    service.evaluate_and_process(records=initial, processing_date=date(2026, 4, 1))

    pre_hold_rows = history_repo.get_all_rows()
    pre_hold_snapshots = [
        (r.entity_key, r.effective_from, r.effective_to, r.is_current, dict(r.attributes))
        for r in pre_hold_rows
    ]

    # Attempt suspicious batch with 3 updates (exceeds volume 1)
    suspicious = [_create_customer(f"CUST-{i}", first_name=f"Modified_{i}") for i in range(1, 4)]
    res = service.evaluate_and_process(records=suspicious, processing_date=date(2026, 4, 10))

    assert res.is_held is True

    post_hold_rows = history_repo.get_all_rows()
    post_hold_snapshots = [
        (r.entity_key, r.effective_from, r.effective_to, r.is_current, dict(r.attributes))
        for r in post_hold_rows
    ]

    # Exact byte/tuple equality
    assert pre_hold_snapshots == post_hold_snapshots


# ── 5. Duplicate Hold Suppression ────────────────────────────


def test_duplicate_suspicious_run_suppresses_duplicate_hold() -> None:
    history_repo = MonitoredEntityHistoryRepository(in_memory=True)
    hold_repo = HeldChangeBatchRepository(in_memory=True)
    scd2_service = CustomerSCD2Service(repository=history_repo)
    cfg = CustomerGuardrailConfig(max_change_volume=1)
    service = CustomerHistoricalGuardrailService(
        config=cfg,
        scd2_service=scd2_service,
        hold_repo=hold_repo,
    )

    # Populate 3 records
    service.evaluate_and_process(
        records=[_create_customer(f"CUST-{i}") for i in range(1, 4)],
        processing_date=date(2026, 4, 1),
    )

    suspicious = [_create_customer(f"CUST-{i}", first_name="Tampered") for i in range(1, 4)]

    # First suspicious submission
    res1 = service.evaluate_and_process(
        records=suspicious,
        processing_date=date(2026, 4, 10),
        run_id="run-dup-test",
        batch_id="batch-dup-test",
    )
    assert res1.is_held is True
    first_hold_id = res1.hold_id

    # Count holds in repo
    assert hold_repo.count_active_holds("customer") == 1

    # Second submission with same batch content
    res2 = service.evaluate_and_process(
        records=suspicious,
        processing_date=date(2026, 4, 10),
        run_id="run-dup-test-2",
        batch_id="batch-dup-test",
    )
    assert res2.is_held is True
    # Reuses/suppresses duplicate active hold
    assert res2.hold_id == first_hold_id
    assert hold_repo.count_active_holds("customer") == 1


# ── 6. Operational Recovery: Release ──────────────────────────


def test_recovery_release_commits_history() -> None:
    history_repo = MonitoredEntityHistoryRepository(in_memory=True)
    hold_repo = HeldChangeBatchRepository(in_memory=True)
    scd2_service = CustomerSCD2Service(repository=history_repo)
    cfg = CustomerGuardrailConfig(max_change_volume=1)
    service = CustomerHistoricalGuardrailService(
        config=cfg,
        scd2_service=scd2_service,
        hold_repo=hold_repo,
    )

    # Initial load: 3 entities
    service.evaluate_and_process(
        records=[_create_customer(f"CUST-{i}", first_name="Orig") for i in range(1, 4)],
        processing_date=date(2026, 4, 1),
    )

    # Suspicious update: 2 entities updated (exceeds volume 1)
    update_records = [
        _create_customer("CUST-1", first_name="ApprovedUpdate_1"),
        _create_customer("CUST-2", first_name="ApprovedUpdate_2"),
        _create_customer("CUST-3", first_name="Orig"),
    ]
    res = service.evaluate_and_process(
        records=update_records,
        processing_date=date(2026, 4, 10),
    )
    assert res.is_held is True
    hold_id = res.hold_id

    # Pre-release check: history unmodified
    c1_pre = service.scd2_service.get_current_customer("CUST-1")
    assert c1_pre.attributes["first_name"] == "Orig"

    # Operator performs release
    resolution = service.release_held_batch(
        hold_id=hold_id,
        operator_reason="Verified valid batch by ops manager",
    )

    assert resolution.success is True
    assert resolution.hold_id == hold_id
    assert resolution.status == HoldStatus.RELEASED

    # Post-release check: hold status updated in repository
    hold_row = service.get_held_batch(hold_id)
    assert hold_row.status == HoldStatus.RELEASED
    assert hold_row.resolved_at is not None

    # Post-release check: changes COMMITTED to history!
    c1_post = service.scd2_service.get_current_customer("CUST-1")
    assert c1_post.attributes["first_name"] == "ApprovedUpdate_1"
    assert c1_post.is_current is True

    c2_post = service.scd2_service.get_current_customer("CUST-2")
    assert c2_post.attributes["first_name"] == "ApprovedUpdate_2"
    assert c2_post.is_current is True

    # Total rows in history: 3 original (2 closed, 1 active) + 2 new active = 5 rows
    assert len(history_repo.get_all_rows()) == 5


# ── 7. Operational Recovery: Discard ──────────────────────────


def test_recovery_discard_leaves_history_permanently_untouched() -> None:
    history_repo = MonitoredEntityHistoryRepository(in_memory=True)
    hold_repo = HeldChangeBatchRepository(in_memory=True)
    scd2_service = CustomerSCD2Service(repository=history_repo)
    cfg = CustomerGuardrailConfig(max_change_volume=1)
    service = CustomerHistoricalGuardrailService(
        config=cfg,
        scd2_service=scd2_service,
        hold_repo=hold_repo,
    )

    # Initial load: 3 entities
    service.evaluate_and_process(
        records=[_create_customer(f"CUST-{i}", first_name="Orig") for i in range(1, 4)],
        processing_date=date(2026, 4, 1),
    )

    # Suspicious update held
    suspicious = [
        _create_customer("CUST-1", first_name="MaliciousPayload"),
        _create_customer("CUST-2", first_name="MaliciousPayload"),
    ]
    res = service.evaluate_and_process(
        records=suspicious,
        processing_date=date(2026, 4, 10),
    )
    assert res.is_held is True
    hold_id = res.hold_id

    # Operator discards held batch
    resolution = service.discard_held_batch(
        hold_id=hold_id,
        operator_reason="Malicious or corrupt feed detected",
    )

    assert resolution.success is True
    assert resolution.status == HoldStatus.DISCARDED

    # Post-discard verification: hold record is DISCARDED
    hold_row = service.get_held_batch(hold_id)
    assert hold_row.status == HoldStatus.DISCARDED

    # History permanently unchanged
    assert len(history_repo.get_all_rows()) == 3
    c1 = service.scd2_service.get_current_customer("CUST-1")
    assert c1.attributes["first_name"] == "Orig"


# ── 8. Operational Recovery: Reprocess ────────────────────────


def test_recovery_reprocess_evaluates_guardrail() -> None:
    history_repo = MonitoredEntityHistoryRepository(in_memory=True)
    hold_repo = HeldChangeBatchRepository(in_memory=True)
    scd2_service = CustomerSCD2Service(repository=history_repo)
    cfg = CustomerGuardrailConfig(max_change_volume=1)
    service = CustomerHistoricalGuardrailService(
        config=cfg,
        scd2_service=scd2_service,
        hold_repo=hold_repo,
    )

    # Initial load
    service.evaluate_and_process(
        records=[_create_customer(f"CUST-{i}") for i in range(1, 4)],
        processing_date=date(2026, 4, 1),
    )

    # Suspicious update
    res = service.evaluate_and_process(
        records=[
            _create_customer("CUST-1", first_name="ReprocessMe"),
            _create_customer("CUST-2", first_name="ReprocessMe"),
        ],
        processing_date=date(2026, 4, 10),
    )
    assert res.is_held is True

    # Reprocess with force_normal=True
    res_reprocess = service.reprocess_held_batch(
        hold_id=res.hold_id,
        force_normal=True,
    )
    assert res_reprocess.success is True
    assert res_reprocess.status in {HoldStatus.REPROCESSED, "REPROCESSED"}

    # Verify history was committed
    c1 = service.scd2_service.get_current_customer("CUST-1")
    assert c1.attributes["first_name"] == "ReprocessMe"


# ── 9. Lineage Preservation in Hold Evidence ─────────────────


def test_lineage_preserved_in_hold_evidence() -> None:
    history_repo = MonitoredEntityHistoryRepository(in_memory=True)
    hold_repo = HeldChangeBatchRepository(in_memory=True)
    scd2_service = CustomerSCD2Service(repository=history_repo)
    cfg = CustomerGuardrailConfig(max_change_volume=1)
    service = CustomerHistoricalGuardrailService(
        config=cfg,
        scd2_service=scd2_service,
        hold_repo=hold_repo,
    )

    # Initial load
    service.evaluate_and_process(
        records=[_create_customer(f"CUST-{i}") for i in range(1, 4)],
        processing_date=date(2026, 4, 1),
    )

    # Trigger hold with rich lineage arguments
    run_id = "run-lineage-999"
    src_id = "crm_eu_central"
    src_schema_fp = "sha256_src_schema_fingerprint"
    map_ver = "map_v3_20260401"
    canon_ver = "v1.0"
    in_fp = "sha256_input_payload_hash"

    records = [
        _create_customer("CUST-1", first_name="Lineage1"),
        _create_customer("CUST-2", first_name="Lineage2"),
    ]

    res = service.evaluate_and_process(
        records=records,
        processing_date=date(2026, 4, 10),
        run_id=run_id,
        source_id=src_id,
        source_schema_fingerprint=src_schema_fp,
        mapping_version_id=map_ver,
        canonical_schema_version=canon_ver,
        input_fingerprint=in_fp,
    )

    assert res.is_held is True
    hold_row = service.get_held_batch(res.hold_id)

    # Validate lineage in evidence
    evidence = hold_row.evidence or {}
    assert "onboarding_lineage" in evidence
    lineage = evidence["onboarding_lineage"]

    assert lineage["onboarding_run_id"] == run_id
    assert lineage["source_id"] == src_id
    assert lineage["source_schema_fingerprint"] == src_schema_fp
    assert lineage["mapping_version_id"] == map_ver
    assert lineage["canonical_schema_version"] == canon_ver
    assert lineage["input_fingerprint"] == in_fp
    assert lineage["change_report_summary"]["changed_count"] == 2


# ── 10. AI Explanation Integration & Resilience ───────────────


def test_ai_explanation_attached_without_altering_decision() -> None:
    history_repo = MonitoredEntityHistoryRepository(in_memory=True)
    hold_repo = HeldChangeBatchRepository(in_memory=True)
    scd2_service = CustomerSCD2Service(repository=history_repo)
    cfg = CustomerGuardrailConfig(
        max_change_volume=1,
        ai_explanation_enabled=True,
    )

    mock_explanation_service = MagicMock(spec=ExplanationService)
    mock_explanation_service.explain_batch.return_value = BatchExplanationResult(
        summary="High change volume detected: 3 customers updated in rapid succession.",
        what_changed="3 customers updated",
        why_flagged="Volume threshold exceeded",
        evidence_points=["3 changes > threshold 1"],
        validation_summary="SCD2 valid",
        containment_summary="Held in containment",
        decision="SUSPICIOUS",
        severity="HIGH",
        provider="gemini-mock",
    )

    service = CustomerHistoricalGuardrailService(
        config=cfg,
        scd2_service=scd2_service,
        hold_repo=hold_repo,
        explanation_service=mock_explanation_service,
    )

    # Initial load
    service.evaluate_and_process(
        records=[_create_customer(f"CUST-{i}") for i in range(1, 4)],
        processing_date=date(2026, 4, 1),
    )

    suspicious = [_create_customer(f"CUST-{i}", first_name="AI_Test") for i in range(1, 4)]
    res = service.evaluate_and_process(
        records=suspicious,
        processing_date=date(2026, 4, 10),
    )

    assert res.is_held is True
    assert res.explanation is not None
    assert "High change volume detected" in res.explanation["summary"]
    mock_explanation_service.explain_batch.assert_called_once()


def test_ai_explanation_failure_does_not_break_deterministic_hold() -> None:
    history_repo = MonitoredEntityHistoryRepository(in_memory=True)
    hold_repo = HeldChangeBatchRepository(in_memory=True)
    scd2_service = CustomerSCD2Service(repository=history_repo)
    cfg = CustomerGuardrailConfig(
        max_change_volume=1,
        ai_explanation_enabled=True,
    )

    # Mock explanation service that crashes with an API or network error
    mock_explanation_service = MagicMock(spec=ExplanationService)
    mock_explanation_service.explain_batch.side_effect = RuntimeError("Gemini API connection timeout")

    service = CustomerHistoricalGuardrailService(
        config=cfg,
        scd2_service=scd2_service,
        hold_repo=hold_repo,
        explanation_service=mock_explanation_service,
    )

    # Initial load
    service.evaluate_and_process(
        records=[_create_customer(f"CUST-{i}") for i in range(1, 4)],
        processing_date=date(2026, 4, 1),
    )

    suspicious = [_create_customer(f"CUST-{i}", first_name="AI_Fail") for i in range(1, 4)]
    # Service must NOT raise an exception; deterministic hold must proceed cleanly!
    res = service.evaluate_and_process(
        records=suspicious,
        processing_date=date(2026, 4, 10),
    )

    assert res.is_held is True
    assert res.decision == GuardrailDecisionType.SUSPICIOUS
    # Hold is still created
    assert res.hold_id is not None
    hold_row = service.get_held_batch(res.hold_id)
    assert hold_row.status == HoldStatus.HELD
    assert "Gemini API connection timeout" in hold_row.evidence.get("explanation_error", "")


# ── 11. OnboardingRunService Integration ───────────────────────


def test_onboarding_run_service_integrate_run_with_guardrail_normal(tmp_path: Path) -> None:
    history_repo = MonitoredEntityHistoryRepository(in_memory=True)
    hold_repo = HeldChangeBatchRepository(in_memory=True)
    scd2_service = CustomerSCD2Service(repository=history_repo)
    guardrail_service = CustomerHistoricalGuardrailService(
        scd2_service=scd2_service,
        hold_repo=hold_repo,
    )

    run_service = OnboardingRunService(
        artifacts_root=tmp_path / "artifacts",
        scd2_service=scd2_service,
        guardrail_service=guardrail_service,
    )

    # Setup mapping
    mapping = ApprovedMappingVersion(
        mapping_version_id="map_crm_v1",
        source_id="src_crm",
        source_fingerprint="fp123",
        approved_by="test_reviewer",
        is_complete=True,
        mappings=[
            ApprovedMappingDefinition(
                source_field="id",
                target_field="customer_id",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="admin@example.com",
                confidence=1.0,
                provenance_reason="ID direct",
            ),
            ApprovedMappingDefinition(
                source_field="fname",
                target_field="first_name",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="admin@example.com",
                confidence=1.0,
                provenance_reason="First name direct",
            ),
            ApprovedMappingDefinition(
                source_field="lname",
                target_field="last_name",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="admin@example.com",
                confidence=1.0,
                provenance_reason="Last name direct",
            ),
            ApprovedMappingDefinition(
                source_field="email_addr",
                target_field="email",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="admin@example.com",
                confidence=1.0,
                provenance_reason="Email direct",
            ),
            ApprovedMappingDefinition(
                source_field="dob",
                target_field="date_of_birth",
                mapping_type=MappingType.TRANSFORMED,
                transformations=[TransformationStep(op=TransformationOpType.PARSE_DATE)],
                decision=ReviewDecisionType.APPROVE,
                reviewer="admin@example.com",
                confidence=1.0,
                provenance_reason="DOB parse date",
            ),
            ApprovedMappingDefinition(
                source_field="cust_status",
                target_field="status",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="admin@example.com",
                confidence=1.0,
                provenance_reason="Status direct",
            ),
            ApprovedMappingDefinition(
                source_field="signup_date",
                target_field="created_at",
                mapping_type=MappingType.TRANSFORMED,
                transformations=[TransformationStep(op=TransformationOpType.PARSE_DATE)],
                decision=ReviewDecisionType.APPROVE,
                reviewer="admin@example.com",
                confidence=1.0,
                provenance_reason="Signup date to created_at",
            ),
        ],
    )
    run_service.register_approved_mapping(mapping)

    data = [
        {"id": "C-101", "fname": "John", "lname": "Doe", "email_addr": "john@example.com", "dob": "1985-01-01", "cust_status": "ACTIVE", "signup_date": "2026-01-01"},
        {"id": "C-102", "fname": "Jane", "lname": "Smith", "email_addr": "jane@example.com", "dob": "1988-02-02", "cust_status": "ACTIVE", "signup_date": "2026-01-01"},
    ]

    # Create run
    req = RunCreateRequest(
        source_id="src_crm",
        mapping_version_id=mapping.mapping_version_id,
        idempotency_key="idemp_norm_001",
        data_payload=data,
    )
    run, _ = run_service.submit_run(req)
    assert run.status in {RunStatus.COMPLETED, RunStatus.PARTIAL}

    # Integrate with guardrail
    res = run_service.integrate_run_with_guardrail(
        run_id=run.run_id,
        processing_date=date(2026, 4, 1),
    )

    assert res.decision == GuardrailDecisionType.NORMAL
    assert res.is_held is False
    assert res.persisted is True

    # Check that artifact was written
    eval_path = run.artifact_paths.extra.get("guardrail_evaluation_path")
    assert eval_path is not None
    assert Path(eval_path).exists()

    with open(eval_path, "r", encoding="utf-8") as f:
        saved_data = json.load(f)
    assert saved_data["decision"] == "NORMAL"
    assert saved_data["is_held"] is False

    # Check history committed
    assert len(history_repo.get_all_rows()) == 2


def test_onboarding_run_service_integrate_run_with_guardrail_suspicious(tmp_path: Path) -> None:
    history_repo = MonitoredEntityHistoryRepository(in_memory=True)
    hold_repo = HeldChangeBatchRepository(in_memory=True)
    scd2_service = CustomerSCD2Service(repository=history_repo)
    # Configure threshold: max 1 change allowed
    cfg = CustomerGuardrailConfig(max_change_volume=1)
    guardrail_service = CustomerHistoricalGuardrailService(
        config=cfg,
        scd2_service=scd2_service,
        hold_repo=hold_repo,
    )

    run_service = OnboardingRunService(
        artifacts_root=tmp_path / "artifacts",
        scd2_service=scd2_service,
        guardrail_service=guardrail_service,
    )

    mapping = ApprovedMappingVersion(
        mapping_version_id="map_crm_v1",
        source_id="src_crm",
        source_fingerprint="fp123",
        approved_by="test_reviewer",
        is_complete=True,
        mappings=[
            ApprovedMappingDefinition(
                source_field="id",
                target_field="customer_id",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="admin@example.com",
                confidence=1.0,
                provenance_reason="ID direct",
            ),
            ApprovedMappingDefinition(
                source_field="fname",
                target_field="first_name",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="admin@example.com",
                confidence=1.0,
                provenance_reason="First name direct",
            ),
            ApprovedMappingDefinition(
                source_field="lname",
                target_field="last_name",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="admin@example.com",
                confidence=1.0,
                provenance_reason="Last name direct",
            ),
            ApprovedMappingDefinition(
                source_field="email_addr",
                target_field="email",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="admin@example.com",
                confidence=1.0,
                provenance_reason="Email direct",
            ),
            ApprovedMappingDefinition(
                source_field="dob",
                target_field="date_of_birth",
                mapping_type=MappingType.TRANSFORMED,
                transformations=[TransformationStep(op=TransformationOpType.PARSE_DATE)],
                decision=ReviewDecisionType.APPROVE,
                reviewer="admin@example.com",
                confidence=1.0,
                provenance_reason="DOB parse date",
            ),
            ApprovedMappingDefinition(
                source_field="cust_status",
                target_field="status",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="admin@example.com",
                confidence=1.0,
                provenance_reason="Status direct",
            ),
            ApprovedMappingDefinition(
                source_field="signup_date",
                target_field="created_at",
                mapping_type=MappingType.TRANSFORMED,
                transformations=[TransformationStep(op=TransformationOpType.PARSE_DATE)],
                decision=ReviewDecisionType.APPROVE,
                reviewer="admin@example.com",
                confidence=1.0,
                provenance_reason="Signup date to created_at",
            ),
        ],
    )
    run_service.register_approved_mapping(mapping)

    # Initial Run (2 customers)
    data1 = [
        {"id": "C-1", "fname": "A", "lname": "X", "email_addr": "a@x.com", "dob": "1990-01-01", "cust_status": "ACTIVE", "signup_date": "2026-01-01"},
        {"id": "C-2", "fname": "B", "lname": "Y", "email_addr": "b@y.com", "dob": "1990-01-01", "cust_status": "ACTIVE", "signup_date": "2026-01-01"},
    ]
    req1 = RunCreateRequest(
        source_id="src_crm",
        mapping_version_id=mapping.mapping_version_id,
        idempotency_key="run_init",
        data_payload=data1,
    )
    run1, _ = run_service.submit_run(req1)
    # Relax threshold for initial load
    guardrail_service.config.max_change_volume = 100
    guardrail_service._apply_guardrail_thresholds(guardrail_service.guardrail_engine, guardrail_service.config)
    run_service.integrate_run_with_guardrail(run1.run_id, processing_date=date(2026, 4, 1))
    assert len(history_repo.get_all_rows()) == 2

    # Second Run (modifies both C-1 and C-2, with threshold back to 1)
    guardrail_service.config.max_change_volume = 1
    guardrail_service._apply_guardrail_thresholds(guardrail_service.guardrail_engine, guardrail_service.config)

    data2 = [
        {"id": "C-1", "fname": "A_Changed", "lname": "X", "email_addr": "a@x.com", "dob": "1990-01-01", "cust_status": "ACTIVE", "signup_date": "2026-01-01"},
        {"id": "C-2", "fname": "B_Changed", "lname": "Y", "email_addr": "b@y.com", "dob": "1990-01-01", "cust_status": "ACTIVE", "signup_date": "2026-01-01"},
    ]
    req2 = RunCreateRequest(
        source_id="src_crm",
        mapping_version_id=mapping.mapping_version_id,
        idempotency_key="run_update",
        data_payload=data2,
    )
    run2, _ = run_service.submit_run(req2)

    res2 = run_service.integrate_run_with_guardrail(run2.run_id, processing_date=date(2026, 4, 10))

    assert res2.is_held is True
    assert res2.decision == GuardrailDecisionType.SUSPICIOUS
    assert res2.persisted is False

    # Check history state: unchanged (still 2 rows with original names)
    history_rows = history_repo.get_all_rows()
    assert len(history_rows) == 2
    for r in history_rows:
        assert not r.attributes["first_name"].endswith("_Changed")


# ── 12. Multi-Domain Realistic Scenarios ──────────────────────


def test_realistic_crm_domain_scenario() -> None:
    """Simulates realistic enterprise CRM customer feed with status updates and contact revisions."""
    history_repo = MonitoredEntityHistoryRepository(in_memory=True)
    hold_repo = HeldChangeBatchRepository(in_memory=True)
    scd2_service = CustomerSCD2Service(repository=history_repo)
    cfg = CustomerGuardrailConfig(
        max_change_volume=10,
        max_population_impact_ratio=0.50,
        max_deactivation_ratio=0.30,
    )
    service = CustomerHistoricalGuardrailService(
        config=cfg,
        scd2_service=scd2_service,
        hold_repo=hold_repo,
    )

    # 1. Day 1: CRM onboarding of 20 accounts
    day1_records = [
        _create_customer(f"CRM-{1000+i}", first_name=f"Lead_{i}", status="ACTIVE")
        for i in range(20)
    ]
    res1 = service.evaluate_and_process(
        records=day1_records,
        processing_date=date(2026, 5, 1),
        source_id="salesforce_crm",
    )
    assert res1.is_held is False
    assert len(history_repo.get_all_rows()) == 20

    # 2. Day 2: Normal account maintenance: 3 email updates, 1 name fix, 1 status change
    day2_records = [
        _create_customer("CRM-1001", email="updated_email_1@corp.com", status="ACTIVE"),
        _create_customer("CRM-1002", email="updated_email_2@corp.com", status="ACTIVE"),
        _create_customer("CRM-1003", email="updated_email_3@corp.com", status="ACTIVE"),
        _create_customer("CRM-1004", first_name="Lead_4_Corrected", status="ACTIVE"),
        _create_customer("CRM-1005", status="SUSPENDED"),  # 1 deactivation out of 20 = 5%
    ] + [
        _create_customer(f"CRM-{1000+i}", first_name=f"Lead_{i}", status="ACTIVE")
        for i in range(6, 20)
    ]

    res2 = service.evaluate_and_process(
        records=day2_records,
        processing_date=date(2026, 5, 2),
        source_id="salesforce_crm",
    )
    assert res2.is_held is False
    assert res2.persisted is True
    # 5 changed, 15 unchanged -> 5 closed versions + 5 new versions = 10 rows + 15 unchanged = 25 total history rows
    assert len(history_repo.get_all_rows()) == 25

    # 3. Day 3: Malicious or faulty CRM script attempts to deactivate 15 accounts
    day3_tampered = [
        _create_customer(f"CRM-{1000+i}", status="INACTIVE") for i in range(15)
    ] + [
        _create_customer(f"CRM-{1000+i}", status="ACTIVE") for i in range(15, 20)
    ]

    res3 = service.evaluate_and_process(
        records=day3_tampered,
        processing_date=date(2026, 5, 3),
        source_id="salesforce_crm",
    )
    # Mass deactivation & high population impact caught!
    assert res3.is_held is True
    assert res3.persisted is False
    # History remains at 25 rows
    assert len(history_repo.get_all_rows()) == 25
