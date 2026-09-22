"""Tests for Milestone 3: Human Approval, Mapping Versioning, Governance Invariants, and Immutability."""

from datetime import datetime, timezone
import json
from pathlib import Path
import pytest
from pydantic import ValidationError

from scd2_copilot.onboarding.adapters.csv_adapter import CSVSourceAdapter
from scd2_copilot.onboarding.canonical import get_canonical_customer_v1
from scd2_copilot.onboarding.approval.service import MappingReviewService, MappingReviewSession
from scd2_copilot.onboarding.exceptions import (
    DuplicateTargetMappingError,
    IncompleteMappingError,
    InvalidSourceFieldError,
    InvalidTargetFieldError,
    InvalidTransformationError,
    MappingApprovalError,
    UnresolvedAmbiguityError,
)
from scd2_copilot.onboarding.mapping.engine import SemanticMappingEngine
from scd2_copilot.onboarding.models.approval import (
    ApprovedMappingDefinition,
    ApprovedMappingVersion,
    FieldReviewDecision,
    ReviewDecisionType,
)
from scd2_copilot.onboarding.models.mapping import (
    FieldMappingProposal,
    LLMFieldProposal,
    LLMMappingBatchResponse,
    MappingProposalBatch,
    MappingType,
    TransformationOpType,
    TransformationStep,
)
from scd2_copilot.onboarding.models.schema_snapshot import ColumnSnapshot
from scd2_copilot.onboarding.models.source import SourceDefinition, SourceType
from scd2_copilot.onboarding.profiler.engine import DataProfiler


def _create_clean_crm_proposal_batch(crm_csv_path: Path) -> MappingProposalBatch:
    """Helper to generate a clean, completely unambiguous CRM proposal batch."""
    defn = SourceDefinition(
        source_id="crm_test",
        source_name="CRM Test",
        source_type=SourceType.CSV,
        connection_config={"file_path": str(crm_csv_path)},
    )
    adapter = CSVSourceAdapter(defn)
    df = adapter.read_data()
    schema = adapter.discover_schema(df)
    profiler = DataProfiler()
    profile = profiler.profile_dataframe("crm_test", df, schema.fingerprint.fingerprint_hash)

    def mock_llm(prompt: str) -> LLMMappingBatchResponse:
        return LLMMappingBatchResponse(
            proposals=[
                LLMFieldProposal(source_field="Cust_ID", target_field="customer_id", mapping_type="DIRECT", confidence=0.98, reason="ID"),
                LLMFieldProposal(source_field="First Name", target_field="first_name", mapping_type="TRANSFORMED", confidence=0.95, reason="First", operations=["TRIM"]),
                LLMFieldProposal(source_field="Last Name", target_field="last_name", mapping_type="TRANSFORMED", confidence=0.95, reason="Last", operations=["TRIM"]),
                LLMFieldProposal(source_field="Contact Email", target_field="email", mapping_type="TRANSFORMED", confidence=0.99, reason="Email", operations=["TRIM", "NORMALIZE_EMAIL"]),
                LLMFieldProposal(source_field="Phone Number", target_field=None, mapping_type="UNMAPPED", confidence=0.90, reason="Phone"),
                LLMFieldProposal(source_field="Birth_Date", target_field="date_of_birth", mapping_type="TRANSFORMED", confidence=0.92, reason="DOB", operations=["PARSE_DATE"]),
                LLMFieldProposal(source_field="Cust_Status", target_field="status", mapping_type="TRANSFORMED", confidence=0.94, reason="Status", operations=["TRIM", "UPPERCASE", "MAP_ENUM"]),
                LLMFieldProposal(source_field="Created_Timestamp", target_field="created_at", mapping_type="DIRECT", confidence=0.96, reason="Created"),
            ]
        )

    engine = SemanticMappingEngine(llm_caller=mock_llm)
    return engine.propose_mappings("crm_test", schema.columns, profile)


