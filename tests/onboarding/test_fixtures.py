"""Tests for synthetic fixtures: verifying CRM, Billing, and Support CSV & JSON datasets."""

import json
from pathlib import Path
import pytest
import httpx
import polars as pl

from scd2_copilot.onboarding.adapters.csv_adapter import CSVSourceAdapter
from scd2_copilot.onboarding.adapters.mock_rest_adapter import MockRESTSourceAdapter
from scd2_copilot.onboarding.models.profile import SamplingConfig, SamplingPolicy
from scd2_copilot.onboarding.models.source import SourceDefinition, SourceType
from scd2_copilot.onboarding.profiler.engine import DataProfiler


def test_crm_fixture_csv_and_rest_parity(crm_csv_path: Path, crm_json_path: Path) -> None:
    # 1. Ingest via CSV adapter
    csv_defn = SourceDefinition(
        source_id="crm_customer",
        source_name="CRM Customer",
        source_type=SourceType.CSV,
        connection_config={"file_path": str(crm_csv_path)},
    )
    csv_adapter = CSVSourceAdapter(csv_defn)
    csv_df = csv_adapter.read_data()
    csv_schema = csv_adapter.discover_schema(csv_df)

    # 2. Ingest via Mock REST adapter using canned JSON
    canned_json = json.loads(crm_json_path.read_text(encoding="utf-8"))
    mock_transport = httpx.MockTransport(lambda req: httpx.Response(200, json=canned_json))
    rest_defn = SourceDefinition(
        source_id="crm_customer",
        source_name="CRM Customer",
        source_type=SourceType.MOCK_REST,
        connection_config={"base_url": "https://api.crm.internal/customers"},
    )
    rest_adapter = MockRESTSourceAdapter(rest_defn, transport=mock_transport)
    rest_df = rest_adapter.read_data()
    rest_schema = rest_adapter.discover_schema(rest_df)

    # Parity assertions
    assert csv_df.height == rest_df.height == 10
    assert csv_df.columns == rest_df.columns

    # Profile both
    profiler = DataProfiler(SamplingConfig(policy=SamplingPolicy.MASKED))
    csv_profile = profiler.profile_dataframe("crm_customer", csv_df, csv_schema.fingerprint.fingerprint_hash)
    rest_profile = profiler.profile_dataframe("crm_customer", rest_df, rest_schema.fingerprint.fingerprint_hash)

    assert csv_profile.total_rows == rest_profile.total_rows == 10
    assert csv_profile.total_columns == rest_profile.total_columns == 8

    # Check PII email was masked
    email_col = csv_profile.get_column("Contact Email")
    assert email_col is not None
    assert all("@" in s and "***" in s for s in email_col.samples)


def test_billing_fixture_profiling(billing_csv_path: Path) -> None:
    defn = SourceDefinition(
        source_id="billing_accounts",
        source_name="Billing Accounts",
        source_type=SourceType.CSV,
        connection_config={"file_path": str(billing_csv_path)},
    )
    adapter = CSVSourceAdapter(defn)
    df = adapter.read_data()
    schema = adapter.discover_schema(df)

    assert df.height == 8
    assert "acct_code" in df.columns
    assert "balance_due" in df.columns

    profiler = DataProfiler()
    profile = profiler.profile_dataframe("billing_accounts", df, schema.fingerprint.fingerprint_hash)

    acct_col = profile.get_column("acct_code")
    assert acct_col is not None
    assert acct_col.is_unique is True

    balance_col = profile.get_column("balance_due")
    assert balance_col is not None
    assert balance_col.null_count == 1  # Horizon Health has null balance


def test_support_fixture_profiling(support_csv_path: Path) -> None:
    defn = SourceDefinition(
        source_id="support_tickets",
        source_name="Support Tickets",
        source_type=SourceType.CSV,
        connection_config={"file_path": str(support_csv_path)},
    )
    adapter = CSVSourceAdapter(defn)
    df = adapter.read_data()
    schema = adapter.discover_schema(df)

    assert df.height == 6
    assert "ticket_ref" in df.columns
    assert "priority_level" in df.columns

    profiler = DataProfiler()
    profile = profiler.profile_dataframe("support_tickets", df, schema.fingerprint.fingerprint_hash)

    priority_col = profile.get_column("priority_level")
    assert priority_col is not None
    assert len(priority_col.top_values) > 0
