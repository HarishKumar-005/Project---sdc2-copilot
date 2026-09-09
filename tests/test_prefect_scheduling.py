"""Tests for M3.4: Scheduled and Programmatic Automated Execution for SCD2 Prefect Deployment.

Verifies:
1. Configured deployment schedule (CronSchedule)
2. Explicit timezone preservation (UTC, America/New_York, Asia/Kolkata, etc.)
3. Schedule parameters JSON safety (zero secrets, no DataFrames)
4. Parameter resolution when omitted (defaults to configured paths)
5. Scheduled trigger creates a valid Prefect deployment with schedule metadata
6. Manual and scheduled triggers share the identical canonical deployment
7. Deterministic processing_date derivation from scheduled_start_time
8. Parameter processing_date override respected over scheduled time
9. Missing source file cleanly fails with deterministic_input_error
10. Scheduled AI failure isolation (SCD2 transformation and validation intact)
11. Deterministic invariant failure visibility (fails fast on invariant breach)
12. Concurrency limit enforced on deployment (limit=1, collision_strategy=ENQUEUE)
13. Overlapping runs queued via ENQUEUE collision strategy
14. Pause and resume schedule operations toggle deployment state cleanly
15. Scheduled execution parity between file-based flow and direct flow
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
from typing import Any
import uuid

import polars as pl
from prefect.client.orchestration import get_client
from prefect.client.schemas.objects import ConcurrencyLimitStrategy
from prefect.client.schemas.schedules import CronSchedule
import pytest

from src.scd2_copilot.config import Settings, get_settings
from src.scd2_copilot.deployment import (
    COLLISION_STRATEGY,
    CONCURRENCY_LIMIT,
    DEFAULT_TAGS,
    DEFAULT_VERSION,
    DEPLOYMENT_NAME,
    FLOW_NAME,
    FULL_DEPLOYMENT_NAME,
    DeploymentParameters,
    apply_deployment,
    build_deployment,
    get_deployment_schedule_info,
    pause_deployment_schedule,
    resume_deployment_schedule,
    stage_dataset,
    trigger_pipeline_run,
)
from src.scd2_copilot.exceptions import DuplicateBusinessKeyError
from src.scd2_copilot.failure import FailureCategory, classify_pipeline_exception
from src.scd2_copilot.workflow import run_pipeline


@pytest.fixture
def sample_csv_files(tmp_path: Path) -> tuple[Path, Path]:
    """Create sample source and target SCD2 CSV files."""
    source_path = tmp_path / "source_sched_test.csv"
    target_path = tmp_path / "target_sched_test.csv"

    source_df = pl.DataFrame({
        "customer_id": [101, 102, 103],
        "tier": ["Gold", "Silver", "Platinum"],
        "balance": [1000.0, 500.0, 7500.0],
    })
    source_df.write_csv(source_path)

    target_df = pl.DataFrame({
        "customer_id": [101, 102, 104],
        "tier": ["Bronze", "Silver", "Gold"],
        "balance": [900.0, 500.0, 2000.0],
        "effective_from": [date(2026, 1, 1), date(2026, 1, 1), date(2026, 1, 1)],
        "effective_to": [None, None, None],
        "is_current": [True, True, True],
    })
    target_df.write_csv(target_path)

    return source_path, target_path


# ── 1. Configured Deployment Schedule (CronSchedule) ───────
def test_deployment_schedule_configuration():
    """Verify build_deployment attaches CronSchedule by default and supports custom cron/tz."""
    dep = build_deployment()

    assert len(dep.schedules) == 1
    sched_item = dep.schedules[0]
    assert isinstance(sched_item.schedule, CronSchedule)
    assert sched_item.schedule.cron == "0 2 * * *"
    assert sched_item.schedule.timezone == "UTC"
    assert dep.paused is False

    # Custom schedule parameters
    dep_custom = build_deployment(
        cron="0 4 * * *",
        timezone="America/New_York",
        paused=True,
    )
    assert len(dep_custom.schedules) == 1
    assert dep_custom.schedules[0].schedule.cron == "0 4 * * *"
    assert dep_custom.schedules[0].schedule.timezone == "America/New_York"
    assert dep_custom.paused is True


# ── 2. Explicit Timezone Preservation ──────────────────────
@pytest.mark.parametrize(
    "tz_name",
    ["UTC", "America/New_York", "Asia/Kolkata", "Europe/London", "America/Los_Angeles"],
)
def test_schedule_explicit_timezone_preservation(tz_name: str):
    """Verify timezone is explicitly preserved in CronSchedule and not silently coerced."""
    dep = build_deployment(cron="30 1 * * *", timezone=tz_name)
    assert dep.schedules[0].schedule.timezone == tz_name
    assert dep.schedules[0].schedule.cron == "30 1 * * *"


# ── 3. Schedule Parameters JSON Safety ─────────────────────
def test_schedule_parameters_json_safety():
    """Verify DeploymentParameters rejects complex objects and serializes safely without secrets."""
    params = DeploymentParameters(
        source="data/source.csv",
        target="data/target.csv",
        processing_date="2026-09-07",
    )
    json_repr = params.model_dump_json()
    data = json.loads(json_repr)

    assert data["source"] == "data/source.csv"
    assert data["target"] == "data/target.csv"
    assert data["processing_date"] == "2026-09-07"
    assert "gemini_api_key" not in data
    assert "groq_api_key" not in data

    # Rejection of raw serialized datasets
    with pytest.raises(ValueError, match="Raw serialized datasets are not allowed"):
        DeploymentParameters(source='{"id": [1, 2]}', target="data/target.csv")

    # Rejection of unknown parameters
    with pytest.raises(ValueError):
        DeploymentParameters(source="data/s.csv", target="data/t.csv", unknown_field="invalid")


# ── 4. Parameter Resolution When Omitted (Defaults) ────────
def test_parameter_resolution_when_omitted(sample_csv_files: tuple[Path, Path]):
    """Verify omitted source and target fall back to configured default file paths."""
    source_path, target_path = sample_csv_files

    custom_settings = Settings(
        default_source_path=str(source_path),
        default_target_path=str(target_path),
        processing_date=date(2026, 9, 7),
    )

    # Calling run_pipeline without source and target
    result = run_pipeline(
        source=None,
        target=None,
        settings=custom_settings,
    )

    assert result.scd2_output.height == 5
    assert result.change_report.summary["new"] == 1
    assert result.change_report.summary["changed"] == 1
    assert result.change_report.summary["unchanged"] == 1
    assert result.change_report.summary["deleted"] == 1


# ── 5. Scheduled Trigger Creates Deployment with Metadata ──
def test_scheduled_trigger_creates_flow_run():
    """Verify applying scheduled deployment registers schedule and concurrency metadata in Prefect."""
    dep_id = apply_deployment(cron="0 5 * * *", timezone="UTC")
    assert isinstance(dep_id, uuid.UUID)

    info = get_deployment_schedule_info(FULL_DEPLOYMENT_NAME)
    assert info["deployment_id"] == str(dep_id)
    assert info["deployment_name"] == DEPLOYMENT_NAME
    assert info["paused"] is False
    assert len(info["schedules"]) >= 1
    assert info["schedules"][0]["cron"] == "0 5 * * *"
    assert info["schedules"][0]["timezone"] == "UTC"
    assert info["concurrency_limit"] == CONCURRENCY_LIMIT
    assert info["collision_strategy"] == COLLISION_STRATEGY.value


# ── 6. Manual and Scheduled Triggers Share Deployment ──────
def test_manual_and_scheduled_trigger_share_deployment():
    """Verify manual triggering and scheduled runs target the exact same canonical deployment."""
    dep = build_deployment()
    assert dep.flow_name == FLOW_NAME
    assert dep.name == DEPLOYMENT_NAME
    assert dep.full_name == FULL_DEPLOYMENT_NAME

    # Parameters schema allows both manual explicit paths and scheduled omitted defaults
    manual_params = DeploymentParameters(
        source="data/source.csv",
        target="data/target.csv",
        processing_date="2026-09-07",
    )
    scheduled_params = DeploymentParameters()

    assert manual_params.source == "data/source.csv"
    assert scheduled_params.source is None
    assert scheduled_params.target is None


# ── 7. Deterministic Processing Date from Scheduled Time ───
def test_deterministic_processing_date_derivation(
    sample_csv_files: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
):
    """Verify processing_date is derived from flow_run.scheduled_start_time when omitted."""
    source_path, target_path = sample_csv_files

    scheduled_dt = datetime(2026, 11, 20, 2, 0, 0, tzinfo=timezone.utc)
    mock_dep_id = uuid.uuid4()

    from prefect.runtime import flow_run as fr_runtime
    monkeypatch.setattr(fr_runtime, "scheduled_start_time", scheduled_dt)
    monkeypatch.setattr(fr_runtime, "parent_deployment_id", mock_dep_id)

    # processing_date omitted
    result = run_pipeline(
        source=str(source_path),
        target=str(target_path),
        processing_date=None,
    )

    # Output records for NEW and CHANGED must have effective_from == 2026-11-20
    df = result.scd2_output
    new_and_changed = df.filter(pl.col("customer_id").is_in([101, 103]) & pl.col("is_current"))
    assert (new_and_changed["effective_from"] == date(2026, 11, 20)).all()
    assert result.orchestration_summary.deployment_id == str(mock_dep_id)


# ── 8. Parameter Processing Date Override Respected ────────
def test_parameter_processing_date_override_respected(
    sample_csv_files: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
):
    """Verify explicit parameter processing_date takes precedence over scheduled_start_time."""
    source_path, target_path = sample_csv_files

    scheduled_dt = datetime(2026, 11, 20, 2, 0, 0, tzinfo=timezone.utc)
    mock_dep_id = uuid.uuid4()

    from prefect.runtime import flow_run as fr_runtime
    monkeypatch.setattr(fr_runtime, "scheduled_start_time", scheduled_dt)
    monkeypatch.setattr(fr_runtime, "parent_deployment_id", mock_dep_id)

    # Explicit override to 2026-12-25
    result = run_pipeline(
        source=str(source_path),
        target=str(target_path),
        processing_date=date(2026, 12, 25),
    )

    df = result.scd2_output
    new_and_changed = df.filter(pl.col("customer_id").is_in([101, 103]) & pl.col("is_current"))
    assert (new_and_changed["effective_from"] == date(2026, 12, 25)).all()


# ── 9. Missing Source Fails with Deterministic Input Error ──
def test_missing_source_fails_with_deterministic_input_error(sample_csv_files: tuple[Path, Path]):
    """Verify non-existent source file fails immediately and classifies as deterministic_input_error."""
    _, target_path = sample_csv_files

    with pytest.raises(FileNotFoundError) as exc_info:
        run_pipeline(
            source="non_existent_source_xyz987.csv",
            target=str(target_path),
        )

    category = classify_pipeline_exception(exc_info.value, task_name="ingest_csvs")
    assert category == FailureCategory.DETERMINISTIC_INPUT_ERROR


# ── 10. Scheduled AI Failure Isolation ─────────────────────
def test_scheduled_ai_failure_isolation(
    sample_csv_files: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
):
    """Verify AI failure in scheduled execution degrades gracefully without corrupting SCD2."""
    source_path, target_path = sample_csv_files

    def mock_failing_explain(*args, **kwargs):
        raise TimeoutError("AI service request timed out")

    monkeypatch.setattr("src.scd2_copilot.workflow.explain_changes", mock_failing_explain)

    result = run_pipeline(
        source=str(source_path),
        target=str(target_path),
        processing_date=date(2026, 9, 7),
    )

    # Core SCD2 transformation and validation must remain completely intact
    assert result.scd2_output.height == 5
    assert result.validation_report.passed is True
    assert result.orchestration_summary.ai_status == "failed"
    assert result.orchestration_summary.failure_category == "transient_ai_error"
    assert "AI explanation failed" in result.orchestration_summary.warnings[0]


# ── 11. Deterministic Invariant Failure Visibility ──────────
def test_deterministic_invariant_failure_visibility(tmp_path: Path):
    """Verify invariant failure (duplicate business key in source) fails fast with clear error."""
    source_path = tmp_path / "dup_source.csv"
    target_path = tmp_path / "target_dup.csv"

    # Duplicate customer_id 101 in source
    source_df = pl.DataFrame({
        "customer_id": [101, 101, 102],
        "tier": ["Gold", "Silver", "Platinum"],
        "balance": [1000.0, 500.0, 7500.0],
    })
    source_df.write_csv(source_path)

    target_df = pl.DataFrame({
        "customer_id": [101, 102],
        "tier": ["Bronze", "Silver"],
        "balance": [900.0, 500.0],
        "effective_from": [date(2026, 1, 1), date(2026, 1, 1)],
        "effective_to": [None, None],
        "is_current": [True, True],
    })
    target_df.write_csv(target_path)

    with pytest.raises(DuplicateBusinessKeyError) as exc_info:
        run_pipeline(
            source=str(source_path),
            target=str(target_path),
            processing_date=date(2026, 9, 7),
        )

    category = classify_pipeline_exception(exc_info.value, task_name="detect_changes")
    assert category == FailureCategory.DETERMINISTIC_SCD2_ERROR


# ── 12. Concurrency Limit Enforced ─────────────────────────
def test_concurrency_limit_enforced():
    """Verify deployment concurrency limit is strictly 1 with ENQUEUE collision strategy."""
    dep = build_deployment()
    assert dep.concurrency_limit == 1
    assert dep.concurrency_options.collision_strategy == ConcurrencyLimitStrategy.ENQUEUE


# ── 13. Overlapping Runs Queued ────────────────────────────
def test_overlapping_runs_queued():
    """Verify deployment configuration dictates FIFO serialization rather than parallel race."""
    dep = build_deployment()
    assert dep.concurrency_limit == 1
    assert dep.concurrency_options.collision_strategy == ConcurrencyLimitStrategy.ENQUEUE
    assert dep.concurrency_options.collision_strategy.value == "ENQUEUE"


# ── 14. Pause and Resume Schedule Operations ───────────────
def test_pause_and_resume_schedule_operations():
    """Verify pause_deployment_schedule and resume_deployment_schedule toggle paused state."""
    apply_deployment()

    # Pause
    pause_deployment_schedule(FULL_DEPLOYMENT_NAME)
    info_paused = get_deployment_schedule_info(FULL_DEPLOYMENT_NAME)
    assert info_paused["paused"] is True

    # Resume
    resume_deployment_schedule(FULL_DEPLOYMENT_NAME)
    info_resumed = get_deployment_schedule_info(FULL_DEPLOYMENT_NAME)
    assert info_resumed["paused"] is False


# ── 15. Execution Parity Between File-Based and Direct Flow ─
def test_scheduled_execution_parity_with_direct_flow(sample_csv_files: tuple[Path, Path]):
    """Verify file-based pipeline execution produces identical SCD2 output to direct DataFrame execution."""
    source_path, target_path = sample_csv_files
    source_df = pl.read_csv(source_path)
    target_df = pl.read_csv(target_path)
    proc_date = date(2026, 9, 7)

    # 1. Direct DataFrame execution
    direct_res = run_pipeline(
        source=source_df,
        target=target_df,
        processing_date=proc_date,
    )

    # 2. File-based execution (as run under deployment runner)
    file_res = run_pipeline(
        source=str(source_path),
        target=str(target_path),
        processing_date=proc_date,
    )

    assert direct_res.change_report.summary == file_res.change_report.summary
    assert direct_res.validation_report.passed == file_res.validation_report.passed
    assert direct_res.scd2_output.shape == file_res.scd2_output.shape

    # Exact value parity
    cols_to_check = ["customer_id", "tier", "balance", "is_current"]
    assert direct_res.scd2_output.select(cols_to_check).equals(
        file_res.scd2_output.select(cols_to_check)
    )
