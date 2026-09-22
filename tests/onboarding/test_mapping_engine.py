"""Tests for SemanticMappingEngine: AI proposals, fallbacks, ambiguity, sanitization, and artifacts."""

import json
from pathlib import Path
import pytest

from scd2_copilot.onboarding.adapters.csv_adapter import CSVSourceAdapter
from scd2_copilot.onboarding.canonical import get_canonical_customer_v1
from scd2_copilot.onboarding.mapping.engine import SemanticMappingEngine
from scd2_copilot.onboarding.models.mapping import (
    FieldMappingProposal,
    LLMFieldProposal,
    LLMMappingBatchResponse,
    MappingProposalBatch,
    MappingType,
    TransformationOpType,
)
from scd2_copilot.onboarding.models.source import SourceDefinition, SourceType
from scd2_copilot.onboarding.profiler.engine import DataProfiler


def test_mapping_engine_with_mocked_llm(crm_csv_path: Path) -> None:
    # Ingest and profile CRM
    defn = SourceDefinition(
        source_id="crm_mock_ai",
        source_name="CRM Mock AI",
        source_type=SourceType.CSV,
        connection_config={"file_path": str(crm_csv_path)},
    )
    adapter = CSVSourceAdapter(defn)
    df = adapter.read_data()
    schema = adapter.discover_schema(df)
    profiler = DataProfiler()
    profile = profiler.profile_dataframe("crm_mock_ai", df, schema.fingerprint.fingerprint_hash)

    # Simulated structured LLM response
    def mock_llm(prompt: str) -> LLMMappingBatchResponse:
        return LLMMappingBatchResponse(
            proposals=[
                LLMFieldProposal(
                    source_field="Cust_ID",
                    target_field="customer_id",
                    mapping_type="DIRECT",
                    confidence=0.98,
                    reason="Cust_ID uniquely identifies customer entities.",
                    operations=[],
                ),
                LLMFieldProposal(
                    source_field="First Name",
                    target_field="first_name",
                    mapping_type="TRANSFORMED",
                    confidence=0.95,
                    reason="First Name corresponds to customer given name.",
                    operations=["TRIM"],
                ),
                LLMFieldProposal(
                    source_field="Last Name",
                    target_field="last_name",
                    mapping_type="TRANSFORMED",
                    confidence=0.95,
                    reason="Last Name corresponds to customer family name.",
                    operations=["TRIM"],
                ),
                LLMFieldProposal(
                    source_field="Contact Email",
                    target_field="email",
                    mapping_type="TRANSFORMED",
                    confidence=0.99,
                    reason="Contact Email is verified email address.",
                    operations=["TRIM", "NORMALIZE_EMAIL"],
                ),
                LLMFieldProposal(
                    source_field="Phone Number",
                    target_field=None,
                    mapping_type="UNMAPPED",
                    confidence=0.90,
                    reason="Phone Number is not part of customer.v1 canonical contract.",
                    operations=[],
                ),
                LLMFieldProposal(
                    source_field="Birth_Date",
                    target_field="date_of_birth",
                    mapping_type="TRANSFORMED",
                    confidence=0.92,
                    reason="Birth_Date maps to date_of_birth.",
                    operations=["PARSE_DATE"],
                ),
                LLMFieldProposal(
                    source_field="Cust_Status",
                    target_field="status",
                    mapping_type="TRANSFORMED",
                    confidence=0.94,
                    reason="Cust_Status maps to customer status enum.",
                    operations=["TRIM", "UPPERCASE", "MAP_ENUM"],
                ),
                LLMFieldProposal(
                    source_field="Created_Timestamp",
                    target_field="created_at",
                    mapping_type="DIRECT",
                    confidence=0.96,
                    reason="Created_Timestamp matches canonical creation timestamp.",
                    operations=[],
                ),
            ]
        )

    engine = SemanticMappingEngine(llm_caller=mock_llm)
    batch = engine.propose_mappings("crm_mock_ai", schema.columns, profile)

    assert isinstance(batch, MappingProposalBatch)
    assert batch.source_id == "crm_mock_ai"
    assert batch.is_fallback is False
    assert batch.provider_used == "custom_llm"
    assert len(batch.proposals) == 8
    assert len(batch.unmapped_canonical_fields) == 0  # all 6 required fields mapped

    email_prop = batch.get_proposal_for_source("Contact Email")
    assert email_prop is not None
    assert email_prop.target_field == "email"
    assert email_prop.mapping_type == MappingType.TRANSFORMED
    ops = [t.op for t in email_prop.transformations]
    assert ops == [TransformationOpType.TRIM, TransformationOpType.NORMALIZE_EMAIL]

    phone_prop = batch.get_proposal_for_source("Phone Number")
    assert phone_prop is not None
    assert phone_prop.target_field is None
    assert phone_prop.mapping_type == MappingType.UNMAPPED