def test_direct_approval_valid_batch(crm_csv_path: Path) -> None:
    batch = _create_clean_crm_proposal_batch(crm_csv_path)
    service = MappingReviewService()
    session = service.create_session(batch)

    # Bulk approve all unambiguous proposals
    approved_cols = session.approve_all_unambiguous(reviewer="data_steward_1")
    assert len(approved_cols) == len(batch.proposals)
    assert session.is_fully_reviewed is True

    # Finalize version
    version = service.finalize_version(
        session=session,
        approved_by="data_steward_1",
        version_number=1,
    )

    assert isinstance(version, ApprovedMappingVersion)
    assert version.source_id == "crm_test"
    assert version.version_number == 1
    assert version.canonical_schema_name == "customer"
    assert version.canonical_schema_version == 1
    assert version.is_complete is True
    assert len(version.unmapped_required_fields) == 0
    assert version.approved_by == "data_steward_1"
    assert len(version.mappings) == len(batch.proposals)

    # Check mapping for Cust_ID
    id_map = version.get_mapping_for_source("Cust_ID")
    assert id_map is not None
    assert id_map.target_field == "customer_id"
    assert id_map.decision == ReviewDecisionType.APPROVE
    assert id_map.reviewer == "data_steward_1"


def test_crm_review_workflow_resolving_heuristic_ambiguity(crm_csv_path: Path) -> None:
    # Heuristic proposal batch where Cust_ID and Phone Number conflict on customer_id
    defn = SourceDefinition(
        source_id="crm_amb_test",
        source_name="CRM Ambiguity Test",
        source_type=SourceType.CSV,
        connection_config={"file_path": str(crm_csv_path)},
    )
    adapter = CSVSourceAdapter(defn)
    df = adapter.read_data()
    schema = adapter.discover_schema(df)
    profiler = DataProfiler()
    profile = profiler.profile_dataframe("crm_amb_test", df, schema.fingerprint.fingerprint_hash)

    engine = SemanticMappingEngine(enable_ai=False)
    batch = engine.propose_mappings("crm_amb_test", schema.columns, profile)

    service = MappingReviewService()
    session = service.create_session(batch)

    # Bulk approve unambiguous columns (6 columns)
    approved = session.approve_all_unambiguous(reviewer="steward_pat")
    assert len(approved) == 6
    assert "Cust_ID" not in approved
    assert "Phone Number" not in approved

    # Both ambiguous fields remain unresolved
    unresolved = session.get_unresolved_proposals()
    unresolved_names = {u.source_field for u in unresolved}
    assert unresolved_names == {"Cust_ID", "Phone Number"}

    # Attempting to finalize before resolving must fail
    with pytest.raises(MappingApprovalError):
        service.finalize_version(session=session, approved_by="steward_pat")

    # Reviewer explicitly resolves ambiguity: Cust_ID -> customer_id, Phone Number -> reject
    session.apply_decision(
        FieldReviewDecision(
            source_field="Cust_ID",
            decision=ReviewDecisionType.OVERRIDE,
            target_field="customer_id",
            reviewer="steward_pat",
            review_notes="Primary customer key confirmed",
        )
    )
    session.apply_decision(
        FieldReviewDecision(
            source_field="Phone Number",
            decision=ReviewDecisionType.REJECT,
            reviewer="steward_pat",
            review_notes="Contact phone excluded from customer.v1",
        )
    )

    # Now finalization succeeds cleanly
    version = service.finalize_version(session=session, approved_by="steward_pat")
    assert version.is_complete is True
    assert version.get_mapping_for_source("Cust_ID").target_field == "customer_id"
    assert version.get_mapping_for_source("Phone Number").target_field is None



def test_bulk_unambiguous_approval_skips_ambiguous() -> None:
    # Batch with 1 clear proposal and 1 ambiguous proposal
    proposals = [
        FieldMappingProposal(
            source_field="cust_id",
            target_field="customer_id",
            mapping_type=MappingType.DIRECT,
            confidence=0.98,
            reason="Clear identifier",
        ),
        FieldMappingProposal(
            source_field="work_email",
            target_field="email",
            mapping_type=MappingType.AMBIGUOUS,
            confidence=0.75,
            reason="Conflict with personal email",
            is_ambiguous=True,
            conflicting_targets=["personal_email"],
        ),
    ]
    batch = MappingProposalBatch(
        source_id="test_src",
        source_fingerprint="abc123hash",
        canonical_schema_name="customer",
        canonical_schema_version=1,
        proposals=proposals,
        provider_used="test",
    )

    service = MappingReviewService()
    session = service.create_session(batch)

    approved = session.approve_all_unambiguous(reviewer="reviewer_alex")
    assert approved == ["cust_id"]

    # work_email must remain unapproved and unresolved
    assert session.get_decision("work_email") is None
    unresolved = session.get_unresolved_proposals()
    assert len(unresolved) == 1
    assert unresolved[0].source_field == "work_email"


