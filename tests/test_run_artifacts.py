"""Tests for M3.5: Persistent SCD2 Run Artifacts and Execution Result Retrieval.

Covers:
1. Config settings for runs directory, artifact persistence toggle, and path resolution.
2. Failure category classification for PERSISTENCE_ERROR.
3. OrchestrationSummary model extension with artifact metadata fields.
4. Full artifact persistence bundle writing (metadata.json, scd2_output.parquet, changes.json, validation.json, explanations.json).
5. Atomic staging and cleanup on failure (no partial corrupted runs).
6. Overwrite protection (raises FileExistsError when overwrite=False).
7. Overwrite allowed when overwrite=True.
8. Columnar parquet schema and type preservation across roundtrip (Date, Boolean, numeric, string, nulls).
9. Exact row count and column count parity.
10. Accurate ChangeReport reconstruction.
11. Accurate ValidationReport reconstruction.
12. Accurate ExplainResult and LLMMetrics reconstruction.
13. Fast metadata retrieval (get_run_metadata) without reading Parquet table into memory.
14. list_runs returns sorted runs and ignores .tmp_* directories.
15. run_exists correctly identifies valid runs.
16. Seamless workflow integration in run_pipeline with OrchestrationSummary population.
17. Persistence failure isolation in run_pipeline without corrupting SCD2 transformation.
18. Streamlit readback roundtrip simulation.
"""

from __future__ import annotations

from datetime import date, datetime
import json
import os
from pathlib import Path
from typing import Any
import uuid

import polars as pl
import pytest

from src.scd2_copilot.config import Settings, get_settings
from src.scd2_copilot.explain import ExplainResult
from src.scd2_copilot.failure import FailureCategory, classify_pipeline_exception
from src.scd2_copilot.models import (
    ChangeRecord,
    ChangeReport,
    ChangeType,
    DeletePolicy,
    Explanation,
    FieldChange,
    LLMMetrics,
    OrchestrationSummary,
    PipelineResult,
    SnapshotMode,
    ValidationReport,
    ValidationRule,
    ValidationStatus,
)


def test_config_artifact_settings(tmp_path: Path):
    """Settings should provide runs_directory, persist_artifacts, and get_runs_dir."""
    settings = Settings(runs_directory=str(tmp_path / "custom_runs"), persist_artifacts=True)
    assert settings.persist_artifacts is True
    assert settings.get_runs_dir() == tmp_path / "custom_runs"


def test_failure_classification_persistence_error():
    """classify_pipeline_exception classifies persistence errors under FailureCategory.PERSISTENCE_ERROR."""
    assert FailureCategory.PERSISTENCE_ERROR.value == "persistence_error"

    exc = IOError("Disk full while writing artifact scd2_output.parquet")
    cat = classify_pipeline_exception(exc, task_name="persist_artifacts")
    assert cat == FailureCategory.PERSISTENCE_ERROR

    exc_msg = PermissionError("Cannot access persistence directory")
    cat_msg = classify_pipeline_exception(exc_msg)
    assert cat_msg == FailureCategory.PERSISTENCE_ERROR


def test_orchestration_summary_artifact_fields():
    """OrchestrationSummary must include artifact metadata fields with valid defaults."""
    summary = OrchestrationSummary()
    assert summary.run_id is None
    assert summary.artifact_status == "none"
    assert summary.artifact_directory is None
    assert summary.artifact_files == []
    assert summary.persistence_duration_seconds == 0.0


def test_artifacts_module_imports():
    """src.scd2_copilot.artifacts should export RunMetadata, PersistedRun, and core persistence functions."""
    from src.scd2_copilot.artifacts import (
        PersistedRun,
        RunMetadata,
        delete_run,
        get_run_metadata,
        list_runs,
        read_run_artifacts,
        run_exists,
        write_run_artifacts,
    )
    assert callable(write_run_artifacts)
    assert callable(read_run_artifacts)
    assert callable(get_run_metadata)
    assert callable(run_exists)
    assert callable(list_runs)
    assert callable(delete_run)


