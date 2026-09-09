"""Tests for M3.3: Production-Grade Prefect Deployment and Execution Infrastructure.

Verifies:
- Canonical flow binding, deployment naming, and metadata
- Parameter schema safety (zero secrets, no DataFrames in parameters)
- Concurrency limit configuration (limit=1, collision_strategy=ENQUEUE)
- Deployment parameter validation and staging
- Deployment registration (apply) and programmatic triggering
- Deterministic parity between direct and reference file-based execution
- Failure semantics and AI isolation preservation
- CLI argument parsing and entrypoint
"""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import tempfile
from typing import Any
from uuid import UUID

import polars as pl
from prefect.client.orchestration import get_client
from prefect.client.schemas.objects import ConcurrencyLimitStrategy
from prefect.states import Cancelled, Completed, Pending, Running, Scheduled
from prefect.utilities.callables import parameter_schema
import pytest

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
    configure_prefect_api_url,
    _is_server_reachable,
    load_deployment_run_artifacts,
    poll_deployment_run_state,
    parse_args,
    stage_dataset,
    trigger_pipeline_run,
    reconcile_deployment_concurrency,
)
from src.scd2_copilot.exceptions import DuplicateBusinessKeyError
from src.scd2_copilot.workflow import run_pipeline


@pytest.fixture
def sample_csv_files(tmp_path: Path) -> tuple[Path, Path]:
    """Create sample source and target SCD2 CSV files."""
    source_path = tmp_path / "source_test.csv"
    target_path = tmp_path / "target_test.csv"

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


# ── 1. Deployment Specification & Naming ───────────────────
def test_deployment_spec_and_naming():
    """Verify deployment is bound to canonical flow with exact naming and metadata."""
    dep = build_deployment()

    assert dep.name == DEPLOYMENT_NAME
    assert dep.flow_name == FLOW_NAME
    assert dep.full_name == FULL_DEPLOYMENT_NAME
    assert dep.version == DEFAULT_VERSION
    assert set(DEFAULT_TAGS).issubset(set(dep.tags))
    assert "src\\scd2_copilot\\workflow.py:run_pipeline" in dep.entrypoint or "src/scd2_copilot/workflow.py:run_pipeline" in dep.entrypoint
    assert dep.concurrency_limit == CONCURRENCY_LIMIT
    assert dep.concurrency_options.collision_strategy == COLLISION_STRATEGY


# ── 2. Parameter Schema Safety (Zero Secrets / No DataFrames) ──
def test_parameter_schema_safety_no_secrets():
    """Verify parameter schema contains no secrets and requires file references."""
    schema = parameter_schema(run_pipeline.fn)
    schema_dump = schema.model_dump() if hasattr(schema, "model_dump") else schema.dict()
    schema_str = json.dumps(schema_dump)

    # Must NOT contain secrets or internal settings definitions
    assert "gemini_api_key" not in schema_str
    assert "groq_api_key" not in schema_str
    assert "openrouter_api_key" not in schema_str
    assert "Settings" not in schema_dump.get("definitions", {})

    # Properties must exist
    props = schema_dump["properties"]
    assert "source" in props
    assert "target" in props
    assert "processing_date" in props
    assert "business_key_override" in props
    assert "tracked_columns_override" in props
    assert "snapshot_mode" in props
    assert "delete_policy" in props


