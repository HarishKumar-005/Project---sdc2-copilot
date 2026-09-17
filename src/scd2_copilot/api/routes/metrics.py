"""Operational metrics routes aggregating pipeline and guardrail telemetry."""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query

from ...config import Settings, get_settings
from ...db.repositories import (
    CheckpointRepository,
    HeldChangeBatchRepository,
    ProcessingRunRepository,
)
from ..auth.dependencies import get_optional_operator
from ..dependencies import get_checkpoint_repo, get_hold_repo, get_run_repo
from ..schemas import OperationalMetricsResponse

logger = logging.getLogger("scd2_copilot.api.routes.metrics")

router = APIRouter(prefix="/metrics", tags=["Operational Metrics"])


@router.get("", response_model=OperationalMetricsResponse, summary="Get aggregated operational metrics")
@router.get("/system", response_model=OperationalMetricsResponse, include_in_schema=False)
def get_metrics(
    source_name: Optional[str] = Query(
        None,
        description="Optional stream source name filter. If omitted, aggregates across all streams.",
    ),
    run_repo: ProcessingRunRepository = Depends(get_run_repo),
    hold_repo: HeldChangeBatchRepository = Depends(get_hold_repo),
    checkpoint_repo: CheckpointRepository = Depends(get_checkpoint_repo),
    settings: Settings = Depends(get_settings),
    _operator: Any = Depends(get_optional_operator),
) -> OperationalMetricsResponse:
    """Calculate and return operational KPI metrics from database repositories."""
    run_counts = run_repo.get_run_counts(source_name=source_name)
    active_holds_count = hold_repo.count_active_holds(source_name=source_name)

    if source_name:
        checkpoint = checkpoint_repo.get_checkpoint(
            source_name=source_name,
            table_name=settings.ingestion_table_name,
        )
    else:
        checkpoint = checkpoint_repo.get_latest_checkpoint(
            table_name=settings.ingestion_table_name,
        )
        if checkpoint is None or checkpoint.watermark_value is None:
            checkpoint = checkpoint_repo.get_latest_checkpoint()

    recent_runs = run_repo.get_recent_runs(limit=1, source_name=source_name)
    latest_run = recent_runs[0] if recent_runs else None

    # Determine system health classification
    if run_counts.get("failed", 0) > 0 and latest_run and latest_run.status.upper() == "FAILED":
        system_health = "critical"
    elif active_holds_count > 0:
        system_health = "degraded"
    else:
        system_health = "nominal"

    return OperationalMetricsResponse(
        total_runs=run_counts.get("total", 0),
        successful_runs=run_counts.get("committed", 0),
        failed_runs=run_counts.get("failed", 0),
        held_runs=run_counts.get("held", 0),
        active_holds_count=active_holds_count,
        total_records_processed=run_counts.get("total_records_processed", 0),
        total_records_changed=run_counts.get("total_records_changed", 0),
        total_records_held=run_counts.get("total_records_held", 0),
        latest_checkpoint=checkpoint.watermark_value if checkpoint else None,
        latest_run_status=latest_run.status if latest_run else None,
        latest_processed_at=latest_run.completed_at or latest_run.started_at if latest_run else None,
        system_health=system_health,
    )
