"""Unit tests for Google OIDC authentication identity models, claims parsing, and metadata attachment."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import patch

import polars as pl
import pytest
import streamlit as st

from src.scd2_copilot.artifacts import (
    RunMetadata,
    list_runs,
    read_run_artifacts,
    write_run_artifacts,
)
from src.scd2_copilot.auth import (
    AuthenticatedUser,
    get_current_user,
    is_auth_configured,
    is_user_logged_in,
)
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


class DummyUserInfo:
    """Mock st.user proxy object for unit testing."""

    def __init__(self, data: dict[str, Any]):
        self._data = data

    def __getattr__(self, name: str) -> Any:
        if name in self._data:
            return self._data[name]
        raise AttributeError(f"No attribute {name}")

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def to_dict(self) -> dict[str, Any]:
        return dict(self._data)


# ── 1. AuthenticatedUser Model Tests ─────────────────────────


def test_authenticated_user_immutability():
    """AuthenticatedUser is frozen and immutable."""
    user = AuthenticatedUser(
        provider="google",
        subject="sub_12345",
        email="alice@example.com",
        name="Alice Doe",
    )
    assert user.provider == "google"
    assert user.subject == "sub_12345"
    assert user.email == "alice@example.com"
    assert user.name == "Alice Doe"

    with pytest.raises(FrozenInstanceError):
        user.name = "Bob"  # type: ignore


def test_authenticated_user_to_audit_dict():
    """to_audit_dict returns only sanitized identity claims, no tokens or excess fields."""
    user = AuthenticatedUser(
        provider="google",
        subject="sub_12345",
        email="alice@example.com",
        name="Alice Doe",
    )
    audit = user.to_audit_dict()
    assert audit == {
        "provider": "google",
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
        "provider": "google",
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


# ── 2. Claims Extraction & Login State Tests ─────────────────


def test_unauthenticated_state():
    """Unauthenticated st.user returns False for is_user_logged_in and None for get_current_user."""
    mock_proxy = DummyUserInfo({"is_logged_in": False})
    with patch.object(st, "user", mock_proxy):
        assert is_user_logged_in() is False
        assert get_current_user() is None


def test_authenticated_state_full_claims():
    """Authenticated st.user parses full claims into AuthenticatedUser."""
    mock_proxy = DummyUserInfo({
        "is_logged_in": True,
        "sub": "google_sub_1001",
        "email": "analyst@corp.com",
        "name": "Data Analyst",
    })
    with patch.object(st, "user", mock_proxy):
        assert is_user_logged_in() is True
        user = get_current_user()
        assert user is not None
        assert user.provider == "google"
        assert user.subject == "google_sub_1001"
        assert user.email == "analyst@corp.com"
        assert user.name == "Data Analyst"


def test_authenticated_state_missing_optional_claims():
    """Authenticated st.user handles missing name and email without crashing."""
    mock_proxy = DummyUserInfo({
        "is_logged_in": True,
        "sub": "google_sub_1002",
        # email and name omitted
    })
    with patch.object(st, "user", mock_proxy):
        assert is_user_logged_in() is True
        user = get_current_user()
        assert user is not None
        assert user.subject == "google_sub_1002"
        assert user.email is None
        assert user.name is None


def test_auth_configured_check():
    """is_auth_configured detects presence or absence of [auth] in st.secrets."""
    with patch.object(st, "secrets", {}):
        assert is_auth_configured() is False

    with patch.object(st, "secrets", {"auth": {"client_id": "cid", "client_secret": "csec"}}):
        assert is_auth_configured() is True

    with patch.object(st, "secrets", {"auth": {"google": {"client_id": "cid", "client_secret": "csec"}}}):
        assert is_auth_configured() is True


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
