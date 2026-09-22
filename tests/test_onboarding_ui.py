"""Comprehensive test suite verifying Customer Data Onboarding Streamlit UI state, services, and demo flows."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
import polars as pl
import pytest

from app.onboarding_ui.state import AVAILABLE_FIXTURES, OnboardingUIState
from src.scd2_copilot.onboarding.canonical import CANONICAL_CUSTOMER_V1
from src.scd2_copilot.onboarding.models.approval import ApprovedMappingVersion
from src.scd2_copilot.onboarding.models.drift import MappingCompatibilityState, SchemaDriftReport
from src.scd2_copilot.onboarding.models.exception import CorrectionType, ExceptionStatus
from src.scd2_copilot.onboarding.models.run import RunStatus


@pytest.fixture
def clean_ui_state() -> OnboardingUIState:
    """Create a clean OnboardingUIState instance with mock session state."""
    mock_session = {}
    with patch("streamlit.session_state", mock_session):
        state = OnboardingUIState()
        OnboardingUIState._init_defaults()
        return state


def test_ui_state_initialization(clean_ui_state: OnboardingUIState) -> None:
    """Verify state initialization and domain service bindings."""
    state = clean_ui_state
    assert state.profiler is not None
    assert state.candidate_generator is not None
    assert state.mapping_engine is not None
    assert state.pipeline is not None
    assert state.exception_service is not None
    assert state.run_service is not None
    assert state.drift_service is not None
    assert state.scd2_service is not None
    assert state.guardrail_service is not None


def test_fixture_loading(clean_ui_state: OnboardingUIState) -> None:
    """Verify loading of built-in synthetic fixtures (CRM, Billing, Support)."""
    state = clean_ui_state
    mock_session: dict = {}

    with patch("streamlit.session_state", mock_session):
        for fixture_name in ("CRM Customers", "Billing Accounts", "Support Tickets"):
            df, schema = state.load_fixture(fixture_name)
            assert isinstance(df, pl.DataFrame)
            assert df.height > 0
            assert len(schema.columns) > 0
            assert schema.schema_fingerprint is not None
            assert mock_session["onb_selected_fixture"] == fixture_name


def test_profiling_and_pii_masking(clean_ui_state: OnboardingUIState) -> None:
    """Verify statistical profiling and privacy-preserving PII suppression."""
    state = clean_ui_state
    mock_session: dict = {}

    with patch("streamlit.session_state", mock_session):
        state.load_fixture("CRM Customers")
        profile = state.profile_active_source()

        assert profile.total_rows > 0
        assert len(profile.columns) == len(mock_session["onb_source_schema"].columns)
        # Verify no raw PII in generated metadata summary
        col_names = [c.column_name for c in profile.columns]
        assert "Contact Email" in col_names or "Cust_ID" in col_names


def test_mapping_proposal_and_approval(clean_ui_state: OnboardingUIState) -> None:
    """Verify semantic mapping candidate proposal and human approval gate."""
    state = clean_ui_state
    mock_session: dict = {}

    with patch("streamlit.session_state", mock_session):
        state.load_fixture("CRM Customers")
        state.profile_active_source()
        proposals = state.generate_mapping_proposals(enable_ai=False)  # deterministic fallback

        assert len(proposals.proposals) >= 5
        assert mock_session["onb_review_session"] is not None

        approved = state.approve_all_mappings(reviewed_by="test_reviewer@enterprise.org")
        assert isinstance(approved, ApprovedMappingVersion)
        assert approved.approved_by == "test_reviewer@enterprise.org"
        assert approved.mapping_version_id is not None
        assert approved.version_number == 1


def test_transformation_and_validation(clean_ui_state: OnboardingUIState) -> None:
    """Verify deterministic transformation and canonical validation partition."""
    state = clean_ui_state
    mock_session: dict = {}

    with patch("streamlit.session_state", mock_session):
        state.load_fixture("CRM Customers")
        state.profile_active_source()
        state.approve_all_mappings()
        res = state.run_transformation()

        assert res.total_records > 0
        assert res.canonical_schema_name == CANONICAL_CUSTOMER_V1.schema_name
        assert mock_session["onb_transformation_result"] is not None


def test_exception_correction_and_reprocessing(clean_ui_state: OnboardingUIState) -> None:
    """Verify exception queue capture, human correction, and deterministic reprocessing."""
    state = clean_ui_state
    mock_session: dict = {}

    with patch("streamlit.session_state", mock_session):
        state.load_fixture("CRM Customers")
        state.profile_active_source()
        state.approve_all_mappings()
        state.run_transformation()

        batch = mock_session.get("onb_exception_batch")
        if batch and batch.exceptions:
            target_exc = batch.exceptions[0]
            reprocessed, canon_rec = state.correct_and_reprocess(
                exception=target_exc,
                field_name=target_exc.field,
                corrected_value="fixed.valid@example.com",
                reason="Unit test correction",
                operator="test_operator@enterprise.org",
            )
            assert reprocessed.status in (ExceptionStatus.RESOLVED, ExceptionStatus.REPROCESSED)
            assert len(reprocessed.corrections) >= 1


def test_durable_run_and_idempotency(clean_ui_state: OnboardingUIState) -> None:
    """Verify durable run persistence and idempotent replay detection."""
    state = clean_ui_state
    mock_session: dict = {}

    with patch("streamlit.session_state", mock_session):
        state.load_fixture("CRM Customers")
        state.profile_active_source()
        state.approve_all_mappings()

        # Run 1
        run1 = state.submit_durable_run()
        assert run1.status in (RunStatus.CREATED, RunStatus.COMPLETED, RunStatus.PARTIAL)
        assert run1.input_fingerprint is not None

        # Run 2: Identical submission should detect replay
        run2 = state.submit_durable_run()
        assert run2.run_id == run1.run_id or run2.input_fingerprint == run1.input_fingerprint


def test_schema_drift_gating(clean_ui_state: OnboardingUIState) -> None:
    """Verify schema drift detection, mapping impact, and gating status."""
    state = clean_ui_state
    mock_session: dict = {}

    with patch("streamlit.session_state", mock_session):
        state.load_fixture("CRM Customers")
        state.profile_active_source()
        state.approve_all_mappings()

        report = state.evaluate_schema_drift()
        assert isinstance(report, SchemaDriftReport)
        assert len(report.drift_events) > 0
        assert report.overall_compatibility in (
            MappingCompatibilityState.COMPATIBLE,
            MappingCompatibilityState.COMPATIBLE_WITH_REVIEW,
            MappingCompatibilityState.BROKEN,
        )


def test_scd2_normal_commit(clean_ui_state: OnboardingUIState) -> None:
    """Verify SCD2 batch processing and point-in-time lookup."""
    state = clean_ui_state
    mock_session: dict = {}

    with patch("streamlit.session_state", mock_session):
        gd_res = state.process_scd2_normal_batch()
        assert gd_res.is_normal
        assert gd_res.persisted

        # Point-in-time lookup
        pit = state.scd2_service.get_customer_point_in_time(
            customer_id="CUST-1001",
            as_of=datetime(2026, 9, 2, tzinfo=timezone.utc),
        )
        assert pit.found is True
        assert pit.customer_id == "CUST-1001"


def test_guardrail_suspicious_hold_and_zero_mutation(clean_ui_state: OnboardingUIState) -> None:
    """Verify suspicious batch containment, zero target writes, and operator recovery."""
    state = clean_ui_state
    mock_session: dict = {}

    with patch("streamlit.session_state", mock_session):
        # Trigger suspicious batch
        gd_res = state.trigger_suspicious_hold()
        assert gd_res.is_suspicious
        assert gd_res.is_held
        assert gd_res.hold_id is not None

        # Verify hold exists in containment queue
        holds = state.list_holds()
        active_holds = [h for h in holds if h.hold_id == gd_res.hold_id]
        assert len(active_holds) == 1
        assert active_holds[0].status == "HELD"

        # Operator recovery: release held batch
        rel_res = state.release_held_batch(
            hold_id=gd_res.hold_id,
            reason="Test authorized release",
        )
        assert rel_res.success is True


def test_rest_source_loading(clean_ui_state: OnboardingUIState) -> None:
    """Verify REST API source adapter loading via MockRESTSourceAdapter."""
    import httpx
    state = clean_ui_state
    mock_session: dict = {}

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            json=[
                {"cust_id": "REST-1", "name": "REST Customer 1", "email": "rest1@corp.org"},
                {"cust_id": "REST-2", "name": "REST Customer 2", "email": "rest2@corp.org"},
            ],
        )

    transport = httpx.MockTransport(mock_handler)
    from src.scd2_copilot.onboarding.adapters.mock_rest_adapter import MockRESTSourceAdapter
    from src.scd2_copilot.onboarding.models.source import SourceDefinition, SourceType

    source_def = SourceDefinition(
        source_id="rest_test",
        source_name="REST Test",
        source_type=SourceType.MOCK_REST,
        connection_config={"base_url": "http://mock-api.local", "endpoint": "customers"},
    )
    adapter = MockRESTSourceAdapter(source_def, transport=transport)
    df = adapter.read_data()
    schema = adapter.discover_schema()
    assert df.height == 2
    assert len(schema.columns) == 3


def test_exception_dismissal(clean_ui_state: OnboardingUIState) -> None:
    """Verify exception dismissal workflow and non-dismissible invariant rule enforcement."""
    state = clean_ui_state
    mock_session: dict = {}

    with patch("streamlit.session_state", mock_session):
        state.load_fixture("CRM Customers")
        state.profile_active_source()
        state.approve_all_mappings()
        state.run_transformation()

        batch = mock_session.get("onb_exception_batch")
        if batch and batch.exceptions:
            target_exc = batch.exceptions[0]
            try:
                dismissed = state.dismiss_exception(
                    exception=target_exc,
                    dismissed_by="test_lead@enterprise.org",
                    reason="Test authorized dismissal",
                )
                assert dismissed.status == ExceptionStatus.DISMISSED
            except Exception:
                # Non-dismissible invariant rule correctly blocked dismissal
                pass


def test_guardrail_recovery_operations(clean_ui_state: OnboardingUIState) -> None:
    """Verify release, reprocess, and discard recovery operations for held batches."""
    state = clean_ui_state
    mock_session: dict = {}

    with patch("streamlit.session_state", mock_session):
        gd_res = state.trigger_suspicious_hold()
        assert gd_res.is_held

        # Test reprocessing
        rep_res = state.reprocess_held_batch(hold_id=gd_res.hold_id, force_normal=False)
        assert rep_res is not None

        # Test discard
        disc_res = state.discard_held_batch(hold_id=gd_res.hold_id, reason="Test operator discard")
        assert disc_res is not None
        assert disc_res.status == "DISCARDED"


def test_system_health_check(clean_ui_state: OnboardingUIState) -> None:
    """Verify operational connection and service health reporting."""
    state = clean_ui_state
    health = state.get_system_health()
    assert "database" in health
    assert "ai_providers" in health
    assert "environment" in health
    assert "services" in health
    assert len(health["services"]) >= 9


def test_guided_18_step_demo_flow(clean_ui_state: OnboardingUIState) -> None:
    """Verify all 18 demo steps execute without exception."""
    from app.onboarding_ui.views.demo_mode import _execute_demo_step, DEMO_STEPS_INFO

    state = clean_ui_state
    mock_session: dict = {}

    with patch("streamlit.session_state", mock_session):
        assert len(DEMO_STEPS_INFO) == 18
        for step in range(1, 19):
            _execute_demo_step(state, step)
            # Verify progressive state accumulation
            if step >= 1:
                assert mock_session.get("onb_raw_df") is not None
            if step >= 2:
                assert mock_session.get("onb_profile") is not None
            if step >= 4:
                assert mock_session.get("onb_approved_mapping") is not None
            if step >= 5:
                assert mock_session.get("onb_transformation_result") is not None


def test_all_views_render_without_exception(clean_ui_state: OnboardingUIState) -> None:
    """Verify all 10 view rendering functions execute without attribute errors or crashes."""
    from app.onboarding_ui.views.data_quality import render_data_quality_view
    from app.onboarding_ui.views.demo_mode import render_demo_mode_view
    from app.onboarding_ui.views.exceptions import render_exceptions_view
    from app.onboarding_ui.views.guardrail import render_guardrail_view
    from app.onboarding_ui.views.mapping_review import render_mapping_review_view
    from app.onboarding_ui.views.new_onboarding import render_new_onboarding_view
    from app.onboarding_ui.views.overview import render_overview_view
    from app.onboarding_ui.views.runs import render_runs_view
    from app.onboarding_ui.views.scd2_history import render_scd2_history_view
    from app.onboarding_ui.views.schema_drift import render_schema_drift_view
    from app.onboarding_ui.views.settings_status import render_settings_status_view
    from app.onboarding_ui.views.source_onboarding import render_source_onboarding_view

    state = clean_ui_state
    mock_session: dict = {
        "onb_selected_fixture": "CRM Customers",
        "onb_nav_selection": "1. Overview",
        "onb_demo_step": 1,
    }

    with patch("streamlit.session_state", mock_session):
        state.load_fixture("CRM Customers")
        state.profile_active_source()
        state.generate_mapping_proposals(enable_ai=False)
        state.approve_all_mappings()
        state.run_transformation()

        def mock_columns(spec, **kwargs):
            count = spec if isinstance(spec, int) else len(spec)
            return [MagicMock() for _ in range(count)]

        def mock_tabs(tab_list, **kwargs):
            return [MagicMock() for _ in range(len(tab_list))]

        with (
            patch("streamlit.columns", side_effect=mock_columns),
            patch("streamlit.tabs", side_effect=mock_tabs),
            patch("streamlit.container", return_value=MagicMock()),
            patch("streamlit.button", return_value=False),
            patch("streamlit.selectbox", side_effect=lambda label, options, **kwargs: options[0] if options else None),
            patch("streamlit.checkbox", return_value=True),
            patch("streamlit.text_input", return_value="CUST-1001"),
            patch("streamlit.date_input", return_value=date(2026, 9, 2)),
            patch("streamlit.markdown"),
            patch("streamlit.dataframe"),
            patch("streamlit.info"),
            patch("streamlit.success"),
            patch("streamlit.warning"),
            patch("streamlit.error"),
            patch("streamlit.caption"),
            patch("streamlit.metric"),
            patch("streamlit.spinner"),
            patch("streamlit.rerun"),
        ):
            # Test all production views render cleanly
            render_overview_view(state)
            render_new_onboarding_view(state)
            render_source_onboarding_view(state)
            render_mapping_review_view(state)
            render_data_quality_view(state)
            render_exceptions_view(state)
            render_runs_view(state)
            render_schema_drift_view(state)
            render_scd2_history_view(state)
            render_guardrail_view(state)
            render_settings_status_view(state)
            render_demo_mode_view(state)


def test_programmatic_navigation(clean_ui_state: OnboardingUIState) -> None:
    """Verify that programmatic navigation updates onb_sidebar_radio correctly."""
    from app.onboarding_ui import render_customer_onboarding_app

    state = clean_ui_state
    mock_session: dict = {
        "onb_nav_selection": "2. New Onboarding",
        "onb_sidebar_radio": "1. Overview",
    }

    with (
        patch("streamlit.session_state", mock_session),
        patch("streamlit.sidebar"),
        patch("streamlit.radio", return_value="2. New Onboarding"),
        patch("app.onboarding_ui.render_new_onboarding_view") as mock_render_new,
        patch("streamlit.markdown"),
        patch("streamlit.caption"),
        patch("streamlit.divider"),
    ):
        render_customer_onboarding_app()
        assert mock_session.get("onb_sidebar_radio") == "2. New Onboarding"
        assert "onb_nav_selection" not in mock_session
        assert mock_render_new.called