def test_rejection_marks_unmapped() -> None:
    proposals = [
        FieldMappingProposal(
            source_field="fax_number",
            target_field=None,
            mapping_type=MappingType.UNMAPPED,
            confidence=0.95,
            reason="Fax not in canonical contract",
        ),
        FieldMappingProposal(
            source_field="customer_id",
            target_field="customer_id",
            mapping_type=MappingType.DIRECT,
            confidence=0.99,
            reason="Matches ID",
        ),
    ]
    batch = MappingProposalBatch(
        source_id="test_reject",
        source_fingerprint="hash_reject",
        canonical_schema_name="customer",
        canonical_schema_version=1,
        proposals=proposals,
        provider_used="test",
    )

    service = MappingReviewService()
    session = service.create_session(batch)

    session.apply_decision(
        FieldReviewDecision(
            source_field="customer_id",
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
        )
    )
    session.apply_decision(
        FieldReviewDecision(
            source_field="fax_number",
            decision=ReviewDecisionType.REJECT,
            reviewer="steward",
            review_notes="Explicitly excluding legacy fax data",
        )
    )

    # Finalize with allow_incomplete=True since we only have customer_id
    version = service.finalize_version(
        session=session,
        approved_by="steward",
        allow_incomplete=True,
    )

    fax_map = version.get_mapping_for_source("fax_number")
    assert fax_map is not None
    assert fax_map.target_field is None
    assert fax_map.mapping_type == MappingType.UNMAPPED
    assert fax_map.decision == ReviewDecisionType.REJECT
    assert "Excluded" in fax_map.provenance_reason or "legacy fax" in fax_map.provenance_reason


def test_human_override_target_and_transformations() -> None:
    proposal = FieldMappingProposal(
        source_field="signup_str",
        target_field=None,
        mapping_type=MappingType.UNMAPPED,
        confidence=0.40,
        reason="Low confidence date string",
    )
    batch = MappingProposalBatch(
        source_id="test_override",
        source_fingerprint="hash_ovr",
        canonical_schema_name="customer",
        canonical_schema_version=1,
        proposals=[proposal],
        provider_used="test",
    )

    service = MappingReviewService()
    session = service.create_session(batch)

    # Human decides signup_str represents created_at, with PARSE_DATE transformation
    override_steps = [
        TransformationStep(
            op=TransformationOpType.PARSE_DATE,
            params={"format": "%d/%m/%Y"},
        ),
    ]
    decision = FieldReviewDecision(
        source_field="signup_str",
        decision=ReviewDecisionType.OVERRIDE,
        target_field="created_at",
        transformations=override_steps,
        reviewer="senior_analyst",
        review_notes="Verified against CRM manual that signup_str is the creation timestamp",
    )
    session.apply_decision(decision)

    version = service.finalize_version(
        session=session,
        approved_by="senior_analyst",
        allow_incomplete=True,
    )

    mapping = version.get_mapping_for_source("signup_str")
    assert mapping is not None
    assert mapping.target_field == "created_at"
    assert mapping.decision == ReviewDecisionType.OVERRIDE
    assert mapping.mapping_type == MappingType.TRANSFORMED
    assert mapping.confidence == 1.0  # Human authoritative override is 1.0
    assert len(mapping.transformations) == 1
    assert mapping.transformations[0].op == TransformationOpType.PARSE_DATE
    assert mapping.transformations[0].params["format"] == "%d/%m/%Y"


def test_invariant_unresolved_ambiguity_fails_direct_approval() -> None:
    proposal = FieldMappingProposal(
        source_field="raw_email",
        target_field="email",
        mapping_type=MappingType.AMBIGUOUS,
        confidence=0.70,
        reason="Multiple email fields detected",
        is_ambiguous=True,
        conflicting_targets=["sec_email"],
    )
    batch = MappingProposalBatch(
        source_id="amb_src",
        source_fingerprint="hash_amb",
        canonical_schema_name="customer",
        canonical_schema_version=1,
        proposals=[proposal],
        provider_used="test",
    )

    service = MappingReviewService()
    session = service.create_session(batch)

    # Attempting to directly APPROVE an ambiguous proposal must raise UnresolvedAmbiguityError
    with pytest.raises(UnresolvedAmbiguityError) as exc_info:
        session.apply_decision(
            FieldReviewDecision(
                source_field="raw_email",
                decision=ReviewDecisionType.APPROVE,
                reviewer="steward",
            )
        )
    assert "Cannot directly approve ambiguous proposal" in str(exc_info.value)