def test_mapping_engine_fallback_when_llm_fails(crm_csv_path: Path) -> None:
    defn = SourceDefinition(
        source_id="crm_fallback",
        source_name="CRM Fallback",
        source_type=SourceType.CSV,
        connection_config={"file_path": str(crm_csv_path)},
    )
    adapter = CSVSourceAdapter(defn)
    df = adapter.read_data()
    schema = adapter.discover_schema(df)
    profiler = DataProfiler()
    profile = profiler.profile_dataframe("crm_fallback", df, schema.fingerprint.fingerprint_hash)

    def failing_llm(prompt: str) -> LLMMappingBatchResponse:
        raise RuntimeError("API rate limit exceeded (HTTP 429)")

    engine = SemanticMappingEngine(llm_caller=failing_llm)
    batch = engine.propose_mappings("crm_fallback", schema.columns, profile)

    # Engine must not crash, must fall back to deterministic candidates
    assert batch.is_fallback is True
    assert "rate limit" in str(batch.fallback_reason).lower()
    assert len(batch.proposals) == 8

    # Cust_ID is still deterministically mapped to customer_id
    id_prop = batch.get_proposal_for_source("Cust_ID")
    assert id_prop is not None
    assert id_prop.target_field == "customer_id"
    assert id_prop.confidence >= 0.85


def test_mapping_engine_sanitizes_unapproved_operations(crm_csv_path: Path) -> None:
    defn = SourceDefinition(
        source_id="crm_sanitize",
        source_name="CRM Sanitize",
        source_type=SourceType.CSV,
        connection_config={"file_path": str(crm_csv_path)},
    )
    adapter = CSVSourceAdapter(defn)
    df = adapter.read_data()
    schema = adapter.discover_schema(df)
    profiler = DataProfiler()
    profile = profiler.profile_dataframe("crm_sanitize", df, schema.fingerprint.fingerprint_hash)

    # Malicious or hallucinated operations returned by model
    def unapproved_ops_llm(prompt: str) -> LLMMappingBatchResponse:
        return LLMMappingBatchResponse(
            proposals=[
                LLMFieldProposal(
                    source_field="Cust_ID",
                    target_field="customer_id",
                    mapping_type="TRANSFORMED",
                    confidence=0.90,
                    reason="Test reason",
                    operations=["TRIM", "EXECUTE_ARBITRARY_PYTHON", "SQL_INJECTION", "LOWERCASE"],
                )
            ]
        )

    engine = SemanticMappingEngine(llm_caller=unapproved_ops_llm)
    batch = engine.propose_mappings("crm_sanitize", schema.columns, profile)

    id_prop = batch.get_proposal_for_source("Cust_ID")
    assert id_prop is not None
    # Only TRIM and LOWERCASE survive; invalid operations are dropped
    ops = [t.op for t in id_prop.transformations]
    assert ops == [TransformationOpType.TRIM, TransformationOpType.LOWERCASE]


