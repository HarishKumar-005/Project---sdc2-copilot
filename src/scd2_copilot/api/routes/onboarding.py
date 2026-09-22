"""FastAPI operational routes for Customer Data Onboarding Runs and Idempotency."""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status

from pydantic import BaseModel, Field

from ...onboarding.exceptions import (
    DriftReportNotFoundError,
    IdempotencyConflictError,
    InvalidRunStateTransitionError,
    RunNotFoundError,
)
from ...onboarding.models.approval import ApprovedMappingVersion
from ...onboarding.models.drift import SchemaDriftReport
from ...onboarding.models.run import (
    RunCancelRequest,
    RunCreateRequest,
    RunListResponse,
    RunResponse,
    RunRetryRequest,
    RunStatus,
)
from ...onboarding.models.schema_snapshot import SourceSchemaSnapshot
from ...onboarding.drift.service import SchemaDriftService
from ...onboarding.runs.service import OnboardingRunService
from ..auth.dependencies import get_optional_operator
from ..dependencies import get_drift_service, get_onboarding_run_service

logger = logging.getLogger("scd2_copilot.api.routes.onboarding")

router = APIRouter(prefix="/onboarding", tags=["Customer Data Onboarding"])


class DriftCompareRequest(BaseModel):
    """Payload for evaluating schema drift and mapping impact between two schema snapshots."""

    prior_schema: SourceSchemaSnapshot = Field(..., description="Prior source schema snapshot")
    current_schema: SourceSchemaSnapshot = Field(..., description="Current source schema snapshot")
    approved_mapping: ApprovedMappingVersion = Field(..., description="Approved mapping version to evaluate")
    persist: bool = Field(default=True, description="Whether to store the resulting drift report")


class DriftReportListResponse(BaseModel):
    """Paginated list of schema drift evaluation reports."""

    reports: list[SchemaDriftReport] = Field(..., description="List of drift reports")
    total: int = Field(..., description="Total count of reports matching filter")
    limit: int = Field(..., description="Limit applied")
    offset: int = Field(..., description="Offset applied")


@router.post(
    "/runs",
    response_model=RunResponse,
    summary="Submit or replay an onboarding run",
    status_code=status.HTTP_201_CREATED,
)
def submit_onboarding_run(
    request: RunCreateRequest,
    response: Response,
    idempotency_key_header: Optional[str] = Header(None, alias="Idempotency-Key"),
    service: OnboardingRunService = Depends(get_onboarding_run_service),
    _operator: Any = Depends(get_optional_operator),
) -> RunResponse:
    """Submit an onboarding run, enforcing idempotency invariants.

    - Returns 201 Created for a new execution.
    - Returns 200 OK with 'X-Idempotent-Replay: true' header for an identical retry.
    - Returns 409 Conflict if the idempotency key was previously used with different parameters.
    - Returns 422 if Idempotency-Key is absent.
    """
    active_key = idempotency_key_header or request.idempotency_key
    if not active_key or not str(active_key).strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "MISSING_IDEMPOTENCY_KEY",
                "message": "Header 'Idempotency-Key' or body field 'idempotency_key' is required.",
            },
        )

    operator_id = getattr(_operator, "email", None) if _operator else None

    try:
        run, is_replay = service.submit_run(
            request,
            idempotency_key_override=idempotency_key_header,
            operator_id=operator_id,
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "IDEMPOTENCY_CONFLICT",
                "message": exc.message,
                "details": exc.details,
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "INVALID_RUN_PAYLOAD",
                "message": str(exc),
            },
        ) from exc

    if is_replay:
        response.status_code = status.HTTP_200_OK
        response.headers["X-Idempotent-Replay"] = "true"
    else:
        response.status_code = status.HTTP_201_CREATED

    return RunResponse.model_validate(run.to_dict())


@router.get(
    "/runs",
    response_model=RunListResponse,
    summary="List onboarding runs with optional filters",
)
def list_onboarding_runs(
    source_id: Optional[str] = Query(None, description="Filter by source system identifier."),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by RunStatus."),
    limit: int = Query(50, ge=1, le=100, description="Max runs to return (1-100)."),
    offset: int = Query(0, ge=0, description="Offset for pagination."),
    service: OnboardingRunService = Depends(get_onboarding_run_service),
    _operator: Any = Depends(get_optional_operator),
) -> RunListResponse:
    """Retrieve paginated onboarding runs ordered descending by created_at."""
    runs, total = service.repository.list_runs(
        source_id=source_id,
        status=status_filter,
        limit=limit,
        offset=offset,
    )

    run_responses = [RunResponse.model_validate(r.to_dict()) for r in runs]
    return RunListResponse(
        runs=run_responses,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/runs/{run_id}",
    response_model=RunResponse,
    summary="Retrieve details and metrics of an onboarding run",
)
def get_onboarding_run(
    run_id: str,
    service: OnboardingRunService = Depends(get_onboarding_run_service),
    _operator: Any = Depends(get_optional_operator),
) -> RunResponse:
    """Retrieve state, metrics, and artifact URIs for a specific onboarding run."""
    run = service.repository.get_by_id(run_id)
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "RUN_NOT_FOUND",
                "message": f"Onboarding run '{run_id}' was not found.",
            },
        )
    return RunResponse.model_validate(run.to_dict())


