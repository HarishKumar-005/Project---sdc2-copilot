"""Unit tests for Google OIDC authentication identity models, claims parsing, and metadata attachment."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import polars as pl
import pytest
import streamlit as st

from uuid import UUID, uuid4

from src.scd2_copilot.artifacts import (
    RunMetadata,
    list_runs,
    read_run_artifacts,
    write_run_artifacts,
)
from src.scd2_copilot.auth import (
    AuthenticatedUser,
    build_google_login_url,
    clear_all_transactions_for_testing,
    consume_oauth_transaction,
    get_current_request_origin,
    get_current_supabase_session,
    get_current_user,
    get_pending_transactions_count,
    get_supabase_access_token,
    handle_auth_callback,
    is_auth_configured,
    is_user_logged_in,
    set_current_supabase_session,
    store_oauth_transaction,
    trigger_logout,
)
from src.scd2_copilot.auth_supabase import SupabaseSession, generate_pkce_pair
from src.scd2_copilot.config import Settings
from src.scd2_copilot.models import (
    ChangeRecord,
    ChangeReport,
    ChangeType,
    DeletePolicy,
    Explanation,
    OrchestrationSummary,
    PipelineResult,
    SnapshotMode,
    ValidationReport,
    ValidationRule,
    ValidationStatus,
)
from src.scd2_copilot.workflow import run_pipeline


# ── 1. AuthenticatedUser Model Tests ─────────────────────────


def test_authenticated_user_immutability():
    """AuthenticatedUser is frozen and immutable."""
    user = AuthenticatedUser(
        provider="supabase",
        subject="sub_12345",
        email="alice@example.com",
        name="Alice Doe",
    )
    assert user.provider == "supabase"
    assert user.subject == "sub_12345"
    assert user.email == "alice@example.com"
    assert user.name == "Alice Doe"

    with pytest.raises(FrozenInstanceError):
        user.name = "Bob"  # type: ignore


def test_authenticated_user_to_audit_dict():
    """to_audit_dict returns only sanitized identity claims, no tokens or excess fields."""
    user = AuthenticatedUser(
        provider="supabase",
        subject="sub_12345",
        email="alice@example.com",
        name="Alice Doe",
    )
    audit = user.to_audit_dict()
    assert audit == {
        "provider": "supabase",
        "subject": "sub_12345",
        "email": "alice@example.com",
        "name": "Alice Doe",
    }
    # Ensure no token fields exist
    assert "token" not in audit
    assert "id_token" not in audit
    assert "access_token" not in audit
    assert "password" not in audit


def test_authenticated_user_from_dict():
    """from_dict safely constructs AuthenticatedUser or returns None on invalid inputs."""
    data = {
        "provider": "supabase",
        "subject": "sub_987",
        "email": "bob@example.com",
        "name": "Bob Smith",
    }
    user = AuthenticatedUser.from_dict(data)
    assert user is not None
    assert user.subject == "sub_987"
    assert user.email == "bob@example.com"

    assert AuthenticatedUser.from_dict(None) is None
    assert AuthenticatedUser.from_dict({}) is None


# ── 2. Supabase Session & Login State Tests ─────────────────


def test_unauthenticated_state():
    """Unauthenticated state returns False for is_user_logged_in and None for get_current_user."""
    set_current_supabase_session(None)
    assert is_user_logged_in() is False
    assert get_current_user() is None
    assert get_supabase_access_token() is None


def test_authenticated_state_full_claims():
    """Active Supabase session provides authenticated identity and access token."""
    user_id = uuid4()
    mock_session = SupabaseSession(
        access_token="supabase_jwt_access_123",
        user_id=user_id,
        email="analyst@corp.com",
        user_metadata={"full_name": "Data Analyst"},
    )
    set_current_supabase_session(mock_session)

    try:
        assert is_user_logged_in() is True
        user = get_current_user()
        assert user is not None
        assert user.provider == "supabase"
        assert user.subject == str(user_id)
        assert user.email == "analyst@corp.com"
        assert user.name == "Data Analyst"
        assert get_supabase_access_token() == "supabase_jwt_access_123"
    finally:
        set_current_supabase_session(None)


def test_authenticated_state_missing_optional_claims():
    """Active Supabase session handles missing user metadata gracefully."""
    user_id = uuid4()
    mock_session = SupabaseSession(
        access_token="supabase_jwt_access_456",
        user_id=user_id,
        email=None,
        user_metadata={},
    )
    set_current_supabase_session(mock_session)

    try:
        assert is_user_logged_in() is True
        user = get_current_user()
        assert user is not None
        assert user.subject == str(user_id)
        assert user.email is None
        assert user.name == str(user_id)
    finally:
        set_current_supabase_session(None)


def test_auth_configured_check():
    """is_auth_configured detects configuration of Supabase Auth."""
    with patch("src.scd2_copilot.auth.SupabaseAuthService") as mock_svc_cls:
        mock_instance = mock_svc_cls.return_value
        mock_instance.is_configured = True
        assert is_auth_configured() is True

        mock_instance.is_configured = False
        assert is_auth_configured() is False


def test_pkce_generation():
    """generate_pkce_pair produces cryptographically valid verifier and challenge."""
    verifier, challenge = generate_pkce_pair()
    assert isinstance(verifier, str)
    assert len(verifier) >= 43
    assert isinstance(challenge, str)
    assert len(challenge) > 0
    assert "=" not in challenge  # Base64URL unpadded


def test_build_google_login_url():
    """build_google_login_url constructs Supabase Google OAuth URL with state nonce and NO verifier in URL."""
    clear_all_transactions_for_testing()
    with patch("src.scd2_copilot.auth.SupabaseAuthService") as mock_svc_cls:
        mock_instance = mock_svc_cls.return_value
        mock_instance.is_configured = True
        mock_instance.build_google_oauth_url.return_value = "https://example.supabase.co/auth/v1/authorize?provider=google&state=abc"

        url = build_google_login_url(redirect_to="http://localhost:8501")
        assert "authorize?provider=google" in url
        # Verify spkce is NEVER in redirect or URL
        assert "spkce" not in url
        # Verify transaction state was recorded in session state and server store
        assert "pending_oauth_state" in st.session_state
        state = st.session_state["pending_oauth_state"]
        assert get_pending_transactions_count() == 1

        # Verify call to service included code_challenge and state
        call_kwargs = mock_instance.build_google_oauth_url.call_args.kwargs
        assert "code_challenge" in call_kwargs
        assert call_kwargs["state"] == state
        assert call_kwargs["redirect_to"] == "http://localhost:8501"


def test_handle_auth_callback_code_exchange():
    """handle_auth_callback exchanges code parameter for session using bound state and clears query params."""
    clear_all_transactions_for_testing()
    user_id = uuid4()
    mock_session = SupabaseSession(
        access_token="token_exchanged_789",
        user_id=user_id,
        email="operator@test.com",
    )

    state = "valid_oauth_state_nonce_123"
    code_verifier = "test_code_verifier_xyz_secure_random"
    store_oauth_transaction(state=state, code_verifier=code_verifier, origin="http://localhost:8501")

    st.query_params["code"] = "auth_code_mock_123"
    st.query_params["state"] = state
    st.session_state["pending_oauth_state"] = state

    with patch("src.scd2_copilot.auth.SupabaseAuthService") as mock_svc_cls, patch.object(st, "rerun"):
        mock_instance = mock_svc_cls.return_value
        mock_instance.exchange_code_for_session.return_value = mock_session

        res = handle_auth_callback()
        assert res == mock_session
        assert is_user_logged_in() is True
        assert "code" not in st.query_params
        assert "state" not in st.query_params
        assert "pending_oauth_state" not in st.session_state

        # Verify transaction was atomically consumed (one-time use)
        assert consume_oauth_transaction(state) is None

    set_current_supabase_session(None)


def test_handle_auth_callback_missing_state_rejected():
    """handle_auth_callback fails closed when callback lacks state and store is empty."""
    clear_all_transactions_for_testing()
    st.query_params["code"] = "orphan_auth_code_without_state"
    st.session_state.pop("pending_oauth_state", None)

    with patch("src.scd2_copilot.auth.SupabaseAuthService") as mock_svc_cls:
        mock_instance = mock_svc_cls.return_value
        res = handle_auth_callback()
        assert res is None
        assert not is_user_logged_in()
        assert "code" not in st.query_params
        assert "auth_error" in st.session_state


def test_handle_auth_callback_omitted_state_uses_latest_transaction():
    """handle_auth_callback succeeds when provider omits state and session is fresh (new tab)."""
    clear_all_transactions_for_testing()
    user_id = uuid4()
    mock_session = SupabaseSession(
        access_token="token_fallback_123",
        user_id=user_id,
        email="operator@test.com",
    )

    # Simulate user initiating login: transaction stored, but callback returns in fresh tab
    state = "state_from_link_button_click"
    code_verifier = "verifier_for_new_tab_test"
    store_oauth_transaction(state=state, code_verifier=code_verifier, origin="http://localhost:8501")

    # In new tab: no state param in query, no pending_oauth_state in session
    st.query_params["code"] = "auth_code_omitted_state"
    st.query_params.pop("state", None)
    st.query_params.pop("oauth_state", None)
    st.session_state.pop("pending_oauth_state", None)

    with patch("src.scd2_copilot.auth.SupabaseAuthService") as mock_svc_cls, patch.object(st, "rerun"):
        mock_instance = mock_svc_cls.return_value
        mock_instance.exchange_code_for_session.return_value = mock_session

        res = handle_auth_callback()
        assert res == mock_session
        assert is_user_logged_in() is True
        assert "code" not in st.query_params
        assert get_pending_transactions_count() == 0

    # Replay attempt fails
    st.query_params["code"] = "auth_code_omitted_state"
    set_current_supabase_session(None)
    with patch("src.scd2_copilot.auth.SupabaseAuthService") as mock_svc_cls:
        res2 = handle_auth_callback()
        assert res2 is None
        assert not is_user_logged_in()

    set_current_supabase_session(None)


def test_handle_auth_callback_replay_rejected():
    """Replaying an already-consumed state fails closed."""
    clear_all_transactions_for_testing()
    state = "replayed_state_nonce"
    store_oauth_transaction(state=state, code_verifier="verifier_abc", origin="http://localhost:8501")

    # First consumption succeeds
    first = consume_oauth_transaction(state)
    assert first == "verifier_abc"

    # Second consumption must fail
    assert consume_oauth_transaction(state) is None


def test_simultaneous_login_initialization_no_collision():
    """User A and User B start OAuth simultaneously; each gets their own isolated verifier without collision."""
    clear_all_transactions_for_testing()
    with patch("src.scd2_copilot.auth.SupabaseAuthService") as mock_svc_cls:
        mock_instance = mock_svc_cls.return_value
        mock_instance.is_configured = True

        # User A initializes login
        build_google_login_url(redirect_to="http://localhost:8501")
        call_A = mock_instance.build_google_oauth_url.call_args.kwargs
        state_A = call_A["state"]
        challenge_A = call_A["code_challenge"]

        # User B initializes login
        build_google_login_url(redirect_to="http://localhost:8501")
        call_B = mock_instance.build_google_oauth_url.call_args.kwargs
        state_B = call_B["state"]
        challenge_B = call_B["code_challenge"]

        # States and challenges must be distinct
        assert state_A != state_B
        assert challenge_A != challenge_B
        assert get_pending_transactions_count() == 2

        # User A completes callback -> receives User A's verifier
        verifier_A = consume_oauth_transaction(state_A)
        assert verifier_A is not None

        # User B completes callback -> receives User B's verifier
        verifier_B = consume_oauth_transaction(state_B)
        assert verifier_B is not None
        assert verifier_A != verifier_B

        # Both transactions are consumed
        assert get_pending_transactions_count() == 0


def test_session_dataclass_repr_redacts_tokens():
    """SupabaseSession __repr__ and __str__ never expose access_token or refresh_token."""
    secret_token = "ey.very_sensitive_secret_jwt_access_token"
    secret_refresh = "sensitive_refresh_token_value_xyz"
    session = SupabaseSession(
        access_token=secret_token,
        user_id=uuid4(),
        email="test@company.com",
        refresh_token=secret_refresh,
    )
    rep = repr(session)
    assert secret_token not in rep
    assert secret_refresh not in rep


def test_manual_token_session_override_removed():
    """Arbitrary api_access_token in session_state is ignored; token strictly requires active SupabaseSession."""
    st.session_state["api_access_token"] = "forged_manual_token"
    set_current_supabase_session(None)
    assert get_supabase_access_token() is None


def test_handle_auth_callback_error():
    """handle_auth_callback records OAuth error in session state."""
    st.query_params["error"] = "access_denied"
    st.query_params["error_description"] = "User cancelled OAuth consent"

    try:
        res = handle_auth_callback()
        assert res is None
        assert "error" not in st.query_params
        assert "access_denied" in st.session_state.get("auth_error", "")
    finally:
        st.session_state.pop("auth_error", None)


def test_trigger_logout():
    """trigger_logout revokes session on server and purges session state."""
    user_id = uuid4()
    mock_session = SupabaseSession(
        access_token="token_to_revoke",
        user_id=user_id,
        email="logout@test.com",
    )
    set_current_supabase_session(mock_session)

    with patch("src.scd2_copilot.auth.SupabaseAuthService") as mock_svc_cls, patch.object(st, "rerun"):
        mock_instance = mock_svc_cls.return_value
        mock_instance.sign_out.return_value = True

        trigger_logout()

        assert is_user_logged_in() is False
        assert get_current_supabase_session() is None
        mock_instance.sign_out.assert_called_once_with("token_to_revoke")


def test_get_current_request_origin_from_url_and_headers():
    """get_current_request_origin correctly extracts origin from context url or headers."""
    # Context with url
    mock_ctx = type("MockCtx", (), {"url": "https://sdc2-copilot.streamlit.app/some_page"})()
    with patch.object(st, "context", mock_ctx):
        assert get_current_request_origin() == "https://sdc2-copilot.streamlit.app"

    # Context with headers (forwarded host on cloud)
    mock_ctx_headers = type(
        "MockCtxHeaders",
        (),
        {
            "url": None,
            "headers": {"x-forwarded-host": "sdc2-copilot.streamlit.app", "x-forwarded-proto": "https"},
        },
    )()
    with patch.object(st, "context", mock_ctx_headers):
        assert get_current_request_origin() == "https://sdc2-copilot.streamlit.app"

    # Context with localhost
    mock_ctx_local = type(
        "MockCtxLocal",
        (),
        {
            "url": None,
            "headers": {"host": "localhost:8501"},
        },
    )()
    with patch.object(st, "context", mock_ctx_local):
        assert get_current_request_origin() == "http://localhost:8501"


# ── 3. Run Metadata Backward Compatibility & Persistence Tests 


def test_run_metadata_backward_compatibility():
    """RunMetadata.from_dict loads legacy JSON lacking created_by cleanly as None."""
    legacy_json = {
        "run_id": "run_20260901_legacy",
        "processing_date": "2026-09-01",
        "snapshot_mode": "full",
        "delete_policy": "soft_delete",
        "total_duration_seconds": 0.42,
    }
    meta = RunMetadata.from_dict(legacy_json)
    assert meta.run_id == "run_20260901_legacy"
    assert meta.created_by is None
    assert meta.metadata_version == "1.1"


def test_write_run_artifacts_persists_created_by(tmp_path: Path):
    """write_run_artifacts includes created_by in metadata.json without secrets."""
    scd2_df = pl.DataFrame({
        "id": [1, 2],
        "val": ["a", "b"],
        "effective_from": [date(2026, 9, 1), date(2026, 9, 1)],
        "effective_to": [None, None],
        "is_current": [True, True],
    })
    cr = ChangeReport(
        new=[ChangeRecord(business_key_values={"id": 1}, change_type=ChangeType.NEW), ChangeRecord(business_key_values={"id": 2}, change_type=ChangeType.NEW)],
        snapshot_mode=SnapshotMode.FULL,
        delete_policy=DeletePolicy.SOFT_DELETE,
    )
    vr = ValidationReport(
        rules=[
            ValidationRule("no_null_keys", ValidationStatus.PASS, "No null keys"),
            ValidationRule("one_current_per_key", ValidationStatus.PASS, "One current row per key"),
            ValidationRule("no_overlapping_dates", ValidationStatus.PASS, "No overlapping validity intervals"),
            ValidationRule("date_consistency", ValidationStatus.PASS, "Valid date ranges"),
            ValidationRule("historical_immutability", ValidationStatus.PASS, "Immutable history"),
        ]
    )
    pipe_res = PipelineResult(
        change_report=cr,
        scd2_output=scd2_df,
        validation_report=vr,
        explanations=[],
        business_key=["id"],
        tracked_columns=["val"],
        execution_time=0.15,
    )

    creator_audit = {
        "provider": "google",
        "subject": "sub_audit_999",
        "email": "auditor@corp.com",
        "name": "Audit User",
    }

    settings = Settings()
    meta = write_run_artifacts(
        pipeline_result=pipe_res,
        run_id="run_test_audit_01",
        base_dir=tmp_path,
        created_by=creator_audit,
        settings=settings,
    )

    assert meta.created_by == creator_audit

    # Read back metadata directly from disk
    persisted = read_run_artifacts("run_test_audit_01", base_dir=tmp_path)
    assert persisted.metadata.created_by == creator_audit

    # Ensure list_runs surfaces created_by
    runs = list_runs(base_dir=tmp_path)
    assert len(runs) == 1
    assert runs[0].created_by == creator_audit


def test_run_pipeline_attaches_created_by(tmp_path: Path):
    """run_pipeline attaches created_by to OrchestrationSummary and metadata."""
    src = pl.DataFrame({"id": [1, 2], "val": ["x", "y"]})
    tgt = pl.DataFrame({
        "id": [1],
        "val": ["x"],
        "effective_from": [date(2026, 9, 1)],
        "effective_to": [None],
        "is_current": [True],
    })

    user_meta = {
        "provider": "google",
        "subject": "sub_pipeline_user",
        "email": "pipeline@test.com",
        "name": "Pipeline Tester",
    }

    settings = Settings()
    settings.runs_directory = str(tmp_path)

    result = run_pipeline(
        source=src,
        target=tgt,
        processing_date=date(2026, 9, 2),
        business_key_override=["id"],
        tracked_columns_override=["val"],
        settings=settings,
        created_by=user_meta,
    )

    assert result.orchestration_summary is not None
    assert result.orchestration_summary.created_by == user_meta

    # Verify persisted run contains created_by
    runs = list_runs(base_dir=tmp_path)
    assert len(runs) >= 1
    matched = [r for r in runs if r.created_by == user_meta]
    assert len(matched) == 1


# ── 12. Request Origin & Redirect Resolution Tests ───────────


def test_get_current_request_origin_from_settings():
    """Verify settings.streamlit_app_url takes top priority."""
    with patch("src.scd2_copilot.auth.get_settings") as mock_settings:
        mock_settings.return_value = Settings(streamlit_app_url="https://scd2-demo.streamlit.app/")
        origin = get_current_request_origin()
        assert origin == "https://scd2-demo.streamlit.app"


def test_get_current_request_origin_from_secrets():
    """Verify st.secrets['STREAMLIT_APP_URL'] is resolved when settings URL is blank."""
    with patch("src.scd2_copilot.auth.get_settings") as mock_settings:
        mock_settings.return_value = Settings(streamlit_app_url="")
        with patch.object(st, "secrets", {"STREAMLIT_APP_URL": "https://cloud-app.streamlit.app/"}):
            origin = get_current_request_origin()
            assert origin == "https://cloud-app.streamlit.app"


def test_get_current_request_origin_from_context_url():
    """Verify st.context.url is used when no explicit settings/secrets are provided."""
    with patch("src.scd2_copilot.auth.get_settings") as mock_settings:
        mock_settings.return_value = Settings(streamlit_app_url="")
        with patch.object(st, "secrets", {}):
            mock_ctx = MagicMock()
            mock_ctx.url = "https://subdomain.streamlit.app/some/path?param=1"
            mock_ctx.headers = {}
            with patch.object(st, "context", mock_ctx):
                origin = get_current_request_origin()
                assert origin == "https://subdomain.streamlit.app"


def test_get_current_request_origin_from_headers():
    """Verify st.context.headers['x-forwarded-host'] is used as fallback."""
    with patch("src.scd2_copilot.auth.get_settings") as mock_settings:
        mock_settings.return_value = Settings(streamlit_app_url="")
        with patch.object(st, "secrets", {}):
            mock_ctx = MagicMock()
            mock_ctx.url = None
            mock_ctx.headers = {
                "x-forwarded-host": "my-proxy-app.streamlit.app",
                "x-forwarded-proto": "https",
            }
            with patch.object(st, "context", mock_ctx):
                origin = get_current_request_origin()
                assert origin == "https://my-proxy-app.streamlit.app"


def test_get_current_request_origin_from_render_env(monkeypatch):
    """Verify RENDER_EXTERNAL_URL is automatically detected on Render."""
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://project-sdc2-copilot.onrender.com/")
    with patch("src.scd2_copilot.auth.get_settings") as mock_settings:
        mock_settings.return_value = Settings(streamlit_app_url="")
        with patch.object(st, "secrets", {}):
            origin = get_current_request_origin()
            assert origin == "https://project-sdc2-copilot.onrender.com"


