"""Semantic Mapping Review and Cryptographic Approval Gate view."""

from __future__ import annotations

from typing import Optional
import streamlit as st

from ..components import (
    render_ai_advisory_box,
    render_lineage_panel,
    render_metric_card,
    render_onboarding_header,
    render_status_badge,
)
from ..state import OnboardingUIState
from src.scd2_copilot.onboarding.canonical import CANONICAL_CUSTOMER_V1
from src.scd2_copilot.onboarding.models.approval import (
    ApprovedMappingVersion,
    FieldReviewDecision,
    ReviewDecisionType,
)
from src.scd2_copilot.onboarding.models.mapping import (
    MappingType,
    TransformationOpType,
    TransformationStep,
)


def render_mapping_review_view(state: OnboardingUIState) -> None:
    """Render the semantic mapping proposal, human review, and immutable approval screen."""
    render_onboarding_header(
        title="Semantic Mapping Review & Approval Gate",
        subtitle="Review AI-suggested candidate mappings, resolve ambiguities, override transformations, and sign off immutable mapping versions.",
    )

    # ── Proposal Generation Controls ────────────────────────────
    c_col1, c_col2, c_col3 = st.columns([1.5, 1.5, 2.0])
    with c_col1:
        gen_btn = st.button("🤖 Generate Mapping Proposals", type="primary", use_container_width=True)
    with c_col2:
        enable_ai = st.checkbox("Enable Gemini AI Reasoning", value=True, help="When enabled, calls Gemini for semantic aliases; falls back to deterministic template if unavailable.")
    with c_col3:
        st.caption("AI proposes candidates based strictly on schema metadata. Human sign-off is mandatory before any data transformation.")

    if gen_btn or st.session_state.get("onb_proposals") is None:
        with st.spinner("Analyzing column names and semantic aliases..."):
            proposals = state.generate_mapping_proposals(enable_ai=enable_ai)
    else:
        proposals = st.session_state["onb_proposals"]

    session = st.session_state.get("onb_review_session")
    approved = st.session_state.get("onb_approved_mapping")

    # ── Section 1: Approved vs Draft Banner ─────────────────────
    if approved:
        st.markdown(
            f"""<div style="background: #f0fdf4; border: 1px solid #86efac; border-radius: 10px; padding: 14px 18px; margin-bottom: 18px; display: flex; justify-content: space-between; align-items: center;">
<div>
<span style="font-weight: 800; font-size: 0.95rem; color: #166534;">🔒 APPROVED MAPPING VERSION ACTIVE</span>
<span style="margin-left: 10px; font-family: monospace; font-size: 0.85rem; color: #15803d;">v{approved.version_number} ({approved.mapping_version_id})</span>
<div style="font-size: 0.82rem; color: #166534; margin-top: 2px;">
Approved by <strong>{approved.approved_by}</strong> on {approved.approved_at.strftime('%Y-%m-%d %H:%M:%S UTC')} • Source Fingerprint: <code>{approved.source_fingerprint[:16]}...</code>
</div>
</div>
<div>
<span class="onb-badge onb-badge-success">SEALED &amp; IMMUTABLE</span>
</div>
</div>""",
            unsafe_allow_html=True,
        )
        st.caption("ℹ️ *Approved mappings are immutable. Modifying field mappings below and re-approving will seal a new mapping version.*")
    else:
        st.markdown(
            """<div style="background: #fffbeb; border: 1px solid #fde68a; border-radius: 10px; padding: 14px 18px; margin-bottom: 18px; display: flex; justify-content: space-between; align-items: center;">
<div>
<span style="font-weight: 800; font-size: 0.95rem; color: #92400e;">⚠️ DRAFT PROPOSALS (Awaiting Operator Sign-Off)</span>
<div style="font-size: 0.82rem; color: #92400e; margin-top: 2px;">
These candidate mappings are advisory proposals generated from schema metadata. Review, override, or reject each field below.
</div>
</div>
<div>
<span class="onb-badge onb-badge-warning">DRAFT ONLY</span>
</div>
</div>""",
            unsafe_allow_html=True,
        )

    # ── Section 2: Mapping Proposals Table ──────────────────────
    st.markdown("### 🗺️ Semantic Mapping Proposals vs Canonical `customer.v1`")

    decisions = session.decisions if session else {}
    table_rows = []
    for p in proposals.proposals:
        dec = decisions.get(p.source_field)
        trans_str = ", ".join(t.op.value for t in p.transformations) if p.transformations else "Direct (None)"
        conf_pct = f"{p.confidence * 100:.0f}%"
        status_label = dec.decision.value if dec else ("AMBIGUOUS (Needs Action)" if p.is_ambiguous else "PENDING")

        table_rows.append({
            "Source Field": p.source_field,
            "Target Canonical Field": (dec.target_field if dec else p.target_field) or "— (Unmapped)",
            "Mapping Type": p.mapping_type.value,
            "Confidence": conf_pct,
            "Transformations": trans_str,
            "Decision": dec.decision.value if dec else "PENDING",
            "Review Status": status_label,
            "Evidence / Reason": p.reason or "Deterministic heuristic match",
        })

    st.dataframe(table_rows, use_container_width=True)

    # ── Section 3: AI Advisory Explanation ──────────────────────
    expl = getattr(proposals, "fallback_reason", None) or f"Provider: {proposals.provider_used} (Fallback: {proposals.is_fallback})"
    render_ai_advisory_box(
        explanation_text=expl,
        title="AI Reasoning & Semantic Proposal Context",
        provider=f"{proposals.provider_used} Advisory",
    )

    # ── Section 4: Interactive Field Review Console ─────────────
    st.markdown("### ✍️ Human-in-the-Loop Review Console")
    with st.container(border=True):
        st.markdown("#### Review Individual Field Mapping")
        st.caption("Apply explicit approval, rejection, or canonical target overrides with approved transformations.")

        prop_options = {p.source_field: p for p in proposals.proposals}
        sel_source = st.selectbox("Select Source Field to Review", list(prop_options.keys()))
        active_prop = prop_options[sel_source]

        if active_prop.is_ambiguous or active_prop.mapping_type == MappingType.AMBIGUOUS:
            st.warning(
                f"⚠️ **Ambiguity Detected:** Source field `{active_prop.source_field}` cannot be silently approved. "
                "You must explicitly confirm or override the target field."
            )

        f_col1, f_col2, f_col3 = st.columns([1.5, 1.5, 1.5])
        canonical_fields = ["(Unmapped / Reject)"] + list(CANONICAL_CUSTOMER_V1.field_names)
        default_target_idx = 0
        if active_prop.target_field and active_prop.target_field in canonical_fields:
            default_target_idx = canonical_fields.index(active_prop.target_field)

        with f_col1:
            chosen_target = st.selectbox("Target Canonical Field", canonical_fields, index=default_target_idx)
        with f_col2:
            chosen_action = st.selectbox("Review Decision", ["APPROVE", "OVERRIDE", "REJECT"], index=0 if default_target_idx > 0 else 2)
        with f_col3:
            operator_name = st.text_input("Reviewer ID", value="lead_data_engineer@enterprise.org")

        rev_notes = st.text_input("Review Notes / Audit Justification", value=f"Verified mapping for {active_prop.source_field}")

        if st.button("Apply Field Decision", type="secondary"):
            if session:
                target_val = None if chosen_target == "(Unmapped / Reject)" else chosen_target
                dec_type = ReviewDecisionType[chosen_action]
                dec = FieldReviewDecision(
                    source_field=active_prop.source_field,
                    decision=dec_type,
                    target_field=target_val,
                    transformations=list(active_prop.transformations),
                    reviewer=operator_name,
                    review_notes=rev_notes,
                )
                try:
                    session.apply_decision(dec)
                    st.success(f"✓ Recorded {chosen_action} decision for `{active_prop.source_field}` → `{target_val}`.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Failed to record decision: {exc}")

    st.markdown("<div style='height: 12px;'></div>", unsafe_allow_html=True)

    # ── Section 5: Formal Approval & Sealing Gate ────────────────
    st.markdown("### 🔒 Formal Sign-Off & Immutable Sealing")
    with st.container(border=True):
        st.markdown("#### Seal Mapping Version")
        st.markdown(
            "Sealing generates an immutable, versioned artifact containing the cryptographic SHA-256 fingerprint. "
            "Downstream deterministic transformation and validation will execute strictly against this sealed version."
        )

        app_col1, app_col2, app_col3 = st.columns([2, 1.5, 1.5])
        with app_col1:
            final_reviewer = st.text_input("Final Signer Identity", value="lead_data_engineer@enterprise.org", key="final_rev_id")
        with app_col2:
            st.text_input("Canonical Contract", value="customer.v1", disabled=True)
        with app_col3:
            st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
            if st.button("✓ Sign & Seal Mapping Version", type="primary", use_container_width=True):
                with st.spinner("Sealing mapping version and generating SHA-256 content hash..."):
                    try:
                        approved_ver = state.approve_all_mappings(reviewed_by=final_reviewer)
                        st.success(f"✓ Mapping Version `v{approved_ver.version_number}` successfully sealed! (Hash: `{approved_ver.content_hash[:12]}...`)")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Sign-off halted by invariant gate: {exc}")

    # ── Next Step ───────────────────────────────────────────────
    st.markdown("---")
    n_col1, n_col2 = st.columns([3, 1])
    with n_col1:
        st.markdown("**Next Step:** Execute deterministic transformation and canonical invariant validation.")
    with n_col2:
        if st.button("Proceed to Data Quality ➔", type="primary", use_container_width=True):
            st.session_state["onb_nav_selection"] = "4. Data Quality"
            st.rerun()
