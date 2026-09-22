"""Schema Drift Detection and Mapping Impact Gating view."""

from __future__ import annotations

import streamlit as st

from ..components import (
    render_lineage_panel,
    render_metric_card,
    render_onboarding_header,
    render_status_badge,
)
from ..state import OnboardingUIState


def render_schema_drift_view(state: OnboardingUIState) -> None:
    """Render the schema drift comparator, structural diff breakdown, and compatibility gating screen."""
    render_onboarding_header(
        title="Schema Drift Detection & Mapping Impact Gating",
        subtitle="Detect structural source schema evolution, analyze mapping impact, and enforce COMPATIBLE vs BROKEN compatibility gates.",
    )

    # ── Action Controls ─────────────────────────────────────────
    c_col1, c_col2 = st.columns([1.5, 3.5])
    with c_col1:
        drift_btn = st.button("▶ Evaluate Source Schema Drift", type="primary", use_container_width=True)
    with c_col2:
        st.caption("Performs deterministic structural diffing between baseline and evolved source schemas, evaluating mapping impact against canonical contract rules.")

    if drift_btn or st.session_state.get("onb_drift_report") is None:
        with st.spinner("Computing structural schema diff and mapping impact..."):
            drift_report = state.evaluate_schema_drift()
    else:
        drift_report = st.session_state["onb_drift_report"]

    # ── Compatibility Gating Banner ─────────────────────────────
    compat_val = drift_report.overall_compatibility.value
    if compat_val == "COMPATIBLE":
        banner_bg, banner_border, text_color = "#f0fdf4", "#86efac", "#166534"
        gate_text = "PASSED — Additive changes are backward-compatible. Downstream SCD2 processing is allowed."
    elif compat_val in ("REVIEW_REQUIRED", "COMPATIBLE_WITH_REVIEW"):
        banner_bg, banner_border, text_color = "#fffbeb", "#fde68a", "#92400e"
        gate_text = "REVIEW REQUIRED — Non-critical schema changes detected. Downstream processing allowed with warnings."
    else:  # BROKEN
        banner_bg, banner_border, text_color = "#fef2f2", "#fecaca", "#991b1b"
        gate_text = "HALTED — Breaking schema drift detected. Downstream SCD2 ingestion is STRICTLY BLOCKED until a new mapping is approved."

    st.markdown(
        f"""<div style="background: {banner_bg}; border: 1px solid {banner_border}; border-radius: 10px; padding: 14px 18px; margin-bottom: 18px; display: flex; justify-content: space-between; align-items: center;">
<div>
<span style="font-weight: 800; font-size: 1.05rem; color: {text_color};">Gating Status: {compat_val}</span>
<div style="font-size: 0.85rem; color: {text_color}; margin-top: 4px;">
{gate_text}
</div>
</div>
<div>
{render_status_badge(compat_val, 'success' if compat_val == 'COMPATIBLE' else ('warning' if compat_val in ('REVIEW_REQUIRED', 'COMPATIBLE_WITH_REVIEW') else 'error'))}
</div>
</div>""",
        unsafe_allow_html=True,
    )

    # ── KPI Metrics Strip ───────────────────────────────────────
    st.markdown("### 📊 Drift & Impact Metrics")
    m_col1, m_col2, m_col3, m_col4 = st.columns(4)

    total_events = len(drift_report.drift_events)
    breaking_events = sum(1 for e in drift_report.impacted_mappings if e.compatibility.value == "BROKEN")

    render_metric_card(m_col1, "Total Drift Events", total_events, "Structural changes", "info")
    render_metric_card(m_col2, "Impacted Mappings", len(drift_report.impacted_mappings), "Evaluated rules", "neutral")
    render_metric_card(m_col3, "Breaking Changes", breaking_events, "Requires re-approval", "error" if breaking_events else "success")
    render_metric_card(m_col4, "Schema Gate", compat_val, "Pipeline boundary", "success" if compat_val == "COMPATIBLE" else "error")

    st.markdown("<div style='height: 12px;'></div>", unsafe_allow_html=True)

    # ── Structural Diff Table ───────────────────────────────────
    st.markdown("### 🔍 Structural Schema Diff Events")
    diff_table = []
    for ev in drift_report.drift_events:
        diff_table.append({
            "Change Type": ev.drift_type.value,
            "Column Name": ev.field_name,
            "Prior Type": ev.old_type or "—",
            "Current Type": ev.new_type or "—",
            "Diff Details": ev.details or "—",
        })

    st.dataframe(diff_table, use_container_width=True)

    # ── Mapping Impact Table ────────────────────────────────────
    st.markdown("### 🗺️ Mapping Impact Assessment")
    impact_table = []
    for imp in drift_report.impacted_mappings:
        impact_table.append({
            "Source Field": imp.source_field,
            "Canonical Target": imp.target_field or "—",
            "Compatibility": imp.compatibility.value,
            "Impact Reason": imp.reason,
            "Action Required": "Update Mapping" if imp.compatibility.value != "COMPATIBLE" else "None",
        })

    st.dataframe(impact_table, use_container_width=True)

    # ── Immutability Governance Note ────────────────────────────
    st.markdown(
        """<div style="font-size: 0.88rem; color: #1e293b; background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 8px; padding: 14px 18px; margin-top: 14px;">
<strong>🔒 Immutability Guarantee:</strong> Approved mappings are <strong>not changed automatically</strong>.
Schema drift detection is strictly analytical and non-destructive. If compatibility is <code>BROKEN</code> or requires review, an operator must evaluate candidate aliases and sign off on a new mapping version in Section 3.
</div>""",
        unsafe_allow_html=True,
    )

    if compat_val != "COMPATIBLE":
        st.markdown("<div style='height: 8px;'></div>", unsafe_allow_html=True)
        if st.button("➔ Review & Update Mapping in Mapping Review", type="secondary"):
            st.session_state["onb_nav_selection"] = "3. Mapping Review"
            st.rerun()

    # ── Next Step ───────────────────────────────────────────────
    st.markdown("---")
    n_col1, n_col2 = st.columns([3, 1])
    with n_col1:
        st.markdown("**Next Step:** Inspect customer historical records and point-in-time entity version lineage.")
    with n_col2:
        if st.button("Proceed to Customer History ➔", type="primary", use_container_width=True):
            st.session_state["onb_nav_selection"] = "8. Customer History"
            st.rerun()
