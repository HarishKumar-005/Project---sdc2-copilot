"""SCD2 Historical Versioning and Temporal Entity State view."""

from __future__ import annotations

from datetime import date, datetime, timezone
import streamlit as st

from ..components import (
    render_lineage_panel,
    render_metric_card,
    render_onboarding_header,
    render_status_badge,
)
from ..state import OnboardingUIState


def render_scd2_history_view(state: OnboardingUIState) -> None:
    """Render the SCD2 historical version timeline and point-in-time lookup screen."""
    render_onboarding_header(
        title="SCD2 Historical Versioning & Temporal State",
        subtitle="Explore chronological customer version lineage under strict [effective_from, effective_to) half-open validity semantics.",
    )

    # ── Action Bar: Ingest Batch ────────────────────────────────
    c_col1, c_col2 = st.columns([1.5, 3.5])
    with c_col1:
        ingest_btn = st.button("▶ Ingest Valid Records to SCD2", type="primary", use_container_width=True)
    with c_col2:
        st.caption("Applies deterministic SCD2 transformation: closes prior versions, creates new active versions, and enforces the 5 invariant rules.")

    if ingest_btn:
        with st.spinner("Processing batch into deterministic SCD2 history..."):
            gd_res = state.process_scd2_normal_batch()
            st.success(f"Batch processed successfully! Status: {gd_res.decision.value} (Persisted: {gd_res.persisted})")

    # ── Customer Historical Search ──────────────────────────────
    st.markdown("### 🔍 Customer Version Lineage Explorer")
    s_col1, s_col2 = st.columns([1.5, 2.5])

    with s_col1:
        customer_id_input = st.text_input("Enter Customer ID", value="CUST-1001", help="Search history by unique business key.")

    history_rows = state.scd2_service.get_customer_history(customer_id=customer_id_input.strip())

    if not history_rows:
        st.info(f"No SCD2 history found for customer `{customer_id_input}` yet. Click 'Ingest Valid Records to SCD2' above to create baseline history.")
    else:
        st.markdown(f"#### Chronological Versions for `{customer_id_input}` ({len(history_rows)} versions found)")

        timeline_data = []
        for r in history_rows:
            eff_to_str = r.effective_to.strftime("%Y-%m-%d %H:%M:%S UTC") if r.effective_to else "NULL (Open-ended)"
            timeline_data.append({
                "History ID": str(r.history_id)[:8] + "...",
                "Customer ID": r.entity_key.get("customer_id", customer_id_input),
                "Effective From": r.effective_from.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "Effective To": eff_to_str,
                "Status": r.attributes.get("status", "ACTIVE"),
                "Attributes Summary": f"{r.attributes.get('first_name', '')} {r.attributes.get('last_name', '')} • {r.attributes.get('email', '')}",
                "Current Flag": "✓ Current" if r.is_current else "Historical (Closed)",
            })

        st.dataframe(timeline_data, use_container_width=True)

    st.markdown("<div style='height: 14px;'></div>", unsafe_allow_html=True)

    # ── Point-in-Time Temporal Lookup ───────────────────────────
    st.markdown("### ⏱️ Point-in-Time Query Simulator")
    st.caption("Point-in-time queries evaluate `effective_from <= T AND (effective_to > T OR effective_to IS NULL)`. Boundary dates belong strictly to the new version.")

    with st.container(border=True):
        pit_col1, pit_col2, pit_col3 = st.columns([1.5, 1.5, 1.5])
        with pit_col1:
            pit_date = st.date_input("Lookup Date", value=date(2026, 9, 2))
        with pit_col2:
            st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
            pit_btn = st.button("Inspect As-Of Timestamp", type="secondary", use_container_width=True)

        if pit_btn:
            lookup_dt = datetime.combine(pit_date, datetime.min.time(), tzinfo=timezone.utc)
            pit_state = state.scd2_service.get_customer_point_in_time(
                customer_id=customer_id_input.strip(),
                as_of=lookup_dt,
            )

            if pit_state.found:
                eff_to_fmt = pit_state.effective_to.strftime('%Y-%m-%d %H:%M:%S UTC') if pit_state.effective_to else 'NULL (Current)'
                st.markdown(
                    f"""<div style="background: #f0fdf4; border: 1px solid #86efac; border-radius: 8px; padding: 12px 16px; margin-top: 10px;">
<strong>Historical Version Active at {lookup_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}:</strong><br>
• Customer: <code>{pit_state.customer_id}</code><br>
• Validity Interval: <code>[{pit_state.effective_from.strftime('%Y-%m-%d %H:%M:%S UTC')}, {eff_to_fmt})</code><br>
• Status: <strong>{pit_state.attributes.get('status', 'ACTIVE')}</strong><br>
• Attributes: <code>{pit_state.attributes}</code>
</div>""",
                    unsafe_allow_html=True,
                )
            else:
                st.warning(f"No active historical version existed for `{customer_id_input}` as of {lookup_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}.")

    # ── Next Step ───────────────────────────────────────────────
    st.markdown("---")
    n_col1, n_col2 = st.columns([3, 1])
    with n_col1:
        st.markdown("**Next Step:** Inspect pre-commit Guardrail evaluation, suspicious hold containment, and operator recovery.")
    with n_col2:
        if st.button("Proceed to Guardrail ➔", type="primary", use_container_width=True):
            st.session_state["onb_nav_selection"] = "9. Guardrail & Containment"
            st.rerun()