# ── 3. Deployment Parameters Validation ────────────────────
def test_deployment_parameters_validation():
    """Test strict validation of deployment input parameters."""
    # Valid parameters
    params = DeploymentParameters(
        source="data/source.csv",
        target="data/target.csv",
        processing_date="2026-09-07",
        business_key_override=["customer_id"],
        tracked_columns_override=["tier", "balance"],
        snapshot_mode="full",
        delete_policy="soft_delete",
        llm_provider="template",
    )
    assert params.source == "data/source.csv"
    assert params.processing_date == "2026-09-07"
    assert params.snapshot_mode == "full"
    assert params.delete_policy == "soft_delete"

    # Case normalization
    params_case = DeploymentParameters(
        source="data/source.csv",
        target="data/target.csv",
        snapshot_mode="INCREMENTAL",
        delete_policy="IGNORE",
        llm_provider="GEMINI",
    )
    assert params_case.snapshot_mode == "incremental"
    assert params_case.delete_policy == "ignore"
    assert params_case.llm_provider == "gemini"

    # Rejection of empty paths
    with pytest.raises(ValueError, match="Dataset path reference must not be empty"):
        DeploymentParameters(source="  ", target="data/target.csv")

    # Rejection of raw serialized data
    with pytest.raises(ValueError, match="Raw serialized datasets are not allowed"):
        DeploymentParameters(source='{"id": [1, 2]}', target="data/target.csv")

    # Rejection of invalid date format
    with pytest.raises(ValueError, match="ISO format"):
        DeploymentParameters(source="data/s.csv", target="data/t.csv", processing_date="09/07/2026")

    # Rejection of invalid snapshot mode
    with pytest.raises(ValueError, match="snapshot_mode must be"):
        DeploymentParameters(source="data/s.csv", target="data/t.csv", snapshot_mode="random_mode")

    # Rejection of invalid delete policy
    with pytest.raises(ValueError, match="delete_policy must be"):
        DeploymentParameters(source="data/s.csv", target="data/t.csv", delete_policy="hard_delete")

    # Rejection of unauthorized extra arguments
    with pytest.raises(ValueError):
        DeploymentParameters(source="data/s.csv", target="data/t.csv", api_key="secret")


# ── 4. Dataset Staging Helper ──────────────────────────────
def test_dataset_staging_helper(tmp_path: Path):
    """Test staging in-memory data to disk for deployment reference."""
    df = pl.DataFrame({"id": [1, 2], "val": ["x", "y"]})
    staged_path = stage_dataset(df, "test_staging_df", staging_dir=tmp_path)
    assert Path(staged_path).exists()
    loaded_df = pl.read_csv(staged_path)
    assert loaded_df.shape == (2, 2)

    # Test bytes staging
    raw_bytes = b"id,val\n3,z\n"
    staged_bytes = stage_dataset(raw_bytes, "test_staging_bytes", staging_dir=tmp_path)
    assert Path(staged_bytes).exists()
    loaded_bytes_df = pl.read_csv(staged_bytes)
    assert loaded_bytes_df.shape == (1, 2)


# ── 5. Deployment Registration & Apply ─────────────────────
def test_deployment_apply_and_persistence():
    """Verify deployment registration creates and stores deployment in Prefect."""
    dep_id = apply_deployment()
    assert isinstance(dep_id, UUID)

    async def verify_in_db():
        async with get_client() as client:
            dep = await client.read_deployment(dep_id)
            assert dep.name == DEPLOYMENT_NAME
            assert dep.concurrency_options.collision_strategy == ConcurrencyLimitStrategy.ENQUEUE
            assert set(DEFAULT_TAGS).issubset(set(dep.tags))

    import asyncio
    asyncio.run(verify_in_db())


# ── 6. Programmatic Triggering ─────────────────────────────
def test_programmatic_triggering(sample_csv_files: tuple[Path, Path]):
    """Verify trigger_pipeline_run creates a scheduled flow run with parameters."""
    source_path, target_path = sample_csv_files
    params = DeploymentParameters(
        source=str(source_path),
        target=str(target_path),
        processing_date="2026-09-07",
        business_key_override=["customer_id"],
        snapshot_mode="full",
        delete_policy="soft_delete",
        llm_provider="template",
    )

    flow_run = trigger_pipeline_run(params, timeout=0)
    assert flow_run.id is not None
    assert flow_run.state.name in {"Scheduled", "Pending", "Running"}
    assert flow_run.parameters["source"] == str(source_path)
    assert flow_run.parameters["target"] == str(target_path)
    assert flow_run.parameters["processing_date"] == "2026-09-07"


