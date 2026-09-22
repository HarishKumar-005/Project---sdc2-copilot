"""Integration and unit tests for FastAPI Schema Drift operational routes (/api/v1/onboarding/drift)."""

from __future__ import annotations

from typing import Generator
import pytest
from fastapi.testclient import TestClient

from src.scd2_copilot.api.app import create_app
from src.scd2_copilot.api.dependencies import (
    get_drift_repository,
    get_drift_service,
)
from src.scd2_copilot.config import Settings
from src.scd2_copilot.onboarding.drift.repository import SchemaDriftRepository
from src.scd2_copilot.onboarding.drift.service import SchemaDriftService
from src.scd2_copilot.onboarding.models.approval import (
    ApprovedMappingDefinition,
    ApprovedMappingVersion,
    ReviewDecisionType,
)
from src.scd2_copilot.onboarding.models.drift import (
    DriftType,
    MappingCompatibilityState,
)
from src.scd2_copilot.onboarding.models.mapping import MappingType
from src.scd2_copilot.onboarding.models.schema_snapshot import (
    ColumnSnapshot,
    SourceSchemaSnapshot,
)
from src.scd2_copilot.onboarding.profiler.fingerprint import compute_schema_fingerprint


@pytest.fixture
def drift_repo() -> SchemaDriftRepository:
    """Provide a clean in-memory drift repository for API tests."""
    repo = SchemaDriftRepository(in_memory=True)
    repo.clear()
    return repo


@pytest.fixture
def test_client(drift_repo: SchemaDriftRepository) -> Generator[TestClient, None, None]:
    """Provide a FastAPI TestClient configured for drift route testing."""
    settings = Settings(
        api_auth_required=False,
        database_url="",
    )
    service = SchemaDriftService(repository=drift_repo)

    app = create_app(settings=settings)
    app.dependency_overrides[get_drift_repository] = lambda: drift_repo
    app.dependency_overrides[get_drift_service] = lambda: service

    with TestClient(app) as client:
        yield client


def _make_snapshot(source_id: str, version: int, columns: list[ColumnSnapshot]) -> dict:
    fp = compute_schema_fingerprint(columns)
    snap = SourceSchemaSnapshot(
        source_id=source_id,
        schema_version=version,
        fingerprint=fp,
        columns=columns,
    )
    return snap.model_dump(mode="json")


def _make_approved_mapping(
    mapping_version_id: str,
    source_id: str,
    mappings: list[ApprovedMappingDefinition],
) -> dict:
    mapping = ApprovedMappingVersion(
        mapping_version_id=mapping_version_id,
        source_id=source_id,
        source_fingerprint="dummy_fp",
        source_schema_version=1,
        canonical_schema_name="customer",
        canonical_schema_version=1,
        version_number=1,
        mappings=mappings,
        is_complete=True,
        unmapped_required_fields=[],
        approved_by="operator",
    )
    return mapping.model_dump(mode="json")


def test_post_drift_compare_returns_200_and_report(test_client: TestClient) -> None:
    """Verify POST /drift/compare compares snapshots and returns SchemaDriftReport."""
    s1_cols = [
        ColumnSnapshot(
            original_name="cust_id",
            normalized_name="cust_id",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=0,
        )
    ]
    s2_cols = [
        ColumnSnapshot(
            original_name="cust_id",
            normalized_name="cust_id",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=0,
        ),
        ColumnSnapshot(
            original_name="bonus_points",
            normalized_name="bonus_points",
            inferred_type="integer",
            polars_type="Int64",
            nullable=True,
            ordinal_position=1,
        ),
    ]

    s1 = _make_snapshot("crm", 1, s1_cols)
    s2 = _make_snapshot("crm", 2, s2_cols)

    mapping = _make_approved_mapping(
        "map_v1",
        "crm",
        [
            ApprovedMappingDefinition(
                source_field="cust_id",
                target_field="customer_id",
                mapping_type=MappingType.DIRECT,
                decision=ReviewDecisionType.APPROVE,
                reviewer="steward",
                confidence=1.0,
                provenance_reason="Direct",
            )
        ],
    )

    payload = {
        "prior_schema": s1,
        "current_schema": s2,
        "approved_mapping": mapping,
        "persist": True,
    }

    res = test_client.post("/api/v1/onboarding/drift/compare", json=payload)
    assert res.status_code == 200
    report = res.json()
    assert report["source_id"] == "crm"
    assert report["overall_compatibility"] == "COMPATIBLE"
    assert report["review_required"] is False
    assert len(report["drift_events"]) == 1
    assert report["drift_events"][0]["drift_type"] == "ADDED_COLUMN"


def test_get_drift_report_by_id_and_not_found(test_client: TestClient) -> None:
    """Verify GET /drift/reports/{report_id} returns 200 when found and 404 when missing."""
    # 404 on missing report
    missing_res = test_client.get("/api/v1/onboarding/drift/reports/nonexistent_report")
    assert missing_res.status_code == 404
    assert missing_res.json()["error"]["code"] == "REPORT_NOT_FOUND"

    # Create a report first via compare
    s1_cols = [
        ColumnSnapshot(
            original_name="id",
            normalized_name="id",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=0,
        )
    ]
    s1 = _make_snapshot("crm", 1, s1_cols)
    s2 = _make_snapshot("crm", 2, s1_cols)
    mapping = _make_approved_mapping("map_v1", "crm", [])

    compare_res = test_client.post(
        "/api/v1/onboarding/drift/compare",
        json={"prior_schema": s1, "current_schema": s2, "approved_mapping": mapping, "persist": True},
    )
    assert compare_res.status_code == 200
    report_id = compare_res.json()["report_id"]

    fetch_res = test_client.get(f"/api/v1/onboarding/drift/reports/{report_id}")
    assert fetch_res.status_code == 200
    assert fetch_res.json()["report_id"] == report_id


def test_list_drift_reports_with_filter(test_client: TestClient) -> None:
    """Verify GET /drift/reports lists reports filtered by source_id."""
    s1_cols = [
        ColumnSnapshot(
            original_name="id",
            normalized_name="id",
            inferred_type="string",
            polars_type="String",
            nullable=False,
            ordinal_position=0,
        )
    ]
    s1_crm = _make_snapshot("crm_src", 1, s1_cols)
    s1_bill = _make_snapshot("bill_src", 1, s1_cols)
    mapping = _make_approved_mapping("map_v1", "crm_src", [])

    # Post 2 crm reports and 1 bill report
    test_client.post(
        "/api/v1/onboarding/drift/compare",
        json={"prior_schema": s1_crm, "current_schema": s1_crm, "approved_mapping": mapping, "persist": True},
    )
    test_client.post(
        "/api/v1/onboarding/drift/compare",
        json={"prior_schema": s1_crm, "current_schema": s1_crm, "approved_mapping": mapping, "persist": True},
    )
    test_client.post(
        "/api/v1/onboarding/drift/compare",
        json={"prior_schema": s1_bill, "current_schema": s1_bill, "approved_mapping": mapping, "persist": True},
    )

    # List all
    all_res = test_client.get("/api/v1/onboarding/drift/reports")
    assert all_res.status_code == 200
    assert all_res.json()["total"] == 3

    # Filter by source_id
    crm_res = test_client.get("/api/v1/onboarding/drift/reports?source_id=crm_src")
    assert crm_res.status_code == 200
    assert crm_res.json()["total"] == 2
    assert len(crm_res.json()["reports"]) == 2
