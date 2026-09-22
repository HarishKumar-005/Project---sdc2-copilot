"""Integration and unit tests for FastAPI Onboarding operational routes (/api/v1/onboarding/runs)."""

from __future__ import annotations

import tempfile
from typing import Generator
import pytest
from fastapi.testclient import TestClient

from src.scd2_copilot.api.app import create_app
from src.scd2_copilot.api.dependencies import (
    get_onboarding_run_repo,
    get_onboarding_run_service,
    set_onboarding_run_repo,
)
from src.scd2_copilot.config import Settings
from src.scd2_copilot.onboarding.models.run import OnboardingRun, RunStatus
from src.scd2_copilot.onboarding.runs.repository import OnboardingRunRepository
from src.scd2_copilot.onboarding.runs.service import OnboardingRunService


@pytest.fixture
def onboarding_repo() -> OnboardingRunRepository:
    """Provide a clean in-memory run repository for API tests."""
    repo = OnboardingRunRepository(in_memory=True)
    repo.clear()
    return repo


@pytest.fixture
def test_client(onboarding_repo: OnboardingRunRepository) -> Generator[TestClient, None, None]:
    """Provide a FastAPI TestClient configured for onboarding route testing."""
    with tempfile.TemporaryDirectory() as tmp_artifacts:
        settings = Settings(
            api_auth_required=False,
            database_url="",
        )
        service = OnboardingRunService(
            repository=onboarding_repo,
            artifacts_root=tmp_artifacts,
        )

        app = create_app(settings=settings)
        app.dependency_overrides[get_onboarding_run_repo] = lambda: onboarding_repo
        app.dependency_overrides[get_onboarding_run_service] = lambda: service

        with TestClient(app) as client:
            yield client


# ── Idempotency and Submission Route Tests ─────────────────


def test_post_run_missing_idempotency_key_returns_422(test_client: TestClient) -> None:
    """Verify that requests without an Idempotency-Key header or body field are rejected with 422."""
    payload = {
        "source_id": "crm_source",
        "data_payload": [{"cust_id": "1", "name": "Alice"}],
    }
    response = test_client.post("/api/v1/onboarding/runs", json=payload)
    assert response.status_code == 422
    data = response.json()
    assert data["error"]["code"] == "MISSING_IDEMPOTENCY_KEY"


def test_post_run_success_returns_201(test_client: TestClient) -> None:
    """Verify that a valid run submission returns 201 Created with run details."""
    payload = {
        "source_id": "crm_api_source",
        "data_payload": [{"cust_id": "101", "name": "Bob"}],
        "auto_execute": False,
    }
    headers = {"Idempotency-Key": "API-RUN-001"}
    response = test_client.post("/api/v1/onboarding/runs", json=payload, headers=headers)
    assert response.status_code == 201
    data = response.json()
    assert data["idempotency_key"] == "API-RUN-001"
    assert data["source_id"] == "crm_api_source"
    assert data["status"] == "CREATED"
    assert data["run_id"].startswith("onb_run_")


def test_post_run_idempotent_replay_returns_200_and_header(test_client: TestClient) -> None:
    """Verify that repeating an identical submission returns 200 OK and X-Idempotent-Replay header."""
    payload = {
        "source_id": "billing_api_source",
        "data_payload": [{"invoice_id": "INV-1", "amount": 100}],
        "auto_execute": False,
    }
    headers = {"Idempotency-Key": "API-REPLAY-KEY-001"}

    # First request -> 201 Created
    res1 = test_client.post("/api/v1/onboarding/runs", json=payload, headers=headers)
    assert res1.status_code == 201
    run1 = res1.json()

    # Second identical request -> 200 OK with replay header
    res2 = test_client.post("/api/v1/onboarding/runs", json=payload, headers=headers)
    assert res2.status_code == 200
    assert res2.headers.get("X-Idempotent-Replay") == "true"
    run2 = res2.json()
    assert run2["run_id"] == run1["run_id"]


def test_post_run_idempotency_conflict_returns_409(test_client: TestClient) -> None:
    """Verify that reusing the same Idempotency-Key with different payload returns 409 Conflict."""
    payload1 = {
        "source_id": "crm_source",
        "data_payload": [{"cust_id": "1", "name": "Alice"}],
        "auto_execute": False,
    }
    headers = {"Idempotency-Key": "CONFLICT-KEY-001"}
    res1 = test_client.post("/api/v1/onboarding/runs", json=payload1, headers=headers)
    assert res1.status_code == 201

    # Second request with different data payload but SAME idempotency key
    payload2 = {
        "source_id": "crm_source",
        "data_payload": [{"cust_id": "2", "name": "Bob"}],
        "auto_execute": False,
    }
    res2 = test_client.post("/api/v1/onboarding/runs", json=payload2, headers=headers)
    assert res2.status_code == 409
    data = res2.json()
    assert data["error"]["code"] == "IDEMPOTENCY_CONFLICT"