# ── 7. Execution Parity (Direct vs File Path Inputs) ────────
def test_execution_parity_file_inputs(sample_csv_files: tuple[Path, Path]):
    """Verify that passing file paths to run_pipeline produces deterministic parity."""
    source_path, target_path = sample_csv_files

    # 1. Run direct with DataFrames
    src_df = pl.read_csv(source_path)
    tgt_df = pl.read_csv(target_path)
    res_direct = run_pipeline(
        source=src_df,
        target=tgt_df,
        processing_date=date(2026, 9, 7),
        business_key_override=["customer_id"],
        snapshot_mode="full",
        delete_policy="soft_delete",
        llm_provider="template",
    )

    # 2. Run with string file paths (as in deployment worker)
    res_deployed = run_pipeline(
        source=str(source_path),
        target=str(target_path),
        processing_date="2026-09-07",
        business_key_override=["customer_id"],
        snapshot_mode="full",
        delete_policy="soft_delete",
        llm_provider="template",
    )

    # Parity assertions
    assert res_direct.change_report.summary == res_deployed.change_report.summary
    assert res_direct.scd2_output.shape == res_deployed.scd2_output.shape
    assert res_direct.validation_report.passed == res_deployed.validation_report.passed
    assert res_direct.business_key == res_deployed.business_key
    assert res_direct.tracked_columns == res_deployed.tracked_columns
    assert res_direct.orchestration_summary is not None
    assert res_deployed.orchestration_summary is not None
    assert res_direct.orchestration_summary.task_statuses["validate_output"] == "COMPLETED"
    assert res_deployed.orchestration_summary.task_statuses["validate_output"] == "COMPLETED"


# ── 8. Failure Semantics Preservation ──────────────────────
def test_failure_semantics_preservation(tmp_path: Path):
    """Verify deterministic failure semantics under file path execution."""
    src_file = tmp_path / "bad_source.csv"
    tgt_file = tmp_path / "bad_target.csv"

    # Duplicate business key in source
    pl.DataFrame({"id": [1, 1], "val": ["a", "b"]}).write_csv(src_file)
    pl.DataFrame({
        "id": [1], "val": ["a"],
        "effective_from": ["2026-01-01"], "effective_to": [None], "is_current": [True],
    }).write_csv(tgt_file)

    with pytest.raises(DuplicateBusinessKeyError):
        run_pipeline(
            source=str(src_file),
            target=str(tgt_file),
            processing_date="2026-09-07",
            business_key_override=["id"],
            llm_provider="template",
        )


# ── 9. AI Degradation Isolation in Deployed Execution ──────
def test_ai_degradation_isolation(sample_csv_files: tuple[Path, Path]):
    """Verify that template LLM provider maintains 100% deterministic success."""
    source_path, target_path = sample_csv_files
    result = run_pipeline(
        source=str(source_path),
        target=str(target_path),
        processing_date="2026-09-07",
        llm_provider="template",
    )
    assert result.validation_report.passed is True
    assert result.explain_result.provider_used == "template"
    assert result.orchestration_summary is not None
    assert result.orchestration_summary.ai_provider == "template"
    assert result.orchestration_summary.task_statuses["explain_changes"] == "COMPLETED"


# ── 10. CLI Interface Verification ────────────────────────
def test_cli_interface():
    """Verify CLI argument parsing handles commands and flags correctly."""
    # Test --apply
    args = parse_args(["--apply"])
    assert args.apply is True
    assert args.serve is False

    # Test --serve
    args = parse_args(["--serve"])
    assert args.serve is True

    # Test --trigger
    args = parse_args([
        "--trigger",
        "--source", "data/s.csv",
        "--target", "data/t.csv",
        "--date", "2026-09-07",
        "--business-keys", "cust_id", "reg_id",
        "--snapshot-mode", "incremental",
        "--delete-policy", "ignore",
        "--provider", "groq",
    ])
    assert args.trigger is True
    assert args.source == "data/s.csv"
    assert args.target == "data/t.csv"
    assert args.date == "2026-09-07"
    assert args.business_keys == ["cust_id", "reg_id"]
    assert args.snapshot_mode == "incremental"
    assert args.delete_policy == "ignore"
    assert args.provider == "groq"


# ── 11. Concurrency Reconciliation and Orphaned Run Recovery ─
def test_cli_queue_management_flags():
    """Verify CLI parses --clear-queue and --reconcile flags properly."""
    args_clear = parse_args(["--clear-queue"])
    assert args_clear.clear_queue is True
    assert args_clear.reconcile is False

    args_rec = parse_args(["--reconcile"])
    assert args_rec.reconcile is True
    assert args_rec.clear_queue is False


