"""Overview / Central Operational Dashboard view for Customer Data Onboarding & Integration Guardrail."""

from __future__ import annotations

import streamlit as st

from ..components import (
    render_metric_card,
    render_onboarding_header,
    render_pipeline_stepper,
    render_status_badge,
)
from ..state import AVAILABLE_FIXTURES, OnboardingUIState


def render_overview_view(state: OnboardingUIState) -> None:
    """Render the central executive summary and pipeline visualizer."""
    render_onboarding_header(
        title="Customer Data Onboarding & Change Guardrail",
        subtitle="Safely onboard heterogeneous customer data, validate it, preserve history, and control risky historical changes.",
    )

    # ── Interactive Pipeline Stepper ────────────────────────────
    current_stage = "SOURCE"
    if st.session_state.get("onb_guardrail_result"):
        current_stage = "GUARDRAIL"
    elif st.session_state.get("onb_scd2_result"):
        current_stage = "SCD2"
    elif st.session_state.get("onb_drift_report"):
        current_stage = "DRIFT"
    elif st.session_state.get("onb_exception_batch"):
        current_stage = "EXCEPTIONS"
    elif st.session_state.get("onb_transformation_result"):
        current_stage = "TRANSFORM"
    elif st.session_state.get("onb_approved_mapping"):
        current_stage = "APPROVE"
    elif st.session_state.get("onb_proposals"):
        current_stage = "MAP"
    elif st.session_state.get("onb_profile"):
        current_stage = "PROFILE"

    render_pipeline_stepper(current_stage)

    # ── Primary Operational Action ──────────────────────────────
    cta_col1, cta_col2 = st.columns([3, 1])
    with cta_col1:
        st.markdown(
            "<div style='font-size: 0.95rem; color: var(--onb-text-2); padding-top: 6px;'>"
            "Ready to onboard new customer records? Upload a CSV or connect a REST endpoint to begin schema discovery."
            "</div>",
            unsafe_allow_html=True,
        )
    with cta_col2:
        if st.button("➕ Start New Onboarding", type="primary", use_container_width=True):
            st.session_state["onb_nav_selection"] = "2. New Onboarding"
            st.rerun()

    st.markdown("<div style='height: 8px;'></div>", unsafe_allow_html=True)

    # ── Operational Summary KPI Cards (Derived from Real State) ─
    st.markdown("### 📊 Operational Governance Overview")
    col1, col2, col3, col4 = st.columns(4)

    raw_df = st.session_state.get("onb_raw_df")
    trans_res = st.session_state.get("onb_transformation_result")
    exc_batch = st.session_state.get("onb_exception_batch")
    runs_list = state.get_all_runs()
    drift_rep = st.session_state.get("onb_drift_report")
    holds = state.list_holds()
    active_holds = sum(1 for h in holds if h.status == "HELD")

    # Metric 1: Total Ingested
    total_seen = trans_res.total_records if trans_res else (raw_df.height if raw_df is not None else 0)
    render_metric_card(
        col1,
        title="Records Ingested",
        value=f"{total_seen:,}" if total_seen else "—",
        subtitle=st.session_state.get("onb_selected_fixture", "No source active"),
        status_type="info" if total_seen else "neutral",
    )

    # Metric 2: Valid vs Invalid
    if trans_res and trans_res.total_records > 0:
        pass_rate = (trans_res.valid_record_count / trans_res.total_records) * 100
        val_status = "success" if pass_rate == 100 else "warning"
        render_metric_card(
            col2,
            title="Canonical Quality",
            value=f"{pass_rate:.1f}%",
            subtitle=f"{trans_res.valid_record_count} valid / {trans_res.invalid_record_count} invalid",
            status_type=val_status,
        )
    else:
        render_metric_card(
            col2,
            title="Canonical Quality",
            value="—",
            subtitle="Validation pending",
            status_type="neutral",
        )

    # Metric 3: Active Exceptions
    if exc_batch:
        open_count = sum(1 for e in exc_batch.exceptions if not e.is_resolved)
        resolved_count = sum(1 for e in exc_batch.exceptions if e.is_resolved)
        exc_status = "error" if open_count > 0 else "success"
        render_metric_card(
            col3,
            title="Exception Queue",
            value=f"{open_count} Open",
            subtitle=f"{resolved_count} resolved",
            status_type=exc_status,
        )
    else:
        render_metric_card(
            col3,
            title="Exception Queue",
            value="0 Open",
            subtitle="Clean pipeline",
            status_type="success",
        )

    # Metric 4: Guardrail Containment Holds
    hold_status = "held" if active_holds > 0 else "success"
    render_metric_card(
        col4,
        title="Quarantined Batches",
        value=f"{active_holds} Active",
        subtitle=f"{len(holds)} total holds evaluated",
        status_type=hold_status,
    )

    st.markdown("<div style='height: 14px;'></div>", unsafe_allow_html=True)

    # ── Quick Navigation Strip ──────────────────────────────────
    st.markdown("### ⚡ Quick Navigation & Actions")
    q_col1, q_col2, q_col3 = st.columns(3)

    with q_col1:
        st.markdown(
            """<div class="onb-card">
<div style="font-weight: 700; font-size: 1.05rem; color: var(--onb-text-1);">📥 New Onboarding</div>
<div style="font-size: 0.85rem; color: var(--onb-text-2); margin-top: 4px; margin-bottom: 14px;">
Upload customer CSV exports or configure live REST endpoints with automated profiling and PII masking.
</div>
</div>""",
            unsafe_allow_html=True,
        )
        if st.button("Start New Ingestion", type="primary", use_container_width=True):
            st.session_state["onb_nav_selection"] = "2. New Onboarding"
            st.rerun()

    with q_col2:
        st.markdown(
            """<div class="onb-card">
<div style="font-weight: 700; font-size: 1.05rem; color: var(--onb-text-1);">🗺️ Mapping Review</div>
<div style="font-size: 0.85rem; color: var(--onb-text-2); margin-top: 4px; margin-bottom: 14px;">
Inspect AI candidate mappings, resolve ambiguous fields, override transformations, and seal immutable versions.
</div>
</div>""",
            unsafe_allow_html=True,
        )
        if st.button("Review Mappings", type="secondary", use_container_width=True):
            st.session_state["onb_nav_selection"] = "3. Mapping Review"
            st.rerun()

    with q_col3:
        st.markdown(
            """<div class="onb-card">
<div style="font-weight: 700; font-size: 1.05rem; color: var(--onb-text-1);">🛡️ Guardrail Console</div>
<div style="font-size: 0.85rem; color: var(--onb-text-2); margin-top: 4px; margin-bottom: 14px;">
Inspect pre-commit suspicious holds, verified historical immutability, and perform operator recovery.
</div>
</div>""",
            unsafe_allow_html=True,
        )
        if st.button("Inspect Guardrails", type="secondary", use_container_width=True):
            st.session_state["onb_nav_selection"] = "9. Guardrail & Containment"
            st.rerun()

    st.markdown("<div style='height: 10px;'></div>", unsafe_allow_html=True)

    # ── Architectural Principles Card ───────────────────────────
    st.markdown("### 🏛️ Core Architectural Invariants")
    p_col1, p_col2, p_col3 = st.columns(3)

    with p_col1:
        st.markdown(
            """<div class="onb-card">
<div style="font-size: 1.2rem; margin-bottom: 6px;">⚙️</div>
<div style="font-weight: 700; font-size: 0.95rem; color: var(--onb-text-1);">Deterministic Engine</div>
<div style="font-size: 0.84rem; color: var(--onb-text-2); margin-top: 4px;">
Native Polars transformations with zero <code>eval/exec</code>. All business key matching, canonical validation, and SCD2 half-open intervals are mathematical and deterministic.
</div>
</div>""",
            unsafe_allow_html=True,
        )

    with p_col2:
        st.markdown(
            """<div class="onb-card">
<div style="font-size: 1.2rem; margin-bottom: 6px;">🛡️</div>
<div style="font-weight: 700; font-size: 0.95rem; color: var(--onb-text-1);">Pre-Commit Guardrail</div>
<div style="font-size: 0.84rem; color: var(--onb-text-2); margin-top: 4px;">
Evaluates change significance <em>before</em> database mutation. Suspicious batches are quarantined with <strong>zero target history mutation</strong>.
</div>
</div>""",
            unsafe_allow_html=True,
        )

    with p_col3:
        st.markdown(
            """<div class="onb-card">
<div style="font-size: 1.2rem; margin-bottom: 6px;">🤖</div>
<div style="font-weight: 700; font-size: 0.95rem; color: var(--onb-text-1);">Advisory GenAI Boundary</div>
<div style="font-size: 0.84rem; color: var(--onb-text-2); margin-top: 4px;">
The LLM acts strictly as a copilot: proposing semantic mappings and drafting root-cause explanations. It has zero authority to silently modify data or system state.
</div>
</div>""",
            unsafe_allow_html=True,
        )
