"""Shared test fixtures for onboarding unit and integration tests."""

from pathlib import Path
import pytest
import httpx


@pytest.fixture
def sample_data_dir() -> Path:
    """Return path to sample-data/onboarding directory."""
    p = Path(__file__).resolve().parents[2] / "sample-data" / "onboarding"
    assert p.exists(), f"Sample data dir not found at {p}"
    return p


@pytest.fixture
def crm_csv_path(sample_data_dir: Path) -> Path:
    return sample_data_dir / "crm_customers.csv"


@pytest.fixture
def crm_json_path(sample_data_dir: Path) -> Path:
    return sample_data_dir / "crm_customers.json"


@pytest.fixture
def billing_csv_path(sample_data_dir: Path) -> Path:
    return sample_data_dir / "billing_accounts.csv"


@pytest.fixture
def billing_json_path(sample_data_dir: Path) -> Path:
    return sample_data_dir / "billing_accounts.json"


@pytest.fixture
def support_csv_path(sample_data_dir: Path) -> Path:
    return sample_data_dir / "support_tickets.csv"


@pytest.fixture
def support_json_path(sample_data_dir: Path) -> Path:
    return sample_data_dir / "support_tickets.json"