def test_reconcile_deployment_concurrency_recovers_orphaned_runs(sample_csv_files: tuple[Path, Path]):
    """Verify reconcile_deployment_concurrency marks stuck Submitting/Running runs as Crashed
    and cleans scheduled runs with missing files.
    """
    source_path, target_path = sample_csv_files
    dep_id = apply_deployment()

    with get_client(sync_client=True) as client:
        # Create a flow run in Running state (orphaned execution simulation)
        run_running = client.create_flow_run_from_deployment(
            deployment_id=dep_id,
            parameters={"source": str(source_path), "target": str(target_path)},
            state=Running(),
        )

        # Create a flow run in Pending state (orphaned submission simulation)
        run_pending = client.create_flow_run_from_deployment(
            deployment_id=dep_id,
            parameters={"source": str(source_path), "target": str(target_path)},
            state=Pending(),
        )

        # Create a flow run in Scheduled state pointing to a non-existent source file
        run_invalid_src = client.create_flow_run_from_deployment(
            deployment_id=dep_id,
            parameters={"source": "non_existent_source_dummy.csv", "target": str(target_path)},
            state=Scheduled(),
        )

        # Reconcile without clearing full queue
        summary = reconcile_deployment_concurrency(FULL_DEPLOYMENT_NAME, cancel_stale_scheduled=False)
        assert summary["crashed"] >= 2
        assert summary["cancelled"] >= 1

        # Check states in DB
        state_running = client.read_flow_run(run_running.id).state
        state_pending = client.read_flow_run(run_pending.id).state
        state_invalid = client.read_flow_run(run_invalid_src.id).state
        assert state_running.name == "Crashed"
        assert state_pending.name == "Crashed"
        assert state_invalid.name == "Cancelled"


def test_reconcile_deployment_concurrency_clear_queue(sample_csv_files: tuple[Path, Path]):
    """Verify reconcile_deployment_concurrency with cancel_stale_scheduled=True cancels queued runs."""
    source_path, target_path = sample_csv_files
    dep_id = apply_deployment()

    with get_client(sync_client=True) as client:
        run_scheduled = client.create_flow_run_from_deployment(
            deployment_id=dep_id,
            parameters={"source": str(source_path), "target": str(target_path)},
            state=Scheduled(),
        )

        summary = reconcile_deployment_concurrency(FULL_DEPLOYMENT_NAME, cancel_stale_scheduled=True)
        assert summary["cancelled"] >= 1

        state_scheduled = client.read_flow_run(run_scheduled.id).state
        assert state_scheduled.name == "Cancelled"


def test_repeated_deployment_apply_no_duplicate_schedules():
    """Verify repeated deployment apply calls maintain exactly one schedule."""
    apply_deployment(cron="0 2 * * *", timezone="UTC")
    apply_deployment(cron="0 2 * * *", timezone="UTC")

    with get_client(sync_client=True) as client:
        dep = client.read_deployment_by_name(FULL_DEPLOYMENT_NAME)
        assert len(dep.schedules) == 1
        assert dep.schedules[0].schedule.cron == "0 2 * * *"


# ── Regression: Ephemeral Server Isolation Fix ────────────


def test_configure_prefect_api_url_sets_env_var(monkeypatch: pytest.MonkeyPatch):
    """Verify configure_prefect_api_url writes PREFECT_API_URL to os.environ.

    Root cause regression: Without this, each process (serve parent, spawned
    flow subprocess, Streamlit) starts its own independent ephemeral Prefect
    server on a random port. The concurrency lease acquired on server A is
    invisible to server B, causing 'concurrency slot lost' failures.
    """
    import os

    # Clear any pre-existing value
    monkeypatch.delenv("PREFECT_API_URL", raising=False)

    url = configure_prefect_api_url("http://127.0.0.1:9999/api")
    assert url == "http://127.0.0.1:9999/api"
    assert os.environ["PREFECT_API_URL"] == "http://127.0.0.1:9999/api"


def test_configure_prefect_api_url_reads_from_settings(monkeypatch: pytest.MonkeyPatch):
    """Verify configure_prefect_api_url reads from Settings when no arg is given."""
    import os

    monkeypatch.delenv("PREFECT_API_URL", raising=False)

    url = configure_prefect_api_url()
    # Default from Settings is http://127.0.0.1:4200/api
    assert url == "http://127.0.0.1:4200/api"
    assert os.environ["PREFECT_API_URL"] == "http://127.0.0.1:4200/api"


def test_is_server_reachable_returns_false_for_unreachable():
    """Verify _is_server_reachable returns False for a non-existent server."""
    result = _is_server_reachable("http://127.0.0.1:59999/api", timeout=0.5)
    assert result is False


