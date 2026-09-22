"""Onboarding Runs and Idempotency Lifecycle view."""

from __future__ import annotations

import streamlit as st

from ..components import (
    render_lineage_panel,
    render_metric_card,
    render_onboarding_header,
    render_status_badge,
)
from ..state import OnboardingUIState


def render_runs_view(state: OnboardingUIState) -> None:
    """Render the onboarding run operations, lifecycle visualizer, and idempotency inspection screen."""
    render_onboarding_header(
        title="Onboarding Runs & Idempotency Tracking",
        subtitle="Track durable onboarding execution lifecycles, inspect cryptographic input fingerprints, and verify idempotent replay safety.",
    )

    # ── Action Controls ─────────────────────────────────────────
    c_col1, c_col2, c_col3 = st.columns([1.5, 1.5, 2.0])

    with c_col1:
        run_btn = st.button("▶ Submit Onboarding Run", type="primary", use_container_width=True)

    with c_col2:
        idemp_btn = st.button("⚡ Test Idempotent Replay", type="secondary", use_container_width=True)

    with c_col3:
        st.caption("Submissions compute SHA-256 request & input fingerprints. Identical payloads return cached replays with 0 duplicate side effects.")

    if run_btn:
        with st.spinner("Submitting and executing durable onboarding run..."):
            run_row = state.submit_durable_run()
            st.success(f"Run `{run_row.run_id}` executed successfully with status: {run_row.status.value}")

    if idemp_btn:
        with st.spinner("Re-submitting identical payload to verify idempotency contract..."):
            run_row = state.submit_durable_run()
            st.info(
                f"**Idempotent Replay Verified:** Header `X-Idempotent-Replay: true` detected.\n\n"
                f"Run `{run_row.run_id}` returned cached execution results with zero duplicate mutations or state corruption."
            )

    # ── Run List Table ──────────────────────────────────────────
    st.markdown("### 📜 Persisted Onboarding Runs")
    all_runs = state.get_all_runs()

    if not all_runs:
        st.info("No runs recorded yet. Click 'Submit Onboarding Run' above to create one.")
        return

    runs_table = []
    for r in reversed(all_runs):
        runs_table.append({
            "Run ID": r.run_id,
            "Source ID": r.source_id,
            "Status": r.status.value,
            "Mapping Version": r.mapping_version_id or "—",
            "Input Fingerprint": r.input_fingerprint[:12] + "..." if r.input_fingerprint else "—",
            "Records Seen": r.metrics.total_records if r.metrics else 0,
            "Valid Records": r.metrics.valid_records if r.metrics else 0,
            "Duration (s)": f"{r.metrics.total_duration_ms / 1000.0:.2f}" if (r.metrics and getattr(r.metrics, "total_duration_ms", None)) else "—",
            "Created At": r.created_at.strftime("%Y-%m-%d %H:%M:%S UTC") if r.created_at else "—",
        })

    st.dataframe(runs_table, use_container_width=True)

    # ── Selected Run Detail & Lifecycle Visualizer ───────────────
    st.markdown("### 🔍 Run Inspection & Lifecycle Trace")
    latest_run = state.get_latest_run()

    if latest_run:
        with st.container(border=True):
            st.markdown(f"#### Run: `{latest_run.run_id}` ({latest_run.status.value})")

            # Lifecycle Stepper
            stages = [
                ("CREATED", "Created"),
                ("PROFILING", "Profiling"),
                ("MAPPING_PENDING", "Mapping"),
                ("APPROVAL_PENDING", "Approval"),
                ("TRANSFORMING", "Transforming"),
                ("VALIDATING", "Validating"),
                ("COMPLETED", "Completed"),
            ]
            stage_names = [s[0] for s in stages]
            active_idx = stage_names.index(latest_run.status.value) if latest_run.status.value in stage_names else len(stages) - 1

            stepper_parts = []
            for idx, (code, label) in enumerate(stages):
                if idx <= active_idx:
                    stepper_parts.append(f"<span style='color: #16a34a; font-weight: 700;'>✓ {label}</span>")
                else:
                    stepper_parts.append(f"<span style='color: #94a3b8;'>○ {label}</span>")
            stepper_html = " ➔ ".join(stepper_parts)

            st.markdown(
                f"""<div style="background: var(--onb-surface-subtle, #f8fafc); border: 1px solid var(--onb-border, #e2e8f0); border-radius: 8px; padding: 12px 16px; margin-bottom: 16px; font-size: 0.88rem;">
<strong>Lifecycle Progression:</strong><br>
{stepper_html}
</div>""",
                unsafe_allow_html=True,
            )

            # Cryptographic Lineage
            render_lineage_panel(
                run_id=latest_run.run_id,
                source_id=latest_run.source_id,
                schema_fingerprint=latest_run.source_schema_fingerprint,
                mapping_version=latest_run.mapping_version_id,
                input_fingerprint=latest_run.input_fingerprint,
            )

    # ── Next Step ───────────────────────────────────────────────
    st.markdown("---")
    n_col1, n_col2 = st.columns([3, 1])
    with n_col1:
        st.markdown("**Next Step:** Detect source schema drift, analyze mapping impact, and evaluate compatibility gating.")
    with n_col2:
        if st.button("Proceed to Schema Drift ➔", type="primary", use_container_width=True):
            st.session_state["onb_nav_selection"] = "7. Schema Drift"
            st.rerun()