@pytest.fixture
def sample_pipeline_result() -> PipelineResult:
    """Fixture providing a complete, populated PipelineResult."""
    scd2_df = pl.DataFrame({
        "customer_id": [101, 101, 102, 103],
        "name": ["Ravi", "Ravi", "Priya", "Arun"],
        "city": ["Chennai", "Bengaluru", "Mumbai", "Delhi"],
        "tier": ["Gold", "Gold", "Silver", "Platinum"],
        "effective_from": [date(2026, 6, 7), date(2026, 6, 8), date(2026, 6, 7), date(2026, 6, 8)],
        "effective_to": [date(2026, 6, 8), None, None, None],
        "is_current": [False, True, True, True],
    })

    change_report = ChangeReport(
        new=[ChangeRecord(business_key_values={"customer_id": 103}, change_type=ChangeType.NEW)],
        changed=[
            ChangeRecord(
                business_key_values={"customer_id": 101},
                change_type=ChangeType.CHANGED,
                field_changes=[FieldChange(column="city", old_value="Chennai", new_value="Bengaluru")],
            )
        ],
        unchanged=[ChangeRecord(business_key_values={"customer_id": 102}, change_type=ChangeType.UNCHANGED)],
        deleted=[],
        processing_date=date(2026, 6, 8),
        snapshot_mode=SnapshotMode.FULL,
        delete_policy=DeletePolicy.SOFT_DELETE,
    )

    val_rules = [
        ValidationRule(name="no_null_keys", status=ValidationStatus.PASS, message="No null keys found"),
        ValidationRule(name="one_current_version", status=ValidationStatus.PASS, message="Exactly one current version"),
        ValidationRule(name="temporal_intervals", status=ValidationStatus.PASS, message="Validity intervals valid"),
    ]
    val_report = ValidationReport(rules=val_rules)

    explanations = [
        Explanation(
            business_key_values={"customer_id": 101},
            change_type=ChangeType.CHANGED,
            text="Customer moved from Chennai to Bengaluru.",
            provider="template",
        )
    ]
    metrics = LLMMetrics(
        provider="template",
        model="template-v1",
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        estimated_cost=0.0,
        request_duration=0.01,
        num_changes_explained=1,
        avg_tokens_per_change=0.0,
    )
    exp_result = ExplainResult(
        explanations=explanations,
        warnings=[],
        provider_used="template",
        metrics=metrics,
    )

    summary = OrchestrationSummary(
        flow_run_id="flw-12345",
        flow_run_name="sunny-badger",
        deployment_id="dep-67890",
        deployment_name="scd2_pipeline/local-processing",
        trigger_type="scheduled",
        started_at=datetime(2026, 6, 8, 2, 0, 0),
        completed_at=datetime(2026, 6, 8, 2, 0, 1),
        total_duration_seconds=1.234,
        task_durations={"detect_changes": 0.5, "transform_scd2": 0.2},
        task_statuses={"detect_changes": "COMPLETED", "transform_scd2": "COMPLETED"},
        row_counts={"source": 3, "target": 2, "output": 4},
        change_counts={"new": 1, "changed": 1, "unchanged": 1, "deleted": 0, "total": 3},
        validation_summary={"pass": 3, "fail": 0, "warn": 0},
        ai_status="template",
        ai_provider="template",
        ai_model="template-v1",
    )

    return PipelineResult(
        change_report=change_report,
        scd2_output=scd2_df,
        validation_report=val_report,
        explanations=explanations,
        metrics=metrics,
        explain_result=exp_result,
        execution_time=1.234,
        business_key=["customer_id"],
        tracked_columns=["name", "city", "tier"],
        provider_used="template",
        orchestration_summary=summary,
    )


def test_write_and_read_run_artifacts_roundtrip(tmp_path: Path, sample_pipeline_result: PipelineResult):
    """write_run_artifacts writes all 5 files, and read_run_artifacts reconstructs the full state."""
    from src.scd2_copilot.artifacts import read_run_artifacts, run_exists, write_run_artifacts

    run_id = "run_test_001"
    meta = write_run_artifacts(
        pipeline_result=sample_pipeline_result,
        run_id=run_id,
        base_dir=tmp_path,
        source_path="data/source.csv",
        target_path="data/target.csv",
        processing_date="2026-06-08",
    )

    assert meta.run_id == run_id
    assert run_exists(run_id, base_dir=tmp_path)

    run_dir = tmp_path / run_id
    assert (run_dir / "metadata.json").is_file()
    assert (run_dir / "scd2_output.parquet").is_file()
    assert (run_dir / "changes.json").is_file()
    assert (run_dir / "validation.json").is_file()
    assert (run_dir / "explanations.json").is_file()

    # Read back
    persisted = read_run_artifacts(run_id, base_dir=tmp_path)
    assert persisted.metadata.run_id == run_id
    assert persisted.metadata.flow_run_id == "flw-12345"
    assert persisted.metadata.trigger_type == "scheduled"
    assert persisted.metadata.source_path == "data/source.csv"

    # Verify Parquet schema and data parity
    orig_df = sample_pipeline_result.scd2_output
    read_df = persisted.scd2_output
    assert read_df.shape == orig_df.shape
    assert read_df.schema == orig_df.schema
    assert read_df.schema["effective_from"] == pl.Date
    assert read_df.schema["effective_to"] == pl.Date
    assert read_df.schema["is_current"] == pl.Boolean
    assert read_df.equals(orig_df)

    # Verify ChangeReport parity
    assert persisted.change_report.summary == sample_pipeline_result.change_report.summary
    assert len(persisted.change_report.changed) == 1
    assert persisted.change_report.changed[0].field_changes[0].column == "city"
    assert persisted.change_report.changed[0].field_changes[0].old_value == "Chennai"
    assert persisted.change_report.changed[0].field_changes[0].new_value == "Bengaluru"

    # Verify ValidationReport parity
    assert persisted.validation_report.passed is True
    assert len(persisted.validation_report.rules) == 3
    assert persisted.validation_report.summary == {"pass": 3, "fail": 0, "warn": 0}

    # Verify ExplainResult & LLMMetrics parity
    assert len(persisted.explain_result.explanations) == 1
    assert persisted.explain_result.explanations[0].text == "Customer moved from Chennai to Bengaluru."
    assert persisted.explain_result.metrics is not None
    assert persisted.explain_result.metrics.model == "template-v1"


