"""Prefect workflow: canonical execution boundary orchestrating the SCD2 pipeline.

Uses @flow and @task decorators for observability, structured logging, failure isolation,
and intentional non-redundant retries. Runs inline within the calling process (no Prefect server daemon required).
"""

from __future__ import annotations

from datetime import date, datetime
import logging
from pathlib import Path
import time
from typing import Any, BinaryIO, Optional, Union

import polars as pl
from prefect import flow, task
from prefect.logging import get_run_logger
from prefect.runtime import flow_run, task_run

from .config import LLMProvider, Settings, get_settings
from .contracts import (
    DataContract,
    persist_quarantine_result,
    quarantine_result_from_validation,
    validate_data_contract,
)
from .detect_changes import detect_changes
from .explain import explain_changes, ExplainResult
from .exceptions import ContractValidationError, DuplicateBusinessKeyError
from .failure import FailureCategory, classify_pipeline_exception
from .ingestion import load_csv, validate_csv_columns
from .models import (
    ChangeReport,
    DeduplicationStatus,
    DeletePolicy,
    Explanation,
    OrchestrationSummary,
    PipelineResult,
    SnapshotMode,
    ValidationReport,
)
from .schema import detect_business_key, detect_tracked_columns
from .transform_scd2 import apply_scd2
from .validate import validate_scd2


def _get_logger():
    """Return the active Prefect run logger if in a flow/task context, else standard logger."""
    try:
        return get_run_logger()
    except Exception:
        return logging.getLogger("scd2_copilot.workflow")


def should_retry_ai_task(task: Any, task_run_obj: Any, state: Any) -> bool:
    """Determine whether an explain_task failure justifies a task-level retry.

    Returns False for:
    - Deterministic errors (ValueError, TypeError, KeyError)
    - Permanent AI errors (auth 401/403, quota exhaustion, 404 model)
    - Normal fallback outcomes (already handled inside explain_changes)

    Returns True ONLY for:
    - True transient network, socket, or timeout errors escaping internal handlers
    """
    exc = None
    if hasattr(state, "result"):
        try:
            state.result(raise_on_failure=True)
        except Exception as e:
            exc = e

    if exc is None:
        return False

    category = classify_pipeline_exception(exc, task_name="explain_changes")
    return category == FailureCategory.TRANSIENT_AI_ERROR


