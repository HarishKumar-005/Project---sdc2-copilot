"""Shared test fixtures and helpers."""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import polars as pl
import pytest

# Optimize Prefect for fast local test execution (disable telemetry and network latencies)
os.environ.setdefault("PREFECT_SERVER_ANALYTICS_ENABLED", "False")
os.environ.setdefault("PREFECT_API_ENABLE_HTTP2", "False")
os.environ.setdefault("PREFECT_LOGGING_TO_API_ENABLED", "False")
os.environ.setdefault("PREFECT_SERVER_DATABASE_TIMEOUT", "30.0")

# Ensure src is importable
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


@pytest.fixture(autouse=True)
def _isolate_test_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure tests run against an isolated runs directory and isolated Prefect database."""
    test_runs_dir = tmp_path / "test_runs"
    test_runs_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("RUNS_DIRECTORY", str(test_runs_dir))

    test_prefect_home = tmp_path / "prefect_home"
    test_prefect_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PREFECT_HOME", str(test_prefect_home))

    # Remove PREFECT_API_URL so tests use ephemeral mode instead of
    # connecting to the production local server on port 4200.
    # deployment.py sets this at module level for production use.
    monkeypatch.delenv("PREFECT_API_URL", raising=False)


@pytest.fixture
def source_df() -> pl.DataFrame:
    """Standard source_today DataFrame."""
    return pl.DataFrame({
        "customer_id": [101, 102, 103, 104],
        "name": ["Ravi", "Priya", "Arun", "Kiran"],
        "city": ["Bengaluru", "Mumbai", "Delhi", "Hyderabad"],
        "tier": ["Gold", "Silver", "Gold", "Bronze"],
    })


@pytest.fixture
def target_df() -> pl.DataFrame:
    """Standard target_yesterday DataFrame with normalized types."""
    return pl.DataFrame({
        "customer_id": [101, 102, 103],
        "name": ["Ravi", "Priya", "Arun"],
        "city": ["Chennai", "Mumbai", "Delhi"],
        "tier": ["Gold", "Silver", "Gold"],
        "effective_from": [date(2026, 6, 7), date(2026, 6, 7), date(2026, 6, 7)],
        "effective_to": [None, None, None],
        "is_current": [True, True, True],
    })


@pytest.fixture
def expected_output_df() -> pl.DataFrame:
    """Standard expected_output DataFrame with normalized types."""
    return pl.DataFrame({
        "customer_id": [101, 101, 102, 103, 104],
        "name": ["Ravi", "Ravi", "Priya", "Arun", "Kiran"],
        "city": ["Chennai", "Bengaluru", "Mumbai", "Delhi", "Hyderabad"],
        "tier": ["Gold", "Gold", "Silver", "Gold", "Bronze"],
        "effective_from": [date(2026, 6, 7), date(2026, 6, 8), date(2026, 6, 7), date(2026, 6, 7), date(2026, 6, 8)],
        "effective_to": [date(2026, 6, 8), None, None, None, None],
        "is_current": [False, True, True, True, True],
    })


@pytest.fixture
def business_key() -> list[str]:
    return ["customer_id"]


@pytest.fixture
def tracked_columns() -> list[str]:
    return ["name", "city", "tier"]


@pytest.fixture
def processing_date() -> date:
    return date(2026, 6, 8)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Deselect benchmark tests by default unless '-m benchmark' is explicitly specified."""
    markexpr = config.getoption("markexpr") or ""
    if "benchmark" not in markexpr:
        selected: list[pytest.Item] = []
        deselected: list[pytest.Item] = []
        for item in items:
            if "benchmark" in item.keywords:
                deselected.append(item)
            else:
                selected.append(item)
        if deselected:
            config.hook.pytest_deselected(items=deselected)
            items[:] = selected

