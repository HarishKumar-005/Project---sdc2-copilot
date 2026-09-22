"""Canonical Transformation and Data Quality Invariant Validation view."""

from __future__ import annotations

import streamlit as st

from ..components import (
    render_lineage_panel,
    render_metric_card,
    render_onboarding_header,
    render_status_badge,
)
from ..state import OnboardingUIState


def render_data_quality_view(state: OnboardingUIState) -> None:
    """Render the deterministic transformation and canonical validation screen."""
    render_onboarding_header(
        title="Canonical Transformation & Invariant Validation",
        subtitle="Execute deterministic Polars transformations and evaluate incoming records against the 4 canonical customer contract rules.",
    )

    # ── Action Bar ──────────────────────────────────────────────
    c_col1, c_col2 = st.columns([1.5, 3.5])
    with c_col1:
        run_btn = st.button("▶ Execute Transformation Pipeline", type="primary", use_container_width=True)
    with c_col2:
        st.caption("Runs native Polars columnar operations using the approved mapping, followed by strict deterministic invariant checks.")

    if run_btn or st.session_state.get("onb_transformation_result") is None:
        with st.spinner("Executing deterministic Polars transformation and canonical validation..."):
            res = state.run_transformation()
    else:
        res = st.session_state["onb_transformation_result"]

    # ── KPI Metrics Strip ───────────────────────────────────────
    st.markdown("### 📊 Validation Summary")
    k_col1, k_col2, k_col3, k_col4 = st.columns(4)

    render_metric_card(k_col1, "Total Records", f"{res.total_records:,}", "Evaluated", "info")

    val_pct = (res.valid_record_count / res.total_records * 100) if res.total_records else 0
    render_metric_card(k_col2, "Valid Canonical Records", f"{res.valid_record_count:,}", f"{val_pct:.1f}% pass rate", "success")

    inval_pct = (res.invalid_record_count / res.total_records * 100) if res.total_records else 0
    inval_status = "error" if res.invalid_record_count > 0 else "success"
    render_metric_card(k_col3, "Invalid Records", f"{res.invalid_record_count:,}", f"{inval_pct:.1f}% quarantined", inval_status)

    render_metric_card(k_col4, "Canonical Contract", res.canonical_schema_name, "customer.v1", "neutral")

    st.markdown("<div style='height: 14px;'></div>", unsafe_allow_html=True)

    # ── Valid vs Invalid Tabs ───────────────────────────────────
    tab_invalid, tab_valid = st.tabs(["⚠️ Quarantined Invalid Records", "✅ Valid Canonical Records"])

    with tab_invalid:
        if res.invalid_records:
            st.markdown(
                """<div style="background: #fef2f2; border: 1px solid #fecaca; border-radius: 8px; padding: 12px 16px; margin-bottom: 14px; font-size: 0.88rem; color: #991b1b;">
<strong>Data Quality Violations Detected:</strong> The following records breached canonical invariants and were diverted away from downstream historical storage. They have been enqueued into the <strong>Exception Queue</strong> for operator correction.
</div>""",
                unsafe_allow_html=True,
            )

            err_rows = []
            for inv in res.invalid_records:
                rec_id = inv.source_record_id or f"row_{inv.row_index}"
                for err in inv.errors:
                    err_rows.append({
                        "Record ID": rec_id,
                        "Field": err.field,
                        "Violated Invariant": err.rule_id,
                        "Severity": err.severity.value,
                        "Failure Reason": err.reason,
                    })

            st.dataframe(err_rows, use_container_width=True)
        else:
            st.success("🎉 Zero validation violations detected! All records conform 100% to canonical contract invariants.")

    with tab_valid:
        if res.valid_records:
            st.markdown(
                f"""<div style="background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 8px; padding: 12px 16px; margin-bottom: 14px; font-size: 0.88rem; color: #166534;">
<strong>{len(res.valid_records)} Canonical Records Verified:</strong> These records satisfy all contract invariants and are safe for historical SCD2 versioning.
</div>""",
                unsafe_allow_html=True,
            )

            clean_rows = [r.model_dump() for r in res.valid_records]
            st.dataframe(clean_rows, use_container_width=True)
        else:
            st.warning("No valid records in this batch.")

    # ── Next Step Navigation ────────────────────────────────────
    st.markdown("---")
    n_col1, n_col2, n_col3 = st.columns([2.5, 1.2, 1.2])
    with n_col1:
        st.markdown("**Next Actions:** Resolve invalid records in the Exception Queue or inspect Onboarding Run history.")
    with n_col2:
        if st.button("Open Exception Queue ➔", type="secondary", use_container_width=True):
            st.session_state["onb_nav_selection"] = "5. Exceptions"
            st.rerun()
    with n_col3:
        if st.button("Proceed to Runs ➔", type="primary", use_container_width=True):
            st.session_state["onb_nav_selection"] = "6. Runs & Idempotency"
            st.rerun()