@task(name="ingest_csvs", retries=0)
def ingest_task(
    source: Union[str, Path, BinaryIO, pl.DataFrame],
    target: Union[str, Path, BinaryIO, pl.DataFrame],
    allow_source_schema_evolution: bool = False,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Load and normalize source and target CSVs or DataFrames."""
    logger = _get_logger()
    source_df = source if isinstance(source, pl.DataFrame) else load_csv(source, dataset_name="source")
    target_df = target if isinstance(target, pl.DataFrame) else load_csv(target, dataset_name="target")

    errors = validate_csv_columns(source_df, target_df)
    if allow_source_schema_evolution:
        # An explicit M4.3 contract owns source-column evolution. Keep all
        # structural/temporal checks here, but let unexpected source columns
        # reach the contract gate for a structured warning/rejection.
        errors = [error for error in errors if not error.startswith("Source columns not found in target:")]
    if errors:
        err_msg = f"CSV validation errors: {'; '.join(errors)}"
        logger.error(err_msg)
        raise ValueError(err_msg)

    logger.info(
        "Ingested source (%d rows, %d cols) and target (%d rows, %d cols)",
        source_df.height,
        source_df.width,
        target_df.height,
        target_df.width,
    )
    return source_df, target_df


@task(name="detect_schema", retries=0)
def schema_task(
    source_df: pl.DataFrame,
    target_df: pl.DataFrame,
    business_key_override: Optional[list[str]] = None,
    tracked_columns_override: Optional[list[str]] = None,
) -> tuple[list[str], list[str]]:
    """Detect or apply overrides for business key and tracked columns."""
    logger = _get_logger()
    if business_key_override:
        business_key = list(business_key_override)
        missing_source = [k for k in business_key if k not in source_df.columns]
        if missing_source:
            raise ValueError(f"Business key column(s) not found in source dataset: {', '.join(missing_source)}")
        missing_target = [k for k in business_key if k not in target_df.columns]
        if missing_target:
            raise ValueError(f"Business key column(s) not found in target dataset: {', '.join(missing_target)}")
    else:
        business_key = detect_business_key(source_df, target_df)

    if tracked_columns_override:
        tracked_columns = list(tracked_columns_override)
        missing_tracked = [c for c in tracked_columns if c not in source_df.columns]
        if missing_tracked:
            raise ValueError(f"Tracked column(s) not found in source dataset: {', '.join(missing_tracked)}")
    else:
        tracked_columns = detect_tracked_columns(source_df, business_key)

    logger.info(
        "Schema resolved: business_key=%s, tracked_columns=%s",
        business_key,
        tracked_columns,
    )
    return business_key, tracked_columns


@task(name="validate_data_contract", retries=0)
def contract_task(source_df: pl.DataFrame, contract: DataContract) -> Any:
    """Validate the incoming source before fingerprinting or SCD2 detection."""
    result = validate_data_contract(source_df, contract)
    if not result.valid:
        duplicate = next((error for error in result.errors if error.category == "duplicate_business_key"), None)
        if duplicate is not None:
            error = DuplicateBusinessKeyError(
                duplicate.reason,
                dataset_name="source",
                business_key=list(contract.business_keys),
                duplicate_keys=[{"key": key} for key in duplicate.keys],
                duplicate_count=len(duplicate.keys),
            )
            error.validation_result = result
            raise error
        raise ContractValidationError(
            "; ".join(error.reason for error in result.errors),
            validation_result=result,
        )
    return result


@task(name="detect_changes", retries=0)
def detect_task(
    source_df: pl.DataFrame,
    target_df: pl.DataFrame,
    business_key: list[str],
    tracked_columns: list[str],
    processing_date: date,
    snapshot_mode: SnapshotMode | str = SnapshotMode.FULL,
    delete_policy: DeletePolicy | str = DeletePolicy.SOFT_DELETE,
) -> ChangeReport:
    """Run deterministic change detection."""
    logger = _get_logger()
    report = detect_changes(
        source_df,
        target_df,
        business_key,
        tracked_columns,
        processing_date,
        snapshot_mode=snapshot_mode,
        delete_policy=delete_policy,
    )
    summary = report.summary
    logger.info(
        "Change detection completed: total=%d (new=%d, changed=%d, unchanged=%d, deleted=%d)",
        summary["total"],
        summary["new"],
        summary["changed"],
        summary["unchanged"],
        summary["deleted"],
    )
    return report


@task(name="transform_scd2", retries=0)
def transform_task(
    source_df: pl.DataFrame,
    target_df: pl.DataFrame,
    change_report: ChangeReport,
    business_key: list[str],
    tracked_columns: list[str],
    processing_date: date,
    delete_policy: Optional[DeletePolicy | str] = None,
) -> pl.DataFrame:
    """Apply SCD2 transformation."""
    logger = _get_logger()
    output_df = apply_scd2(
        source_df,
        target_df,
        change_report,
        business_key,
        tracked_columns,
        processing_date,
        delete_policy=delete_policy,
    )
    logger.info(
        "SCD2 transformation completed: %d total rows in resulting table",
        output_df.height,
    )
    return output_df


@task(name="validate_output", retries=0)
def validate_task(
    scd2_output: pl.DataFrame,
    business_key: list[str],
) -> ValidationReport:
    """Validate the SCD2 output against all invariant rules."""
    logger = _get_logger()
    val_report = validate_scd2(scd2_output, business_key)
    logger.info(
        "SCD2 validation completed: passed=%s (pass=%d, fail=%d, warn=%d)",
        val_report.passed,
        val_report.summary["pass"],
        val_report.summary["fail"],
        val_report.summary["warn"],
    )
    return val_report


@task(
    name="explain_changes",
    retries=1,
    retry_delay_seconds=2,
    timeout_seconds=60,
    retry_condition_fn=should_retry_ai_task,
)
def explain_task(
    change_report: ChangeReport,
    settings: Settings,
) -> ExplainResult:
    """Generate LLM explanations for detected changes with retries on transient failure."""
    logger = _get_logger()
    result = explain_changes(change_report, settings=settings)
    logger.info(
        "Change explanations completed: provider='%s', explanations=%d, fallbacks=%d",
        result.provider_used,
        len(result.explanations),
        result.fallback_count,
    )
    return result


@task(name="persist_artifacts", retries=0)
def persist_task(
    pipeline_result: PipelineResult,
    run_id: Optional[str],
    settings: Settings,
    source_path: Optional[str] = None,
    target_path: Optional[str] = None,
    processing_date: Optional[date] = None,
    snapshot_mode: Optional[SnapshotMode] = None,
    delete_policy: Optional[DeletePolicy] = None,
    overwrite: bool = False,
    execution_fingerprint: Optional[str] = None,
    is_reused: bool = False,
    reused_from_run_id: Optional[str] = None,
    deduplication_status: str = "new_execution",
    created_by: Optional[dict[str, Any]] = None,
) -> Any:
    """Persist pipeline outputs and metadata to isolated filesystem directory."""
    from .artifacts import write_run_artifacts
    return write_run_artifacts(
        pipeline_result=pipeline_result,
        run_id=run_id,
        base_dir=settings.get_runs_dir(),
        source_path=source_path,
        target_path=target_path,
        processing_date=processing_date,
        snapshot_mode=snapshot_mode,
        delete_policy=delete_policy,
        overwrite=overwrite,
        settings=settings,
        execution_fingerprint=execution_fingerprint,
        is_reused=is_reused,
        reused_from_run_id=reused_from_run_id,
        deduplication_status=deduplication_status,
        created_by=created_by,
    )


@flow(name="scd2_pipeline")
def run_pipeline(
    source: Optional[Union[str, Path, BinaryIO, pl.DataFrame]] = None,
    target: Optional[Union[str, Path, BinaryIO, pl.DataFrame]] = None,
    processing_date: Optional[Union[date, str]] = None,
    business_key_override: Optional[list[str]] = None,
    tracked_columns_override: Optional[list[str]] = None,
    snapshot_mode: Optional[Union[SnapshotMode, str]] = None,
    delete_policy: Optional[Union[DeletePolicy, str]] = None,
    llm_provider: Optional[str] = None,
    settings: Optional[Any] = None,
    run_id: Optional[str] = None,
    persist_artifacts: Optional[bool] = None,
    raise_on_persistence_error: bool = False,
    reuse_existing: Optional[bool] = None,
    force_recompute: bool = False,
    data_contract: Optional[Any] = None,
    created_by: Optional[dict[str, Any]] = None,
) -> PipelineResult:
    """Execute the full SCD2 pipeline under Prefect 3 orchestration.

    Args:
        source: Path, buffer, or DataFrame for today's source data (default: settings.default_source_path).
        target: Path, buffer, or DataFrame for yesterday's SCD2 target data (default: settings.default_target_path).
        processing_date: Override processing date (defaults to scheduled start date if scheduled, else config/today).
        business_key_override: Override auto-detected business key.
        tracked_columns_override: Override auto-detected tracked columns.
        snapshot_mode: Override snapshot mode (full or incremental).
        delete_policy: Override delete policy (soft_delete or ignore).
        llm_provider: Optional LLM provider name ('gemini', 'groq', 'template').
        settings: App settings (default: loaded from .env).

    Returns:
        PipelineResult with all execution outputs, validation, explanations, and metadata.
    """
    t_start = time.perf_counter()
    started_at = datetime.now()
    logger = _get_logger()

    task_durations: dict[str, float] = {}
    task_statuses: dict[str, str] = {}
    warnings: list[str] = []

    if settings is None:
        settings = get_settings()

    if source is None:
        source = settings.default_source_path
    if target is None:
        target = settings.default_target_path

    if llm_provider is not None:
        settings.llm_provider = LLMProvider(llm_provider.lower())

    if processing_date is None:
        derived_date = None
        try:
            from prefect.context import FlowRunContext
            ctx = FlowRunContext.get()
            dep_id_val = getattr(flow_run, "parent_deployment_id", None)
            if dep_id_val is None and ctx and ctx.flow_run:
                dep_id_val = getattr(ctx.flow_run, "deployment_id", None)

            sched_time = getattr(flow_run, "scheduled_start_time", None)
            if sched_time is None and ctx and ctx.flow_run:
                sched_time = getattr(ctx.flow_run, "expected_start_time", None) or getattr(ctx.flow_run, "start_time", None)

            is_scheduled_run = (dep_id_val is not None) or (ctx and ctx.flow_run and getattr(ctx.flow_run, "auto_scheduled", False))
            if is_scheduled_run and sched_time is not None:
                derived_date = sched_time.date()
        except Exception:
            pass

        processing_date = derived_date if derived_date is not None else settings.processing_date
    elif isinstance(processing_date, str):
        processing_date = date.fromisoformat(processing_date)

    if snapshot_mode is None:
        snapshot_mode = settings.snapshot_mode
    elif isinstance(snapshot_mode, str):
        snapshot_mode = SnapshotMode(snapshot_mode.lower())

    if delete_policy is None:
        delete_policy = settings.delete_policy
    elif isinstance(delete_policy, str):
        delete_policy = DeletePolicy(delete_policy.lower())

    if persist_artifacts is None:
        persist_artifacts = getattr(settings, "persist_artifacts", True)

    if reuse_existing is None:
        reuse_existing = getattr(settings, "idempotency_enabled", True) and persist_artifacts
    if force_recompute:
        reuse_existing = False

    from .artifacts import generate_run_id
    current_run_id = run_id or generate_run_id()

    logger.info(
        "Starting SCD2 pipeline flow: run_id=%s, processing_date=%s, snapshot_mode=%s, delete_policy=%s, reuse_existing=%s",
        current_run_id,
        processing_date,
        snapshot_mode,
        delete_policy,
        reuse_existing,
    )

    # Step 1: Ingest
    t_step = time.perf_counter()
    try:
        source_df, target_df = ingest_task(
            source,
            target,
            allow_source_schema_evolution=data_contract is not None,
        )
        task_durations["ingest_csvs"] = time.perf_counter() - t_step
        task_statuses["ingest_csvs"] = "COMPLETED"
    except Exception as exc:
        task_durations["ingest_csvs"] = time.perf_counter() - t_step
        task_statuses["ingest_csvs"] = "FAILED"
        category = classify_pipeline_exception(exc, task_name="ingest_csvs")
        logger.error("ingest_csvs failed (%s): %s", category.value, exc)
        raise

    # Step 2: Schema detection
    t_step = time.perf_counter()
    try:
        business_key, tracked_columns = schema_task(
            source_df,
            target_df,
            business_key_override=business_key_override,
            tracked_columns_override=tracked_columns_override,
        )
        task_durations["detect_schema"] = time.perf_counter() - t_step
        task_statuses["detect_schema"] = "COMPLETED"
    except Exception as exc:
        task_durations["detect_schema"] = time.perf_counter() - t_step
        task_statuses["detect_schema"] = "FAILED"
        category = classify_pipeline_exception(exc, task_name="detect_schema")
        logger.error("detect_schema failed (%s): %s", category.value, exc)
        raise

    # Step 3: Contract gate (M4.3).  It is intentionally before M3.6
    # fingerprint reuse and before deterministic change detection.
    t_step = time.perf_counter()
    contract_obj = data_contract
    if contract_obj is None:
        contract_obj = DataContract.from_dataframes(
            source_df,
            target_df,
            business_key,
            tracked_columns,
        )
    elif isinstance(contract_obj, dict):
        contract_obj = DataContract.from_dict(contract_obj)
    try:
        contract_result = contract_task(source_df, contract_obj)
        task_durations["validate_data_contract"] = time.perf_counter() - t_step
        task_statuses["validate_data_contract"] = "COMPLETED"
        warnings.extend(error.reason for error in contract_result.warnings)
    except (ContractValidationError, DuplicateBusinessKeyError) as exc:
        task_durations["validate_data_contract"] = time.perf_counter() - t_step
        task_statuses["validate_data_contract"] = "FAILED"
        quarantine = quarantine_result_from_validation(
            exc.validation_result,
            source_identity=getattr(source, "name", source if isinstance(source, (str, Path)) else "dataframe"),
            run_id=current_run_id,
        )
        try:
            persist_quarantine_result(quarantine, settings.get_quarantine_dir())
        except Exception as quarantine_exc:
            logger.error("Could not persist quarantine record: %s", quarantine_exc)
        exc.quarantine_result = quarantine
        logger.error("validate_data_contract rejected input: %s", exc)
        raise

    # Resolve flow runtime identifiers if available
    flow_id = None
    flow_name = None
    deployment_id = None
    deployment_name = None
    trigger_type = "manual"
    try:
        from prefect.context import FlowRunContext
        ctx = FlowRunContext.get()
        fid = flow_run.get_id() if hasattr(flow_run, "get_id") else None
        if not fid and ctx and ctx.flow_run:
            fid = ctx.flow_run.id
        if fid:
            flow_id = str(fid)
        fname = flow_run.get_name() if hasattr(flow_run, "get_name") else None
        if not fname and ctx and ctx.flow_run:
            fname = ctx.flow_run.name
        if fname:
            flow_name = str(fname)
        pdid = getattr(flow_run, "parent_deployment_id", None)
        if not pdid and ctx and ctx.flow_run:
            pdid = getattr(ctx.flow_run, "deployment_id", None)
        if pdid:
            deployment_id = str(pdid)
            deployment_name = "scd2_pipeline/local-processing"
        if ctx and ctx.flow_run and getattr(ctx.flow_run, "auto_scheduled", False):
            trigger_type = "scheduled"
    except Exception:
        pass

    # Execution Fingerprinting & Idempotency Gate (M3.6)
    from .artifacts import (
        compute_execution_fingerprint,
        find_run_by_fingerprint,
        read_run_artifacts,
    )

    execution_fingerprint = compute_execution_fingerprint(
        source=source if isinstance(source, (str, Path)) else source_df,
        target=target if isinstance(target, (str, Path)) else target_df,
        processing_date=processing_date,
        snapshot_mode=snapshot_mode,
        delete_policy=delete_policy,
        business_key=business_key,
        tracked_columns=tracked_columns,
        algorithm=getattr(settings, "fingerprint_algorithm", "sha256"),
    )
    logger.info("Execution fingerprint: %s", execution_fingerprint)

    found_run: Optional[Any] = None
    if reuse_existing and not force_recompute:
        found_run = find_run_by_fingerprint(execution_fingerprint, base_dir=settings.get_runs_dir())

    if found_run is not None:
        logger.info(
            "Reusing existing completed run '%s' for fingerprint %s (bypassing SCD2 engine & LLM)",
            found_run.run_id,
            execution_fingerprint,
        )
        persisted = read_run_artifacts(found_run.run_id, base_dir=settings.get_runs_dir())
        exec_time = time.perf_counter() - t_start
        completed_at = datetime.now()

        task_statuses["detect_changes"] = "REUSED"
        task_statuses["transform_scd2"] = "REUSED"
        task_statuses["validate_output"] = "REUSED"
        task_statuses["explain_changes"] = "REUSED"
        task_statuses["persist_artifacts"] = "SKIPPED"
        for t in ["detect_changes", "transform_scd2", "validate_output", "explain_changes", "persist_artifacts"]:
            task_durations[t] = 0.0
        task_durations["total_pipeline"] = exec_time

        summary = OrchestrationSummary(
            flow_run_id=flow_id,
            flow_run_name=flow_name,
            deployment_id=deployment_id,
            deployment_name=deployment_name,
            trigger_type=trigger_type,
            started_at=started_at,
            completed_at=completed_at,
            total_duration_seconds=exec_time,
            task_durations=task_durations,
            task_statuses=task_statuses,
            row_counts=dict(found_run.row_counts),
            change_counts=dict(found_run.change_counts),
            validation_summary=dict(found_run.validation_summary),
            ai_status=found_run.ai_status,
            ai_provider=found_run.ai_provider or "template",
            ai_model=found_run.ai_model,
            ai_fallback_count=found_run.ai_fallback_count,
            failure_category=None,
            error_message=None,
            warnings=list(found_run.warnings),
            run_id=current_run_id,
            execution_fingerprint=execution_fingerprint,
            is_reused=True,
            reused_from_run_id=found_run.run_id,
            deduplication_status=DeduplicationStatus.REUSED_EXECUTION.value,
            artifact_status="reused",
            artifact_directory=found_run.artifact_directory,
            artifact_files=list(found_run.artifact_files.values()),
            persistence_duration_seconds=0.0,
            created_by=getattr(found_run, "created_by", None) or created_by,
        )

        return PipelineResult(
            change_report=persisted.change_report,
            scd2_output=persisted.scd2_output,
            validation_report=persisted.validation_report,
            explanations=persisted.explain_result.explanations,
            metrics=persisted.explain_result.metrics,
            explain_result=persisted.explain_result,
            execution_time=exec_time,
            source_df=source_df,
            target_df=target_df,
            business_key=business_key,
            tracked_columns=tracked_columns,
            provider_used=persisted.explain_result.provider_used,
            orchestration_summary=summary,
        )

    dedup_status = (
        DeduplicationStatus.FORCED_REEXECUTION.value
        if force_recompute
        else DeduplicationStatus.NEW_EXECUTION.value
    )

    # Step 3: Change detection
    t_step = time.perf_counter()
    try:
        change_report = detect_task(
            source_df,
            target_df,
            business_key,
            tracked_columns,
            processing_date,
            snapshot_mode=snapshot_mode,
            delete_policy=delete_policy,
        )
        task_durations["detect_changes"] = time.perf_counter() - t_step
        task_statuses["detect_changes"] = "COMPLETED"
    except Exception as exc:
        task_durations["detect_changes"] = time.perf_counter() - t_step
        task_statuses["detect_changes"] = "FAILED"
        category = classify_pipeline_exception(exc, task_name="detect_changes")
        logger.error("detect_changes failed (%s): %s", category.value, exc)
        raise

    # Step 4: SCD2 transformation
    t_step = time.perf_counter()
    try:
        scd2_output = transform_task(
            source_df,
            target_df,
            change_report,
            business_key,
            tracked_columns,
            processing_date,
            delete_policy=delete_policy,
        )
        task_durations["transform_scd2"] = time.perf_counter() - t_step
        task_statuses["transform_scd2"] = "COMPLETED"
    except Exception as exc:
        task_durations["transform_scd2"] = time.perf_counter() - t_step
        task_statuses["transform_scd2"] = "FAILED"
        category = classify_pipeline_exception(exc, task_name="transform_scd2")
        logger.error("transform_scd2 failed (%s): %s", category.value, exc)
        raise

    # Step 5: Validation
    t_step = time.perf_counter()
    try:
        validation_report = validate_task(scd2_output, business_key)
        task_durations["validate_output"] = time.perf_counter() - t_step
        task_statuses["validate_output"] = "COMPLETED"
    except Exception as exc:
        task_durations["validate_output"] = time.perf_counter() - t_step
        task_statuses["validate_output"] = "FAILED"
        category = classify_pipeline_exception(exc, task_name="validate_output")
        logger.error("validate_output failed (%s): %s", category.value, exc)
        raise

    # Step 6: Explanations (with failure isolation)
    t_step = time.perf_counter()
    ai_status = "success"
    ai_failure_cat = None
    ai_err_msg = None
    try:
        explain_result = explain_task(change_report, settings)
        task_durations["explain_changes"] = time.perf_counter() - t_step
        task_statuses["explain_changes"] = "COMPLETED"
        if explain_result.warnings:
            warnings.extend(explain_result.warnings)
        if explain_result.provider_used == "template" and settings.get_effective_provider().value != "template":
            ai_status = "template"
        elif "partial template fallback" in explain_result.provider_used:
            ai_status = "fallback"
        elif "fallback model" in str(explain_result.warnings).lower():
            ai_status = "fallback"
    except Exception as exc:
        task_durations["explain_changes"] = time.perf_counter() - t_step
        task_statuses["explain_changes"] = "FAILED"
        cat_enum = classify_pipeline_exception(exc, task_name="explain_changes")
        ai_failure_cat = cat_enum.value
        ai_err_msg = str(exc)
        ai_status = "failed"
        logger.error("explain_changes failed after retries (%s): %s", ai_failure_cat, exc, exc_info=True)
        explain_result = ExplainResult(
            explanations=[],
            warnings=[f"AI explanation failed ({ai_failure_cat}): {exc}"],
            provider_used="failed",
        )
        warnings.append(f"AI explanation failed ({ai_failure_cat}): {exc}")

    exec_time = time.perf_counter() - t_start
    task_durations["total_pipeline"] = exec_time
    completed_at = datetime.now()
    logger.info("SCD2 pipeline flow completed in %.4fs", exec_time)

    row_counts = {
        "source": source_df.height,
        "target": target_df.height,
        "output": scd2_output.height,
    }
    change_counts = dict(change_report.summary)
    val_summary = {
        "passed": validation_report.passed,
        "pass": validation_report.summary["pass"],
        "fail": validation_report.summary["fail"],
        "warn": validation_report.summary["warn"],
    }
    ai_model = getattr(explain_result.metrics, "model", None)

    summary = OrchestrationSummary(
        flow_run_id=flow_id,
        flow_run_name=flow_name,
        deployment_id=deployment_id,
        deployment_name=deployment_name,
        trigger_type=trigger_type,
        started_at=started_at,
        completed_at=completed_at,
        total_duration_seconds=exec_time,
        task_durations=task_durations,
        task_statuses=task_statuses,
        row_counts=row_counts,
        change_counts=change_counts,
        validation_summary=val_summary,
        ai_status=ai_status,
        ai_provider=explain_result.provider_used,
        ai_model=ai_model,
        ai_fallback_count=explain_result.fallback_count,
        failure_category=ai_failure_cat,
        error_message=ai_err_msg,
        warnings=warnings,
        run_id=current_run_id if persist_artifacts else None,
        execution_fingerprint=execution_fingerprint,
        is_reused=False,
        reused_from_run_id=None,
        deduplication_status=dedup_status,
        created_by=created_by,
    )

    result = PipelineResult(
        change_report=change_report,
        scd2_output=scd2_output,
        validation_report=validation_report,
        explanations=explain_result.explanations,
        metrics=explain_result.metrics,
        explain_result=explain_result,
        execution_time=exec_time,
        source_df=source_df,
        target_df=target_df,
        business_key=business_key,
        tracked_columns=tracked_columns,
        provider_used=explain_result.provider_used,
        orchestration_summary=summary,
    )

    # Step 7: Persist Run Artifacts (M3.5)
    if persist_artifacts:
        t_persist = time.perf_counter()
        try:
            run_meta = persist_task(
                pipeline_result=result,
                run_id=current_run_id,
                settings=settings,
                source_path=str(source) if isinstance(source, (str, Path)) else None,
                target_path=str(target) if isinstance(target, (str, Path)) else None,
                processing_date=processing_date,
                snapshot_mode=snapshot_mode,
                delete_policy=delete_policy,
                execution_fingerprint=execution_fingerprint,
                is_reused=False,
                reused_from_run_id=None,
                deduplication_status=dedup_status,
                created_by=created_by,
            )
            persist_duration = time.perf_counter() - t_persist
            task_durations["persist_artifacts"] = persist_duration
            task_statuses["persist_artifacts"] = "COMPLETED"
            summary.run_id = run_meta.run_id
            summary.artifact_status = "persisted"
            summary.artifact_directory = run_meta.artifact_directory
            summary.artifact_files = list(run_meta.artifact_files.values())
            summary.persistence_duration_seconds = persist_duration
        except Exception as exc:
            persist_duration = time.perf_counter() - t_persist
            task_durations["persist_artifacts"] = persist_duration
            task_statuses["persist_artifacts"] = "FAILED"
            summary.artifact_status = "failed"
            summary.persistence_duration_seconds = persist_duration
            cat = classify_pipeline_exception(exc, task_name="persist_artifacts")
            if not summary.failure_category:
                summary.failure_category = cat.value
                summary.error_message = str(exc)
            logger.error("persist_artifacts failed (%s): %s", cat.value, exc, exc_info=True)
            warnings.append(f"Persistence failed ({cat.value}): {exc}")
            if raise_on_persistence_error:
                raise
    else:
        summary.artifact_status = "skipped"
        summary.run_id = None

    return result