def test_invariant_resolving_ambiguity_with_override_and_reject() -> None:
    proposals = [
        FieldMappingProposal(
            source_field="work_email",
            target_field="email",
            mapping_type=MappingType.AMBIGUOUS,
            confidence=0.75,
            reason="Ambiguous email",
            is_ambiguous=True,
            conflicting_targets=["personal_email"],
        ),
        FieldMappingProposal(
            source_field="personal_email",
            target_field="email",
            mapping_type=MappingType.AMBIGUOUS,
            confidence=0.70,
            reason="Ambiguous email",
            is_ambiguous=True,
            conflicting_targets=["work_email"],
        ),
    ]
    batch = MappingProposalBatch(
        source_id="amb_resolve_src",
        source_fingerprint="hash_amb_res",
        canonical_schema_name="customer",
        canonical_schema_version=1,
        proposals=proposals,
        provider_used="test",
    )

    service = MappingReviewService()
    session = service.create_session(batch)

    # Resolve by overriding work_email to canonical email, and rejecting personal_email
    session.apply_decision(
        FieldReviewDecision(
            source_field="work_email",
            decision=ReviewDecisionType.OVERRIDE,
            target_field="email",
            transformations=[TransformationStep(op=TransformationOpType.NORMALIZE_EMAIL)],
            reviewer="lead_steward",
            review_notes="Enterprise policy: use work email as primary canonical email",
        )
    )
    session.apply_decision(
        FieldReviewDecision(
            source_field="personal_email",
            decision=ReviewDecisionType.REJECT,
            reviewer="lead_steward",
            review_notes="Ignore personal email in canonical ingestion",
        )
    )

    # Finalization succeeds because ambiguity is resolved and no duplicate target exists
    version = service.finalize_version(
        session=session,
        approved_by="lead_steward",
        allow_incomplete=True,
    )
    assert version.get_mapping_for_source("work_email").target_field == "email"
    assert version.get_mapping_for_source("personal_email").target_field is None


def test_invariant_duplicate_target_mapping_fails_finalization() -> None:
    proposals = [
        FieldMappingProposal(
            source_field="cust_num_1",
            target_field="customer_id",
            mapping_type=MappingType.DIRECT,
            confidence=0.90,
            reason="ID 1",
        ),
        FieldMappingProposal(
            source_field="cust_num_2",
            target_field="customer_id",
            mapping_type=MappingType.DIRECT,
            confidence=0.90,
            reason="ID 2",
        ),
    ]
    batch = MappingProposalBatch(
        source_id="dup_target_src",
        source_fingerprint="hash_dup",
        canonical_schema_name="customer",
        canonical_schema_version=1,
        proposals=proposals,
        provider_used="test",
    )

    service = MappingReviewService()
    session = service.create_session(batch)

    # Even if operator overrides both to customer_id, finalization must catch duplicate targets
    session.apply_decision(
        FieldReviewDecision(
            source_field="cust_num_1",
            decision=ReviewDecisionType.OVERRIDE,
            target_field="customer_id",
            reviewer="steward",
        )
    )
    session.apply_decision(
        FieldReviewDecision(
            source_field="cust_num_2",
            decision=ReviewDecisionType.OVERRIDE,
            target_field="customer_id",
            reviewer="steward",
        )
    )

    with pytest.raises(DuplicateTargetMappingError) as exc_info:
        service.finalize_version(session=session, approved_by="steward", allow_incomplete=True)
    assert "Duplicate canonical target mapping detected" in str(exc_info.value)
    assert "customer_id" in str(exc_info.value)


def test_invariant_invalid_canonical_target_fails() -> None:
    proposal = FieldMappingProposal(
        source_field="code",
        target_field=None,
        mapping_type=MappingType.UNMAPPED,
        confidence=0.50,
        reason="None",
    )
    batch = MappingProposalBatch(
        source_id="inv_target_src",
        source_fingerprint="hash_inv",
        canonical_schema_name="customer",
        canonical_schema_version=1,
        proposals=[proposal],
        provider_used="test",
    )

    service = MappingReviewService()
    session = service.create_session(batch)

    # Hallucinated or non-existent canonical target must raise InvalidTargetFieldError
    with pytest.raises(InvalidTargetFieldError) as exc_info:
        session.apply_decision(
            FieldReviewDecision(
                source_field="code",
                decision=ReviewDecisionType.OVERRIDE,
                target_field="hallucinated_column_name",
                reviewer="steward",
            )
        )
    assert "does not exist in canonical schema" in str(exc_info.value)


