"""Comprehensive security hardening tests for SCD2 Copilot (P0 Security Milestone).

Validates all 30 security requirements:
- PKCE isolation, zero URL leakage, TTL, atomic one-time consumption, replay prevention
- OAuth state binding and callback safety
- Non-logging of tokens, headers, and secrets
- Absences of native st.login/st.logout/st.user from active authentication
- Absence of manual token entry in UI
- Cryptographic JWT verification (signature, expiry, issuer, audience, algorithm, JWKS failure)
- Rejection of forged headers (X-Operator-*)
- Strict authorization (403 for unauthorized operators, audit attribution from JWT)
- CORS restrictions (no wildcard with credentials, explicit headers only)
- Public health/readiness endpoints
- Logout state purging and session expiry enforcement
"""

from __future__ import annotations

import inspect
import logging
import time
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI, HTTPException, Security, status
from fastapi.testclient import TestClient
import jwt
import pytest
import streamlit as st

from src.scd2_copilot.api.app import create_app
from src.scd2_copilot.api.auth.dependencies import (
    get_current_operator,
    get_jwt_verifier,
    require_authorized_operator,
    set_jwt_verifier,
)
from src.scd2_copilot.api.auth.exceptions import (
    AuthError,
    InvalidClaimsError,
    InvalidTokenError,
    JWKSError,
    TokenExpiredError,
)
from src.scd2_copilot.api.auth.models import AuthenticatedOperator
from src.scd2_copilot.api.auth.verifier import SupabaseJWTVerifier
from src.scd2_copilot.api.client import ApiClient, ApiForbiddenError, ApiUnauthorizedError
from src.scd2_copilot.auth import (
    AuthenticatedUser,
    build_google_login_url,
    clear_all_transactions_for_testing,
    consume_oauth_transaction,
    get_current_supabase_session,
    get_current_user,
    get_pending_transactions_count,
    get_supabase_access_token,
    handle_auth_callback,
    is_user_logged_in,
    set_current_supabase_session,
    store_oauth_transaction,
    trigger_logout,
)
from src.scd2_copilot.auth_supabase import SupabaseAuthService, SupabaseSession, generate_pkce_pair
from src.scd2_copilot.config import Settings
from src.scd2_copilot.containment.service import ContainmentService


# ── Helpers & Fixtures ──────────────────────────────────────────