def test_settings_has_prefect_api_url_field(monkeypatch: pytest.MonkeyPatch):
    """Verify the Settings model exposes prefect_api_url with the standard default."""
    from src.scd2_copilot.config import Settings

    monkeypatch.delenv("PREFECT_API_URL", raising=False)
    s = Settings()
    assert hasattr(s, "prefect_api_url")
    assert s.prefect_api_url == "http://127.0.0.1:4200/api"


def test_configure_prefect_api_url_overrides_existing(monkeypatch: pytest.MonkeyPatch):
    """Verify configure_prefect_api_url overwrites a stale PREFECT_API_URL value."""
    import os

    monkeypatch.setenv("PREFECT_API_URL", "http://stale:9999/api")
    url = configure_prefect_api_url("http://127.0.0.1:4200/api")
    assert url == "http://127.0.0.1:4200/api"
    assert os.environ["PREFECT_API_URL"] == "http://127.0.0.1:4200/api"


# ── Regression: Streamlit Deployment UI Polling & Artifact Loading ──


def test_load_deployment_run_artifacts_completed(source_df: pl.DataFrame, target_df: pl.DataFrame, tmp_path: Path):
    """Verify load_deployment_run_artifacts reconstructs all artifacts for a completed flow run."""
    staged_src = stage_dataset(source_df, "test_load_src", staging_dir=tmp_path / "stg")
    staged_tgt = stage_dataset(target_df, "test_load_tgt", staging_dir=tmp_path / "stg")
    apply_deployment()

    params = DeploymentParameters(
        source=staged_src,
        target=staged_tgt,
        processing_date="2026-06-08",
        business_key_override=["customer_id"],
        tracked_columns_override=["name", "city", "tier"],
        llm_provider="template",
    )

    flow_run = trigger_pipeline_run(params, timeout=0)

    # Execute pipeline to persist artifacts
    run_pipeline(
        source=staged_src,
        target=staged_tgt,
        processing_date="2026-06-08",
        business_key_override=["customer_id"],
        tracked_columns_override=["name", "city", "tier"],
        llm_provider="template",
    )

    # Transition flow run to Completed
    with get_client(sync_client=True) as client:
        client.set_flow_run_state(flow_run.id, Completed(), force=True)

    state_name, err, persisted = load_deployment_run_artifacts(flow_run.id)
    assert state_name == "Completed"
    assert err is None
    assert persisted is not None
    assert persisted.scd2_output.height == 5
    assert persisted.change_report.summary["new"] == 1
    assert persisted.change_report.summary["changed"] == 1
    assert persisted.validation_report.passed is True
    assert len(persisted.explain_result.explanations) == 2


def test_load_deployment_run_artifacts_reused_idempotent(source_df: pl.DataFrame, target_df: pl.DataFrame, tmp_path: Path):
    """Verify load_deployment_run_artifacts resolves reused runs via execution fingerprint (M3.6)."""
    staged_src = stage_dataset(source_df, "test_reused_src", staging_dir=tmp_path / "stg")
    staged_tgt = stage_dataset(target_df, "test_reused_tgt", staging_dir=tmp_path / "stg")
    apply_deployment()

    params = DeploymentParameters(
        source=staged_src,
        target=staged_tgt,
        processing_date="2026-06-08",
        business_key_override=["customer_id"],
        tracked_columns_override=["name", "city", "tier"],
        llm_provider="template",
    )

    # 1. First run -> NEW_EXECUTION
    fr1 = trigger_pipeline_run(params, timeout=0)
    run_pipeline(
        source=staged_src,
        target=staged_tgt,
        processing_date="2026-06-08",
        business_key_override=["customer_id"],
        tracked_columns_override=["name", "city", "tier"],
        llm_provider="template",
    )
    with get_client(sync_client=True) as client:
        client.set_flow_run_state(fr1.id, Completed(), force=True)

    state1, err1, p1 = load_deployment_run_artifacts(fr1.id)
    assert state1 == "Completed"
    assert p1 is not None

    # 2. Second run -> REUSED_EXECUTION
    fr2 = trigger_pipeline_run(params, timeout=0)
    res2 = run_pipeline(
        source=staged_src,
        target=staged_tgt,
        processing_date="2026-06-08",
        business_key_override=["customer_id"],
        tracked_columns_override=["name", "city", "tier"],
        llm_provider="template",
    )
    assert res2.orchestration_summary.is_reused is True
    with get_client(sync_client=True) as client:
        client.set_flow_run_state(fr2.id, Completed(), force=True)

    state2, err2, p2 = load_deployment_run_artifacts(fr2.id)
    assert state2 == "Completed"
    assert err2 is None
    assert p2 is not None
    assert p2.scd2_output.height == p1.scd2_output.height
    assert p2.metadata.execution_fingerprint == p1.metadata.execution_fingerprint


