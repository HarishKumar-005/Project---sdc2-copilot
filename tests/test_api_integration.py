"""Live integration tests for the FastAPI Operational API against Supabase PostgreSQL (V2.6)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Generator
from uuid import UUID, uuid4
import pytest
from fastapi.testclient import TestClient

from src.scd2_copilot.api.app import create_app
from src.scd2_copilot.api.auth.dependencies import set_jwt_verifier
from src.scd2_copilot.api.auth.models import AuthenticatedOperator
from src.scd2_copilot.api.auth.verifier import SupabaseJWTVerifier
from src.scd2_copilot.config import Settings
from src.scd2_copilot.db.connection import DatabaseManager
from src.scd2_copilot.db.models import (
    HeldChangeBatchRow,
    HoldSeverity,
    HoldStatus,
    ProcessingRunRow,
    RunStatus,
)
from src.scd2_copilot.db.repositories import (
    HeldChangeBatchRepository,
    ProcessingRunRepository,
)


def _db_is_reachable() -> bool:
    try:
        mgr = DatabaseManager()
        return mgr.ping()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_is_reachable(),
    reason="Live PostgreSQL/Supabase database is not configured or not reachable",
)


@pytest.fixture(scope="module")
def live_client() -> Generator[TestClient, None, None]:
    """Provide a TestClient connected to real Supabase PostgreSQL with mocked operator JWT verifier."""
    settings = Settings(
        api_auth_required=False,
        recovery_operator_emails=["live.operator@company.com"],
    )
    app = create_app(settings=settings)

    # Use a mock verifier that treats "live.operator.token" as authorized operator
    from unittest.mock import MagicMock

    verifier_mock = MagicMock(spec=SupabaseJWTVerifier)

    def _mock_verify(token: str) -> AuthenticatedOperator:
        if token == "live.operator.token":
            return AuthenticatedOperator(
                user_id=uuid4(),
                email="live.operator@company.com",
                user_metadata={"full_name": "Live Operator"},
                is_authorized_operator=True,
            )
        elif token == "unauthorized.token":
            return AuthenticatedOperator(
                user_id=uuid4(),
                email="guest@test.com",
                user_metadata={"full_name": "Guest"},
                is_authorized_operator=False,
            )
        raise ValueError("Invalid token")

    verifier_mock.verify_token.side_effect = _mock_verify
    set_jwt_verifier(verifier_mock)

    with TestClient(app) as client:
        yield client

    set_jwt_verifier(None)


def test_live_health_and_readiness(live_client: TestClient) -> None:
    res_health = live_client.get("/health")
    assert res_health.status_code == 200
    assert res_health.json()["status"] == "healthy"

    res_ready = live_client.get("/ready")
    assert res_ready.status_code == 200
    assert res_ready.json()["status"] == "ready"
    assert res_ready.json()["database_connected"] is True


def test_live_metrics_query(live_client: TestClient) -> None:
    res = live_client.get("/api/v1/metrics")
    assert res.status_code == 200
    data = res.json()
    assert "total_runs" in data
    assert "active_holds_count" in data
    assert "system_health" in data


def test_live_list_inventory(live_client: TestClient) -> None:
    res = live_client.get("/api/v1/inventory?limit=5")
    assert res.status_code == 200
    data = res.json()
    assert "total" in data
    assert "records" in data


def test_live_hold_lifecycle(live_client: TestClient) -> None:
    """Test creating a real hold, retrieving via API, releasing via API, and verifying status."""
    db = DatabaseManager()
    run_repo = ProcessingRunRepository(db)
    hold_repo = HeldChangeBatchRepository(db)

    now = datetime.now(timezone.utc)
    test_run_id = uuid4()
    test_hold_id = uuid4()

    # 1. Insert a test run & hold in live DB
    with db.transaction() as conn:
        run_repo.create_run(
            source_name="inventory_source",
            run_id=test_run_id,
            started_at=now,
            status=RunStatus.HELD.value,
            conn=conn,
        )
        hold_repo.create_hold(
            run_id=test_run_id,
            source_name="inventory_source",
            severity=HoldSeverity.HIGH.value,
            reason="Live API test containment",
            records_affected=5,
            evidence={"test": True, "explanation": {"what_changed": "Live API integration test hold"}},
            status=HoldStatus.HELD.value,
            hold_id=test_hold_id,
            conn=conn,
        )

    try:
        # 2. Retrieve hold via API
        res = live_client.get(f"/api/v1/holds/{test_hold_id}")
        assert res.status_code == 200
        hold_data = res.json()
        assert hold_data["hold_id"] == str(test_hold_id)
        assert hold_data["status"] == "HELD"
        assert hold_data["explanation"]["what_changed"] == "Live API integration test hold"

        # 3. Unauthorized attempt to release fails with 403
        res_unauth = live_client.post(
            f"/api/v1/holds/{test_hold_id}/release",
            json={"operator_reason": "Attempt unauthorized"},
            headers={"Authorization": "Bearer unauthorized.token"},
        )
        assert res_unauth.status_code == 403

        # 4. Discard hold via authorized API call
        res_discard = live_client.post(
            f"/api/v1/holds/{test_hold_id}/discard",
            json={"operator_reason": "Clean up live test", "advance_checkpoint": False},
            headers={"Authorization": "Bearer live.operator.token"},
        )
        assert res_discard.status_code == 200
        discard_data = res_discard.json()
        assert discard_data["status"] == "DISCARDED"

        # 5. Verify hold is now DISCARDED in live DB
        res_updated = live_client.get(f"/api/v1/holds/{test_hold_id}")
        assert res_updated.status_code == 200
        assert res_updated.json()["status"] == "DISCARDED"

    finally:
        # Cleanup test entities
        try:
            with db.transaction() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM held_change_batch WHERE hold_id = %s", (test_hold_id,))
                    cur.execute("DELETE FROM processing_run WHERE run_id = %s", (test_run_id,))
        except Exception:
            pass