# ── Query & Detail Route Tests ─────────────────────────────


def test_get_run_by_id_found_and_not_found(test_client: TestClient) -> None:
    """Verify GET /runs/{run_id} returns 200 for existing and 404 for unknown run."""
    # 404 for nonexistent run
    res_404 = test_client.get("/api/v1/onboarding/runs/onb_run_nonexistent")
    assert res_404.status_code == 404
    assert res_404.json()["error"]["code"] == "RUN_NOT_FOUND"

    # Create run and fetch it
    payload = {
        "source_id": "crm_detail_test",
        "data_payload": [{"id": 1}],
        "auto_execute": False,
    }
    created_res = test_client.post(
        "/api/v1/onboarding/runs",
        json=payload,
        headers={"Idempotency-Key": "DETAIL-KEY-001"},
    )
    assert created_res.status_code == 201
    run_id = created_res.json()["run_id"]

    fetch_res = test_client.get(f"/api/v1/onboarding/runs/{run_id}")
    assert fetch_res.status_code == 200
    assert fetch_res.json()["run_id"] == run_id


def test_list_runs_pagination_and_filters(test_client: TestClient) -> None:
    """Verify listing runs with source_id filter and pagination parameters."""
    for i in range(3):
        test_client.post(
            "/api/v1/onboarding/runs",
            json={"source_id": "crm_list", "data_payload": [{"idx": i}], "auto_execute": False},
            headers={"Idempotency-Key": f"LIST-KEY-CRM-{i}"},
        )
    for i in range(2):
        test_client.post(
            "/api/v1/onboarding/runs",
            json={"source_id": "billing_list", "data_payload": [{"idx": i}], "auto_execute": False},
            headers={"Idempotency-Key": f"LIST-KEY-BILL-{i}"},
        )

    # Filter by source_id
    res_crm = test_client.get("/api/v1/onboarding/runs?source_id=crm_list")
    assert res_crm.status_code == 200
    data_crm = res_crm.json()
    assert data_crm["total"] == 3
    assert len(data_crm["runs"]) == 3
    assert all(r["source_id"] == "crm_list" for r in data_crm["runs"])

    # Limit and offset pagination
    res_page = test_client.get("/api/v1/onboarding/runs?limit=2&offset=1")
    assert res_page.status_code == 200
    data_page = res_page.json()
    assert data_page["limit"] == 2
    assert data_page["offset"] == 1
    assert len(data_page["runs"]) == 2
    assert data_page["total"] == 5


# ── Operational Actions (Cancel & Retry) ────────────────────


def test_cancel_run_endpoint(test_client: TestClient) -> None:
    """Verify POST /runs/{run_id}/cancel halts an active or pending run."""
    created = test_client.post(
        "/api/v1/onboarding/runs",
        json={"source_id": "cancel_source", "data_payload": [{"x": 1}], "auto_execute": False},
        headers={"Idempotency-Key": "CANCEL-API-KEY-001"},
    ).json()
    run_id = created["run_id"]

    cancel_res = test_client.post(
        f"/api/v1/onboarding/runs/{run_id}/cancel",
        json={"reason": "Operator manual cancel"},
    )
    assert cancel_res.status_code == 200
    data = cancel_res.json()
    assert data["status"] == "CANCELLED"
    assert data["error_message"] == "Operator manual cancel"

    # Cancelling nonexistent run -> 404
    res_404 = test_client.post("/api/v1/onboarding/runs/unknown_run/cancel")
    assert res_404.status_code == 404


def test_retry_run_endpoint(test_client: TestClient, onboarding_repo: OnboardingRunRepository) -> None:
    """Verify POST /runs/{run_id}/retry transitions a failed run back to CREATED."""
    created = test_client.post(
        "/api/v1/onboarding/runs",
        json={"source_id": "retry_source", "data_payload": [{"x": 1}], "auto_execute": False},
        headers={"Idempotency-Key": "RETRY-API-KEY-001"},
    ).json()
    run_id = created["run_id"]

    # Manually transition run to FAILED in repo
    run = onboarding_repo.get_by_id(run_id)
    assert run is not None
    run.transition_to(RunStatus.FAILED, error_message="Temporary network error")
    onboarding_repo.update(run)

    # Retry the run
    retry_res = test_client.post(f"/api/v1/onboarding/runs/{run_id}/retry")
    assert retry_res.status_code == 200
    data = retry_res.json()
    assert data["status"] == "CREATED"

    # Attempting to retry a non-failed run -> 409 Conflict
    conflict_res = test_client.post(f"/api/v1/onboarding/runs/{run_id}/retry")
    assert conflict_res.status_code == 409
    assert conflict_res.json()["error"]["code"] == "INVALID_RUN_STATE_TRANSITION"