def test_overwrite_protection_and_allowed(tmp_path: Path, sample_pipeline_result: PipelineResult):
    """write_run_artifacts raises FileExistsError if overwrite=False, and succeeds if overwrite=True."""
    from src.scd2_copilot.artifacts import write_run_artifacts

    run_id = "run_immutable_001"
    write_run_artifacts(sample_pipeline_result, run_id=run_id, base_dir=tmp_path)

    # Re-writing with overwrite=False must raise FileExistsError
    with pytest.raises(FileExistsError, match="already exists"):
        write_run_artifacts(sample_pipeline_result, run_id=run_id, base_dir=tmp_path, overwrite=False)

    # Re-writing with overwrite=True succeeds
    meta2 = write_run_artifacts(sample_pipeline_result, run_id=run_id, base_dir=tmp_path, overwrite=True)
    assert meta2.run_id == run_id


def test_atomic_staging_cleanup_on_failure(tmp_path: Path, sample_pipeline_result: PipelineResult, monkeypatch: pytest.MonkeyPatch):
    """If writing fails during staging, staging dir is removed and no partial run exists."""
    from src.scd2_copilot.artifacts import run_exists, write_run_artifacts

    run_id = "run_fail_001"

    # Force a failure during validation.json writing
    def faulty_dump(*args, **kwargs):
        raise IOError("Simulated disk error during staging")

    monkeypatch.setattr("json.dump", faulty_dump)

    with pytest.raises(IOError, match="Simulated disk error"):
        write_run_artifacts(sample_pipeline_result, run_id=run_id, base_dir=tmp_path)

    # Run must not exist
    assert not run_exists(run_id, base_dir=tmp_path)
    # Staging dir must be cleaned up
    tmp_dirs = [d for d in tmp_path.iterdir() if d.name.startswith(".tmp_")]
    assert len(tmp_dirs) == 0


def test_get_run_metadata_fast_retrieval(tmp_path: Path, sample_pipeline_result: PipelineResult, monkeypatch: pytest.MonkeyPatch):
    """get_run_metadata retrieves metadata without invoking polars read_parquet."""
    from src.scd2_copilot.artifacts import get_run_metadata, write_run_artifacts

    run_id = "run_fast_meta"
    write_run_artifacts(sample_pipeline_result, run_id=run_id, base_dir=tmp_path)

    # If read_parquet is called, fail the test
    def fail_read_parquet(*args, **kwargs):
        raise AssertionError("read_parquet should not be called by get_run_metadata")

    monkeypatch.setattr(pl, "read_parquet", fail_read_parquet)

    meta = get_run_metadata(run_id, base_dir=tmp_path)
    assert meta.run_id == run_id
    assert meta.flow_run_name == "sunny-badger"
    assert meta.row_counts["output"] == 4


def test_list_runs_and_delete_run(tmp_path: Path, sample_pipeline_result: PipelineResult):
    """list_runs returns runs ordered descending and ignores .tmp_* dirs; delete_run removes run."""
    from src.scd2_copilot.artifacts import delete_run, list_runs, run_exists, write_run_artifacts

    # Create dummy hidden staging dir
    (tmp_path / ".tmp_orphan").mkdir()

    write_run_artifacts(sample_pipeline_result, run_id="run_A", base_dir=tmp_path)
    write_run_artifacts(sample_pipeline_result, run_id="run_B", base_dir=tmp_path)

    runs = list_runs(base_dir=tmp_path)
    run_ids = [r.run_id for r in runs]
    assert "run_A" in run_ids
    assert "run_B" in run_ids
    assert ".tmp_orphan" not in run_ids

    # Limit test
    limited = list_runs(base_dir=tmp_path, limit=1)
    assert len(limited) == 1

    # Delete run
    deleted = delete_run("run_A", base_dir=tmp_path)
    assert deleted is True
    assert not run_exists("run_A", base_dir=tmp_path)

    # Deleting non-existent run returns False
    assert delete_run("non_existent", base_dir=tmp_path) is False


