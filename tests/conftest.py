"""Shared test fixtures and helpers."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import polars as pl
import pytest

# Ensure src is importable
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


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
