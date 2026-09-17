"""Comprehensive unit tests for the SCD2 Copilot Operational API & Auth Verifier (V2.6)."""

from __future__ import annotations

from datetime import date, datetime, timezone
import time
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4
import pytest

from fastapi.testclient import TestClient
import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization

from src.scd2_copilot.api.app import create_app
from src.scd2_copilot.api.auth.dependencies import set_jwt_verifier
from src.scd2_copilot.api.auth.exceptions import (
    AuthError,
    InvalidClaimsError,
    InvalidTokenError,
    MissingTokenError,
    TokenExpiredError,
    UnauthorizedOperatorError,
)
from src.scd2_copilot.api.auth.models import AuthenticatedOperator
from src.scd2_copilot.api.auth.verifier import SupabaseJWTVerifier
from src.scd2_copilot.api.dependencies import (
    get_checkpoint_repo,
    get_containment_service,
    get_db,
    get_history_repo,
    get_hold_repo,
    get_inventory_repo,
    get_run_repo,
)
from src.scd2_copilot.config import Settings
from src.scd2_copilot.containment.service import (
    ContainmentService,
    HoldNotFoundError,
    HoldResolutionResult,
    InvalidHoldTransitionError,
)
from src.scd2_copilot.db.connection import DatabaseManager
from src.scd2_copilot.db.models import (
    HeldChangeBatchRow,
    HoldSeverity,
    HoldStatus,
    InventoryHistoryRow,
    InventorySourceRow,
    ProcessingCheckpointRow,
    ProcessingRunRow,
    RunStatus,
)
from src.scd2_copilot.db.repositories import (
    CheckpointRepository,
    HeldChangeBatchRepository,
    InventoryHistoryRepository,
    InventorySourceRepository,
    ProcessingRunRepository,
)


# ── Cryptographic Key Pair Fixtures ─────────────────────────


