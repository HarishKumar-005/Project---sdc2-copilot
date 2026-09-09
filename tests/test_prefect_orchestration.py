"""Comprehensive verification suite for Prefect 3 workflow orchestration.

Tests canonical flow execution, task retries, fail-fast behavior on deterministic errors,
AI failure isolation, exact deterministic parity with direct engine execution,
and backward compatibility with direct .fn task calls.
"""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path
from unittest.mock import patch

import polars as pl
import pytest

from src.scd2_copilot.config import LLMProvider, Settings
from src.scd2_copilot.detect_changes import detect_changes
from src.scd2_copilot.exceptions import DuplicateBusinessKeyError
from src.scd2_copilot.models import DeletePolicy, PipelineResult, SnapshotMode
from src.scd2_copilot.transform_scd2 import apply_scd2
from src.scd2_copilot.validate import validate_scd2
from src.scd2_copilot.workflow import (
    detect_task,
    explain_task,
    ingest_task,
    run_pipeline,
    schema_task,
    transform_task,
    validate_task,
)


@pytest.fixture
def offline_settings() -> Settings:
    """Fixture providing settings configured strictly for offline template provider."""
    return Settings(
        _env_file=None,
        gemini_api_key="",
        groq_api_key="",
        llm_provider=LLMProvider.TEMPLATE,
        processing_date=date(2026, 9, 7),
        snapshot_mode=SnapshotMode.FULL,
        delete_policy=DeletePolicy.SOFT_DELETE,
    )


@pytest.fixture
def sample_dfs() -> tuple[pl.DataFrame, pl.DataFrame]:
    """Sample source and target DataFrames for orchestration tests."""
    source_df = pl.DataFrame({
        "id": [101, 102, 104],
        "name": ["Alice", "Bob", "David"],
        "salary": [105000, 120000, 95000],
    })
    target_df = pl.DataFrame({
        "id": [101, 102, 103],
        "name": ["Alice", "Bob", "Charlie"],
        "salary": [100000, 120000, 80000],
        "effective_from": [date(2026, 1, 1), date(2026, 1, 1), date(2026, 1, 1)],
        "effective_to": [None, None, None],
        "is_current": [True, True, True],
    })
    return source_df, target_df


# ── 1. End-to-End Execution Tests ─────────────────────────────────────────────


def test_prefect_flow_e2e_from_csv_files(tmp_path: Path, offline_settings: Settings, sample_dfs: tuple[pl.DataFrame, pl.DataFrame]):
    """Execute run_pipeline passing temporary CSV file paths."""
    source_df, target_df = sample_dfs
    source_path = tmp_path / "source.csv"
    target_path = tmp_path / "target.csv"
    source_df.write_csv(source_path)
    target_df.write_csv(target_path)

    result = run_pipeline(
        source=str(source_path),
        target=str(target_path),
        processing_date=date(2026, 9, 7),
        settings=offline_settings,
    )

    assert isinstance(result, PipelineResult)
    assert result.validation_report.passed is True
    assert result.scd2_output is not None
    assert result.scd2_output.height == 5  # 1 historical closed + 4 active/closed
    assert result.change_report.summary["changed"] == 1
    assert result.change_report.summary["unchanged"] == 1
    assert result.change_report.summary["new"] == 1
    assert result.change_report.summary["deleted"] == 1
    assert result.provider_used == "template"
    assert result.execution_time is not None
    assert result.execution_time >= 0.0
    assert result.source_df is not None
    assert result.target_df is not None


def test_prefect_flow_e2e_from_dataframes(offline_settings: Settings, sample_dfs: tuple[pl.DataFrame, pl.DataFrame]):
    """Execute run_pipeline passing pre-loaded DataFrames with overrides (Streamlit pattern)."""
    source_df, target_df = sample_dfs

    result = run_pipeline(
        source=source_df,
        target=target_df,
        processing_date=date(2026, 9, 7),
        business_key_override=["id"],
        tracked_columns_override=["name", "salary"],
        snapshot_mode=SnapshotMode.FULL,
        delete_policy=DeletePolicy.SOFT_DELETE,
        settings=offline_settings,
    )

    assert isinstance(result, PipelineResult)
    assert result.validation_report.passed is True
    assert result.business_key == ["id"]
    assert result.tracked_columns == ["name", "salary"]
    assert result.explain_result is not None
    assert len(result.explanations) == 3  # new, changed, deleted explained
    assert result.metrics is not None


# ── 2. Parity Verification ───────────────────────────────────────────────────


def test_prefect_flow_exact_parity_with_direct_engine(offline_settings: Settings, sample_dfs: tuple[pl.DataFrame, pl.DataFrame]):
    """Verify byte-for-byte and logical parity between Prefect flow and direct engine execution."""
    source_df, target_df = sample_dfs
    proc_date = date(2026, 9, 7)
    bkey = ["id"]
    tcols = ["name", "salary"]

    # Direct engine execution
    direct_report = detect_changes(
        source_df, target_df, bkey, tcols, proc_date,
        snapshot_mode=SnapshotMode.FULL,
        delete_policy=DeletePolicy.SOFT_DELETE,
    )
    direct_scd2 = apply_scd2(
        source_df, target_df, direct_report, bkey, tcols, proc_date,
        delete_policy=DeletePolicy.SOFT_DELETE,
    )
    direct_val = validate_scd2(direct_scd2, bkey)

    # Prefect orchestrated flow execution
    flow_result = run_pipeline(
        source=source_df,
        target=target_df,
        processing_date=proc_date,
        business_key_override=bkey,
        tracked_columns_override=tcols,
        snapshot_mode=SnapshotMode.FULL,
        delete_policy=DeletePolicy.SOFT_DELETE,
        settings=offline_settings,
    )

    # Strict equality checks
    assert flow_result.scd2_output.equals(direct_scd2)
    assert flow_result.change_report.summary == direct_report.summary
    assert flow_result.validation_report.passed == direct_val.passed
    assert len(flow_result.validation_report.rules) == len(direct_val.rules)
    for f_rule, d_rule in zip(flow_result.validation_report.rules, direct_val.rules):
        assert f_rule.name == d_rule.name
        assert f_rule.status == d_rule.status


