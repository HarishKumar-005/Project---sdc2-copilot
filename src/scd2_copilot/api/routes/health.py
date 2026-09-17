"""Health and readiness probe routes."""

from __future__ import annotations

from datetime import datetime, timezone
import logging

from fastapi import APIRouter, Depends, HTTPException, status

from ...config import Settings, get_settings
from ...db.connection import DatabaseManager
from ..dependencies import get_db
from ..schemas import HealthResponse, ReadinessResponse

logger = logging.getLogger("scd2_copilot.api.routes.health")

router = APIRouter(tags=["Health & Readiness"])


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
def get_liveness() -> HealthResponse:
    """Liveness probe confirming that the API service process is active.

    Fast, self-contained check that does not touch external dependencies.
    """
    return HealthResponse(
        status="healthy",
        version="v1",
        timestamp=datetime.now(timezone.utc),
    )


@router.get("/ready", response_model=ReadinessResponse, summary="Readiness probe")
def get_readiness(
    db: DatabaseManager = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ReadinessResponse:
    """Readiness probe validating whether backend database connectivity is available.

    Returns 503 Service Unavailable if database is unreachable.
    """
    now = datetime.now(timezone.utc)
    if not settings.has_database:
        logger.warning("Readiness probe called but DATABASE_URL is not configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "DATABASE_NOT_CONFIGURED",
                "message": "Database connection string is not configured.",
            },
        )

    try:
        db_healthy = db.ping()
    except Exception as exc:
        logger.warning("Readiness probe database health check failed: %s", exc)
        db_healthy = False

    if not db_healthy:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "DATABASE_UNAVAILABLE",
                "message": "Database connection pool is currently unavailable.",
            },
        )

    return ReadinessResponse(
        status="ready",
        database_connected=True,
        timestamp=now,
        checks={"database_pool": True},
    )
