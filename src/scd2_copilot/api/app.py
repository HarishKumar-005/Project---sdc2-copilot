"""FastAPI application factory for SCD2 Copilot Operational API."""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging
import time
from typing import Any, AsyncGenerator, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import psycopg

from ..config import Settings, get_settings
from ..containment.exceptions import (
    ContainmentError,
    HoldNotFoundError,
    HoldReplayError,
    InvalidHoldTransitionError,
)
from ..db.connection import DatabaseManager
from ..db.exceptions import EntityNotFoundError

from ..onboarding.exceptions import (
    DriftReportNotFoundError,
    IdempotencyConflictError,
    InvalidRunStateTransitionError,
    RunNotFoundError,
)

from .dependencies import get_db, set_db
from .routes import (
    health_router,
    history_router,
    holds_router,
    inventory_router,
    metrics_router,
    monitors_router,
    onboarding_router,
    runs_router,
)

logger = logging.getLogger("scd2_copilot.api")

API_VERSION = "v1"


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    """Create and configure the FastAPI application instance."""
    app_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        logger.info("Starting SCD2 Copilot Operational API (v%s)...", API_VERSION)
        # Verify or initialize DB pool
        if app_settings.has_database:
            try:
                db = DatabaseManager(settings=app_settings)
                set_db(db)
                logger.info("Database manager initialized for API")
            except Exception as exc:
                logger.warning("Database manager initialization deferred: %s", exc)
        yield
        logger.info("Shutting down SCD2 Copilot Operational API...")
        db = get_db(app_settings)
        if db is not None:
            try:
                db.close()
            except Exception:
                pass

    app = FastAPI(
        title="SCD2 Copilot Operational API",
        description="Headless operational boundary for live data change monitoring, guardrail holds, and SCD2 analytics.",
        version=API_VERSION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # ── Middleware ─────────────────────────────────────

    # 1. Request correlation ID middleware
    @app.middleware("http")
    async def request_correlation_middleware(request: Request, call_next: Any) -> Any:
        incoming_id = request.headers.get("X-Request-ID")
        request_id = incoming_id.strip() if incoming_id and incoming_id.strip() else str(uuid4())
        request.state.request_id = request_id

        start_time = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start_time) * 1000

        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-Ms"] = f"{duration_ms:.2f}"
        return response

    # 2. CORS Middleware
    allowed_origins = [o for o in app_settings.cors_allowed_origins if o != "*"] or ["http://localhost:8501"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept", "X-Request-ID"],
    )

    # ── Global Exception Handlers ──────────────────────

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        detail = exc.detail
        if isinstance(detail, dict):
            code = detail.get("code", "HTTP_ERROR")
            msg = detail.get("message", "An error occurred.")
            extra_details = {k: v for k, v in detail.items() if k not in ("code", "message")}
        else:
            code = "HTTP_ERROR"
            msg = str(detail)
            extra_details = None

        return JSONResponse(
            status_code=exc.status_code,
            headers=exc.headers,
            content={
                "error": {
                    "code": code,
                    "message": msg,
                    "request_id": request_id,
                    "details": extra_details or None,
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        clean_errors = []
        for err in exc.errors():
            clean_errors.append({
                "loc": [str(x) for x in err.get("loc", [])],
                "msg": err.get("msg"),
                "type": err.get("type"),
            })
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Invalid request parameter or payload.",
                    "request_id": request_id,
                    "details": clean_errors,
                }
            },
        )

    @app.exception_handler(HoldNotFoundError)
    @app.exception_handler(EntityNotFoundError)
    async def not_found_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "error": {
                    "code": "NOT_FOUND",
                    "message": str(exc),
                    "request_id": request_id,
                }
            },
        )

    @app.exception_handler(InvalidHoldTransitionError)
    async def conflict_handler(request: Request, exc: InvalidHoldTransitionError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": {
                    "code": "INVALID_HOLD_TRANSITION",
                    "message": exc.message,
                    "request_id": request_id,
                    "details": {
                        "hold_id": str(exc.hold_id),
                        "current_status": exc.current_status,
                        "target_status": exc.target_status,
                    },
                }
            },
        )

    @app.exception_handler(HoldReplayError)
    async def unprocessable_handler(request: Request, exc: HoldReplayError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "HOLD_REPLAY_FAILED",
                    "message": exc.message,
                    "request_id": request_id,
                    "details": {"hold_id": str(exc.hold_id)},
                }
            },
        )

    @app.exception_handler(IdempotencyConflictError)
    async def idempotency_conflict_handler(request: Request, exc: IdempotencyConflictError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": {
                    "code": "IDEMPOTENCY_CONFLICT",
                    "message": exc.message,
                    "request_id": request_id,
                    "details": exc.details,
                }
            },
        )

    @app.exception_handler(InvalidRunStateTransitionError)
    async def run_transition_handler(request: Request, exc: InvalidRunStateTransitionError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": {
                    "code": "INVALID_RUN_STATE_TRANSITION",
                    "message": exc.message,
                    "request_id": request_id,
                    "details": exc.details,
                }
            },
        )

    @app.exception_handler(RunNotFoundError)
    async def run_not_found_handler(request: Request, exc: RunNotFoundError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "error": {
                    "code": "RUN_NOT_FOUND",
                    "message": exc.message,
                    "request_id": request_id,
                    "details": exc.details,
                }
            },
        )

    @app.exception_handler(DriftReportNotFoundError)
    async def drift_report_not_found_handler(request: Request, exc: DriftReportNotFoundError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "error": {
                    "code": "REPORT_NOT_FOUND",
                    "message": exc.message,
                    "request_id": request_id,
                    "details": exc.details,
                }
            },
        )


    @app.exception_handler(psycopg.Error)
    async def database_exception_handler(request: Request, exc: psycopg.Error) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        # Sanitize exception message to prevent leaking SQL or connection credentials
        logger.error("Database error during request %s: %s", request_id, exc)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {
                    "code": "DATABASE_ERROR",
                    "message": "Database operation failed. No changes were committed.",
                    "request_id": request_id,
                }
            },
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        logger.exception("Unhandled server error during request %s: %s", request_id, exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": "INTERNAL_SERVER_ERROR",
                    "message": "An unexpected error occurred processing your request.",
                    "request_id": request_id,
                }
            },
        )

    # ── Routes ─────────────────────────────────────────

    # 1. Health & readiness mounted at root
    app.include_router(health_router)

    # 2. Versioned API routes under /api/v1
    api_v1_router = FastAPI().router
    api_v1_router.prefix = "/api/v1"
    api_v1_router.include_router(runs_router)
    api_v1_router.include_router(onboarding_router)
    api_v1_router.include_router(holds_router)
    api_v1_router.include_router(history_router)
    api_v1_router.include_router(inventory_router)
    api_v1_router.include_router(metrics_router)
    api_v1_router.include_router(monitors_router)

    app.include_router(api_v1_router)

    @app.get("/", summary="API Root", include_in_schema=False)
    def root() -> dict[str, Any]:
        return {
            "name": "SCD2 Copilot Operational API",
            "version": API_VERSION,
            "status": "online",
            "docs": "/docs",
        }

    return app


# Default app instance for ASGI servers (e.g. uvicorn src.scd2_copilot.api.app:app)
app = create_app()