# ── 3. Task Configuration & Retries ──────────────────────────────────────────


def test_task_retries_and_policy_configuration():
    """Verify that deterministic tasks have retries=0 and explain_task has non-redundant retry policy."""
    assert ingest_task.retries == 0
    assert schema_task.retries == 0
    assert detect_task.retries == 0
    assert transform_task.retries == 0
    assert validate_task.retries == 0

    assert explain_task.retries == 1
    assert explain_task.retry_delay_seconds == 2
    assert explain_task.timeout_seconds == 60
    assert explain_task.retry_condition_fn is not None


# ── 4. Fail-Fast on Deterministic Errors ──────────────────────────────────────


def test_prefect_fail_fast_on_duplicate_source_keys(offline_settings: Settings):
    """Verify that duplicate business keys raise DuplicateBusinessKeyError without retrying."""
    dup_source = pl.DataFrame({
        "id": [101, 101],
        "name": ["Alice", "Alice Dup"],
        "salary": [100000, 110000],
    })
    target_df = pl.DataFrame({
        "id": [101],
        "name": ["Alice"],
        "salary": [100000],
        "effective_from": [date(2026, 1, 1)],
        "effective_to": [None],
        "is_current": [True],
    })

    with pytest.raises(DuplicateBusinessKeyError) as exc_info:
        run_pipeline(
            source=dup_source,
            target=target_df,
            business_key_override=["id"],
            settings=offline_settings,
        )

    assert "duplicate" in str(exc_info.value).lower()


def test_prefect_fail_fast_on_invalid_csv_structure(offline_settings: Settings):
    """Verify that incompatible/empty schemas raise ValueError during ingestion."""
    invalid_source = pl.DataFrame()
    invalid_target = pl.DataFrame()

    with pytest.raises(ValueError) as exc_info:
        run_pipeline(
            source=invalid_source,
            target=invalid_target,
            settings=offline_settings,
        )

    assert "CSV validation errors" in str(exc_info.value)


# ── 5. AI Failure Isolation ──────────────────────────────────────────────────


def test_prefect_ai_failure_isolation(offline_settings: Settings, sample_dfs: tuple[pl.DataFrame, pl.DataFrame]):
    """Verify that unexpected AI explanation failure does not crash the flow or discard valid SCD2 data."""
    source_df, target_df = sample_dfs

    with patch("src.scd2_copilot.workflow.explain_task", side_effect=RuntimeError("Simulated LLM service crash")):
        result = run_pipeline(
            source=source_df,
            target=target_df,
            processing_date=date(2026, 9, 7),
            settings=offline_settings,
        )

    # Core data and validation must remain 100% intact and valid
    assert result.validation_report.passed is True
    assert result.scd2_output is not None
    assert result.scd2_output.height == 5
    assert result.provider_used == "failed"
    assert result.explain_result is not None
    assert any("Simulated LLM service crash" in w for w in result.explain_result.warnings)


# ── 6. Task .fn Backward Compatibility ───────────────────────────────────────


def test_workflow_tasks_fn_backward_compatibility(sample_dfs: tuple[pl.DataFrame, pl.DataFrame]):
    """Verify that direct task.fn calls work seamlessly for backwards compatibility."""
    source_df, target_df = sample_dfs
    proc_date = date(2026, 9, 7)

    # Test ingest_task.fn
    s_out, t_out = ingest_task.fn(source_df, target_df)
    assert s_out.height == source_df.height
    assert t_out.height == target_df.height

    # Test schema_task.fn
    bkey, tcols = schema_task.fn(s_out, t_out, business_key_override=["id"])
    assert bkey == ["id"]
    assert "salary" in tcols

    # Test detect_task.fn
    rep = detect_task.fn(s_out, t_out, bkey, tcols, proc_date)
    assert rep.summary["total"] == 4

    # Test transform_task.fn
    tx = transform_task.fn(s_out, t_out, rep, bkey, tcols, proc_date)
    assert tx.height == 5

    # Test validate_task.fn
    val = validate_task.fn(tx, bkey)
    assert val.passed is True


# ── 7. Streamlit App Boundary Verification ───────────────────────────────────


def test_streamlit_app_boundary_imports_and_uses_run_pipeline():
    """Verify that app/streamlit_app.py imports and executes run_pipeline rather than engine internals."""
    app_path = Path(__file__).resolve().parent.parent / "app" / "streamlit_app.py"
    content = app_path.read_text(encoding="utf-8")
    parsed = ast.parse(content)

    imported_names = set()
    for node in ast.walk(parsed):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported_names.add(alias.name)

    assert "run_pipeline" in imported_names, "run_pipeline must be imported in streamlit_app.py"
    assert "apply_scd2" not in imported_names, "apply_scd2 should no longer be imported in streamlit_app.py"
    assert "validate_scd2" not in imported_names, "validate_scd2 should no longer be imported in streamlit_app.py"
    assert "explain_changes" not in imported_names, "explain_changes should no longer be imported in streamlit_app.py"
    assert "run_pipeline(" in content, "run_pipeline must be called in streamlit_app.py"