def test_invariant_invalid_source_field_fails() -> None:
    proposal = FieldMappingProposal(
        source_field="real_col",
        target_field="customer_id",
        mapping_type=MappingType.DIRECT,
        confidence=0.95,
        reason="ID",
    )
    batch = MappingProposalBatch(
        source_id="inv_src_field",
        source_fingerprint="hash_isf",
        canonical_schema_name="customer",
        canonical_schema_version=1,
        proposals=[proposal],
        provider_used="test",
    )

    service = MappingReviewService()
    session = service.create_session(batch)

    with pytest.raises(InvalidSourceFieldError) as exc_info:
        session.apply_decision(
            FieldReviewDecision(
                source_field="ghost_col",
                decision=ReviewDecisionType.APPROVE,
                reviewer="steward",
            )
        )
    assert "does not exist in the proposal batch" in str(exc_info.value)


def test_invariant_invalid_transformation_op_fails() -> None:
    proposal = FieldMappingProposal(
        source_field="fname",
        target_field="first_name",
        mapping_type=MappingType.DIRECT,
        confidence=0.90,
        reason="name",
    )
    batch = MappingProposalBatch(
        source_id="inv_op_src",
        source_fingerprint="hash_op",
        canonical_schema_name="customer",
        canonical_schema_version=1,
        proposals=[proposal],
        provider_used="test",
    )

    service = MappingReviewService()
    session = service.create_session(batch)

    # Submitting an unapproved transformation operation raises InvalidTransformationError
    with pytest.raises(Exception):
        # Either Pydantic schema validation or service validation catches invalid enum
        invalid_step = TransformationStep(op="EXECUTE_RAW_SQL", params={})  # type: ignore
        session.apply_decision(
            FieldReviewDecision(
                source_field="fname",
                decision=ReviewDecisionType.OVERRIDE,
                target_field="first_name",
                transformations=[invalid_step],
                reviewer="steward",
            )
        )


def test_invariant_unreviewed_proposals_fails_finalization() -> None:
    proposals = [
        FieldMappingProposal(
            source_field="id",
            target_field="customer_id",
            mapping_type=MappingType.DIRECT,
            confidence=0.95,
            reason="ID",
        ),
        FieldMappingProposal(
            source_field="extra_info",
            target_field=None,
            mapping_type=MappingType.UNMAPPED,
            confidence=0.90,
            reason="None",
        ),
    ]
    batch = MappingProposalBatch(
        source_id="unrev_src",
        source_fingerprint="hash_unrev",
        canonical_schema_name="customer",
        canonical_schema_version=1,
        proposals=proposals,
        provider_used="test",
    )

    service = MappingReviewService()
    session = service.create_session(batch)

    # Only review the first column; leave extra_info unreviewed
    session.apply_decision(
        FieldReviewDecision(
            source_field="id",
            decision=ReviewDecisionType.APPROVE,
            reviewer="steward",
        )
    )

    with pytest.raises(MappingApprovalError) as exc_info:
        service.finalize_version(session=session, approved_by="steward", allow_incomplete=True)
    assert "source fields have not been reviewed: ['extra_info']" in str(exc_info.value)


def test_invariant_incomplete_required_fields_enforcement(billing_csv_path: Path) -> None:
    # Billing dataset lacks first_name and last_name, and acct_code/client_full_name are ambiguous
    defn = SourceDefinition(
        source_id="billing_test",
        source_name="Billing Test",
        source_type=SourceType.CSV,
        connection_config={"file_path": str(billing_csv_path)},
    )
    adapter = CSVSourceAdapter(defn)
    df = adapter.read_data()
    schema = adapter.discover_schema(df)
    profiler = DataProfiler()
    profile = profiler.profile_dataframe("billing_test", df, schema.fingerprint.fingerprint_hash)

    engine = SemanticMappingEngine(enable_ai=False)
    batch = engine.propose_mappings("billing_test", schema.columns, profile)

    service = MappingReviewService()
    session = service.create_session(batch)
    # Approve unambiguous fields
    session.approve_all_unambiguous(reviewer="steward")

    # Resolve ambiguous fields: acct_code -> customer_id, client_full_name -> reject
    session.apply_decision(
        FieldReviewDecision(
            source_field="acct_code",
            decision=ReviewDecisionType.OVERRIDE,
            target_field="customer_id",
            reviewer="steward",
            review_notes="Account code is primary customer key in billing",
        )
    )
    session.apply_decision(
        FieldReviewDecision(
            source_field="client_full_name",
            decision=ReviewDecisionType.REJECT,
            reviewer="steward",
            review_notes="Exclude composite full name (cannot split safely here)",
        )
    )

    # Incomplete mapping without explicit flag must raise IncompleteMappingError (missing first_name, last_name)
    with pytest.raises(IncompleteMappingError) as exc_info:
        service.finalize_version(session=session, approved_by="steward", allow_incomplete=False)
    assert "Required canonical fields are not mapped" in str(exc_info.value)
    assert "first_name" in str(exc_info.value)
    assert "last_name" in str(exc_info.value)

    # Explicit allow_incomplete=True creates a valid but partial version marked is_complete=False
    partial_version = service.finalize_version(
        session=session,
        approved_by="steward",
        allow_incomplete=True,
    )
    assert partial_version.is_complete is False
    assert "first_name" in partial_version.unmapped_required_fields
    assert "last_name" in partial_version.unmapped_required_fields