def test_run_pipeline_persists_artifacts(tmp_path: Path, source_df: pl.DataFrame, target_df: pl.DataFrame):
    """run_pipeline automatically persists artifacts when enabled and records metadata in OrchestrationSummary."""
    from src.scd2_copilot.artifacts import read_run_artifacts, run_exists
    from src.scd2_copilot.workflow import run_pipeline

    settings = Settings(runs_directory=str(tmp_path), persist_artifacts=True)

    result = run_pipeline(
        source=source_df,
        target=target_df,
        processing_date="2026-06-08",
        settings=settings,
        run_id="run_pipeline_auto_001",
    )

    summary = result.orchestration_summary
    assert summary is not None
    assert summary.artifact_status == "persisted"
    assert summary.run_id == "run_pipeline_auto_001"
    assert summary.artifact_directory == str(tmp_path / "run_pipeline_auto_001")
    assert "scd2_output.parquet" in summary.artifact_files
    assert summary.persistence_duration_seconds > 0.0
    assert summary.task_statuses.get("persist_artifacts") == "COMPLETED"

    # Confirm run is retrievable on disk
    assert run_exists("run_pipeline_auto_001", base_dir=tmp_path)
    persisted = read_run_artifacts("run_pipeline_auto_001", base_dir=tmp_path)
    assert persisted.scd2_output.equals(result.scd2_output)
    assert persisted.validation_report.passed == result.validation_report.passed


def test_run_pipeline_persistence_disabled(tmp_path: Path, source_df: pl.DataFrame, target_df: pl.DataFrame):
    """run_pipeline skips persistence when persist_artifacts is False."""
    from src.scd2_copilot.artifacts import list_runs
    from src.scd2_copilot.workflow import run_pipeline

    settings = Settings(runs_directory=str(tmp_path), persist_artifacts=False)

    result = run_pipeline(
        source=source_df,
        target=target_df,
        processing_date="2026-06-08",
        settings=settings,
    )

    summary = result.orchestration_summary
    assert summary is not None
    assert summary.artifact_status == "skipped"
    assert summary.run_id is None
    assert len(list_runs(base_dir=tmp_path)) == 0


def test_run_pipeline_persistence_failure_isolated(tmp_path: Path, source_df: pl.DataFrame, target_df: pl.DataFrame, monkeypatch: pytest.MonkeyPatch):
    """If artifact writing fails in run_pipeline, SCD2 result is preserved and failure is classified."""
    from src.scd2_copilot.workflow import run_pipeline
    import src.scd2_copilot.artifacts as artifacts_mod

    settings = Settings(runs_directory=str(tmp_path), persist_artifacts=True)

    def fail_write(*args, **kwargs):
        raise IOError("Disk quota exceeded on artifact store")

    monkeypatch.setattr(artifacts_mod, "write_run_artifacts", fail_write)

    result = run_pipeline(
        source=source_df,
        target=target_df,
        processing_date="2026-06-08",
        settings=settings,
        persist_artifacts=True,
    )

    summary = result.orchestration_summary
    assert summary is not None
    assert summary.artifact_status == "failed"
    assert summary.failure_category == "persistence_error"
    assert "Disk quota exceeded" in str(summary.error_message)
    assert summary.task_statuses.get("persist_artifacts") == "FAILED"
    # SCD2 output and validation must remain intact!
    assert result.scd2_output is not None
    assert result.scd2_output.height > 0
    assert result.validation_report.passed is True