@router.post(
    "/runs/{run_id}/retry",
    response_model=RunResponse,
    summary="Retry a failed onboarding run",
)
def retry_onboarding_run(
    run_id: str,
    payload: Optional[RunRetryRequest] = None,
    service: OnboardingRunService = Depends(get_onboarding_run_service),
    _operator: Any = Depends(get_optional_operator),
) -> RunResponse:
    """Retry a failed run from CREATED state."""
    options = payload.options if payload else None
    file_path = payload.file_path if payload else None
    data_payload = payload.data_payload if payload else None
    try:
        run = service.retry_run(
            run_id,
            options=options,
            file_path=file_path,
            data_payload=data_payload,
        )
        return RunResponse.model_validate(run.to_dict())
    except RunNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RUN_NOT_FOUND", "message": exc.message},
        ) from exc
    except InvalidRunStateTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "INVALID_RUN_STATE_TRANSITION",
                "message": exc.message,
                "details": exc.details,
            },
        ) from exc


@router.post(
    "/runs/{run_id}/cancel",
    response_model=RunResponse,
    summary="Cancel an active onboarding run",
)
def cancel_onboarding_run(
    run_id: str,
    payload: Optional[RunCancelRequest] = None,
    service: OnboardingRunService = Depends(get_onboarding_run_service),
    _operator: Any = Depends(get_optional_operator),
) -> RunResponse:
    """Cancel an active or pending onboarding run."""
    reason = payload.reason if payload else None
    try:
        run = service.cancel_run(run_id, reason=reason)
        return RunResponse.model_validate(run.to_dict())
    except RunNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RUN_NOT_FOUND", "message": exc.message},
        ) from exc
    except InvalidRunStateTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "INVALID_RUN_STATE_TRANSITION",
                "message": exc.message,
                "details": exc.details,
            },
        ) from exc


# ── M7 Schema Drift and Mapping Impact Endpoints ────────────────

@router.post(
    "/drift/compare",
    response_model=SchemaDriftReport,
    summary="Evaluate schema drift and mapping impact between source schema snapshots",
    status_code=status.HTTP_200_OK,
)
def compare_schema_drift(
    request: DriftCompareRequest,
    service: SchemaDriftService = Depends(get_drift_service),
    _operator: Any = Depends(get_optional_operator),
) -> SchemaDriftReport:
    """Compare prior and current source schema snapshots against an approved mapping version.

    Evaluates structural changes (added, removed, type-changed, nullability-changed, possible rename)
    and determines deterministic mapping compatibility (COMPATIBLE, COMPATIBLE_WITH_REVIEW, BROKEN).
    """
    try:
        report = service.evaluate_drift(
            prior_schema=request.prior_schema,
            current_schema=request.current_schema,
            approved_mapping=request.approved_mapping,
            persist=request.persist,
        )
        return report
    except Exception as exc:
        logger.exception("Failed to evaluate schema drift: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "DRIFT_EVALUATION_FAILED",
                "message": str(exc),
            },
        ) from exc


@router.get(
    "/drift/reports/{report_id}",
    response_model=SchemaDriftReport,
    summary="Retrieve a stored schema drift evaluation report",
)
def get_drift_report(
    report_id: str,
    service: SchemaDriftService = Depends(get_drift_service),
    _operator: Any = Depends(get_optional_operator),
) -> SchemaDriftReport:
    """Retrieve a previously persisted schema drift evaluation report."""
    report = service.get_report(report_id)
    if not report:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "REPORT_NOT_FOUND",
                "message": f"Schema drift report '{report_id}' was not found.",
            },
        )
    return report


@router.get(
    "/drift/reports",
    response_model=DriftReportListResponse,
    summary="List schema drift evaluation reports",
)
def list_drift_reports(
    source_id: Optional[str] = Query(None, description="Filter by source identifier"),
    mapping_version_id: Optional[str] = Query(None, description="Filter by mapping version identifier"),
    limit: int = Query(50, ge=1, le=100, description="Max reports to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    service: SchemaDriftService = Depends(get_drift_service),
    _operator: Any = Depends(get_optional_operator),
) -> DriftReportListResponse:
    """List schema drift evaluation reports ordered descending by created_at."""
    reports, total = service.list_reports(
        source_id=source_id,
        mapping_version_id=mapping_version_id,
        limit=limit,
        offset=offset,
    )
    return DriftReportListResponse(
        reports=reports,
        total=total,
        limit=limit,
        offset=offset,
    )

