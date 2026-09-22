"""Exception Queue and Structured Reprocessing view."""

from __future__ import annotations

import streamlit as st

from ..components import (
    render_lineage_panel,
    render_metric_card,
    render_onboarding_header,
    render_status_badge,
)
from ..state import OnboardingUIState
from src.scd2_copilot.onboarding.exceptions import NonDismissibleExceptionError
from src.scd2_copilot.onboarding.models.exception import ExceptionStatus


def render_exceptions_view(state: OnboardingUIState) -> None:
    """Render the exception queue, human correction form, and reprocessing audit console."""
    render_onboarding_header(
        title="Exception Queue & Structured Reprocessing",
        subtitle="Inspect quarantined records, apply structured human corrections, and deterministically reprocess records into valid canonical customer state.",
    )

    batch = st.session_state.get("onb_exception_batch")

    if not batch or not batch.exceptions:
        st.info("🎉 **Exception Queue is Empty.** No quarantined records are currently pending review.")
        if st.session_state.get("onb_transformation_result") is None:
            st.caption("Tip: Ingest data in Section 2 and run the transformation pipeline in Section 4 to discover any defective source records.")
        return

    # ── Exception Queue Metrics ─────────────────────────────────
    open_count = sum(1 for e in batch.exceptions if not e.is_resolved and e.status != ExceptionStatus.DISMISSED)
    resolved_count = sum(1 for e in batch.exceptions if e.is_resolved)
    dismissed_count = sum(1 for e in batch.exceptions if e.status == ExceptionStatus.DISMISSED)
    total_excs = len(batch.exceptions)

    m_col1, m_col2, m_col3, m_col4 = st.columns(4)
    render_metric_card(m_col1, "Total Exceptions", total_excs, f"Batch {batch.batch_id[:8]}", "info")
    render_metric_card(m_col2, "Open Pending", open_count, "Requires action", "error" if open_count else "success")
    render_metric_card(m_col3, "Resolved & Replayed", resolved_count, "Successfully fixed", "success")
    render_metric_card(m_col4, "Dismissed", dismissed_count, "Policy authorized", "neutral")

    st.markdown("<div style='height: 12px;'></div>", unsafe_allow_html=True)

    # ── Exception Table & Status Filtering ──────────────────────
    st.markdown("### 📋 Quarantined Record Exceptions")
    filter_tabs = st.radio(
        "Status Filter",
        ["ALL", "OPEN", "CORRECTED", "REPROCESSED", "RESOLVED", "DISMISSED"],
        index=0,
        horizontal=True,
    )

    filtered_excs = batch.exceptions
    if filter_tabs != "ALL":
        filtered_excs = [e for e in batch.exceptions if e.status.value == filter_tabs]

    exc_table_rows = []
    for e in filtered_excs:
        rec_id = e.source_record_id or f"row_{e.row_index}"
        exc_table_rows.append({
            "Exception ID": e.exception_id,
            "Record ID": rec_id,
            "Field": e.field,
            "Violated Invariant": e.rule_id,
            "Defect Reason": e.reason,
            "Status": e.status.value,
            "Corrections": len(e.corrections),
            "Created At": e.created_at.strftime("%Y-%m-%d %H:%M:%S UTC") if e.created_at else "—",
        })

    st.dataframe(exc_table_rows, use_container_width=True)

    st.markdown("<div style='height: 14px;'></div>", unsafe_allow_html=True)

    # ── Interactive Operator Correction Console ─────────────────
    st.markdown("### 🛠️ Interactive Operator Correction & Resolution Console")
    with st.container(border=True):
        st.markdown("#### Inspect & Act on Quarantined Record")

        exc_options = {f"{e.exception_id} (Record: {e.source_record_id or f'row_{e.row_index}'} • Field: {e.field} • {e.status.value})": e for e in batch.exceptions}
        sel_label = st.selectbox("Select Exception to Inspect", list(exc_options.keys()))
        selected_exc = exc_options[sel_label]

        raw_val = selected_exc.raw_record.get(selected_exc.field, "NULL")
        st.markdown(
            f"""<div style="background: var(--onb-surface-subtle, #f8fafc); border: 1px solid var(--onb-border, #e2e8f0); border-radius: 8px; padding: 12px 16px; margin-bottom: 16px;">
<strong>Deterministic Failure Details:</strong><br>
• Violated Invariant Rule: <code>{selected_exc.rule_id}</code><br>
• Error Category: <code>{selected_exc.category.value}</code><br>
• Defect Reason: <span style="color: #dc2626;">{selected_exc.reason}</span><br>
• Current Observed Value: <code>{raw_val}</code><br>
• Current Lifecycle State: <strong>{selected_exc.status.value}</strong>
</div>""",
            unsafe_allow_html=True,
        )

        act_tab_correct, act_tab_dismiss = st.tabs(["✏️ Apply Correction & Reprocess", "🚫 Dismiss Exception"])

        with act_tab_correct:
            c_form_col1, c_form_col2 = st.columns(2)
            with c_form_col1:
                suggested_val = "fixed.customer@enterprise.org" if "email" in selected_exc.field.lower() else ("ACTIVE" if "status" in selected_exc.field.lower() else "")
                new_value = st.text_input("Corrected Replacement Value", value=suggested_val)
                operator_id = st.text_input("Operator Identifier", value="lead_data_engineer@enterprise.org")

            with c_form_col2:
                correction_reason = st.text_area("Correction Audit Justification", value="Verified against customer master records.")
                st.markdown("<div style='height: 4px;'></div>", unsafe_allow_html=True)
                if st.button("✓ Submit Correction & Reprocess Record", type="primary", use_container_width=True):
                    with st.spinner("Applying correction and re-executing validation pipeline..."):
                        try:
                            reprocessed_exc, canon_rec = state.correct_and_reprocess(
                                exception=selected_exc,
                                field_name=selected_exc.field,
                                corrected_value=new_value,
                                reason=correction_reason,
                                operator=operator_id,
                            )
                            rec_identifier = selected_exc.source_record_id or f"row_{selected_exc.row_index}"
                            st.success(f"✓ Record `{rec_identifier}` reprocessed! Status is now {reprocessed_exc.status.value}.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Correction rejected by validation engine: {exc}")

        with act_tab_dismiss:
            st.markdown("Dismissing an exception acknowledges the defect and removes it from active operator queues without mutating downstream data.")
            d_col1, d_col2 = st.columns(2)
            with d_col1:
                dismiss_reason = st.text_input("Dismissal Justification (Mandatory)", value="Known edge case, acknowledged by customer data owner.")
                dismiss_operator = st.text_input("Authorizing Operator", value="compliance_officer@enterprise.org")
            with d_col2:
                confirm_dismiss = st.checkbox("I confirm this non-critical defect may be dismissed under policy authorization.")
                st.markdown("<div style='height: 4px;'></div>", unsafe_allow_html=True)
                if st.button("🚫 Authorize & Dismiss Exception", type="secondary", use_container_width=True):
                    if not confirm_dismiss:
                        st.warning("Please check the confirmation box to proceed with dismissal.")
                    elif not dismiss_reason.strip():
                        st.warning("A mandatory dismissal justification note is required.")
                    else:
                        try:
                            dismissed = state.dismiss_exception(
                                exception=selected_exc,
                                dismissed_by=dismiss_operator,
                                reason=dismiss_reason,
                            )
                            st.success(f"✓ Exception `{dismissed.exception_id}` marked as DISMISSED.")
                            st.rerun()
                        except NonDismissibleExceptionError as nde:
                            st.error(f"🚫 Cannot dismiss exception: Invariant rule `{nde.details.get('rule_id')}` is classified as strictly NON-DISMISSIBLE.")
                        except Exception as exc:
                            st.error(f"Failed to dismiss exception: {exc}")

        # Audit History of Corrections
        if selected_exc.corrections:
            st.markdown("##### Correction Audit Trail")
            for c in selected_exc.corrections:
                st.markdown(
                    f"• **{c.applied_at.strftime('%Y-%m-%d %H:%M:%S UTC')}** by `{c.applied_by}`: "
                    f"Set `{c.field}` = `'{c.corrected_value}'` ({c.reason})"
                )

        # Reprocessing History
        if selected_exc.reprocessing_history:
            st.markdown("##### Reprocessing Attempts")
            for r in selected_exc.reprocessing_history:
                res_icon = "✓ Success" if r.success else "✕ Failed"
                st.markdown(f"• **{r.attempted_at.strftime('%Y-%m-%d %H:%M:%S UTC')}** by `{r.reprocessed_by}`: {res_icon}")

    # ── Next Step ───────────────────────────────────────────────
    st.markdown("---")
    n_col1, n_col2 = st.columns([3, 1])
    with n_col1:
        st.markdown("**Next Step:** Inspect durable onboarding run state, idempotency replay, and execution metrics.")
    with n_col2:
        if st.button("Proceed to Runs ➔", type="primary", use_container_width=True):
            st.session_state["onb_nav_selection"] = "6. Runs & Idempotency"
            st.rerun()