def test_readback_streamlit_roundtrip(tmp_path: Path, sample_pipeline_result: PipelineResult):
    """A loaded persisted run populates all dashboard state fields with complete fidelity."""
    from src.scd2_copilot.artifacts import read_run_artifacts, write_run_artifacts

    run_id = "run_streamlit_test"
    write_run_artifacts(sample_pipeline_result, run_id=run_id, base_dir=tmp_path)

    # Simulate Streamlit session state population
    loaded = read_run_artifacts(run_id, base_dir=tmp_path)

    session_state: dict[str, Any] = {
        "pipeline_status": "complete",
        "scd2_output": loaded.scd2_output,
        "change_report": loaded.change_report,
        "validation_report": loaded.validation_report,
        "explain_result": loaded.explain_result,
        "execution_time": loaded.metadata.total_duration_seconds,
        "business_key": loaded.metadata.business_key,
        "tracked_columns": loaded.metadata.tracked_columns,
        "provider_used": loaded.metadata.ai_provider,
        "persisted_run_id": loaded.metadata.run_id,
    }

    assert session_state["pipeline_status"] == "complete"
    assert session_state["scd2_output"].height == 4
    assert session_state["validation_report"].passed is True
    assert len(session_state["change_report"].changed) == 1
    assert session_state["persisted_run_id"] == "run_streamlit_test"


def test_parquet_schema_complex_types_and_nulls(tmp_path: Path):
    """Parquet storage preserves complex Polars types, nulls in dates and strings with exact fidelity."""
    from src.scd2_copilot.artifacts import read_run_artifacts, write_run_artifacts

    complex_df = pl.DataFrame({
        "id": [1, 2, 3],
        "nullable_str": ["Alpha", None, "Gamma"],
        "nullable_int": [10, None, 30],
        "nullable_float": [1.5, 2.5, None],
        "effective_from": [date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)],
        "effective_to": [date(2026, 2, 1), None, None],
        "is_current": [False, True, True],
    })

    pipe_res = PipelineResult(
        change_report=ChangeReport(),
        scd2_output=complex_df,
        validation_report=ValidationReport(),
        business_key=["id"],
        tracked_columns=["nullable_str", "nullable_int", "nullable_float"],
    )

    write_run_artifacts(pipe_res, run_id="run_complex_schema", base_dir=tmp_path)
    loaded = read_run_artifacts("run_complex_schema", base_dir=tmp_path)

    assert loaded.scd2_output.schema == complex_df.schema
    assert loaded.scd2_output.equals(complex_df)
    assert loaded.scd2_output["effective_to"].is_null().to_list() == [False, True, True]
    assert loaded.scd2_output["nullable_str"].is_null().to_list() == [False, True, False]


def test_corrupted_run_handling(tmp_path: Path, sample_pipeline_result: PipelineResult):
    """Corrupted run folders missing metadata or parquet are ignored gracefully by list_runs and run_exists."""
    from src.scd2_copilot.artifacts import list_runs, run_exists, write_run_artifacts

    # Valid run
    write_run_artifacts(sample_pipeline_result, run_id="run_good", base_dir=tmp_path)

    # Corrupt run: missing metadata.json
    bad_dir_1 = tmp_path / "run_corrupt_no_meta"
    bad_dir_1.mkdir()
    (bad_dir_1 / "scd2_output.parquet").write_bytes(b"dummy")

    # Corrupt run: empty metadata.json
    bad_dir_2 = tmp_path / "run_corrupt_bad_meta"
    bad_dir_2.mkdir()
    (bad_dir_2 / "metadata.json").write_text("{bad json", encoding="utf-8")

    assert run_exists("run_good", base_dir=tmp_path) is True
    assert run_exists("run_corrupt_no_meta", base_dir=tmp_path) is False
    assert run_exists("run_corrupt_bad_meta", base_dir=tmp_path) is False

    runs = list_runs(base_dir=tmp_path)
    assert len(runs) == 1
    assert runs[0].run_id == "run_good"


def test_deployment_cli_list_runs(tmp_path: Path, sample_pipeline_result: PipelineResult, capsys: pytest.CaptureFixture):
    """deployment CLI --list-runs outputs persisted run summaries."""
    from src.scd2_copilot.artifacts import write_run_artifacts
    from src.scd2_copilot.deployment import main

    write_run_artifacts(sample_pipeline_result, run_id="run_cli_test_99", base_dir=tmp_path)

    # Test CLI call pointing to tmp_path
    settings = Settings(runs_directory=str(tmp_path))
    import src.scd2_copilot.artifacts as artifacts_mod
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(artifacts_mod, "get_settings", lambda: settings)

    exit_code = main(["--list-runs"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Persisted Pipeline Runs:" in captured.out
    assert "run_cli_test_99" in captured.out


def test_deployment_parameters_run_id_and_persist():
    """DeploymentParameters validates run_id and persist_artifacts."""
    from src.scd2_copilot.deployment import DeploymentParameters

    dp = DeploymentParameters(run_id="custom_run_456", persist_artifacts=False)
    dumped = dp.model_dump(exclude_none=True)
    assert dumped["run_id"] == "custom_run_456"
    assert dumped["persist_artifacts"] is False