def test_mapping_engine_detects_conflicts_and_ambiguity() -> None:
    # Synthetic case where two columns claim the same canonical field
    from scd2_copilot.onboarding.models.schema_snapshot import ColumnSnapshot

    cols = [
        ColumnSnapshot(original_name="work_email", normalized_name="work_email", inferred_type="string", polars_type="String", nullable=False, ordinal_position=0),
        ColumnSnapshot(original_name="personal_email", normalized_name="personal_email", inferred_type="string", polars_type="String", nullable=False, ordinal_position=1),
    ]
    import polars as pl
    df = pl.DataFrame({"work_email": ["a@w.com"], "personal_email": ["a@p.com"]})
    profiler = DataProfiler()
    profile = profiler.profile_dataframe("email_conflict", df)

    def conflicting_llm(prompt: str) -> LLMMappingBatchResponse:
        return LLMMappingBatchResponse(
            proposals=[
                LLMFieldProposal(source_field="work_email", target_field="email", mapping_type="DIRECT", confidence=0.85, reason="Work email"),
                LLMFieldProposal(source_field="personal_email", target_field="email", mapping_type="DIRECT", confidence=0.80, reason="Personal email"),
            ]
        )

    engine = SemanticMappingEngine(llm_caller=conflicting_llm)
    batch = engine.propose_mappings("email_conflict", cols, profile)

    p1 = batch.get_proposal_for_source("work_email")
    p2 = batch.get_proposal_for_source("personal_email")
    assert p1 is not None and p2 is not None
    assert p1.is_ambiguous is True
    assert p2.is_ambiguous is True
    assert p1.mapping_type == MappingType.AMBIGUOUS
    assert "personal_email" in p1.conflicting_targets
    assert "work_email" in p2.conflicting_targets


def test_mapping_engine_identifies_unmapped_required_fields(billing_csv_path: Path) -> None:
    # Billing data has no first_name or last_name
    defn = SourceDefinition(
        source_id="billing_unmapped_test",
        source_name="Billing Test",
        source_type=SourceType.CSV,
        connection_config={"file_path": str(billing_csv_path)},
    )
    adapter = CSVSourceAdapter(defn)
    df = adapter.read_data()
    schema = adapter.discover_schema(df)
    profiler = DataProfiler()
    profile = profiler.profile_dataframe("billing_unmapped_test", df, schema.fingerprint.fingerprint_hash)

    engine = SemanticMappingEngine(enable_ai=False)  # deterministic fallback
    batch = engine.propose_mappings("billing_unmapped_test", schema.columns, profile)

    # first_name and last_name are required in customer.v1, but missing in billing accounts
    assert "first_name" in batch.unmapped_canonical_fields
    assert "last_name" in batch.unmapped_canonical_fields


def test_save_and_load_proposal_artifact(tmp_path: Path, crm_csv_path: Path) -> None:
    defn = SourceDefinition(
        source_id="artifact_test",
        source_name="Artifact Test",
        source_type=SourceType.CSV,
        connection_config={"file_path": str(crm_csv_path)},
    )
    adapter = CSVSourceAdapter(defn)
    df = adapter.read_data()
    schema = adapter.discover_schema(df)
    profiler = DataProfiler()
    profile = profiler.profile_dataframe("artifact_test", df, schema.fingerprint.fingerprint_hash)

    engine = SemanticMappingEngine(enable_ai=False)
    batch = engine.propose_mappings("artifact_test", schema.columns, profile)

    artifact_file = tmp_path / "mapping_proposals.json"
    saved_path = SemanticMappingEngine.save_proposal_artifact(batch, artifact_file)
    assert saved_path.exists()

    reloaded_dict = json.loads(saved_path.read_text(encoding="utf-8"))
    reloaded_batch = MappingProposalBatch.model_validate(reloaded_dict)
    assert reloaded_batch.source_id == "artifact_test"
    assert len(reloaded_batch.proposals) == len(batch.proposals)
