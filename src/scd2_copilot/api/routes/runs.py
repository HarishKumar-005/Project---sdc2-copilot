"""Operational routes for inspection of processing runs."""

from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import UUID


from fastapi import APIRouter, Depends, HTTPException, Query, status

from ...db.repositories import ProcessingRunRepository
from ..auth.dependencies import get_optional_operator
from ..dependencies import get_run_repo
from ..schemas import RunListResponse, RunResponse

logger = logging.getLogger("scd2_copilot.api.routes.runs")

router = APIRouter(prefix="/runs", tags=["Processing Runs"])


@router.get("", response_model=RunListResponse, summary="List recent processing runs")
def list_runs(
    limit: int = Query(20, ge=1, le=100, description="Maximum runs to return (1-100)."),
    source_name: Optional[str] = Query(None, description="Optional filter by source stream name."),
    status_filter: Optional[str] = Query(None, alias="status", description="Optional status filter."),
    run_repo: ProcessingRunRepository = Depends(get_run_repo),
    _operator: Any = Depends(get_optional_operator),
) -> RunListResponse:
    """Fetch bounded recent processing runs ordered by started_at DESC."""
    runs = run_repo.get_recent_runs(limit=limit, source_name=source_name)

    if status_filter:
        clean_status = status_filter.strip().upper()
        runs = [r for r in runs if r.status.upper() == clean_status]

    run_responses = [
        RunResponse(
            run_id=r.run_id,
            source_name=r.source_name,
            status=r.status,
            started_at=r.started_at,
            completed_at=r.completed_at,
            records_seen=r.records_seen,
            records_changed=r.records_changed,
            records_held=r.records_held,
            error_message=r.error_message,
            created_at=r.created_at,
        )
        for r in runs
    ]

    return RunListResponse(
        runs=run_responses,
        total=len(run_responses),
        limit=limit,
        offset=0,
    )


@router.get("/{run_id}", response_model=RunResponse, summary="Get processing run details")
def get_run(
    run_id: UUID,
    run_repo: ProcessingRunRepository = Depends(get_run_repo),
    _operator: Any = Depends(get_optional_operator),
) -> RunResponse:
    """Retrieve detailed state and metrics for a specific processing run."""
    run = run_repo.get_run(run_id=run_id)
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "RUN_NOT_FOUND",
                "message": f"Processing run '{run_id}' was not found.",
            },
        )

    return RunResponse(
        run_id=run.run_id,
        source_name=run.source_name,
        status=run.status,
        started_at=run.started_at,
        completed_at=run.completed_at,
        records_seen=run.records_seen,
        records_changed=run.records_changed,
        records_held=run.records_held,
        error_message=run.error_message,
        created_at=run.created_at,
    )
