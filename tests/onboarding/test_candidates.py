"""Tests for DeterministicCandidateGenerator on synthetic CRM, Billing, and Support sources."""

from pathlib import Path
import pytest

from scd2_copilot.onboarding.adapters.csv_adapter import CSVSourceAdapter
from scd2_copilot.onboarding.canonical import get_canonical_customer_v1
from scd2_copilot.onboarding.mapping.candidates import DeterministicCandidateGenerator
from scd2_copilot.onboarding.models.mapping import TransformationOpType
from scd2_copilot.onboarding.models.source import SourceDefinition, SourceType
from scd2_copilot.onboarding.profiler.engine import DataProfiler


def test_crm_candidate_generation(crm_csv_path: Path) -> None:
    defn = SourceDefinition(
        source_id="crm_customer",
        source_name="CRM Customer",
        source_type=SourceType.CSV,
        connection_config={"file_path": str(crm_csv_path)},
    )
    adapter = CSVSourceAdapter(defn)
    df = adapter.read_data()
    schema = adapter.discover_schema(df)
    profiler = DataProfiler()
    profile = profiler.profile_dataframe("crm_customer", df, schema.fingerprint.fingerprint_hash)

    canonical = get_canonical_customer_v1()
    gen = DeterministicCandidateGenerator(canonical)
    candidates = gen.generate_candidates(schema.columns, profile)

    # 1. Cust_ID -> customer_id
    id_cands = candidates.get("Cust_ID", [])
    assert len(id_cands) > 0
    assert id_cands[0].candidate_target_field == "customer_id"
    assert id_cands[0].heuristic_confidence >= 0.85
    assert "unique_identifier_confirmed" in id_cands[0].evidence.matched_signals

    # 2. First Name -> first_name
    fn_cands = candidates.get("First Name", [])
    assert len(fn_cands) > 0
    assert fn_cands[0].candidate_target_field == "first_name"

    # 3. Last Name -> last_name
    ln_cands = candidates.get("Last Name", [])
    assert len(ln_cands) > 0
    assert ln_cands[0].candidate_target_field == "last_name"

    # 4. Contact Email -> email with TRIM and NORMALIZE_EMAIL transformations
    email_cands = candidates.get("Contact Email", [])
    assert len(email_cands) > 0
    assert email_cands[0].candidate_target_field == "email"
    ops = [t.op for t in email_cands[0].suggested_transformations]
    assert TransformationOpType.TRIM in ops
    assert TransformationOpType.NORMALIZE_EMAIL in ops

    # 5. Birth_Date -> date_of_birth
    dob_cands = candidates.get("Birth_Date", [])
    assert len(dob_cands) > 0
    assert dob_cands[0].candidate_target_field == "date_of_birth"

    # 6. Cust_Status -> status with enum operations
    status_cands = candidates.get("Cust_Status", [])
    assert len(status_cands) > 0
    assert status_cands[0].candidate_target_field == "status"
    ops_status = [t.op for t in status_cands[0].suggested_transformations]
    assert TransformationOpType.MAP_ENUM in ops_status

    # 7. Created_Timestamp -> created_at
    ts_cands = candidates.get("Created_Timestamp", [])
    assert len(ts_cands) > 0
    assert ts_cands[0].candidate_target_field == "created_at"


def test_billing_candidate_generation(billing_csv_path: Path) -> None:
    defn = SourceDefinition(
        source_id="billing_accounts",
        source_name="Billing Accounts",
        source_type=SourceType.CSV,
        connection_config={"file_path": str(billing_csv_path)},
    )
    adapter = CSVSourceAdapter(defn)
    df = adapter.read_data()
    schema = adapter.discover_schema(df)
    profiler = DataProfiler()
    profile = profiler.profile_dataframe("billing_accounts", df, schema.fingerprint.fingerprint_hash)

    canonical = get_canonical_customer_v1()
    gen = DeterministicCandidateGenerator(canonical)
    candidates = gen.generate_candidates(schema.columns, profile)

    # acct_code -> customer_id
    assert candidates["acct_code"][0].candidate_target_field == "customer_id"
    # billing_email -> email
    assert candidates["billing_email"][0].candidate_target_field == "email"
    # account_state -> status
    assert candidates["account_state"][0].candidate_target_field == "status"


def test_candidate_generation_reproducibility(crm_csv_path: Path) -> None:
    defn = SourceDefinition(
        source_id="crm_rep",
        source_name="CRM Rep",
        source_type=SourceType.CSV,
        connection_config={"file_path": str(crm_csv_path)},
    )
    adapter = CSVSourceAdapter(defn)
    df = adapter.read_data()
    schema = adapter.discover_schema(df)
    profiler = DataProfiler()
    profile = profiler.profile_dataframe("crm_rep", df, schema.fingerprint.fingerprint_hash)

    canonical = get_canonical_customer_v1()
    gen = DeterministicCandidateGenerator(canonical)
    run1 = gen.generate_candidates(schema.columns, profile)
    run2 = gen.generate_candidates(schema.columns, profile)

    assert run1.keys() == run2.keys()
    for col_name in run1:
        list1 = run1[col_name]
        list2 = run2[col_name]
        assert len(list1) == len(list2)
        for c1, c2 in zip(list1, list2):
            assert c1.candidate_target_field == c2.candidate_target_field
            assert c1.heuristic_confidence == c2.heuristic_confidence
