"""Operational routes for held-batch inspection, containment evidence, and recovery actions."""

from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ...containment.exceptions import (
    ContainmentError,
    HoldNotFoundError,
    HoldReplayError,
    InvalidHoldTransitionError,
)
from ...containment.service import ContainmentService
from ...db.models import HeldChangeBatchRow
from ...db.repositories import HeldChangeBatchRepository
from ..auth.dependencies import get_optional_operator, require_authorized_operator
from ..auth.models import AuthenticatedOperator
from ..dependencies import get_containment_service, get_hold_repo
from ..schemas import (
    DiscardHoldRequest,
    HoldListResponse,
    HoldResponse,
    RecoveryActionResponse,
    ReleaseHoldRequest,
    ReprocessHoldRequest,
)

logger = logging.getLogger("scd2_copilot.api.routes.holds")

router = APIRouter(prefix="/holds", tags=["Held Batches & Containment"])


def _to_hold_response(row: HeldChangeBatchRow) -> HoldResponse:
    """Convert database row to API response model, extracting attached explanation if present."""
    explanation = None
    if row.evidence and isinstance(row.evidence, dict):
        explanation = row.evidence.get("explanation")

    return HoldResponse(
        hold_id=row.hold_id,
        run_id=row.run_id,
        source_name=row.source_name,
        severity=row.severity,
        reason=row.reason,
        records_affected=row.records_affected,
        status=row.status,
        created_at=row.created_at,
        resolved_at=row.resolved_at,
        evidence=row.evidence,
        explanation=explanation,
    )


@router.get("", response_model=HoldListResponse, summary="List held batches")
def list_holds(
    limit: int = Query(20, ge=1, le=100, description="Maximum holds to return (1-100)."),
    status_filter: Optional[str] = Query(None, alias="status", description="Filter by hold status: HELD | RELEASED | DISCARDED | REPROCESSED."),
    source_name: Optional[str] = Query(None, description="Filter by source stream name."),
    hold_repo: HeldChangeBatchRepository = Depends(get_hold_repo),
    _operator: Any = Depends(get_optional_operator),
) -> HoldListResponse:
    """Fetch bounded held change batches ordered by created_at DESC."""
    clean_status = status_filter.strip().upper() if status_filter else None
    rows = hold_repo.get_recent_holds(
        limit=limit,
        status=clean_status,
        source_name=source_name,
    )
    hold_responses = [_to_hold_response(r) for r in rows]

    return HoldListResponse(
        holds=hold_responses,
        total=len(hold_responses),
        limit=limit,
        offset=0,
    )


@router.get("/{hold_id}", response_model=HoldResponse, summary="Get held batch details and explanation")
def get_hold(
    hold_id: UUID,
    hold_repo: HeldChangeBatchRepository = Depends(get_hold_repo),
    _operator: Any = Depends(get_optional_operator),
) -> HoldResponse:
    """Retrieve detailed state, guardrail evidence, and AI explanation for a held batch."""
    hold = hold_repo.get_hold(hold_id=hold_id)
    if not hold:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "HOLD_NOT_FOUND",
                "message": f"Held change batch '{hold_id}' was not found.",
            },
        )
    return _to_hold_response(hold)


@router.post("/{hold_id}/release", response_model=RecoveryActionResponse, summary="Release held batch downstream")
def release_hold(
    hold_id: UUID,
    request: ReleaseHoldRequest = ReleaseHoldRequest(),
    containment: ContainmentService = Depends(get_containment_service),
    operator: AuthenticatedOperator = Depends(require_authorized_operator),
) -> RecoveryActionResponse:
    """Release a held batch, overriding the guardrail and applying changes downstream.

    Requires an authenticated and authorized operator.
    """
    logger.info(
        "Release requested for hold %s by operator %s (%s). Reason: %s",
        hold_id,
        operator.user_id,
        operator.email,
        request.operator_reason,
    )
    operator_reason = request.operator_reason or f"Released by operator {operator.display_name}"

    try:
        result = containment.release_held_batch(
            hold_id=hold_id,
            operator_reason=operator_reason,
        )
        return RecoveryActionResponse(
            hold_id=result.hold_id,
            status=result.status,
            success=result.success,
            message=result.message,
            resolved_at=result.resolved_at,
            records_affected=result.records_affected,
            checkpoint_advanced_to=result.checkpoint_advanced_to,
            is_idempotent=result.is_idempotent,
            operator_id=operator.user_id,
        )
    except HoldNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "HOLD_NOT_FOUND", "message": exc.message},
        ) from exc
    except InvalidHoldTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "INVALID_HOLD_TRANSITION", "message": exc.message},
        ) from exc
    except HoldReplayError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "HOLD_REPLAY_FAILED", "message": exc.message},
        ) from exc
    except ContainmentError as exc:
        logger.error("Containment service failure during release: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "CONTAINMENT_ERROR", "message": "Failed to release held batch."},
        ) from exc


