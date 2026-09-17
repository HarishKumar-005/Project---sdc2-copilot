"""End-to-end integration tests for Supabase Auth JWT verification and ApiClient recovery operations (V2.6).

Verifies the single authoritative authentication path:
  Supabase Auth (ES256 JWT)
    ↓
  SupabaseSession
    ↓
  ApiClient (token_provider)
    ↓
  Authorization: Bearer <access_token>
    ↓
  FastAPI get_current_operator
    ↓
  SupabaseJWTVerifier (cryptographic ES256 verification via JWKS)
    ↓
  AuthenticatedOperator (derived strictly from JWT claims)
    ↓
  require_operator authorization check
    ↓
  ContainmentService (RELEASE / REPROCESS / DISCARD)
"""

from __future__ import annotations

from datetime import datetime, timezone
import time
from unittest.mock import MagicMock
from uuid import UUID, uuid4
import pytest

from cryptography.hazmat.primitives.asymmetric import ec
import httpx
import jwt

from src.scd2_copilot.api.app import create_app
from src.scd2_copilot.api.auth.dependencies import set_jwt_verifier
from src.scd2_copilot.api.auth.verifier import SupabaseJWTVerifier
from src.scd2_copilot.api.client import (
    ApiClient,
    ApiForbiddenError,
    ApiUnauthorizedError,
)
from src.scd2_copilot.api.dependencies import (
    get_containment_service,
    get_db,
    get_hold_repo,
)
from src.scd2_copilot.auth_supabase import SupabaseSession
from src.scd2_copilot.config import Settings
from src.scd2_copilot.containment.service import (
    ContainmentService,
    HoldResolutionResult,
)
from src.scd2_copilot.db.connection import DatabaseManager
from src.scd2_copilot.db.models import (
    HeldChangeBatchRow,
    HoldSeverity,
    HoldStatus,
)
from src.scd2_copilot.db.repositories import HeldChangeBatchRepository


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


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        database_url="postgresql://operator:password@localhost:5432/scd2_test",
        api_auth_required=True,
        supabase_auth_issuer="https://test-project.supabase.co/auth/v1",
        supabase_auth_audience="authenticated",
        recovery_operator_emails=["operator@company.com", "lead@company.com"],
    )


def _make_jwt(
    private_key,
    user_id: UUID,
    email: str,
    issuer: str = "https://test-project.supabase.co/auth/v1",
    audience: str = "authenticated",
    exp_delta_seconds: int = 3600,
    full_name: str = "Test Operator",
    role: str = "authenticated",
) -> str:
    """Helper to generate a signed ES256 Supabase access token."""
    payload = {
        "sub": str(user_id),
        "iss": issuer,
        "aud": audience,
        "exp": int(time.time()) + exp_delta_seconds,
        "email": email,
        "user_metadata": {"full_name": full_name},
        "app_metadata": {"provider": "google", "role": role},
    }
    return jwt.encode(
        payload,
        private_key,
        algorithm="ES256",
        headers={"kid": "test-key-id", "alg": "ES256"},
    )


@pytest.fixture
def test_environment(test_settings, mock_jwks_client):
    """Set up FastAPI app, mock containment service, verifier, and transport."""
    mock_db = MagicMock(spec=DatabaseManager)
    mock_db.is_configured = True
    mock_db.ping.return_value = True

    mock_hold_repo = MagicMock(spec=HeldChangeBatchRepository)
    mock_containment_svc = MagicMock(spec=ContainmentService)

    app = create_app(settings=test_settings)
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_hold_repo] = lambda: mock_hold_repo
    app.dependency_overrides[get_containment_service] = lambda: mock_containment_svc

    verifier = SupabaseJWTVerifier(
        settings=test_settings,
        jwks_client=mock_jwks_client,
        expected_issuer=test_settings.supabase_auth_issuer,
        expected_audience=test_settings.supabase_auth_audience,
    )
    set_jwt_verifier(verifier)

    from fastapi.testclient import TestClient

    test_client = TestClient(app)

    yield {
        "app": app,
        "http_client": test_client,
        "containment_svc": mock_containment_svc,
        "hold_repo": mock_hold_repo,
        "settings": test_settings,
        "verifier": verifier,
    }

    set_jwt_verifier(None)


def test_e2e_authorized_recovery_action(test_environment, ec_key_pair):
    """Authorized operator with valid Supabase JWT triggers hold release via ApiClient."""
    private_key, _ = ec_key_pair
    env = test_environment
    containment_svc = env["containment_svc"]

    operator_id = uuid4()
    operator_email = "operator@company.com"
    token = _make_jwt(private_key, user_id=operator_id, email=operator_email)

    session = SupabaseSession(
        access_token=token,
        user_id=operator_id,
        email=operator_email,
        user_metadata={"full_name": "Authorized Operator"},
    )

    # ApiClient resolves token dynamically from token_provider
    api_client = ApiClient(
        base_url="http://testserver",
        client=env["http_client"],
        token_provider=lambda: session.access_token,
    )

    hold_id = uuid4()
    now = datetime.now(timezone.utc)
    containment_svc.release_held_batch.return_value = HoldResolutionResult(
        hold_id=hold_id,
        status=HoldStatus.RELEASED.value,
        success=True,
        message="Hold successfully released",
        resolved_at=now,
        records_affected=15,
        checkpoint_advanced_to=now,
        is_idempotent=False,
    )

    # Perform action through ApiClient
    result = api_client.release_hold(hold_id=hold_id, operator_reason="Batch reviewed and approved")

    assert result.hold_id == hold_id
    assert result.status == "RELEASED"
    assert result.success is True
    assert result.records_affected == 15

    # Verify result has operator identity derived strictly from JWT
    assert result.operator_id == operator_id

    # Verify ContainmentService received release call with operator reason
    containment_svc.release_held_batch.assert_called_once_with(
        hold_id=hold_id,
        operator_reason="Batch reviewed and approved",
    )