@pytest.fixture(scope="session")
def ec_key_pair():
    """Generate an EC P-256 key pair for signing and verifying test JWTs."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()
    return private_key, public_key


@pytest.fixture
def test_settings() -> Settings:
    """Provide isolated test settings for API tests."""
    return Settings(
        database_url="postgresql://operator:password@localhost:5432/scd2_test",
        api_auth_required=False,
        supabase_auth_issuer="https://test.supabase.co/auth/v1",
        supabase_auth_audience="authenticated",
        recovery_operator_emails=["operator@example.com", "admin@company.org"],
    )


@pytest.fixture
def mock_db() -> MagicMock:
    """Mock database manager."""
    db = MagicMock(spec=DatabaseManager)
    db.is_configured = True
    db.redacted_url = "postgresql://operator:***@localhost:5432/scd2_test"
    db.ping.return_value = True
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = (1,)
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    db.get_connection.return_value.__enter__.return_value = mock_conn
    return db


@pytest.fixture
def mock_run_repo() -> MagicMock:
    return MagicMock(spec=ProcessingRunRepository)


@pytest.fixture
def mock_hold_repo() -> MagicMock:
    return MagicMock(spec=HeldChangeBatchRepository)


@pytest.fixture
def mock_checkpoint_repo() -> MagicMock:
    return MagicMock(spec=CheckpointRepository)


@pytest.fixture
def mock_history_repo() -> MagicMock:
    return MagicMock(spec=InventoryHistoryRepository)


@pytest.fixture
def mock_inventory_repo() -> MagicMock:
    return MagicMock(spec=InventorySourceRepository)


@pytest.fixture
def mock_containment_service() -> MagicMock:
    return MagicMock(spec=ContainmentService)


@pytest.fixture
def mock_jwt_verifier() -> MagicMock:
    verifier = MagicMock(spec=SupabaseJWTVerifier)
    return verifier


@pytest.fixture
def client(
    test_settings: Settings,
    mock_db: MagicMock,
    mock_run_repo: MagicMock,
    mock_hold_repo: MagicMock,
    mock_checkpoint_repo: MagicMock,
    mock_history_repo: MagicMock,
    mock_inventory_repo: MagicMock,
    mock_containment_service: MagicMock,
    mock_jwt_verifier: MagicMock,
) -> TestClient:
    """Create FastAPI test client with mocked dependencies."""
    app = create_app(settings=test_settings)

    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_run_repo] = lambda: mock_run_repo
    app.dependency_overrides[get_hold_repo] = lambda: mock_hold_repo
    app.dependency_overrides[get_checkpoint_repo] = lambda: mock_checkpoint_repo
    app.dependency_overrides[get_history_repo] = lambda: mock_history_repo
    app.dependency_overrides[get_inventory_repo] = lambda: mock_inventory_repo
    app.dependency_overrides[get_containment_service] = lambda: mock_containment_service

    set_jwt_verifier(mock_jwt_verifier)

    with TestClient(app) as test_client:
        yield test_client

    set_jwt_verifier(None)


# ── Health & Readiness Probes ───────────────────────────────


def test_health_probe(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["version"] == "v1"
    assert "timestamp" in data
    assert "X-Request-ID" in response.headers


def test_readiness_probe_healthy(client: TestClient, mock_db: MagicMock) -> None:
    mock_db.ping.return_value = True
    response = client.get("/ready")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"
    assert data["database_connected"] is True


def test_readiness_probe_unhealthy(client: TestClient, mock_db: MagicMock) -> None:
    mock_db.ping.return_value = False
    response = client.get("/ready")
    assert response.status_code == 503
    data = response.json()
    assert "error" in data
    assert data["error"]["code"] == "DATABASE_UNAVAILABLE"


# ── Request Correlation & ID ────────────────────────────────


def test_request_correlation_custom_id(client: TestClient) -> None:
    custom_id = "req-custom-trace-12345"
    response = client.get("/health", headers={"X-Request-ID": custom_id})
    assert response.status_code == 200
    assert response.headers.get("X-Request-ID") == custom_id


# ── Processing Runs Endpoints ───────────────────────────────


def test_list_runs(client: TestClient, mock_run_repo: MagicMock) -> None:
    now = datetime.now(timezone.utc)
    run_id = uuid4()
    mock_run = ProcessingRunRow(
        run_id=run_id,
        source_name="inventory_source",
        status=RunStatus.COMMITTED.value,
        started_at=now,
        records_seen=10,
        records_changed=3,
        records_held=0,
        created_at=now,
    )
    mock_run_repo.get_recent_runs.return_value = [mock_run]

    response = client.get("/api/v1/runs?limit=10")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["runs"][0]["run_id"] == str(run_id)
    assert data["runs"][0]["status"] == "COMMITTED"
    assert data["runs"][0]["records_seen"] == 10


def test_get_run_detail_found(client: TestClient, mock_run_repo: MagicMock) -> None:
    now = datetime.now(timezone.utc)
    run_id = uuid4()
    mock_run = ProcessingRunRow(
        run_id=run_id,
        source_name="inventory_source",
        status=RunStatus.COMMITTED.value,
        started_at=now,
        records_seen=10,
        created_at=now,
    )
    mock_run_repo.get_run.return_value = mock_run

    response = client.get(f"/api/v1/runs/{run_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["run_id"] == str(run_id)


def test_get_run_detail_not_found(client: TestClient, mock_run_repo: MagicMock) -> None:
    mock_run_repo.get_run.return_value = None
    random_id = uuid4()
    response = client.get(f"/api/v1/runs/{random_id}")
    assert response.status_code == 404
    data = response.json()
    assert "error" in data
    assert data["error"]["code"] == "RUN_NOT_FOUND"


# ── Held Batches Endpoints ──────────────────────────────────


def test_list_holds(client: TestClient, mock_hold_repo: MagicMock) -> None:
    now = datetime.now(timezone.utc)
    hold_id = uuid4()
    run_id = uuid4()
    mock_hold = HeldChangeBatchRow(
        hold_id=hold_id,
        run_id=run_id,
        source_name="inventory_source",
        severity=HoldSeverity.HIGH.value,
        reason="High changed record ratio",
        records_affected=25,
        evidence={
            "triggered_rules": [{"rule_name": "HighChangeRatio"}],
            "explanation": {"what_changed": "25 records modified"},
        },
        status=HoldStatus.HELD.value,
        created_at=now,
    )
    mock_hold_repo.get_recent_holds.return_value = [mock_hold]

    response = client.get("/api/v1/holds?status=HELD")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["holds"][0]["hold_id"] == str(hold_id)
    assert data["holds"][0]["severity"] == "HIGH"
    assert data["holds"][0]["explanation"]["what_changed"] == "25 records modified"


def test_get_hold_detail_not_found(client: TestClient, mock_hold_repo: MagicMock) -> None:
    mock_hold_repo.get_hold.return_value = None
    random_id = uuid4()
    response = client.get(f"/api/v1/holds/{random_id}")
    assert response.status_code == 404
    data = response.json()
    assert data["error"]["code"] == "HOLD_NOT_FOUND"


# ── Recovery Operations & JWT Authorization ─────────────────


def test_recovery_release_missing_token(client: TestClient) -> None:
    """Recovery actions without Authorization header must fail closed with 401."""
    hold_id = uuid4()
    response = client.post(f"/api/v1/holds/{hold_id}/release", json={"operator_reason": "Approve"})
    assert response.status_code == 401
    data = response.json()
    assert "error" in data
    assert data["error"]["code"] == "MISSING_TOKEN"


def test_recovery_release_unauthorized_operator(
    client: TestClient,
    mock_jwt_verifier: MagicMock,
) -> None:
    """A valid JWT for an unlisted email or non-operator user must return 403 Forbidden."""
    hold_id = uuid4()
    unauthorized_operator = AuthenticatedOperator(
        user_id=uuid4(),
        email="guest@stranger.com",
        user_metadata={"full_name": "Guest User"},
        is_authorized_operator=False,
        app_metadata={"role": "viewer"},
    )
    mock_jwt_verifier.verify_token.return_value = unauthorized_operator

    response = client.post(
        f"/api/v1/holds/{hold_id}/release",
        json={"operator_reason": "Try unauthorized release"},
        headers={"Authorization": "Bearer fake.jwt.token"},
    )
    assert response.status_code == 403
    data = response.json()
    assert data["error"]["code"] == "FORBIDDEN_OPERATOR"


def test_recovery_release_authorized_success(
    client: TestClient,
    mock_jwt_verifier: MagicMock,
    mock_containment_service: MagicMock,
) -> None:
    """An authorized operator can successfully trigger release."""
    hold_id = uuid4()
    authorized_op = AuthenticatedOperator(
        user_id=uuid4(),
        email="operator@example.com",
        user_metadata={"full_name": "Operations Lead"},
        is_authorized_operator=True,
    )
    mock_jwt_verifier.verify_token.return_value = authorized_op

    now = datetime.now(timezone.utc)
    mock_containment_service.release_held_batch.return_value = HoldResolutionResult(
        hold_id=hold_id,
        status=HoldStatus.RELEASED.value,
        success=True,
        message="Hold released downstream",
        resolved_at=now,
        records_affected=15,
        checkpoint_advanced_to=now,
        is_idempotent=False,
    )

    response = client.post(
        f"/api/v1/holds/{hold_id}/release",
        json={"operator_reason": "Verified legitimate bulk update"},
        headers={"Authorization": "Bearer valid.operator.token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["hold_id"] == str(hold_id)
    assert data["status"] == "RELEASED"
    assert data["success"] is True
    assert data["records_affected"] == 15
    mock_containment_service.release_held_batch.assert_called_once()


def test_recovery_reprocess_authorized_success(
    client: TestClient,
    mock_jwt_verifier: MagicMock,
    mock_containment_service: MagicMock,
) -> None:
    """An authorized operator can reprocess a held batch."""
    hold_id = uuid4()
    authorized_op = AuthenticatedOperator(
        user_id=uuid4(),
        email="admin@company.org",
        user_metadata={"full_name": "Admin"},
        is_authorized_operator=True,
    )
    mock_jwt_verifier.verify_token.return_value = authorized_op

    now = datetime.now(timezone.utc)
    mock_containment_service.reprocess_held_batch.return_value = HoldResolutionResult(
        hold_id=hold_id,
        status=HoldStatus.REPROCESSED.value,
        success=True,
        message="Hold reprocessed",
        resolved_at=now,
        records_affected=10,
        checkpoint_advanced_to=now,
        is_idempotent=False,
    )

    response = client.post(
        f"/api/v1/holds/{hold_id}/reprocess",
        json={"force_normal": True},
        headers={"Authorization": "Bearer valid.admin.token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "REPROCESSED"
    assert data["success"] is True
    mock_containment_service.reprocess_held_batch.assert_called_once_with(
        hold_id=hold_id,
        force_normal=True,
    )


def test_recovery_discard_authorized_success(
    client: TestClient,
    mock_jwt_verifier: MagicMock,
    mock_containment_service: MagicMock,
) -> None:
    """An authorized operator can discard a suspicious batch."""
    hold_id = uuid4()
    authorized_op = AuthenticatedOperator(
        user_id=uuid4(),
        email="operator@example.com",
        user_metadata={"full_name": "Operations Lead"},
        is_authorized_operator=True,
    )
    mock_jwt_verifier.verify_token.return_value = authorized_op

    now = datetime.now(timezone.utc)
    mock_containment_service.discard_held_batch.return_value = HoldResolutionResult(
        hold_id=hold_id,
        status=HoldStatus.DISCARDED.value,
        success=True,
        message="Hold discarded",
        resolved_at=now,
        records_affected=0,
        checkpoint_advanced_to=now,
        is_idempotent=False,
    )

    response = client.post(
        f"/api/v1/holds/{hold_id}/discard",
        json={"operator_reason": "Corrupted upstream feed", "advance_checkpoint": True},
        headers={"Authorization": "Bearer valid.operator.token"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "DISCARDED"
    assert data["success"] is True


def test_recovery_invalid_transition_conflict(
    client: TestClient,
    mock_jwt_verifier: MagicMock,
    mock_containment_service: MagicMock,
) -> None:
    """Attempting an invalid hold transition raises 409 Conflict."""
    hold_id = uuid4()
    authorized_op = AuthenticatedOperator(
        user_id=uuid4(),
        email="operator@example.com",
        is_authorized_operator=True,
    )
    mock_jwt_verifier.verify_token.return_value = authorized_op
    mock_containment_service.release_held_batch.side_effect = InvalidHoldTransitionError(
        f"Hold {hold_id} is in status DISCARDED and cannot transition to RELEASED."
    )

    response = client.post(
        f"/api/v1/holds/{hold_id}/release",
        json={"operator_reason": "Retry"},
        headers={"Authorization": "Bearer valid.operator.token"},
    )
    assert response.status_code == 409
    data = response.json()
    assert data["error"]["code"] == "INVALID_HOLD_TRANSITION"


# ── Historical Inventory & Source Inventory Endpoints ───────


def test_get_history_found(client: TestClient, mock_history_repo: MagicMock) -> None:
    row1 = InventoryHistoryRow(
        history_id=uuid4(),
        sku_id="SKU-1001",
        warehouse_id="WH-EAST",
        quantity_on_hand=50,
        reorder_level=10,
        status="ACTIVE",
        effective_from=datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc),
        effective_to=datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc),
        is_current=False,
    )
    row2 = InventoryHistoryRow(
        history_id=uuid4(),
        sku_id="SKU-1001",
        warehouse_id="WH-EAST",
        quantity_on_hand=75,
        reorder_level=10,
        status="ACTIVE",
        effective_from=datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc),
        effective_to=None,
        is_current=True,
    )
    mock_history_repo.fetch_history_for_keys.return_value = [row1, row2]

    response = client.get("/api/v1/history/SKU-1001/WH-EAST")
    assert response.status_code == 200
    data = response.json()
    assert data["sku_id"] == "SKU-1001"
    assert data["warehouse_id"] == "WH-EAST"
    assert data["total_versions"] == 2
    assert data["versions"][1]["is_current"] is True
    assert data["versions"][1]["effective_to"] is None


def test_list_inventory_source(client: TestClient, mock_inventory_repo: MagicMock) -> None:
    now = datetime.now(timezone.utc)
    inv1 = InventorySourceRow(
        sku_id="SKU-1001",
        warehouse_id="WH-EAST",
        quantity_on_hand=100,
        reorder_level=20,
        status="ACTIVE",
        updated_at=now,
    )
    mock_inventory_repo.fetch_inventory_updated_after.return_value = [inv1]

    response = client.get("/api/v1/inventory?limit=50")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["records"][0]["sku_id"] == "SKU-1001"


# ── Operational Metrics Endpoint ────────────────────────────


def test_get_metrics(
    client: TestClient,
    mock_run_repo: MagicMock,
    mock_hold_repo: MagicMock,
    mock_checkpoint_repo: MagicMock,
) -> None:
    mock_run_repo.get_run_counts.return_value = {
        "total": 50,
        "committed": 45,
        "held": 4,
        "failed": 1,
        "total_records_processed": 500,
        "total_records_changed": 50,
        "total_records_held": 10,
    }
    mock_hold_repo.count_active_holds.return_value = 2
    mock_run_repo.get_recent_runs.return_value = []
    now = datetime.now(timezone.utc)
    ckpt = ProcessingCheckpointRow(
        source_name="inventory_source",
        watermark_value=now,
        last_successful_run_id=uuid4(),
        updated_at=now,
    )
    mock_checkpoint_repo.get_checkpoint.return_value = ckpt
    mock_checkpoint_repo.get_latest_checkpoint.return_value = ckpt

    # 1. Canonical route GET /api/v1/metrics (aggregates all sources by default)
    response = client.get("/api/v1/metrics")
    assert response.status_code == 200
    data = response.json()
    assert data["total_runs"] == 50
    assert data["successful_runs"] == 45
    assert data["active_holds_count"] == 2
    assert data["system_health"] == "degraded"
    mock_run_repo.get_run_counts.assert_called_with(source_name=None)

    # 2. Compatibility alias route GET /api/v1/metrics/system
    resp_alias = client.get("/api/v1/metrics/system")
    assert resp_alias.status_code == 200
    assert resp_alias.json()["total_runs"] == 50

    # 3. Stream source filter query param
    resp_filtered = client.get("/api/v1/metrics?source_name=custom_stream")
    assert resp_filtered.status_code == 200
    mock_run_repo.get_run_counts.assert_called_with(source_name="custom_stream")


def test_readiness_response_db_connected_property() -> None:
    from src.scd2_copilot.api.schemas import ReadinessResponse

    now = datetime.now(timezone.utc)
    res_true = ReadinessResponse(
        status="ready",
        database_connected=True,
        timestamp=now,
    )
    assert res_true.database_connected is True
    assert res_true.db_connected is True

    res_false = ReadinessResponse(
        status="unready",
        database_connected=False,
        timestamp=now,
    )
    assert res_false.database_connected is False
    assert res_false.db_connected is False


# ── SupabaseJWTVerifier Direct Unit Tests ───────────────────


def test_verifier_token_validation(ec_key_pair) -> None:
    """Verify SupabaseJWTVerifier decoding, issuer check, audience check, and operator role resolution."""
    private_key, public_key = ec_key_pair

    mock_jwk_client = MagicMock()
    mock_signing_key = MagicMock()
    mock_signing_key.key = public_key
    mock_jwk_client.get_signing_key_from_jwt.return_value = mock_signing_key

    settings = Settings(
        supabase_auth_issuer="https://test.supabase.co/auth/v1",
        supabase_auth_audience="authenticated",
        recovery_operator_emails=["verified.operator@company.com"],
    )

    verifier = SupabaseJWTVerifier(
        settings=settings,
        jwks_client=mock_jwk_client,
        expected_issuer="https://test.supabase.co/auth/v1",
        expected_audience="authenticated",
    )

    user_id = str(uuid4())
    valid_payload = {
        "sub": user_id,
        "iss": "https://test.supabase.co/auth/v1",
        "aud": "authenticated",
        "exp": int(time.time()) + 3600,
        "email": "verified.operator@company.com",
        "user_metadata": {"full_name": "Verified Operator"},
        "app_metadata": {"role": "operator"},
    }

    token = jwt.encode(
        valid_payload,
        private_key,
        algorithm="ES256",
        headers={"kid": "test-key-id", "alg": "ES256"},
    )

    operator = verifier.verify_token(token)
    assert isinstance(operator, AuthenticatedOperator)
    assert str(operator.user_id) == user_id
    assert operator.email == "verified.operator@company.com"
    assert operator.display_name == "Verified Operator"
    assert operator.is_authorized_operator is True

    guest_payload = dict(valid_payload, email="guest@viewer.com", app_metadata={})
    guest_token = jwt.encode(
        guest_payload,
        private_key,
        algorithm="ES256",
        headers={"kid": "test-key-id", "alg": "ES256"},
    )
    guest_operator = verifier.verify_token(guest_token)
    assert guest_operator.is_authorized_operator is False


def test_verifier_expired_token(ec_key_pair) -> None:
    private_key, public_key = ec_key_pair

    mock_jwk_client = MagicMock()
    mock_signing_key = MagicMock()
    mock_signing_key.key = public_key
    mock_jwk_client.get_signing_key_from_jwt.return_value = mock_signing_key

    verifier = SupabaseJWTVerifier(
        jwks_client=mock_jwk_client,
        expected_issuer="https://test.supabase.co/auth/v1",
        expected_audience="authenticated",
    )

    expired_payload = {
        "sub": str(uuid4()),
        "iss": "https://test.supabase.co/auth/v1",
        "aud": "authenticated",
        "exp": int(time.time()) - 100,
    }
    token = jwt.encode(
        expired_payload,
        private_key,
        algorithm="ES256",
        headers={"kid": "test-key-id", "alg": "ES256"},
    )

    with pytest.raises(TokenExpiredError):
        verifier.verify_token(token)


def test_verifier_invalid_issuer(ec_key_pair) -> None:
    private_key, public_key = ec_key_pair

    mock_jwk_client = MagicMock()
    mock_signing_key = MagicMock()
    mock_signing_key.key = public_key
    mock_jwk_client.get_signing_key_from_jwt.return_value = mock_signing_key

    verifier = SupabaseJWTVerifier(
        jwks_client=mock_jwk_client,
        expected_issuer="https://test.supabase.co/auth/v1",
        expected_audience="authenticated",
    )

    bad_iss_payload = {
        "sub": str(uuid4()),
        "iss": "https://evil.attacker.com/auth/v1",
        "aud": "authenticated",
        "exp": int(time.time()) + 3600,
    }
    token = jwt.encode(
        bad_iss_payload,
        private_key,
        algorithm="ES256",
        headers={"kid": "test-key-id", "alg": "ES256"},
    )

    with pytest.raises(InvalidClaimsError):
        verifier.verify_token(token)
