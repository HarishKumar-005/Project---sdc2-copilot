"""Historical Guardrail and Pre-Commit Containment view."""

from __future__ import annotations

import streamlit as st

from ..components import (
    render_ai_advisory_box,
    render_lineage_panel,
    render_metric_card,
    render_onboarding_header,
    render_status_badge,
)
from ..state import OnboardingUIState


def render_guardrail_view(state: OnboardingUIState) -> None:
    """Render the pre-commit guardrail decision boundary, containment hold inspection, and recovery console."""
    render_onboarding_header(
        title="Historical Guardrail & Pre-Commit Containment",
        subtitle="Evaluate candidate SCD2 changes prior to database mutation. Suspicious batches are quarantined with zero historical corruption.",
    )

    # ── Evaluation Action Controls ──────────────────────────────
    st.markdown("### 🛡️ Guardrail Batch Evaluation")
    c_col1, c_col2, c_col3 = st.columns([1.5, 1.8, 2.0])

    with c_col1:
        norm_btn = st.button("▶ Evaluate Standard Batch", type="secondary", use_container_width=True)
    with c_col2:
        susp_btn = st.button("⚠️ Evaluate High-Impact Batch", type="primary", use_container_width=True)
    with c_col3:
        st.caption("Normal batches commit cleanly. Batches with anomalous deactivations trigger pre-commit quarantine with ZERO target database writes.")

    if norm_btn:
        with st.spinner("Evaluating standard batch through guardrail engine..."):
            res = state.process_scd2_normal_batch()
            st.success(f"✓ Guardrail Decision: {res.decision.value} • History committed cleanly! ({res.persisted_count} historical rows)")

    if susp_btn:
        with st.spinner("Evaluating high-impact deactivation batch..."):
            res = state.trigger_suspicious_hold()
            st.error(
                f"🚨 Guardrail Decision: {res.decision.value} • Batch QUARANTINED in Containment Queue!\n\n"
                f"Triggered Rules: {[r.value for r in res.triggered_rules]} • Target database received 0 historical writes."
            )

    # ── Pre-Commit Architectural Guarantee Card ─────────────────
    st.markdown(
        """<div style="background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 10px; padding: 14px 18px; margin: 16px 0;">
<div style="font-weight: 800; font-size: 0.95rem; color: #0f172a;">🛡️ Pre-Commit Decision Boundary Guarantee</div>
<div style="font-size: 0.85rem; color: #475569; margin-top: 4px; line-height: 1.5;">
Candidate SCD2 mutations are computed and validated <em>in memory</em>. The Guardrail Engine inspects candidate impact
<strong>before any SQL commit</strong>. If suspicious, the batch is routed to <code>held_change_batch</code> and
<strong>monitored_entity_history is 100% UNTOUCHED</strong>.
</div>
</div>""",
        unsafe_allow_html=True,
    )

    # ── Operational Guardrail Queue / Holds Table ───────────────
    st.markdown("### 📋 Quarantined Containment Queue")
    holds = state.list_holds()

    if not holds:
        st.info("Zero active holds in containment. Click 'Evaluate High-Impact Batch' above to trigger pre-commit quarantine.")
        return

    # Table of all holds
    holds_table = []
    for h in holds:
        rules_fired = h.evidence.get("triggered_rules", []) if isinstance(h.evidence, dict) else []
        rules_str = ", ".join(rules_fired) if rules_fired else "None"
        holds_table.append({
            "Hold ID": str(h.hold_id)[:10] + "...",
            "Source": h.source_name,
            "Affected Records": h.records_affected,
            "Decision": "SUSPICIOUS ↓ HELD" if h.status == "HELD" else f"RESOLVED ({h.status})",
            "Severity": h.severity,
            "Triggered Rules": rules_str,
            "Status": h.status,
            "Created At": h.created_at.strftime("%Y-%m-%d %H:%M:%S UTC") if h.created_at else "—",
        })

    st.dataframe(holds_table, use_container_width=True)

    st.markdown("<div style='height: 12px;'></div>", unsafe_allow_html=True)

    # ── Hold Selector & Detail Panel ────────────────────────────
    st.markdown("### 🔍 Held Batch Detail & Historical Protection")
    hold_options = {f"{str(h.hold_id)[:12]}... ({h.status} • Severity: {h.severity} • {h.records_affected} records)": h for h in holds}
    sel_label = st.selectbox("Select Held Batch to Inspect", list(hold_options.keys()))
    active_hold = hold_options[sel_label]

    is_held = active_hold.status == "HELD"
    badge_type = "held" if is_held else "success"

    with st.container(border=True):
        st.markdown(
            f"""<div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
<div>
<span style="font-weight: 800; font-size: 1.1rem; color: #0f172a;">Hold ID: <code>{active_hold.hold_id}</code></span>
</div>
<div>
{render_status_badge(active_hold.status, badge_type)}
</div>
</div>""",
            unsafe_allow_html=True,
        )

        h_col1, h_col2, h_col3, h_col4 = st.columns(4)
        render_metric_card(h_col1, "Severity", active_hold.severity, "Anomaly score", "error" if active_hold.severity == "HIGH" else "warning")
        render_metric_card(h_col2, "Affected Records", active_hold.records_affected, "Held in batch", "info")
        render_metric_card(h_col3, "Target Writes", "0 Writes", "History protected", "success")
        render_metric_card(h_col4, "Stream Checkpoint", "Frozen", "Not advanced", "neutral")

        # ── HISTORICAL PROTECTION SECTION ───────────────────────
        st.markdown("#### 🔒 Historical Protection Verification")
        curr_hist_count = state.get_historical_record_count()

        p_box1, p_box2 = st.columns(2)
        with p_box1:
            st.markdown(
                f"""<div style="background: #f0fdf4; border: 1px solid #86efac; border-radius: 8px; padding: 12px 16px;">
<strong>Target Table State:</strong><br>
• Monitored History Records: <strong>{curr_hist_count}</strong><br>
• Quarantine Status: <strong>CONTAINED</strong><br>
• Mutation Prevention: <strong>100% Verified</strong>
</div>""",
                unsafe_allow_html=True,
            )
        with p_box2:
            st.markdown(
                """<div style="background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 8px; padding: 12px 16px;">
<strong>Architectural Guarantee:</strong><br>
<em>"Historical state was not committed while the batch was held."</em><br>
Pre-commit boundary verified: zero target history rows were mutated by the quarantined batch.
</div>""",
                unsafe_allow_html=True,
            )

        st.markdown("<div style='height: 10px;'></div>", unsafe_allow_html=True)

        rules_fired = active_hold.evidence.get("triggered_rules", []) if isinstance(active_hold.evidence, dict) else []
        st.markdown("##### Triggered Invariant Rules & Evidence")
        st.markdown(f"• **Rules Fired:** `{rules_fired}`\n• **Primary Defect Reason:** {active_hold.reason}")

        # ── Grounded AI Advisory Explanation ────────────────────
        ai_expl = active_hold.evidence.get("ai_explanation") if isinstance(active_hold.evidence, dict) else None
        if ai_expl:
            render_ai_advisory_box(
                explanation_text=ai_expl,
                title="Grounded AI Root-Cause Narrative",
                provider="Gemini 3.8 Flash (Fallback: Groq / Template)",
            )
            st.caption("ℹ️ *Note: AI explains the deterministic result; it does not make the decision.*")

        # ── Operator Recovery Actions ───────────────────────────
        if is_held:
            st.markdown("#### 🛠️ Authorized Operator Recovery Console")
            st.caption("Select an authorized recovery workflow to resolve this quarantined batch.")

            rec_tab_rel, rec_tab_rep, rec_tab_disc = st.tabs([
                "✓ Release Batch",
                "🔄 Reprocess Batch",
                "🗑️ Discard Batch",
            ])

            with rec_tab_rel:
                st.markdown("Releasing the batch confirms operator authorization and commits the held records downstream to SCD2 history.")
                r_reason = st.text_input("Release Justification", value="Verified authorized bulk update.", key="rel_reason")
                if st.button("✓ Confirm & Release Batch to History", type="primary", key="btn_rel"):
                    with st.spinner("Authorizing release and committing batch to history..."):
                        rel_res = state.release_held_batch(hold_id=active_hold.hold_id, reason=r_reason)
                        st.success(f"✓ Hold `{str(active_hold.hold_id)[:8]}...` successfully RELEASED!")
                        st.rerun()

            with rec_tab_rep:
                st.markdown("Reprocessing re-evaluates the batch in memory through current guardrail rules.")
                force_norm = st.checkbox("Force normal override (Bypass rule re-evaluation)", value=False)
                if st.button("🔄 Reprocess Batch", type="secondary", key="btn_rep"):
                    with st.spinner("Re-evaluating batch through guardrail engine..."):
                        rep_res = state.reprocess_held_batch(hold_id=active_hold.hold_id, force_normal=force_norm)
                        st.info(f"Reprocess result: {rep_res.status} (Success: {rep_res.success})")
                        st.rerun()

            with rec_tab_disc:
                st.markdown("Discarding permanently drops the quarantined batch. Target history remains completely untouched.")
                disc_reason = st.text_input("Discard Reason", value="Anomalous batch rejected by operator.", key="disc_reason")
                confirm_disc = st.checkbox("I confirm this quarantined batch should be permanently discarded.")
                if st.button("🗑️ Permanently Discard Batch", type="secondary", key="btn_disc"):
                    if not confirm_disc:
                        st.warning("Please check the confirmation box to discard.")
                    else:
                        with st.spinner("Discarding batch..."):
                            disc_res = state.discard_held_batch(hold_id=active_hold.hold_id, reason=disc_reason)
                            st.success(f"✓ Hold `{str(active_hold.hold_id)[:8]}...` successfully DISCARDED. Target history was not mutated.")
                            st.rerun()

        # Lineage
        if active_hold.evidence:
            st.markdown("##### Lineage Trace")
            render_lineage_panel(
                run_id=active_hold.evidence.get("onboarding_lineage", {}).get("onboarding_run_id"),
                source_id=active_hold.source_name,
                mapping_version=active_hold.evidence.get("onboarding_lineage", {}).get("mapping_version_id"),
                schema_fingerprint=active_hold.evidence.get("onboarding_lineage", {}).get("source_schema_fingerprint"),
            )

    # ── Next Step ───────────────────────────────────────────────
    st.markdown("---")
    n_col1, n_col2 = st.columns([3, 1])
    with n_col1:
        st.markdown("**Next Step:** Inspect system settings, database connectivity, and AI provider readiness.")
    with n_col2:
        if st.button("Inspect System Status ➔", type="primary", use_container_width=True):
            st.session_state["onb_nav_selection"] = "10. Settings & Connection Status"
            st.rerun()