def test_e2e_reprocess_and_discard_actions(test_environment, ec_key_pair):
    """Authorized operator can perform REPROCESS and DISCARD recovery actions via ApiClient."""
    private_key, _ = ec_key_pair
    env = test_environment
    containment_svc = env["containment_svc"]

    operator_id = uuid4()
    operator_email = "lead@company.com"
    token = _make_jwt(private_key, user_id=operator_id, email=operator_email)

    api_client = ApiClient(
        base_url="http://testserver",
        client=env["http_client"],
        token_provider=lambda: token,
    )

    hold_id = uuid4()
    now = datetime.now(timezone.utc)

    # 1. Reprocess
    containment_svc.reprocess_held_batch.return_value = HoldResolutionResult(
        hold_id=hold_id,
        status=HoldStatus.REPROCESSED.value,
        success=True,
        message="Hold reprocessed",
        resolved_at=now,
        records_affected=10,
        checkpoint_advanced_to=now,
        is_idempotent=False,
    )
    reprocess_res = api_client.reprocess_hold(
        hold_id=hold_id, force_normal=True
    )
    assert reprocess_res.status == "REPROCESSED"
    assert reprocess_res.operator_id == operator_id
    containment_svc.reprocess_held_batch.assert_called_once_with(
        hold_id=hold_id,
        force_normal=True,
    )

    # 2. Discard
    containment_svc.discard_held_batch.return_value = HoldResolutionResult(
        hold_id=hold_id,
        status=HoldStatus.DISCARDED.value,
        success=True,
        message="Hold discarded",
        resolved_at=now,
        records_affected=0,
        checkpoint_advanced_to=now,
        is_idempotent=False,
    )
    discard_res = api_client.discard_hold(
        hold_id=hold_id, operator_reason="Corrupt source data", advance_checkpoint=True
    )
    assert discard_res.status == "DISCARDED"
    assert discard_res.operator_id == operator_id
    containment_svc.discard_held_batch.assert_called_once_with(
        hold_id=hold_id,
        operator_reason="Corrupt source data",
        advance_checkpoint=True,
    )


def test_e2e_unauthorized_user_forbidden(test_environment, ec_key_pair):
    """Valid JWT for a non-operator user receives HTTP 403 Forbidden."""
    private_key, _ = ec_key_pair
    env = test_environment

    viewer_id = uuid4()
    viewer_email = "viewer@company.com"  # Not in recovery_operator_emails
    token = _make_jwt(private_key, user_id=viewer_id, email=viewer_email)

    api_client = ApiClient(
        base_url="http://testserver",
        client=env["http_client"],
        token_provider=lambda: token,
    )

    hold_id = uuid4()
    with pytest.raises(ApiForbiddenError) as exc_info:
        api_client.release_hold(hold_id=hold_id, operator_reason="Unauthorized release")

    assert exc_info.value.status_code == 403
    assert "not authorized" in str(exc_info.value).lower()


def test_e2e_forged_headers_ignored(test_environment):
    """Direct HTTP request with spoofed identity headers and no Bearer token returns 401 Unauthorized."""
    env = test_environment
    http_client = env["http_client"]

    hold_id = uuid4()
    # Spoof operator headers without an Authorization token
    resp = http_client.post(
        f"/api/v1/holds/{hold_id}/release",
        json={"operator_reason": "Trying to bypass auth"},
        headers={
            "X-Operator-Id": str(uuid4()),
            "X-Operator-Email": "operator@company.com",
            "X-Operator-Role": "operator",
        },
    )
    assert resp.status_code == 401
    data = resp.json()
    assert data["error"]["code"] == "MISSING_TOKEN"


def test_e2e_tampered_signature_rejected(test_environment):
    """Token signed with a different private key returns 401 Unauthorized (InvalidTokenError)."""
    env = test_environment
    attacker_private_key = ec.generate_private_key(ec.SECP256R1())
    tampered_token = _make_jwt(
        attacker_private_key,
        user_id=uuid4(),
        email="operator@company.com",
    )

    api_client = ApiClient(
        base_url="http://testserver",
        client=env["http_client"],
        token_provider=lambda: tampered_token,
    )

    hold_id = uuid4()
    with pytest.raises(ApiUnauthorizedError) as exc_info:
        api_client.release_hold(hold_id=hold_id, operator_reason="Tampered key attempt")

    assert exc_info.value.status_code == 401
    assert "signature" in str(exc_info.value).lower() or "invalid" in str(exc_info.value).lower()


def test_e2e_expired_token_rejected(test_environment, ec_key_pair):
    """Expired token returns 401 Unauthorized (TokenExpiredError)."""
    private_key, _ = ec_key_pair
    env = test_environment
    expired_token = _make_jwt(
        private_key,
        user_id=uuid4(),
        email="operator@company.com",
        exp_delta_seconds=-300,  # Expired 5 minutes ago
    )

    api_client = ApiClient(
        base_url="http://testserver",
        client=env["http_client"],
        token_provider=lambda: expired_token,
    )

    hold_id = uuid4()
    with pytest.raises(ApiUnauthorizedError) as exc_info:
        api_client.release_hold(hold_id=hold_id, operator_reason="Expired session attempt")

    assert exc_info.value.status_code == 401
    assert "expired" in str(exc_info.value).lower()
