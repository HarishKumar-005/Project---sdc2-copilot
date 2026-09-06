"""Prefect workflow: thin wrapper orchestrating the SCD2 pipeline.

Uses @flow and @task decorators for observability. Runs inline
within the calling process (no Prefect server required).
"""

from __future__ import annotations

from datetime import date
from typing import Optional

import polars as pl
from prefect import flow, task

from .config import Settings, get_settings
from .detect_changes import detect_changes
from .explain import explain_changes, ExplainResult
from .ingestion import load_csv, validate_csv_columns
from .models import ChangeReport, Explanation, PipelineResult, ValidationReport
from .schema import detect_business_key, detect_tracked_columns
from .transform_scd2 import apply_scd2
from .validate import validate_scd2


@task(name="ingest_csvs")
def ingest_task(
    source, target
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Load and normalize source and target CSVs."""
    source_df = load_csv(source)
    target_df = load_csv(target)

    errors = validate_csv_columns(source_df, target_df)
    if errors:
        raise ValueError(f"CSV validation errors: {'; '.join(errors)}")

    return source_df, target_df


@task(name="detect_schema")
def schema_task(
    source_df: pl.DataFrame,
    target_df: pl.DataFrame,
    business_key_override: Optional[list[str]] = None,
) -> tuple[list[str], list[str]]:
    """Detect business key and tracked columns."""
    if business_key_override:
        business_key = business_key_override
    else:
        business_key = detect_business_key(source_df, target_df)

    tracked_columns = detect_tracked_columns(source_df, business_key)
    return business_key, tracked_columns


@task(name="detect_changes")
def detect_task(
    source_df: pl.DataFrame,
    target_df: pl.DataFrame,
    business_key: list[str],
    tracked_columns: list[str],
    processing_date: date,
) -> ChangeReport:
    """Run deterministic change detection."""
    return detect_changes(
        source_df, target_df, business_key, tracked_columns, processing_date
    )


@task(name="transform_scd2")
def transform_task(
    source_df: pl.DataFrame,
    target_df: pl.DataFrame,
    change_report: ChangeReport,
    business_key: list[str],
    tracked_columns: list[str],
    processing_date: date,
) -> pl.DataFrame:
    """Apply SCD2 transformation."""
    return apply_scd2(
        source_df, target_df, change_report,
        business_key, tracked_columns, processing_date
    )


@task(name="validate_output")
def validate_task(
    scd2_output: pl.DataFrame,
    business_key: list[str],
) -> ValidationReport:
    """Validate the SCD2 output."""
    return validate_scd2(scd2_output, business_key)


@task(name="explain_changes")
def explain_task(
    change_report: ChangeReport,
    settings: Settings,
) -> ExplainResult:
    """Generate LLM explanations for detected changes."""
    return explain_changes(change_report, settings=settings)


@flow(name="scd2_pipeline")
def run_pipeline(
    source,
    target,
    processing_date: Optional[date] = None,
    business_key_override: Optional[list[str]] = None,
    settings: Optional[Settings] = None,
) -> PipelineResult:
    """Execute the full SCD2 pipeline.

    Args:
        source: Path or file-like for today's source CSV.
        target: Path or file-like for yesterday's SCD2 target CSV.
        processing_date: Override processing date (default: today).
        business_key_override: Override auto-detected business key.
        settings: App settings (default: loaded from .env).

    Returns:
        PipelineResult with all outputs.
    """
    if settings is None:
        settings = get_settings()

    if processing_date is None:
        processing_date = settings.processing_date

    # Step 1: Ingest
    source_df, target_df = ingest_task(source, target)

    # Step 2: Schema detection
    business_key, tracked_columns = schema_task(
        source_df, target_df, business_key_override
    )

    # Step 3: Change detection
    change_report = detect_task(
        source_df, target_df, business_key, tracked_columns, processing_date
    )

    # Step 4: SCD2 transformation
    scd2_output = transform_task(
        source_df, target_df, change_report,
        business_key, tracked_columns, processing_date
    )

    # Step 5: Validation
    validation_report = validate_task(scd2_output, business_key)

    # Step 6: Explanations
    explain_result = explain_task(change_report, settings)

    return PipelineResult(
        change_report=change_report,
        scd2_output=scd2_output,
        validation_report=validation_report,
        explanations=explain_result.explanations,
        metrics=explain_result.metrics,
    )
