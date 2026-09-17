"""Unit tests for ApiClient HTTP adapter and V2 Streamlit UI components (V2.6)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4
import pytest
import httpx

from src.scd2_copilot.api.client import (
    ApiClient,
    ApiClientError,
    ApiConflictError,
    ApiConnectionError,
    ApiForbiddenError,
    ApiNotFoundError,
    ApiUnauthorizedError,
)
from src.scd2_copilot.api.schemas import (
    HealthResponse,
    HoldListResponse,
    HoldResponse,
    OperationalMetricsResponse,
    ReadinessResponse,
    RecoveryActionResponse,
    RunListResponse,
    RunResponse,
)
from app.ui_components import render_v2_hold_card, render_v2_system_health_strip
from app.v2_monitor import _render_containment_tab, _render_runs_tab


# ── ApiClient Unit Tests ────────────────────────────────────


def test_client_init_and_token_management() -> None:
    client = ApiClient(base_url="http://api.internal:8000/", access_token="initial.token")
    assert client.base_url == "http://api.internal:8000"
    assert client.has_access_token is True
    headers = client._get_headers()
    assert headers["Authorization"] == "Bearer initial.token"

    client.set_access_token("new.jwt.token")
    headers = client._get_headers()
    assert headers["Authorization"] == "Bearer new.jwt.token"

    client.set_access_token(None)
    assert client.has_access_token is False
    headers = client._get_headers()
    assert "Authorization" not in headers


def test_client_dynamic_token_provider() -> None:
    """Verify ApiClient dynamically resolves token from token_provider on every call."""
    mock_httpx = MagicMock(spec=httpx.Client)
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.is_error = False
    mock_resp.json.return_value = {
        "status": "healthy",
        "version": "v1",
        "timestamp": "2026-09-16T12:00:00Z",
    }
    mock_httpx.request.return_value = mock_resp

    current_token = "token_one"
    client = ApiClient(client=mock_httpx, token_provider=lambda: current_token)

    assert client.has_access_token is True
    assert client._resolve_token() == "token_one"
    headers = client._get_headers()
    assert headers["Authorization"] == "Bearer token_one"

    # Token changes dynamically
    current_token = "token_two"
    assert client._resolve_token() == "token_two"
    headers = client._get_headers()
    assert headers["Authorization"] == "Bearer token_two"

    # Explicit access_token parameter overrides token_provider
    client.set_access_token("explicit_token")
    headers = client._get_headers()
    assert headers["Authorization"] == "Bearer explicit_token"



def test_client_check_health() -> None:
    mock_httpx = MagicMock(spec=httpx.Client)
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.is_error = False
    mock_resp.json.return_value = {
        "status": "healthy",
        "version": "v1",
        "timestamp": "2026-09-16T12:00:00Z",
    }
    mock_httpx.request.return_value = mock_resp

    client = ApiClient(client=mock_httpx)
    res = client.check_health()
    assert isinstance(res, HealthResponse)
    assert res.status == "healthy"
    assert res.version == "v1"


def test_client_check_readiness() -> None:
    mock_httpx = MagicMock(spec=httpx.Client)
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.is_error = False
    mock_resp.json.return_value = {
        "status": "ready",
        "database_connected": True,
        "timestamp": "2026-09-16T12:00:00Z",
        "checks": {"database": True},
    }
    mock_httpx.request.return_value = mock_resp

    client = ApiClient(client=mock_httpx)
    res = client.check_readiness()
    assert isinstance(res, ReadinessResponse)
    assert res.status == "ready"
    assert res.database_connected is True
    assert res.db_connected is True


def test_client_get_metrics() -> None:
    mock_httpx = MagicMock(spec=httpx.Client)
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.is_error = False
    mock_resp.json.return_value = {
        "total_runs": 100,
        "successful_runs": 95,
        "held_runs": 4,
        "failed_runs": 1,
        "active_holds_count": 2,
        "system_health": "degraded",
        "latest_checkpoint": "2026-09-16T10:00:00Z",
        "total_records_processed": 5000,
    }
    mock_httpx.request.return_value = mock_resp

    client = ApiClient(client=mock_httpx)
    res = client.get_metrics()
    assert isinstance(res, OperationalMetricsResponse)
    assert res.total_runs == 100
    assert res.active_holds_count == 2
    assert res.system_health == "degraded"
    assert mock_httpx.request.call_args[1]["params"] is None

    # Test with source_name filter
    res_filtered = client.get_metrics(source_name="inventory")
    assert isinstance(res_filtered, OperationalMetricsResponse)
    assert mock_httpx.request.call_args[1]["params"] == {"source_name": "inventory"}


def test_client_recovery_actions() -> None:
    mock_httpx = MagicMock(spec=httpx.Client)
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.is_error = False
    hold_id = uuid4()
    mock_resp.json.return_value = {
        "hold_id": str(hold_id),
        "status": "RELEASED",
        "success": True,
        "message": "Hold released downstream",
        "records_affected": 10,
        "resolved_at": "2026-09-16T12:00:00Z",
    }
    mock_httpx.request.return_value = mock_resp

    client = ApiClient(client=mock_httpx, access_token="test.jwt")
    res = client.release_hold(hold_id=hold_id, operator_reason="Approved")
    assert isinstance(res, RecoveryActionResponse)
    assert res.hold_id == hold_id
    assert res.status == "RELEASED"
    assert res.success is True
    assert res.records_affected == 10

    # Verify request payload & method
    mock_httpx.request.assert_called_once()
    call_args = mock_httpx.request.call_args
    assert call_args.kwargs["method"] == "POST"
    assert f"/api/v1/holds/{hold_id}/release" in call_args.kwargs["url"]
    assert call_args.kwargs["json"]["operator_reason"] == "Approved"


def test_client_error_classification() -> None:
    mock_httpx = MagicMock(spec=httpx.Client)

    # 1. Connection error
    mock_httpx.request.side_effect = httpx.ConnectError("Connection refused")
    client = ApiClient(client=mock_httpx)
    with pytest.raises(ApiConnectionError):
        client.check_health()

    # 2. 401 Unauthorized
    mock_httpx.request.side_effect = None
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.is_error = True
    mock_resp.status_code = 401
    mock_resp.json.return_value = {"error": {"code": "UNAUTHORIZED", "message": "Expired token"}}
    mock_httpx.request.return_value = mock_resp
    with pytest.raises(ApiUnauthorizedError) as exc_info:
        client.list_runs()
    assert "Expired token" in str(exc_info.value)

    # 3. 403 Forbidden
    mock_resp.status_code = 403
    mock_resp.json.return_value = {"error": {"code": "FORBIDDEN", "message": "Not an operator"}}
    with pytest.raises(ApiForbiddenError):
        client.list_runs()

    # 4. 404 Not Found
    mock_resp.status_code = 404
    mock_resp.json.return_value = {"error": {"code": "NOT_FOUND", "message": "Run not found"}}
    with pytest.raises(ApiNotFoundError):
        client.get_run(uuid4())

    # 5. 409 Conflict
    mock_resp.status_code = 409
    mock_resp.json.return_value = {"error": {"code": "CONFLICT", "message": "Already released"}}
    with pytest.raises(ApiConflictError):
        client.release_hold(uuid4())

    # 6. General 500
    mock_resp.status_code = 500
    mock_resp.json.return_value = {"error": {"code": "INTERNAL_ERROR", "message": "Server failure"}}
    with pytest.raises(ApiClientError):
        client.get_metrics()


# ── V2 UI Components Rendering Smoke Tests ──────────────────


def test_render_v2_system_health_strip_smoke() -> None:
    """Smoke test ensuring render_v2_system_health_strip runs without exceptions."""
    metrics = OperationalMetricsResponse(
        total_runs=25,
        successful_runs=20,
        held_runs=4,
        failed_runs=1,
        active_holds_count=1,
        system_health="degraded",
        latest_checkpoint=datetime.now(timezone.utc),
        total_records_processed=1200,
    )
    with patch("streamlit.columns") as mock_cols:
        mock_cols.return_value = [MagicMock() for _ in range(5)]
        with patch("streamlit.markdown") as mock_md:
            render_v2_system_health_strip(
                health_status="healthy",
                db_connected=True,
                metrics=metrics,
                last_refresh_time=datetime.now(timezone.utc),
            )
            assert mock_md.called


def test_render_v2_system_health_strip_metrics_none() -> None:
    """Ensure render_v2_system_health_strip renders database as connected even if metrics is None."""
    with patch("streamlit.columns") as mock_cols:
        mock_cols.return_value = [MagicMock() for _ in range(5)]
        with patch("streamlit.markdown") as mock_md:
            render_v2_system_health_strip(
                health_status="healthy",
                db_connected=True,
                metrics=None,
                last_refresh_time=datetime.now(timezone.utc),
            )
            assert mock_md.called
            calls = [call[0][0] for call in mock_md.call_args_list]
            # Verify DB card renders "Connected"
            assert any("● Connected" in c for c in calls)
            # Verify metrics cards render "—"
            assert any("—" in c for c in calls)


def test_render_v2_hold_card_smoke() -> None:
    """Smoke test ensuring render_v2_hold_card renders all tabs and handles actions."""
    hold_data = HoldResponse(
        hold_id=uuid4(),
        run_id=uuid4(),
        source_name="inventory_source",
        reason="Suspicious high deletion volume",
        severity="HIGH",
        status="HELD",
        records_affected=15,
        evidence={
            "triggered_rules": [
                {"rule_name": "HighDeletionVolume", "severity": "HIGH", "description": "Deleted 15 records"}
            ],
            "guardrail_evidence": {"deleted_count": 15},
        },
        explanation={
            "what_changed": "15 records marked deleted",
            "why_flagged": "Breached deletion quota",
            "containment_summary": "Quarantined downstream",
            "provider": "Gemini",
            "model": "gemini-3.8-flash",
            "grounding_passed": True,
            "evidence_points": ["15 items vanished from source"],
        },
        created_at=datetime.now(timezone.utc),
    )

    on_release = MagicMock()
    on_reprocess = MagicMock()
    on_discard = MagicMock()

    with patch("streamlit.markdown") as mock_md, patch("streamlit.tabs") as mock_tabs:
        mock_tab_list = [MagicMock(), MagicMock(), MagicMock()]
        mock_tabs.return_value = mock_tab_list

        render_v2_hold_card(
            hold=hold_data,
            on_release_callback=on_release,
            on_reprocess_callback=on_reprocess,
            on_discard_callback=on_discard,
        )
        assert mock_md.called


def test_render_containment_tab_correlation() -> None:
    """Ensure containment tab renders latest run and latest hold, and auto-syncs selection."""
    mock_client = MagicMock(spec=ApiClient)
    now = datetime.now(timezone.utc)
    run_id = uuid4()
    hold1_id = uuid4()
    hold2_id = uuid4()

    mock_client.list_runs.return_value = RunListResponse(
        runs=[
            RunResponse(
                run_id=run_id,
                source_name="inventory_source",
                started_at=now,
                completed_at=now,
                created_at=now,
                status="COMPLETED",
                records_seen=10,
                records_changed=2,
                records_held=1,
            )
        ],
        total=1,
        limit=10,
    )

    hold1 = HoldResponse(
        hold_id=hold1_id,
        run_id=run_id,
        source_name="inventory_source",
        reason="Suspicious quantity swing",
        severity="HIGH",
        status="HELD",
        records_affected=1,
        created_at=now,
    )
    hold2 = HoldResponse(
        hold_id=hold2_id,
        run_id=run_id,
        source_name="inventory_source",
        reason="Older hold",
        severity="MEDIUM",
        status="RELEASED",
        records_affected=2,
        created_at=now,
    )

    mock_client.list_holds.return_value = HoldListResponse(
        holds=[hold1, hold2],
        total=2,
        limit=50,
    )

    mock_session = {}
    with patch("streamlit.columns", return_value=[MagicMock(), MagicMock()]), \
         patch("streamlit.selectbox", side_effect=lambda label, options, **kwargs: options[0]), \
         patch("streamlit.markdown") as mock_md, \
         patch("streamlit.session_state", mock_session), \
         patch("app.v2_monitor.render_v2_hold_card") as mock_render_card:

        _render_containment_tab(api_client=mock_client)

        # Assert latest run card and latest hold banner were rendered in markdown
        md_calls = [c[0][0] for c in mock_md.call_args_list if c[0]]
        assert any("LATEST PROCESSING RUN" in c for c in md_calls)
        assert any(str(run_id)[:8] in c for c in md_calls)
        assert any("LATEST DETECTED HOLD" in c for c in md_calls)
        assert any(str(hold1_id)[:8] in c for c in md_calls)

        # Verify hold card was rendered with hold1 (the latest)
        assert mock_render_card.called
        assert mock_render_card.call_args.kwargs["hold"].hold_id == hold1_id

        # Verify session state tracked the latest hold id
        assert mock_session.get("v2_last_seen_newest_hold_id") == str(hold1_id)
        assert mock_session.get("v2_selected_hold_id") == str(hold1_id)


def test_render_runs_tab_correlation() -> None:
    """Ensure runs tab renders latest run banner and table."""
    mock_client = MagicMock(spec=ApiClient)
    now = datetime.now(timezone.utc)
    run_id = uuid4()

    mock_client.list_runs.return_value = RunListResponse(
        runs=[
            RunResponse(
                run_id=run_id,
                source_name="inventory_source",
                started_at=now,
                completed_at=now,
                created_at=now,
                status="COMPLETED",
                records_seen=5,
                records_changed=1,
                records_held=0,
            )
        ],
        total=1,
        limit=10,
    )

    with patch("streamlit.columns", return_value=[MagicMock(), MagicMock()]), \
         patch("streamlit.selectbox", side_effect=lambda label, options, **kwargs: options[0]), \
         patch("streamlit.markdown") as mock_md, \
         patch("streamlit.dataframe") as mock_df, \
         patch("streamlit.expander"):

        _render_runs_tab(api_client=mock_client)

        md_calls = [c[0][0] for c in mock_md.call_args_list if c[0]]
        assert any("LATEST PROCESSING RUN" in c for c in md_calls)
        assert any(str(run_id) in c for c in md_calls)
        assert mock_df.called

