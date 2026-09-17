"""Typed HTTP client for interacting with the SCD2 Copilot Operational API."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any, Callable, Optional
from uuid import UUID

import httpx

from .schemas import (
    DiscardHoldRequest,
    EntityHistoryQueryResponse,
    HealthResponse,
    HistoryQueryResponse,
    HoldListResponse,
    HoldResponse,
    InventoryListResponse,
    InventoryRecordResponse,
    MonitorConfigRequest,
    MonitorConfigResponse,
    MonitorListResponse,
    MonitorValidationResponse,
    OperationalMetricsResponse,
    ReadinessResponse,
    RecoveryActionResponse,
    ReleaseHoldRequest,
    ReprocessHoldRequest,
    RunListResponse,
    RunResponse,
)

logger = logging.getLogger("scd2_copilot.api.client")


class ApiClientError(Exception):
    """Base exception for API client failures."""

    def __init__(self, message: str, status_code: Optional[int] = None, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code or "API_ERROR"


class ApiConnectionError(ApiClientError):
    """Raised when the API server is offline or unreachable."""

    def __init__(self, message: str = "API service is unreachable.") -> None:
        super().__init__(message=message, status_code=None, code="CONNECTION_ERROR")


class ApiUnauthorizedError(ApiClientError):
    """Raised when request lacks valid authentication (401)."""

    def __init__(self, message: str = "Authentication required or token expired.") -> None:
        super().__init__(message=message, status_code=401, code="UNAUTHORIZED")


class ApiForbiddenError(ApiClientError):
    """Raised when authenticated user lacks authorization for the operation (403)."""

    def __init__(self, message: str = "Operation not permitted for current user.") -> None:
        super().__init__(message=message, status_code=403, code="FORBIDDEN")


class ApiNotFoundError(ApiClientError):
    """Raised when requested entity is not found (404)."""

    def __init__(self, message: str = "Resource not found.") -> None:
        super().__init__(message=message, status_code=404, code="NOT_FOUND")


class ApiConflictError(ApiClientError):
    """Raised when state transition is invalid (409)."""

    def __init__(self, message: str = "Resource state conflict.") -> None:
        super().__init__(message=message, status_code=409, code="CONFLICT")


class ApiClient:
    """Client for the SCD2 Copilot Operational API.

    Used by the Streamlit monitoring interface to fetch telemetry,
    view held batches, and trigger authorized recovery actions.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        access_token: Optional[str] = None,
        token_provider: Optional[Callable[[], Optional[str]]] = None,
        timeout: float = 10.0,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._access_token: Optional[str] = access_token
        self._token_provider = token_provider
        self.timeout = timeout
        self._client = client or httpx.Client(timeout=timeout)

    def set_access_token(self, token: Optional[str]) -> None:
        """Update the active Supabase access token for subsequent authenticated requests."""
        self._access_token = token

    def set_token_provider(self, provider: Optional[Callable[[], Optional[str]]]) -> None:
        """Update dynamic token provider callback."""
        self._token_provider = provider

    def _resolve_token(self, token_override: Optional[str] = None) -> Optional[str]:
        """Resolve access token checking override, static token, and dynamic provider."""
        if token_override and token_override.strip():
            return token_override.strip()
        if self._access_token and self._access_token.strip():
            return self._access_token.strip()
        if self._token_provider:
            try:
                t = self._token_provider()
                if t and t.strip():
                    return t.strip()
            except Exception as exc:
                logger.debug("Error retrieving token from dynamic provider: %s", exc)
        return None

    @property
    def has_access_token(self) -> bool:
        """True if an access token is configured or available via provider."""
        return bool(self._resolve_token())

    def _get_headers(self, token_override: Optional[str] = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "SCD2Copilot-Streamlit/2.6",
        }
        token = self._resolve_token(token_override)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _handle_error_response(self, response: httpx.Response) -> None:
        """Extract structured error details and raise corresponding typed exception."""
        code = "API_ERROR"
        msg = f"HTTP {response.status_code}: {response.text}"

        try:
            body = response.json()
            if isinstance(body, dict) and "error" in body:
                err_dict = body["error"]
                code = err_dict.get("code", code)
                msg = err_dict.get("message", msg)
            elif isinstance(body, dict) and "detail" in body:
                detail = body["detail"]
                if isinstance(detail, dict):
                    code = detail.get("code", code)
                    msg = detail.get("message", msg)
                else:
                    msg = str(detail)
        except Exception:
            pass

        if response.status_code == 401:
            raise ApiUnauthorizedError(msg)
        elif response.status_code == 403:
            raise ApiForbiddenError(msg)
        elif response.status_code == 404:
            raise ApiNotFoundError(msg)
        elif response.status_code == 409:
            raise ApiConflictError(msg)
        else:
            raise ApiClientError(message=msg, status_code=response.status_code, code=code)

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        json_data: Optional[dict[str, Any]] = None,
        token_override: Optional[str] = None,
    ) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = self._get_headers(token_override)

        try:
            response = self._client.request(
                method=method,
                url=url,
                params=params,
                json=json_data,
                headers=headers,
            )
        except httpx.ConnectError as exc:
            logger.debug("Connection refused to API at %s: %s", url, exc)
            raise ApiConnectionError(f"Cannot connect to API server at {self.base_url}.") from exc
        except httpx.TimeoutException as exc:
            logger.warning("API request to %s timed out after %.1fs", url, self.timeout)
            raise ApiConnectionError(f"API request timed out ({self.timeout}s).") from exc
        except Exception as exc:
            logger.error("Network error requesting %s: %s", url, exc)
            raise ApiConnectionError(f"Network error requesting API: {exc}") from exc

        if response.is_error:
            self._handle_error_response(response)

        return response.json()

    # ── Health & Readiness Probes ──────────────────────

    def check_health(self) -> HealthResponse:
        """Call GET /health."""
        data = self._request("GET", "/health")
        return HealthResponse(**data)

    def check_readiness(self) -> ReadinessResponse:
        """Call GET /ready."""
        data = self._request("GET", "/ready")
        return ReadinessResponse(**data)

    # ── Operational Metrics ────────────────────────────

    def get_metrics(self, source_name: Optional[str] = None) -> OperationalMetricsResponse:
        """Call GET /api/v1/metrics."""
        params = {"source_name": source_name} if source_name else None
        data = self._request("GET", "/api/v1/metrics", params=params)
        return OperationalMetricsResponse(**data)

    # ── Processing Runs ────────────────────────────────

    def list_runs(
        self,
        limit: int = 20,
        source_name: Optional[str] = None,
        status: Optional[str] = None,
    ) -> RunListResponse:
        """Call GET /api/v1/runs."""
        params: dict[str, Any] = {"limit": limit}
        if source_name:
            params["source_name"] = source_name
        if status:
            params["status"] = status
        data = self._request("GET", "/api/v1/runs", params=params)
        return RunListResponse(**data)

    def get_run(self, run_id: UUID) -> RunResponse:
        """Call GET /api/v1/runs/{run_id}."""
        data = self._request("GET", f"/api/v1/runs/{run_id}")
        return RunResponse(**data)

    # ── Held Batches & Recovery ────────────────────────

    def list_holds(
        self,
        limit: int = 20,
        status: Optional[str] = None,
        source_name: Optional[str] = None,
    ) -> HoldListResponse:
        """Call GET /api/v1/holds."""
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if source_name:
            params["source_name"] = source_name
        data = self._request("GET", "/api/v1/holds", params=params)
        return HoldListResponse(**data)

    def get_hold(self, hold_id: UUID) -> HoldResponse:
        """Call GET /api/v1/holds/{hold_id}."""
        data = self._request("GET", f"/api/v1/holds/{hold_id}")
        return HoldResponse(**data)

    def release_hold(
        self,
        hold_id: UUID,
        operator_reason: Optional[str] = None,
        access_token: Optional[str] = None,
    ) -> RecoveryActionResponse:
        """Call POST /api/v1/holds/{hold_id}/release with Supabase Bearer token."""
        payload = ReleaseHoldRequest(operator_reason=operator_reason).model_dump()
        data = self._request(
            "POST",
            f"/api/v1/holds/{hold_id}/release",
            json_data=payload,
            token_override=access_token,
        )
        return RecoveryActionResponse(**data)

    def reprocess_hold(
        self,
        hold_id: UUID,
        force_normal: bool = False,
        access_token: Optional[str] = None,
    ) -> RecoveryActionResponse:
        """Call POST /api/v1/holds/{hold_id}/reprocess with Supabase Bearer token."""
        payload = ReprocessHoldRequest(force_normal=force_normal).model_dump()
        data = self._request(
            "POST",
            f"/api/v1/holds/{hold_id}/reprocess",
            json_data=payload,
            token_override=access_token,
        )
        return RecoveryActionResponse(**data)

    def discard_hold(
        self,
        hold_id: UUID,
        operator_reason: Optional[str] = None,
        advance_checkpoint: bool = True,
        access_token: Optional[str] = None,
    ) -> RecoveryActionResponse:
        """Call POST /api/v1/holds/{hold_id}/discard with Supabase Bearer token."""
        payload = DiscardHoldRequest(
            operator_reason=operator_reason,
            advance_checkpoint=advance_checkpoint,
        ).model_dump()
        data = self._request(
            "POST",
            f"/api/v1/holds/{hold_id}/discard",
            json_data=payload,
            token_override=access_token,
        )
        return RecoveryActionResponse(**data)

    # ── Historical Inventory ───────────────────────────

    def get_history(self, sku_id: str, warehouse_id: str) -> HistoryQueryResponse:
        """Call GET /api/v1/history/{sku_id}/{warehouse_id}."""
        data = self._request("GET", f"/api/v1/history/{sku_id}/{warehouse_id}")
        return HistoryQueryResponse(**data)

    def get_entity_history(
        self,
        source_name: str,
        entity_key: dict[str, Any],
    ) -> EntityHistoryQueryResponse:
        """Call GET /api/v1/history/{source_name}/entity?key=<json>."""
        import json

        key_str = json.dumps(entity_key)
        data = self._request(
            "GET",
            f"/api/v1/history/{source_name}/entity",
            params={"key": key_str},
        )
        return EntityHistoryQueryResponse(**data)

    # ── Operational Inventory Source ───────────────────

    def list_inventory(self, limit: int = 50) -> InventoryListResponse:
        """Call GET /api/v1/inventory."""
        data = self._request("GET", "/api/v1/inventory", params={"limit": limit})
        return InventoryListResponse(**data)

    def get_inventory_record(self, sku_id: str, warehouse_id: str) -> InventoryRecordResponse:
        """Call GET /api/v1/inventory/{sku_id}/{warehouse_id}."""
        data = self._request("GET", f"/api/v1/inventory/{sku_id}/{warehouse_id}")
        return InventoryRecordResponse(**data)

    # ── Monitor Configuration (V3 Phase 1) ─────────────

    def list_monitors(self) -> MonitorListResponse:
        """Call GET /api/v1/monitors."""
        data = self._request("GET", "/api/v1/monitors")
        return MonitorListResponse(**data)

    def get_monitor(self, name: str = "default") -> MonitorConfigResponse:
        """Call GET /api/v1/monitors/{name}."""
        data = self._request("GET", f"/api/v1/monitors/{name}")
        return MonitorConfigResponse(**data)

    def validate_monitor_config(
        self,
        config: Optional[dict[str, Any]] = None,
    ) -> MonitorValidationResponse:
        """Call POST /api/v1/monitors/validate."""
        data = self._request("POST", "/api/v1/monitors/validate", json_data=config)
        return MonitorValidationResponse(**data)

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()
