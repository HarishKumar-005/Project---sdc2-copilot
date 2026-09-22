"""Settings and System Connection Status view."""

from __future__ import annotations

import streamlit as st

from ..components import (
    render_metric_card,
    render_onboarding_header,
    render_status_badge,
)
from ..state import OnboardingUIState


def render_settings_status_view(state: OnboardingUIState) -> None:
    """Render the system configuration, database connectivity, and AI provider status console."""
    render_onboarding_header(
        title="Settings & System Connection Status",
        subtitle="Operational observability into database connectivity, configured AI reasoning providers, and domain service health.",
    )

    health = state.get_system_health()
    db_info = health["database"]
    ai_info = health["ai_providers"]
    env_info = health["environment"]
    svc_info = health["services"]

    # ── Quick Status Metrics ────────────────────────────────────
    st.markdown("### 📊 Infrastructure Health Summary")
    c1, c2, c3, c4 = st.columns(4)

    db_status_type = "success" if db_info["connected"] else ("info" if not db_info["configured"] else "error")
    db_val = "Connected" if db_info["connected"] else ("In-Memory" if not db_info["configured"] else "Failed")
    render_metric_card(c1, "PostgreSQL Database", db_val, "Transactional store", db_status_type)

    ai_prov = ai_info["effective_provider"].upper()
    ai_status_type = "success" if ai_info["has_gemini"] or ai_info["has_groq"] else "neutral"
    render_metric_card(c2, "AI Engine", ai_prov, ai_info.get("gemini_model", "Template"), ai_status_type)

    render_metric_card(c3, "Snapshot Mode", env_info["snapshot_mode"], "Change detection", "info")
    render_metric_card(c4, "Delete Policy", env_info["delete_policy"], "History preservation", "neutral")

    st.markdown("<div style='height: 14px;'></div>", unsafe_allow_html=True)

    # ── Database & Storage Configuration ────────────────────────
    st.markdown("### 🗄️ Database & Persistence Layer")
    with st.container(border=True):
        st.markdown("#### PostgreSQL / Supabase Connection")
        db_col1, db_col2 = st.columns([2, 1])

        with db_col1:
            st.markdown(f"**Connection URL (Redacted):** `{db_info['redacted_url']}`")
            if db_info["connected"]:
                st.success("✓ PostgreSQL database connection active and responsive (`SELECT 1` verified).")
            elif not db_info["configured"]:
                st.info("ℹ️ Running in **In-Memory Repository Mode**. Monitored entity history and hold records are stored in thread-safe memory repositories.")
            else:
                st.error(f"⚠️ Database connection failed: {db_info['error']}")

        with db_col2:
            st.markdown("**Storage Engines:**")
            st.markdown("• `monitored_entity_history`: SCD Type 2 dimension store")
            st.markdown("• `held_change_batch`: Pre-commit containment quarantine")
            st.markdown("• `onboarding_runs`: Execution audit & idempotency log")

    st.markdown("<div style='height: 12px;'></div>", unsafe_allow_html=True)

    # ── AI Reasoning & Fallback Pipeline ────────────────────────
    st.markdown("### 🤖 Semantic Reasoning & AI Provider Pipeline")
    with st.container(border=True):
        st.markdown("#### Provider Chain & Model Registry")
        ai_col1, ai_col2 = st.columns([1.5, 1.5])

        with ai_col1:
            st.markdown("**Provider Availability:**")
            gem_icon = "🟢" if ai_info["has_gemini"] else "⚪"
            groq_icon = "🟢" if ai_info["has_groq"] else "⚪"
            st.markdown(f"{gem_icon} **Google Gemini API:** `{'Configured' if ai_info['has_gemini'] else 'Not configured (fallback active)'}`")
            st.markdown(f"{groq_icon} **Groq Cloud API:** `{'Configured' if ai_info['has_groq'] else 'Not configured'}`")
            st.markdown("🟢 **Deterministic Template Fallback:** `Always Available (Zero-cost safety net)`")

        with ai_col2:
            st.markdown("**Configured Models & Routing:**")
            st.markdown(f"• **Primary Model:** `{ai_info.get('gemini_model', 'gemini-3.8-flash')}`")
            fallbacks = ai_info.get("gemini_fallback_models", [])
            fb_str = ", ".join(f"`{m}`" for m in fallbacks) if fallbacks else "None"
            st.markdown(f"• **Fallback Models:** {fb_str}")
            st.caption("AI proposals are strictly advisory. In accordance with the project contract, human approval is mandatory for all schema mappings.")

    st.markdown("<div style='height: 12px;'></div>", unsafe_allow_html=True)

    # ── Domain Service Operational Health ───────────────────────
    st.markdown("### ⚙️ Domain Services Health")
    with st.container(border=True):
        st.markdown("#### Bound In-Process Services")
        s_cols = st.columns(2)
        half = len(svc_info) // 2
        items = list(svc_info.items())

        with s_cols[0]:
            for name, status_val in items[:half]:
                st.markdown(f"• **{name}:** `{status_val}`")

        with s_cols[1]:
            for name, status_val in items[half:]:
                st.markdown(f"• **{name}:** `{status_val}`")