@router.post("/{hold_id}/reprocess", response_model=RecoveryActionResponse, summary="Reprocess held batch")
def reprocess_hold(
    hold_id: UUID,
    request: ReprocessHoldRequest = ReprocessHoldRequest(),
    containment: ContainmentService = Depends(get_containment_service),
    operator: AuthenticatedOperator = Depends(require_authorized_operator),
) -> RecoveryActionResponse:
    """Re-evaluate SCD2 transformation, validation, and guardrails for a held batch.

    Requires an authenticated and authorized operator.
    """
    logger.info(
        "Reprocess requested for hold %s by operator %s (force_normal=%s)",
        hold_id,
        operator.user_id,
        request.force_normal,
    )
    try:
        result = containment.reprocess_held_batch(
            hold_id=hold_id,
            force_normal=request.force_normal,
        )
        return RecoveryActionResponse(
            hold_id=result.hold_id,
            status=result.status,
            success=result.success,
            message=result.message,
            resolved_at=result.resolved_at,
            records_affected=result.records_affected,
            checkpoint_advanced_to=result.checkpoint_advanced_to,
            is_idempotent=result.is_idempotent,
            operator_id=operator.user_id,
        )
    except HoldNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "HOLD_NOT_FOUND", "message": exc.message},
        ) from exc
    except InvalidHoldTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "INVALID_HOLD_TRANSITION", "message": exc.message},
        ) from exc
    except HoldReplayError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "HOLD_REPLAY_FAILED", "message": exc.message},
        ) from exc
    except ContainmentError as exc:
        logger.error("Containment service failure during reprocess: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "CONTAINMENT_ERROR", "message": "Failed to reprocess held batch."},
        ) from exc


@router.post("/{hold_id}/discard", response_model=RecoveryActionResponse, summary="Discard held batch")
def discard_hold(
    hold_id: UUID,
    request: DiscardHoldRequest = DiscardHoldRequest(),
    containment: ContainmentService = Depends(get_containment_service),
    operator: AuthenticatedOperator = Depends(require_authorized_operator),
) -> RecoveryActionResponse:
    """Permanently discard a held batch (permanent quarantine rejection).

    Requires an authenticated and authorized operator.
    """
    logger.info(
        "Discard requested for hold %s by operator %s (%s). Advance checkpoint: %s",
        hold_id,
        operator.user_id,
        operator.email,
        request.advance_checkpoint,
    )
    operator_reason = request.operator_reason or f"Discarded by operator {operator.display_name}"

    try:
        result = containment.discard_held_batch(
            hold_id=hold_id,
            operator_reason=operator_reason,
            advance_checkpoint=request.advance_checkpoint,
        )
        return RecoveryActionResponse(
            hold_id=result.hold_id,
            status=result.status,
            success=result.success,
            message=result.message,
            resolved_at=result.resolved_at,
            records_affected=result.records_affected,
            checkpoint_advanced_to=result.checkpoint_advanced_to,
            is_idempotent=result.is_idempotent,
            operator_id=operator.user_id,
        )
    except HoldNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "HOLD_NOT_FOUND", "message": exc.message},
        ) from exc
    except InvalidHoldTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "INVALID_HOLD_TRANSITION", "message": exc.message},
        ) from exc
    except ContainmentError as exc:
        logger.error("Containment service failure during discard: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "CONTAINMENT_ERROR", "message": "Failed to discard held batch."},
        ) from exc
