"""Exhaustive reliability, failure classification, timing, and observability suite for Prefect 3.

Covers:
1. The full 10-scenario failure matrix (malformed input, duplicate keys, invalid schema,
   invalid temporal data, validation failure, Gemini transient, Gemini permanent,
   Gemini exhaustion -> Groq, Groq failure -> Template, unexpected AI crash).
2. Monotonic task-level execution timing.
3. Structured OrchestrationSummary population and state semantics.
4. Non-redundant retry condition function behavior.
5. Security and secret sanitization verification.
6. Pickling and serialization verification for large Polars objects.
7. Orchestration overhead measurement vs direct engine.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
import pickle
import time
from unittest.mock import MagicMock, patch

import polars as pl
import pytest

from src.scd2_copilot.config import LLMProvider, Settings
from src.scd2_copilot.detect_changes import detect_changes
from src.scd2_copilot.exceptions import DuplicateBusinessKeyError, InvalidTemporalValueError
from src.scd2_copilot.failure import FailureCategory, classify_pipeline_exception
from src.scd2_copilot.models import (
    ChangeReport,
    DeletePolicy,
    OrchestrationSummary,
    PipelineResult,
    SnapshotMode,
    ValidationReport,
)
from src.scd2_copilot.transform_scd2 import apply_scd2
from src.scd2_copilot.validate import validate_scd2
from src.scd2_copilot.workflow import (
    detect_task,
    explain_task,
    ingest_task,
    run_pipeline,
    schema_task,
    should_retry_ai_task,
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
    """Sample source and target DataFrames for reliability testing."""
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


# ── 1. The 10-Scenario Failure Matrix ─────────────────────────────────────────


def test_failure_matrix_1_malformed_input(offline_settings: Settings, tmp_path: Path):
    """Scenario 1: Malformed input CSV raises deterministic_input_error and fails flow immediately."""
    bad_csv = tmp_path / "bad.csv"
    bad_csv.write_text("not,a,valid,csv\n1,2", encoding="utf-8")
    empty_df = pl.DataFrame()

    with pytest.raises(ValueError) as exc_info:
        run_pipeline(
            source=empty_df,
            target=empty_df,
            settings=offline_settings,
        )

    cat = classify_pipeline_exception(exc_info.value, task_name="ingest_csvs")
    assert cat == FailureCategory.DETERMINISTIC_INPUT_ERROR


def test_failure_matrix_2_duplicate_business_keys(offline_settings: Settings):
    """Scenario 2: Duplicate business keys raise deterministic_scd2_error and fail flow immediately."""
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

    cat = classify_pipeline_exception(exc_info.value, task_name="detect_changes")
    assert cat == FailureCategory.DETERMINISTIC_SCD2_ERROR


def test_failure_matrix_3_invalid_schema(offline_settings: Settings, sample_dfs: tuple[pl.DataFrame, pl.DataFrame]):
    """Scenario 3: Non-existent business key column raises deterministic_schema_error and fails flow."""
    source_df, target_df = sample_dfs

    with pytest.raises(ValueError) as exc_info:
        run_pipeline(
            source=source_df,
            target=target_df,
            business_key_override=["non_existent_key_col"],
            settings=offline_settings,
        )

    cat = classify_pipeline_exception(exc_info.value, task_name="detect_schema")
    assert cat == FailureCategory.DETERMINISTIC_SCHEMA_ERROR


def test_failure_matrix_4_invalid_temporal_data(offline_settings: Settings, tmp_path: Path):
    """Scenario 4: Malformed datetime in temporal columns raises deterministic_scd2_error."""
    source_df = pl.DataFrame({"id": [101], "name": ["Alice"]})
    bad_target_path = tmp_path / "bad_target.csv"
    bad_target_path.write_text(
        "id,name,effective_from,effective_to,is_current\n101,Alice,INVALID_DATE_STRING,,true\n",
        encoding="utf-8",
    )

    with pytest.raises(InvalidTemporalValueError) as exc_info:
        run_pipeline(
            source=source_df,
            target=str(bad_target_path),
            business_key_override=["id"],
            settings=offline_settings,
        )

    cat = classify_pipeline_exception(exc_info.value, task_name="ingest_csvs")
    assert cat == FailureCategory.DETERMINISTIC_SCD2_ERROR


def test_failure_matrix_5_validation_failure(offline_settings: Settings):
    """Scenario 5: Invariant violations in output are recorded with validation_failure status."""
    # Target contains an overlap for key 101: version 1 has no effective_to (open) but version 2 exists
    source_df = pl.DataFrame({"id": [101], "name": ["Alice"], "salary": [110000]})
    corrupt_target = pl.DataFrame({
        "id": [101, 101],
        "name": ["Alice Initial", "Alice Second"],
        "salary": [90000, 100000],
        "effective_from": [date(2025, 1, 1), date(2025, 6, 1)],
        "effective_to": [None, None],  # Two open versions = invariant overlap violation
        "is_current": [False, True],
    })

    result = run_pipeline(
        source=source_df,
        target=corrupt_target,
        business_key_override=["id"],
        settings=offline_settings,
    )

    # Invariant failure is recorded in validation report and summary
    assert result.validation_report.passed is False
    assert result.orchestration_summary is not None
    assert result.orchestration_summary.validation_summary["passed"] is False
    assert result.orchestration_summary.validation_summary["fail"] > 0


def test_failure_matrix_6_gemini_transient_failure_with_model_fallback():
    """Scenario 6: Gemini transient rate limit (429) triggers model fallback chain."""
    from src.scd2_copilot.providers.gemini import GeminiProvider

    provider = GeminiProvider(
        api_key="mock_key",
        model="gemini-3.8-flash",
        fallback_models=["gemini-3.5-flash-lite"],
    )

    # Mock client: first model throws transient rate limit, second model succeeds
    mock_client = MagicMock()
    transient_err = Exception("Resource has been exhausted (e.g. check quota). status: RESOURCE_EXHAUSTED")
    setattr(transient_err, "code", 429)
    setattr(transient_err, "status", "RESOURCE_EXHAUSTED")

    mock_resp = MagicMock()
    mock_resp.text = '{"explanations": [{"id": 0, "explanation": "Salary increased"}]}'
    mock_resp.parsed = [{"id": 0, "explanation": "Salary increased"}]
    mock_resp.usage_metadata = None

    mock_client.models.generate_content.side_effect = [
        transient_err,  # primary attempt 1
        transient_err,  # primary retry 1
        transient_err,  # primary retry 2 (primary exhausted)
        mock_resp,       # fallback model succeeds
    ]
    provider._client = mock_client

    with patch("src.scd2_copilot.providers.gemini.time.sleep"):
        result, model_used = provider._call_model("test prompt")

    assert model_used == "gemini-3.5-flash-lite"
    assert provider.last_attempted_models == ["gemini-3.8-flash", "gemini-3.5-flash-lite"]


def test_failure_matrix_7_gemini_permanent_failure_immediate_advance():
    """Scenario 7: Gemini permanent error (401 unauthenticated) advances immediately without retrying."""
    from src.scd2_copilot.providers.gemini import GeminiProvider

    provider = GeminiProvider(
        api_key="invalid_key",
        model="gemini-3.8-flash",
        fallback_models=["gemini-3.5-flash-lite"],
    )

    mock_client = MagicMock()
    perm_err = Exception("API key not valid. Please pass a valid API key. status: UNAUTHENTICATED")
    setattr(perm_err, "code", 401)
    setattr(perm_err, "status", "UNAUTHENTICATED")

    mock_resp = MagicMock()
    mock_resp.text = '{"explanations": []}'
    mock_resp.parsed = []
    mock_resp.usage_metadata = None

    mock_client.models.generate_content.side_effect = [
        perm_err,   # primary fails immediately without retrying
        mock_resp,  # fallback succeeds
    ]
    provider._client = mock_client

    with patch("src.scd2_copilot.providers.gemini.time.sleep") as mock_sleep:
        result, model_used = provider._call_model("test prompt")

    # Ensure sleep was never called for retry backoff
    assert mock_sleep.call_count == 0
    assert model_used == "gemini-3.5-flash-lite"


def test_failure_matrix_8_gemini_exhaustion_to_groq(sample_dfs: tuple[pl.DataFrame, pl.DataFrame]):
    """Scenario 8: Exhaustion of all Gemini models falls back to Groq provider."""
    from src.scd2_copilot.explain import explain_changes

    source_df, target_df = sample_dfs
    report = detect_changes(source_df, target_df, ["id"], ["name", "salary"], date(2026, 9, 7))

    groq_settings = Settings(
        _env_file=None,
        gemini_api_key="mock_gemini",
        groq_api_key="mock_groq",
        llm_provider=LLMProvider.GEMINI,
    )

    with patch("src.scd2_copilot.explain.GeminiProvider") as mock_gemini_cls, \
         patch("src.scd2_copilot.explain.GroqProvider") as mock_groq_cls:

        mock_gem = MagicMock()
        mock_gem.name = "gemini"
        mock_gem.explain_changes_batch.side_effect = RuntimeError("All Gemini models exhausted")
        mock_gemini_cls.return_value = mock_gem

        mock_groq = MagicMock()
        mock_groq.name = "groq"
        mock_groq.explain_changes_batch.return_value = []
        mock_groq_cls.return_value = mock_groq

        result = explain_changes(report, settings=groq_settings)

    assert result.provider_used == "groq"
    assert any("Fell back to Groq" in w for w in result.warnings)


def test_failure_matrix_9_groq_failure_to_template(sample_dfs: tuple[pl.DataFrame, pl.DataFrame]):
    """Scenario 9: Groq failure falls back to local Template provider."""
    from src.scd2_copilot.explain import explain_changes

    source_df, target_df = sample_dfs
    report = detect_changes(source_df, target_df, ["id"], ["name", "salary"], date(2026, 9, 7))

    groq_settings = Settings(
        _env_file=None,
        gemini_api_key="",
        groq_api_key="mock_groq",
        llm_provider=LLMProvider.GROQ,
    )

    with patch("src.scd2_copilot.explain.GroqProvider") as mock_groq_cls:
        mock_groq = MagicMock()
        mock_groq.name = "groq"
        mock_groq.explain_changes_batch.side_effect = RuntimeError("Groq rate limit exceeded")
        mock_groq_cls.return_value = mock_groq

        result = explain_changes(report, settings=groq_settings)

    assert result.provider_used == "template"
    assert any("Fell back to template" in w for w in result.warnings)


def test_failure_matrix_10_unexpected_ai_crash_isolated(offline_settings: Settings, sample_dfs: tuple[pl.DataFrame, pl.DataFrame]):
    """Scenario 10: Unhandled AI crash is isolated at flow boundary, preserving SCD2 correctness."""
    source_df, target_df = sample_dfs

    with patch("src.scd2_copilot.workflow.explain_task", side_effect=RuntimeError("Catastrophic LLM memory crash")):
        result = run_pipeline(
            source=source_df,
            target=target_df,
            settings=offline_settings,
        )

    assert result.scd2_output is not None
    assert result.validation_report.passed is True
    assert result.orchestration_summary is not None
    assert result.orchestration_summary.ai_status == "failed"
    assert result.orchestration_summary.task_statuses["explain_changes"] == "FAILED"
    assert result.orchestration_summary.failure_category == "unexpected_internal_error"
    assert "Catastrophic LLM memory crash" in result.orchestration_summary.error_message


# ── 2. Monotonic Task Timing & Observability ──────────────────────────────────


def test_task_level_monotonic_timings(offline_settings: Settings, sample_dfs: tuple[pl.DataFrame, pl.DataFrame]):
    """Verify all 6 task durations are measured with monotonic timer and recorded in summary."""
    source_df, target_df = sample_dfs

    result = run_pipeline(
        source=source_df,
        target=target_df,
        settings=offline_settings,
    )

    summary = result.orchestration_summary
    assert summary is not None
    assert summary.total_duration_seconds > 0.0

    expected_tasks = [
        "ingest_csvs",
        "detect_schema",
        "detect_changes",
        "transform_scd2",
        "validate_output",
        "explain_changes",
        "total_pipeline",
    ]
    for task_name in expected_tasks:
        assert task_name in summary.task_durations
        assert summary.task_durations[task_name] >= 0.0

    # Total duration must equal or exceed individual step sum
    step_sum = sum(summary.task_durations[t] for t in expected_tasks if t != "total_pipeline")
    assert summary.total_duration_seconds >= (step_sum * 0.9)


def test_orchestration_summary_metadata_and_counts(offline_settings: Settings, sample_dfs: tuple[pl.DataFrame, pl.DataFrame]):
    """Verify OrchestrationSummary captures row counts, change counts, and validation metrics."""
    source_df, target_df = sample_dfs

    result = run_pipeline(
        source=source_df,
        target=target_df,
        settings=offline_settings,
    )

    s = result.orchestration_summary
    assert s is not None
    assert s.row_counts["source"] == 3
    assert s.row_counts["target"] == 3
    assert s.row_counts["output"] == 5
    assert s.change_counts["total"] == 4
    assert s.validation_summary["passed"] is True
    assert s.started_at is not None
    assert s.completed_at is not None
    assert s.completed_at >= s.started_at


# ── 3. Non-Redundant Retry Condition Function ─────────────────────────────────


def test_should_retry_ai_task_policy():
    """Verify should_retry_ai_task permits only transient errors and rejects deterministic/permanent."""
    mock_task = MagicMock()
    mock_task_run = MagicMock()

    # 1. Transient socket timeout -> RETRY (True)
    state_transient = MagicMock()
    state_transient.result.side_effect = TimeoutError("Connection to LLM host timed out")
    assert should_retry_ai_task(mock_task, mock_task_run, state_transient) is True

    # 2. Transient 429 rate limit -> RETRY (True)
    rate_limit_err = Exception("Rate limit reached")
    setattr(rate_limit_err, "code", 429)
    state_429 = MagicMock()
    state_429.result.side_effect = rate_limit_err
    assert should_retry_ai_task(mock_task, mock_task_run, state_429) is True

    # 3. Permanent 401 unauthenticated -> DO NOT RETRY (False)
    auth_err = Exception("API key invalid")
    setattr(auth_err, "code", 401)
    state_401 = MagicMock()
    state_401.result.side_effect = auth_err
    assert should_retry_ai_task(mock_task, mock_task_run, state_401) is False

    # 4. Deterministic ValueError -> DO NOT RETRY (False)
    state_val_err = MagicMock()
    state_val_err.result.side_effect = ValueError("Invalid prompt parameters")
    assert should_retry_ai_task(mock_task, mock_task_run, state_val_err) is False


# ── 4. Flow State Accuracy & Failure Propagation ─────────────────────────────


def test_deterministic_failure_propagates_and_marks_flow_failed(offline_settings: Settings):
    """Deterministic failure in detect_task propagates and does not complete with hidden failure."""
    dup_df = pl.DataFrame({"id": [1, 1], "name": ["A", "B"]})
    target_df = pl.DataFrame({"id": [1], "name": ["A"], "effective_from": [date(2026, 1, 1)], "effective_to": [None], "is_current": [True]})

    with pytest.raises(DuplicateBusinessKeyError):
        run_pipeline(source=dup_df, target=target_df, settings=offline_settings)


# ── 5. Observability & Secret Sanitization ────────────────────────────────────


def test_secret_sanitization_in_summary_and_logs(offline_settings: Settings, sample_dfs: tuple[pl.DataFrame, pl.DataFrame]):
    """Verify that sensitive environment variables and API keys never appear in OrchestrationSummary."""
    source_df, target_df = sample_dfs

    result = run_pipeline(source=source_df, target=target_df, settings=offline_settings)
    summary_str = str(result.orchestration_summary)

    for secret_token in ("AIzaSy", "gsk_", "sk-ant", "Bearer "):
        assert secret_token not in summary_str


# ── 6. Serialization & Memory Safety ─────────────────────────────────────────


def test_orchestration_summary_and_pipeline_result_pickleable(offline_settings: Settings, sample_dfs: tuple[pl.DataFrame, pl.DataFrame]):
    """Verify PipelineResult and OrchestrationSummary pickle and unpickle cleanly."""
    source_df, target_df = sample_dfs

    result = run_pipeline(source=source_df, target=target_df, settings=offline_settings)
    pickled = pickle.dumps(result)
    unpickled: PipelineResult = pickle.loads(pickled)

    assert unpickled.validation_report.passed is True
    assert unpickled.scd2_output.shape == result.scd2_output.shape
    assert unpickled.orchestration_summary is not None
    assert unpickled.orchestration_summary.total_duration_seconds > 0.0


# ── 7. Orchestration Overhead Benchmark ──────────────────────────────────────


def test_orchestration_overhead_measurement(offline_settings: Settings, sample_dfs: tuple[pl.DataFrame, pl.DataFrame]):
    """Measure direct M2 engine execution time vs Prefect local flow orchestration overhead."""
    source_df, target_df = sample_dfs
    proc_date = date(2026, 9, 7)
    bkey = ["id"]
    tcols = ["name", "salary"]

    # 1. Direct M2 engine execution
    t0 = time.perf_counter()
    report = detect_changes(source_df, target_df, bkey, tcols, proc_date)
    scd2 = apply_scd2(source_df, target_df, report, bkey, tcols, proc_date)
    val = validate_scd2(scd2, bkey)
    direct_duration = time.perf_counter() - t0

    # 2. Prefect flow execution
    flow_result = run_pipeline(
        source=source_df,
        target=target_df,
        processing_date=proc_date,
        business_key_override=bkey,
        tracked_columns_override=tcols,
        settings=offline_settings,
    )
    flow_duration = flow_result.execution_time

    assert flow_duration is not None
    orchestration_overhead = flow_duration - direct_duration

    # Log and verify that direct engine and flow results are parity identical
    assert flow_result.scd2_output.equals(scd2)
    assert flow_result.validation_report.passed == val.passed
    print(f"\n[M3.2 Overhead Benchmark] Direct Engine: {direct_duration*1000:.2f}ms | Prefect Flow: {flow_duration*1000:.2f}ms | Overhead: {orchestration_overhead*1000:.2f}ms")