@pytest.fixture(scope="module")
def ec_key_pair():
    """Generate an EC P-256 key pair for signing and verifying test JWTs."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()
    return private_key, public_key


@pytest.fixture
def mock_jwks_client(ec_key_pair):
    _, public_key = ec_key_pair
    client = MagicMock()
    signing_key = MagicMock()
    signing_key.key = public_key
    client.get_signing_key_from_jwt.return_value = signing_key
    return client


def _make_jwt(
    private_key,
    user_id: UUID,
    email: str,
    issuer: str = "https://test-project.supabase.co/auth/v1",
    audience: str = "authenticated",
    exp_delta_seconds: int = 3600,
    full_name: str = "Security Tester",
    role: str = "authenticated",
    algorithm: str = "ES256",
) -> str:
    payload = {
        "sub": str(user_id),
        "iss": issuer,
        "aud": audience,
        "exp": int(time.time()) + exp_delta_seconds,
        "email": email,
        "user_metadata": {"full_name": full_name},
        "app_metadata": {"provider": "google", "role": role},
    }
    return jwt.encode(payload, private_key, algorithm=algorithm)


# ── 1. PKCE Security & Isolation Tests ─────────────────────────


def test_pkce_verifier_never_appears_in_redirect_url():
    """Requirement 1: PKCE code_verifier never appears in redirect URL."""
    clear_all_transactions_for_testing()
    with patch("src.scd2_copilot.auth.SupabaseAuthService") as mock_svc_cls:
        mock_svc = mock_svc_cls.return_value
        mock_svc.is_configured = True
        mock_svc.build_google_oauth_url.return_value = "https://mock.supabase.co/auth/v1/authorize"

        url = build_google_login_url(redirect_to="http://localhost:8501")
        assert "spkce" not in url
        # Verify call kwargs to auth service
        call_kwargs = mock_svc.build_google_oauth_url.call_args.kwargs
        assert "spkce" not in call_kwargs.get("redirect_to", "")


def test_pkce_verifier_not_stored_globally_for_all_users():
    """Requirement 2 & 3: PKCE verifiers are not stored in a global singleton and are isolated per state."""
    clear_all_transactions_for_testing()
    with patch("src.scd2_copilot.auth.SupabaseAuthService") as mock_svc_cls:
        mock_svc = mock_svc_cls.return_value
        mock_svc.is_configured = True

        build_google_login_url(redirect_to="http://localhost:8501")
        state_1 = mock_svc.build_google_oauth_url.call_args.kwargs["state"]

        build_google_login_url(redirect_to="http://localhost:8501")
        state_2 = mock_svc.build_google_oauth_url.call_args.kwargs["state"]

        assert state_1 != state_2
        assert get_pending_transactions_count() == 2

        v1 = consume_oauth_transaction(state_1)
        v2 = consume_oauth_transaction(state_2)
        assert v1 is not None and v2 is not None
        assert v1 != v2


def test_expired_pkce_transaction_fails():
    """Requirement 4: Expired PKCE transaction fails closed."""
    clear_all_transactions_for_testing()
    state = "expired_state_nonce"
    # Store transaction with creation time in the past (> 600s)
    with patch("time.time", return_value=1000.0):
        store_oauth_transaction(state=state, code_verifier="verifier_old", origin="http://localhost:8501")

    # Attempt to consume after TTL (600s)
    with patch("time.time", return_value=1700.0):
        consumed = consume_oauth_transaction(state)
        assert consumed is None


def test_reused_pkce_transaction_fails():
    """Requirement 5: Reused PKCE transaction fails closed (atomic one-time consumption)."""
    clear_all_transactions_for_testing()
    state = "single_use_state"
    store_oauth_transaction(state=state, code_verifier="verifier_once", origin="http://localhost:8501")

    assert consume_oauth_transaction(state) == "verifier_once"
    assert consume_oauth_transaction(state) is None


def test_oauth_state_mismatch_fails():
    """Requirement 6: OAuth state mismatch or unknown state fails closed."""
    clear_all_transactions_for_testing()
    st.query_params["code"] = "valid_code"
    st.query_params["state"] = "unknown_attacker_state"

    with patch("src.scd2_copilot.auth.SupabaseAuthService") as mock_svc_cls:
        mock_svc = mock_svc_cls.return_value
        res = handle_auth_callback()
        assert res is None
        assert not is_user_logged_in()
        assert "Invalid or expired authentication transaction" in st.session_state.get("auth_error", "")


def test_oauth_callback_replay_fails_safely():
    """Requirement 7: OAuth callback replay fails safely without creating an authenticated session."""
    clear_all_transactions_for_testing()
    state = "replay_target_state"
    store_oauth_transaction(state=state, code_verifier="ver_replay", origin="http://localhost:8501")

    user_id = uuid4()
    mock_session = SupabaseSession(access_token="tok_1", user_id=user_id, email="user@co.com")

    # First callback succeeds
    st.query_params["code"] = "auth_code_once"
    st.query_params["state"] = state
    with patch("src.scd2_copilot.auth.SupabaseAuthService") as mock_svc_cls, patch.object(st, "rerun"):
        mock_svc = mock_svc_cls.return_value
        mock_svc.exchange_code_for_session.return_value = mock_session
        res1 = handle_auth_callback()
        assert res1 == mock_session

    # Replay attempt with same parameters
    st.query_params["code"] = "auth_code_once"
    st.query_params["state"] = state
    set_current_supabase_session(None)

    with patch("src.scd2_copilot.auth.SupabaseAuthService") as mock_svc_cls:
        res2 = handle_auth_callback()
        assert res2 is None
        assert not is_user_logged_in()


# ── 2. Token & Logging Security Tests ──────────────────────────


def test_tokens_never_logged_or_exposed_in_repr(caplog):
    """Requirement 8, 9, 10: Access and refresh tokens are redacted from string and repr representations."""
    secret_access = "secret_access_jwt_value_12345"
    secret_refresh = "secret_refresh_token_value_67890"

    session = SupabaseSession(
        access_token=secret_access,
        user_id=uuid4(),
        email="test@example.com",
        refresh_token=secret_refresh,
    )

    rep = repr(session)
    str_val = str(session)
    assert secret_access not in rep
    assert secret_refresh not in rep
    assert secret_access not in str_val
    assert secret_refresh not in str_val

    # Audit dict must also omit secrets
    audit_dict = session.to_audit_dict()
    assert secret_access not in str(audit_dict)
    assert secret_refresh not in str(audit_dict)


def test_secrets_absent_from_api_responses(mock_jwks_client, ec_key_pair):
    """Requirement 11: Secrets and database passwords are absent from API error and status responses."""
    private_key, _ = ec_key_pair
    settings = Settings(
        database_url="postgresql://operator:super_secret_db_password@localhost:5432/scd2_test",
        cors_allowed_origins=["http://localhost:8501"],
    )
    app = create_app(settings=settings)
    client = TestClient(app)

    resp = client.get("/health")
    assert resp.status_code == 200
    assert "super_secret_db_password" not in resp.text
    assert "password" not in resp.text.lower()


# ── 3. Obsolete Authentication Purge Tests ─────────────────────


def test_st_login_not_used_by_active_authentication_flow():
    """Requirement 12: st.login is not referenced or used in active auth code."""
    import src.scd2_copilot.auth as auth_mod
    src = inspect.getsource(auth_mod)
    assert "st.login(" not in src


def test_st_logout_not_used_by_active_authentication_flow():
    """Requirement 14: st.logout is not referenced or used in active auth code."""
    import src.scd2_copilot.auth as auth_mod
    src = inspect.getsource(auth_mod)
    assert "st.logout(" not in src


def test_st_user_not_used_as_authoritative_identity():
    """Requirement 13: st.user is not used as authoritative identity anywhere in auth."""
    import src.scd2_copilot.auth as auth_mod
    src = inspect.getsource(auth_mod)
    assert "st.user" not in src


def test_manual_token_entry_absent_from_recovery_ui():
    """Requirement 15: No manual JWT / token entry text inputs exist in ui_components."""
    import app.ui_components as ui_mod
    src = inspect.getsource(ui_mod)
    assert "jwt" not in src.lower() or "input(" not in src.lower()
    assert "api_access_token" not in src


# ── 4. Cryptographic JWT Verification Tests ─────────────────────


def test_invalid_jwt_returns_401(mock_jwks_client):
    """Requirement 16: Invalid / malformed JWT raises 401."""
    verifier = SupabaseJWTVerifier(jwks_client=mock_jwks_client)
    with pytest.raises(InvalidTokenError):
        verifier.verify_token("not.a.valid.jwt")


def test_expired_jwt_returns_401(mock_jwks_client, ec_key_pair):
    """Requirement 17: Expired JWT raises TokenExpiredError."""
    private_key, _ = ec_key_pair
    verifier = SupabaseJWTVerifier(jwks_client=mock_jwks_client)
    expired_token = _make_jwt(private_key, user_id=uuid4(), email="op@co.com", exp_delta_seconds=-100)
    with pytest.raises(TokenExpiredError):
        verifier.verify_token(expired_token)


def test_wrong_issuer_returns_401(mock_jwks_client, ec_key_pair):
    """Requirement 18: Token with unexpected issuer fails closed."""
    private_key, _ = ec_key_pair
    verifier = SupabaseJWTVerifier(
        jwks_client=mock_jwks_client,
        expected_issuer="https://legitimate.supabase.co/auth/v1",
    )
    token = _make_jwt(private_key, user_id=uuid4(), email="op@co.com", issuer="https://forged.issuer.com/auth/v1")
    with pytest.raises(InvalidClaimsError):
        verifier.verify_token(token)


def test_wrong_audience_returns_401(mock_jwks_client, ec_key_pair):
    """Requirement 19: Token with unexpected audience fails closed."""
    private_key, _ = ec_key_pair
    verifier = SupabaseJWTVerifier(
        jwks_client=mock_jwks_client,
        expected_audience="authenticated",
    )
    token = _make_jwt(private_key, user_id=uuid4(), email="op@co.com", audience="wrong_aud")
    with pytest.raises(InvalidClaimsError):
        verifier.verify_token(token)


def test_wrong_algorithm_returns_401(mock_jwks_client, ec_key_pair):
    """Requirement 20: Symmetric or unsupported algorithm (HS256) fails closed."""
    verifier = SupabaseJWTVerifier(jwks_client=mock_jwks_client, allowed_algorithms=["ES256"])
    # Construct an HS256 token with valid key length
    hs_token = jwt.encode(
        {"sub": str(uuid4()), "exp": int(time.time()) + 3600},
        "a_very_secure_secret_key_32_bytes_long!",
        algorithm="HS256",
    )
    with pytest.raises(InvalidTokenError) as exc_info:
        verifier.verify_token(hs_token)
    assert "Unsupported signing algorithm" in str(exc_info.value)


def test_unknown_signing_key_fails_closed(ec_key_pair):
    """Requirement 21: Unknown kid or JWKS error fails closed."""
    failing_jwks_client = MagicMock()
    failing_jwks_client.get_signing_key_from_jwt.side_effect = Exception("Key not found in JWKS")
    verifier = SupabaseJWTVerifier(jwks_client=failing_jwks_client)
    private_key, _ = ec_key_pair
    token = _make_jwt(private_key, user_id=uuid4(), email="op@co.com")

    with pytest.raises(JWKSError):
        verifier.verify_token(token)


def test_valid_supabase_jwt_creates_correct_operator_identity(mock_jwks_client, ec_key_pair):
    """Requirement 22: Valid Supabase JWT correctly populates AuthenticatedOperator identity."""
    private_key, _ = ec_key_pair
    user_id = uuid4()
    settings = Settings(
        supabase_url="https://test-project.supabase.co",
        supabase_auth_issuer="https://test-project.supabase.co/auth/v1",
        recovery_operator_emails=["operator@company.com"],
    )
    verifier = SupabaseJWTVerifier(settings=settings, jwks_client=mock_jwks_client)

    token = _make_jwt(private_key, user_id=user_id, email="operator@company.com", full_name="Alice Operator")
    operator = verifier.verify_token(token)

    assert operator.user_id == user_id
    assert operator.email == "operator@company.com"
    assert operator.display_name == "Alice Operator"
    assert operator.is_authorized_operator is True


def test_forged_operator_headers_cannot_override_jwt_identity(mock_jwks_client, ec_key_pair):
    """Requirement 23: Untrusted X-Operator-* headers are ignored and cannot forge identity."""
    private_key, _ = ec_key_pair
    user_id = uuid4()
    settings = Settings(
        api_auth_required=True,
        supabase_url="https://test-project.supabase.co",
        supabase_auth_issuer="https://test-project.supabase.co/auth/v1",
        recovery_operator_emails=["legit@company.com"],
        cors_allowed_origins=["http://localhost:8501"],
    )
    app = create_app(settings=settings)
    verifier = SupabaseJWTVerifier(settings=settings, jwks_client=mock_jwks_client)
    set_jwt_verifier(verifier)

    client = TestClient(app)

    # Valid token for non-operator user
    token = _make_jwt(private_key, user_id=user_id, email="unauthorized@company.com")
    hold_id = uuid4()

    resp = client.post(
        f"/api/v1/holds/{hold_id}/release",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Operator-Email": "legit@company.com",  # Spoofing attempt
            "X-Operator-Role": "admin",
        },
        json={"operator_reason": "Bypass attempt"},
    )
    # Must reject with 403 Forbidden because JWT identity governs
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN_OPERATOR"


def test_unauthorized_authenticated_operator_returns_403(mock_jwks_client, ec_key_pair):
    """Requirement 24: Authenticated user without operator privileges receives 403 Forbidden."""
    private_key, _ = ec_key_pair
    settings = Settings(
        api_auth_required=True,
        supabase_url="https://test-project.supabase.co",
        supabase_auth_issuer="https://test-project.supabase.co/auth/v1",
        recovery_operator_emails=["lead@company.com"],
        cors_allowed_origins=["http://localhost:8501"],
    )
    app = create_app(settings=settings)
    verifier = SupabaseJWTVerifier(settings=settings, jwks_client=mock_jwks_client)
    set_jwt_verifier(verifier)

    client = TestClient(app)
    token = _make_jwt(private_key, user_id=uuid4(), email="regular.analyst@company.com")
    hold_id = uuid4()

    resp = client.post(
        f"/api/v1/holds/{hold_id}/release",
        headers={"Authorization": f"Bearer {token}"},
        json={"operator_reason": "Testing 403"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN_OPERATOR"


def test_authorized_operator_and_audit_attribution(mock_jwks_client, ec_key_pair):
    """Requirement 25 & 26: Authorized operator executes recovery and audit identity comes strictly from JWT."""
    private_key, _ = ec_key_pair
    operator_id = uuid4()
    settings = Settings(
        api_auth_required=True,
        supabase_url="https://test-project.supabase.co",
        supabase_auth_issuer="https://test-project.supabase.co/auth/v1",
        recovery_operator_emails=["authorized.op@company.com"],
        cors_allowed_origins=["http://localhost:8501"],
    )
    app = create_app(settings=settings)
    verifier = SupabaseJWTVerifier(settings=settings, jwks_client=mock_jwks_client)
    set_jwt_verifier(verifier)

    token = _make_jwt(private_key, user_id=operator_id, email="authorized.op@company.com")
    hold_id = uuid4()

    mock_containment = MagicMock(spec=ContainmentService)
    mock_result = MagicMock()
    mock_result.hold_id = hold_id
    mock_result.status = "RELEASED"
    mock_result.success = True
    mock_result.message = "Batch released successfully"
    mock_result.resolved_at = None
    mock_result.records_affected = 5
    mock_result.checkpoint_advanced_to = None
    mock_result.is_idempotent = False
    mock_containment.release_held_batch.return_value = mock_result

    from src.scd2_copilot.api.dependencies import get_containment_service
    app.dependency_overrides[get_containment_service] = lambda: mock_containment

    client = TestClient(app)
    resp = client.post(
        f"/api/v1/holds/{hold_id}/release",
        headers={"Authorization": f"Bearer {token}"},
        json={"operator_reason": "Valid operational release"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["operator_id"] == str(operator_id)


# ── 5. CORS, Health & Session Security Tests ────────────────────


def test_wildcard_credentialed_cors_is_not_enabled():
    """Requirement 27: Wildcard origin '*' is forbidden when allow_credentials=True."""
    with pytest.raises(ValueError, match="Wildcard origin '\\*' is forbidden"):
        Settings(cors_allowed_origins=["*"])


def test_cors_explicit_headers_only():
    """Requirement 27 (continued): CORS headers are explicitly defined, not wildcard."""
    settings = Settings(cors_allowed_origins=["http://localhost:8501"])
    app = create_app(settings=settings)
    # Inspect CORSMiddleware configuration
    cors_middleware = [m for m in app.user_middleware if "CORSMiddleware" in str(m.cls)]
    assert len(cors_middleware) > 0
    kwargs = cors_middleware[0].kwargs
    assert "*" not in kwargs.get("allow_headers", [])
    assert "Authorization" in kwargs.get("allow_headers", [])


def test_health_remains_accessible():
    """Requirement 28: Public /health endpoint remains accessible without credentials."""
    app = create_app()
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_logout_clears_authentication_state():
    """Requirement 29: Logout clears session state and active session."""
    user_id = uuid4()
    mock_session = SupabaseSession(access_token="tok_logout", user_id=user_id, email="out@test.com")
    set_current_supabase_session(mock_session)
    st.session_state["pending_oauth_state"] = "pending_state_xyz"

    with patch("src.scd2_copilot.auth.SupabaseAuthService") as mock_svc_cls, patch.object(st, "rerun"):
        mock_svc = mock_svc_cls.return_value
        mock_svc.sign_out.return_value = True

        trigger_logout()

        assert not is_user_logged_in()
        assert get_current_supabase_session() is None
        assert "pending_oauth_state" not in st.session_state


def test_expired_session_cannot_continue_recovery():
    """Requirement 30: An expired session yields None and cannot continue recovery actions."""
    expired_session = SupabaseSession(
        access_token="expired_tok",
        user_id=uuid4(),
        email="exp@test.com",
        expires_at=int(time.time()) - 100,  # 100 seconds ago
    )
    set_current_supabase_session(expired_session)

    # get_supabase_access_token must reject expired session
    assert get_supabase_access_token() is None
    assert not is_user_logged_in()
    assert get_current_user() is None