def test_immutability_enforcement(crm_csv_path: Path) -> None:
    batch = _create_clean_crm_proposal_batch(crm_csv_path)
    service = MappingReviewService()
    session = service.create_session(batch)
    session.approve_all_unambiguous("steward")
    version = service.finalize_version(session, approved_by="steward")

    # Frozen Pydantic model must reject attribute mutation
    with pytest.raises((ValidationError, TypeError)):
        version.version_number = 99  # type: ignore

    with pytest.raises((ValidationError, TypeError)):
        version.is_complete = False  # type: ignore

    with pytest.raises((ValidationError, TypeError)):
        version.approved_by = "malicious_actor"  # type: ignore


def test_version_revision_creates_new_version_with_lineage(crm_csv_path: Path) -> None:
    batch = _create_clean_crm_proposal_batch(crm_csv_path)
    service = MappingReviewService()
    session_v1 = service.create_session(batch)
    session_v1.approve_all_unambiguous("lead_steward")
    v1 = service.finalize_version(
        session=session_v1,
        approved_by="lead_steward",
        version_number=1,
    )
    assert v1.version_number == 1
    assert v1.supersedes_version_id is None

    # Revise version: operator changes Birth_Date transformation or target
    session_v2 = service.revise_version(v1)

    # Apply override to Birth_Date
    session_v2.apply_decision(
        FieldReviewDecision(
            source_field="Birth_Date",
            decision=ReviewDecisionType.OVERRIDE,
            target_field="date_of_birth",
            transformations=[
                TransformationStep(
                    op=TransformationOpType.PARSE_DATE,
                    params={"format": "%Y-%m-%d"},
                )
            ],
            reviewer="compliance_officer",
            review_notes="Strict ISO date format required for DOB compliance",
        )
    )

    v2 = service.finalize_version(
        session=session_v2,
        approved_by="compliance_officer",
        version_number=2,
        supersedes_version_id=v1.mapping_version_id,
    )

    assert v2.version_number == 2
    assert v2.supersedes_version_id == v1.mapping_version_id
    assert v2.approved_by == "compliance_officer"

    # Verify DOB mapping updated in v2
    dob_v2 = v2.get_mapping_for_source("Birth_Date")
    assert dob_v2 is not None
    assert dob_v2.decision == ReviewDecisionType.OVERRIDE
    assert dob_v2.reviewer == "compliance_officer"
    assert dob_v2.transformations[0].params["format"] == "%Y-%m-%d"

    # Verify v1 remains completely unchanged
    dob_v1 = v1.get_mapping_for_source("Birth_Date")
    assert dob_v1.decision == ReviewDecisionType.APPROVE
    assert dob_v1.reviewer == "lead_steward"


def test_save_and_load_version_artifact(tmp_path: Path, crm_csv_path: Path) -> None:
    batch = _create_clean_crm_proposal_batch(crm_csv_path)
    service = MappingReviewService()
    session = service.create_session(batch)
    session.approve_all_unambiguous("audit_user")
    version = service.finalize_version(session, approved_by="audit_user")

    artifact_file = tmp_path / "approved_mapping_v1.json"
    saved_path = MappingReviewService.save_version_artifact(version, artifact_file)
    assert saved_path.exists()

    reloaded = MappingReviewService.load_version_artifact(saved_path)
    assert reloaded.mapping_version_id == version.mapping_version_id
    assert reloaded.version_number == version.version_number
    assert reloaded.source_id == version.source_id
    assert reloaded.is_complete == version.is_complete
    assert len(reloaded.mappings) == len(version.mappings)

    # Immutability must hold on reloaded object as well
    with pytest.raises((ValidationError, TypeError)):
        reloaded.version_number = 5  # type: ignore

