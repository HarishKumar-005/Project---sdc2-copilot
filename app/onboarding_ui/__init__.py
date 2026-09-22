"""Customer Data Onboarding & Integration Guardrail Streamlit UI Package."""

from __future__ import annotations

from pathlib import Path
import streamlit as st

from .state import OnboardingUIState
from .views import (
    render_data_quality_view,
    render_demo_mode_view,
    render_exceptions_view,
    render_guardrail_view,
    render_mapping_review_view,
    render_new_onboarding_view,
    render_overview_view,
    render_runs_view,
    render_scd2_history_view,
    render_schema_drift_view,
    render_settings_status_view,
    render_source_onboarding_view,
)

THEME_CSS_PATH = Path(__file__).parent.parent / "onboarding_theme.css"


def inject_onboarding_theme() -> None:
    """Inject the clean, high-contrast light theme CSS into Streamlit."""
    if THEME_CSS_PATH.exists():
        css_content = THEME_CSS_PATH.read_text(encoding="utf-8")
        st.markdown(f"<style>{css_content}</style>", unsafe_allow_html=True)


def render_customer_onboarding_app() -> None:
    """Main rendering entry point for the Customer Data Onboarding & Integration Guardrail UI."""
    inject_onboarding_theme()
    state = OnboardingUIState.get_instance()

    # Sidebar Navigation
    with st.sidebar:
        st.markdown("### Onboarding Navigation")
        nav_options = [
            "1. Overview",
            "2. New Onboarding",
            "3. Mapping Review",
            "4. Data Quality",
            "5. Exceptions Queue",
            "6. Runs & Idempotency",
            "7. Schema Drift",
            "8. Customer History",
            "9. Guardrail & Containment",
            "10. Settings & Connection Status",
        ]

        # Sync with programmatic navigation from view buttons
        if "onb_nav_selection" in st.session_state:
            target_nav = st.session_state.pop("onb_nav_selection")
            for opt in nav_options:
                if target_nav == opt or (target_nav in opt) or (opt in target_nav):
                    st.session_state["onb_sidebar_radio"] = opt
                    break

        if "onb_sidebar_radio" not in st.session_state or st.session_state["onb_sidebar_radio"] not in nav_options:
            st.session_state["onb_sidebar_radio"] = nav_options[0]

        selected_nav = st.radio(
            "Select Section",
            options=nav_options,
            key="onb_sidebar_radio",
            label_visibility="collapsed",
        )

        st.divider()
        st.caption("Customer Data Onboarding & Guardrail v1.0")

    # Route to Selected View
    if "1. Overview" in selected_nav:
        render_overview_view(state)
    elif "2. New Onboarding" in selected_nav or "2. Source" in selected_nav:
        render_new_onboarding_view(state)
    elif "3. Mapping Review" in selected_nav:
        render_mapping_review_view(state)
    elif "4. Data Quality" in selected_nav:
        render_data_quality_view(state)
    elif "5. Exceptions" in selected_nav:
        render_exceptions_view(state)
    elif "6. Runs" in selected_nav:
        render_runs_view(state)
    elif "7. Schema Drift" in selected_nav:
        render_schema_drift_view(state)
    elif "8. Customer History" in selected_nav or "8. SCD2" in selected_nav:
        render_scd2_history_view(state)
    elif "9. Guardrail" in selected_nav:
        render_guardrail_view(state)
    elif "10. Settings" in selected_nav or "10. Status" in selected_nav:
        render_settings_status_view(state)
    elif "10. Demo" in selected_nav:
        render_demo_mode_view(state)
    else:
        render_overview_view(state)


__all__ = ["render_customer_onboarding_app", "OnboardingUIState"]