def test_load_deployment_run_artifacts_non_completed(source_df: pl.DataFrame, target_df: pl.DataFrame, tmp_path: Path):
    """Verify load_deployment_run_artifacts returns (state, message, None) for non-completed runs."""
    staged_src = stage_dataset(source_df, "test_noncomp_src", staging_dir=tmp_path / "stg")
    staged_tgt = stage_dataset(target_df, "test_noncomp_tgt", staging_dir=tmp_path / "stg")
    apply_deployment()

    params = DeploymentParameters(
        source=staged_src,
        target=staged_tgt,
        processing_date="2026-06-08",
        business_key_override=["customer_id"],
        tracked_columns_override=["name", "city", "tier"],
        llm_provider="template",
    )

    # timeout=0 returns immediately while in Scheduled state
    flow_run = trigger_pipeline_run(params, timeout=0)
    assert not flow_run.state.is_completed()

    state_name, msg, persisted = load_deployment_run_artifacts(flow_run.id)
    assert state_name in ("Scheduled", "Pending")
    assert persisted is None


def test_poll_deployment_run_state_success(source_df: pl.DataFrame, target_df: pl.DataFrame, tmp_path: Path):
    """Verify poll_deployment_run_state successfully returns completed state and invokes callback."""
    staged_src = stage_dataset(source_df, "test_poll_src", staging_dir=tmp_path / "stg")
    staged_tgt = stage_dataset(target_df, "test_poll_tgt", staging_dir=tmp_path / "stg")
    apply_deployment()

    params = DeploymentParameters(
        source=staged_src,
        target=staged_tgt,
        processing_date="2026-06-08",
        business_key_override=["customer_id"],
        tracked_columns_override=["name", "city", "tier"],
        llm_provider="template",
    )

    flow_run = trigger_pipeline_run(params, timeout=0)
    run_pipeline(
        source=staged_src,
        target=staged_tgt,
        processing_date="2026-06-08",
        business_key_override=["customer_id"],
        tracked_columns_override=["name", "city", "tier"],
        llm_provider="template",
    )
    with get_client(sync_client=True) as client:
        client.set_flow_run_state(flow_run.id, Completed(), force=True)

    callback_states: list[str] = []
    def on_state(name: str, msg: Optional[str]):
        callback_states.append(name)

    term_state, err, persisted = poll_deployment_run_state(
        flow_run_id=flow_run.id,
        timeout=10.0,
        poll_interval=0.2,
        status_callback=on_state,
    )

    assert term_state == "Completed"
    assert err is None
    assert persisted is not None
    assert persisted.scd2_output.height == 5
    assert "Completed" in callback_states


def test_poll_deployment_run_state_cancelled(source_df: pl.DataFrame, target_df: pl.DataFrame, tmp_path: Path):
    """Verify poll_deployment_run_state recognizes Cancelled flow runs and captures error message."""
    staged_src = stage_dataset(source_df, "test_cancel_src", staging_dir=tmp_path / "stg")
    staged_tgt = stage_dataset(target_df, "test_cancel_tgt", staging_dir=tmp_path / "stg")
    apply_deployment()

    params = DeploymentParameters(
        source=staged_src,
        target=staged_tgt,
        processing_date="2026-06-08",
        business_key_override=["customer_id"],
        tracked_columns_override=["name", "city", "tier"],
        llm_provider="template",
    )

    flow_run = trigger_pipeline_run(params, timeout=0)

    # Cancel the flow run
    with get_client(sync_client=True) as client:
        client.set_flow_run_state(flow_run.id, Cancelled(message="User cancelled test run"), force=True)

    term_state, err_msg, persisted = poll_deployment_run_state(
        flow_run_id=flow_run.id,
        timeout=10.0,
        poll_interval=0.2,
    )

    assert term_state == "Cancelled"
    assert persisted is None
    assert "User cancelled" in (err_msg or "")


