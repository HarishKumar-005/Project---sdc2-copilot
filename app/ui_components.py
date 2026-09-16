"""Reusable UI components for the SCD2 Copilot dashboard.

All rendering functions accept data models and return nothing (or widget states)
and write directly to the Streamlit page. No business logic lives here.
"""

from __future__ import annotations

import html as html_mod
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

import polars as pl
import streamlit as st

from src.scd2_copilot.models import (
    ChangeReport,
    ChangeType,
    Explanation,
    ValidationReport,
    ValidationStatus,
    LLMMetrics,
)
from src.scd2_copilot.explain import ExplainResult
from src.scd2_copilot.auth import AuthenticatedUser

# ── Inline SVG icon library ────────────────────────────
# Monoline 16×16 icons, stroke-based. Self-contained with zero external dependencies.
# Every icon uses currentColor so it inherits text/CSS color automatically.

_ICONS = {
    "calendar": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="12" height="11" rx="1.5"/><path d="M5 1.5v3M11 1.5v3M2 7h12"/></svg>',
    "cpu": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"><rect x="4" y="4" width="8" height="8" rx="1"/><rect x="6" y="6" width="4" height="4" rx=".5"/><path d="M6 1.5v2M10 1.5v2M6 12.5v2M10 12.5v2M1.5 6h2M1.5 10h2M12.5 6h2M12.5 10h2"/></svg>',
    "check_circle": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6.5"/><path d="M5.5 8.2l1.8 1.8 3.2-3.5"/></svg>',
    "x_circle": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"><circle cx="8" cy="8" r="6.5"/><path d="M5.5 5.5l5 5M10.5 5.5l-5 5"/></svg>',
    "alert_triangle": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M8 1.5L1.5 13.5h13L8 1.5z"/><path d="M8 6v3M8 11.5v.01"/></svg>',
    "upload": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 10.5v2a1 1 0 001 1h9a1 1 0 001-1v-2"/><path d="M8 10V3M5 5.5L8 2.5l3 3"/></svg>',
    "folder": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M2 4.5a1 1 0 011-1h3.5l1.5 1.5H13a1 1 0 011 1v6a1 1 0 01-1 1H3a1 1 0 01-1-1V4.5z"/></svg>',
    "key": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="5.5" cy="10.5" r="3"/><path d="M8 8l5.5-5.5M11 5l2.5.5.5-2.5"/></svg>',
    "download": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 10.5v2a1 1 0 001 1h9a1 1 0 001-1v-2"/><path d="M8 2.5v8M5 8l3 3 3-3"/></svg>',
    "settings": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"><circle cx="8" cy="8" r="2"/><path d="M8 1.5v2M8 12.5v2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M1.5 8h2M12.5 8h2M3.4 12.6l1.4-1.4M11.2 4.8l1.4-1.4"/></svg>',
    "shield": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M8 1.5L2.5 4v4c0 3.5 2.5 5.5 5.5 6.5 3-1 5.5-3 5.5-6.5V4L8 1.5z"/></svg>',
    "shield_check": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M8 1.5L2.5 4v4c0 3.5 2.5 5.5 5.5 6.5 3-1 5.5-3 5.5-6.5V4L8 1.5z"/><path d="M5.5 8.2l1.8 1.8 3.2-3.5"/></svg>',
    "search": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5L14 14"/></svg>',
    "clock": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6.5"/><path d="M8 4v4l2.5 1.5"/></svg>',
    "message": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 3h11a1 1 0 011 1v7a1 1 0 01-1 1H5l-3 2.5V4a1 1 0 011-1z"/></svg>',
    "table": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"><rect x="2" y="2" width="12" height="12" rx="1.5"/><path d="M2 6h12M2 10h12M6 2v12M10 2v12"/></svg>',
    "chart": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"><rect x="2" y="2" width="12" height="12" rx="1.5"/><path d="M5 10V7M8 10V5M11 10V8"/></svg>',
    "history": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6.5"/><path d="M8 4v4l-2 2"/><path d="M2 8h1M13 8h1"/></svg>',
    "play": '<svg class="icon" viewBox="0 0 16 16" fill="currentColor" stroke="none"><path d="M5 3l8 5-8 5V3z"/></svg>',
    "refresh": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 7A5.5 5.5 0 0113 5.5"/><path d="M13.5 2v4h-4"/><path d="M13.5 9A5.5 5.5 0 013 10.5"/><path d="M2.5 14v-4h4"/></svg>',
    "plus_circle": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6.5"/><path d="M8 5v6M5 8h6"/></svg>',
    "minus_circle": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6.5"/><path d="M5 8h6"/></svg>',
    "edit": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 13.5h11M9.5 3l3 3-7 7H2.5v-3l7-7z"/></svg>',
    "trash": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M3 4h10M6 4V2.5h4V4M4.5 4v8.5a1 1 0 001 1h5a1 1 0 001-1V4"/></svg>',
    "file_text": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M4 1.5h5.5L13 5v8.5a1 1 0 01-1 1H4a1 1 0 01-1-1v-12a1 1 0 011-1z"/><path d="M9 1.5V5h3.5"/><path d="M5.5 8h5M5.5 10.5h5"/></svg>',
    "zap": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M9 1.5L3.5 9H8l-1 5.5L12.5 7H8l1-5.5z"/></svg>',
    "database": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"><ellipse cx="8" cy="4" rx="5.5" ry="2.5"/><path d="M2.5 4v8c0 1.38 2.46 2.5 5.5 2.5s5.5-1.12 5.5-2.5V4"/><path d="M2.5 8c0 1.38 2.46 2.5 5.5 2.5s5.5-1.12 5.5-2.5"/></svg>',
    "user": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="5" r="3"/><path d="M2.5 14c0-3 2.5-5 5.5-5s5.5 2 5.5 5"/></svg>',
}


def _icon(name: str) -> str:
    """Return inline SVG markup for a named icon."""
    return _ICONS.get(name, "")


# ── Theme injection ────────────────────────────────────

_CSS_PATH = Path(__file__).parent / "dashboard_theme.css"


def inject_theme() -> None:
    """Read the CSS file once and inject it into the page."""
    css = _CSS_PATH.read_text(encoding="utf-8")
    st.markdown(f"<style>{css}</style>", unsafe_allow_html=True)


# ── 1. Product Header ──────────────────────────────────


# ── Authentication UI Components ───────────────────────


def render_login_gate() -> None:
    """Render a clean, professional sign-in page when user is unauthenticated."""
    from src.scd2_copilot.auth import (
        is_auth_configured,
        sync_redirect_uri_with_host,
        trigger_google_login,
    )

    # Proactively align redirect_uri with current host origin if deployed
    sync_redirect_uri_with_host()

    col_l, col_center, col_r = st.columns([1.2, 2.0, 1.2])
    with col_center:
        st.markdown(
            f"""
            <div style="text-align: center; margin-top: 56px; margin-bottom: 24px;">
                <div style="display: inline-flex; align-items: center; justify-content: center; gap: 10px; font-size: 2rem; font-weight: 700; color: var(--text-primary, #ffffff); margin-bottom: 6px;">
                    {_icon("database")} SCD2 Copilot
                </div>
                <div style="font-size: 0.95rem; color: var(--text-secondary, #8b949e);">
                    Historical Data Change &amp; Analytics Platform
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        with st.container(border=True):
            st.markdown("### Sign in")
            st.markdown(
                "<div style='font-size: 0.9rem; color: var(--text-secondary, #8b949e); margin-top: -6px; margin-bottom: 20px;'>"
                "Sign in with your account to access your workspace."
                "</div>",
                unsafe_allow_html=True,
            )

            if st.button("Continue with Google", type="primary", width="stretch", icon=":material/login:"):
                trigger_google_login()

            if not is_auth_configured():
                st.markdown("<div style='height: 12px;'></div>", unsafe_allow_html=True)
                st.info(
                    "Authentication credentials not configured in `.streamlit/secrets.toml`. "
                    "Provide `client_id`, `client_secret`, and `cookie_secret` to enable login."
                )


def render_user_badge(user: Optional[AuthenticatedUser]) -> None:
    """Render authenticated user identity and logout control in the sidebar."""
    if not user:
        return
    st.sidebar.markdown("---")
    st.sidebar.caption("SIGNED IN AS")
    display_name = user.name or (user.email.split("@")[0] if user.email else user.subject)
    user_svg = _icon("user")
    st.sidebar.markdown(
        f'<div style="display: flex; align-items: center; gap: 8px; font-weight: 600; font-size: 0.95rem; margin-top: 2px; margin-bottom: 4px; color: var(--text-primary, #ffffff);">'
        f'{user_svg} <span>{html_mod.escape(display_name)}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )
    if user.email:
        st.sidebar.caption(user.email)
    from src.scd2_copilot.auth import trigger_logout
    if st.sidebar.button("Log out", key="btn_auth_logout", width="stretch", icon=":material/logout:"):
        trigger_logout()


def render_header(
    processing_date: date,
    provider_name: str = "template",
    provider_ready: bool = True,
    pipeline_status: str = "idle",
    persisted_run_id: Optional[str] = None,
    user: Optional[AuthenticatedUser] = None,
) -> None:
    """Top-of-page clean product header with synchronized date and subtle status badge."""
    provider_dot = "dot-green" if provider_ready else "dot-red"
    provider_label = f"{provider_name.capitalize()}"

    status_map = {
        "idle": ("dot-gray", "Idle"),
        "running": ("dot-yellow", "Analyzing..."),
        "completed": ("dot-green", "Analysis Complete"),
        "deployed": ("dot-blue", "Queued in Background"),
        "cancelled": ("dot-yellow", "Cancelled"),
        "error": ("dot-red", "Error"),
    }
    status_dot, status_text = status_map.get(pipeline_status, ("dot-gray", "Idle"))

    meta_badges = [
        f'<span class="header-badge">{_icon("calendar")} Effective Date: <strong>{processing_date.isoformat()}</strong></span>',
        f'<span class="header-badge"><span class="dot {provider_dot}"></span> AI Engine: <strong>{provider_label}</strong></span>',
        f'<span class="header-badge"><span class="dot {status_dot}"></span> Status: <strong>{status_text}</strong></span>',
    ]
    if user and (user.name or user.email):
        user_label = user.name or user.email
        meta_badges.append(
            f'<span class="header-badge">{_icon("user")} User: <strong>{html_mod.escape(user_label)}</strong></span>'
        )
    if persisted_run_id:
        meta_badges.append(
            f'<span class="header-badge" style="border-color:var(--accent);">'
            f'{_icon("history")} Loaded Run: <code>{persisted_run_id[:16]}...</code></span>'
        )
    badges_html = "\n".join(f"        {b}" for b in meta_badges)

    header_html = (
        f'<div class="dashboard-header">\n'
        f'  <div class="app-title">{_icon("database")} SCD2 Copilot</div>\n'
        f'  <div class="app-subtitle">Deterministic historical change detection &bull; AI-assisted explanations &bull; Zero data guesswork</div>\n'
        f'  <div class="header-meta">\n{badges_html}\n  </div>\n'
        f'</div>'
    )
    st.markdown(header_html, unsafe_allow_html=True)


# ── 2. KPI Strip ──────────────────────────────────────


def _kpi_card(label: str, value: Any, css_class: str = "") -> str:
    """Return HTML for a single KPI card."""
    escaped = html_mod.escape(str(value))
    return (
        f'<div class="kpi-card">'
        f'  <div class="kpi-label">{html_mod.escape(label)}</div>'
        f'  <div class="kpi-value {css_class}">{escaped}</div>'
        f'</div>'
    )


def render_kpi_strip(
    summary: Optional[dict] = None,
    validation_passed: Optional[bool] = None,
    exec_time: Optional[float] = None,
    provider: str = "—",
) -> None:
    """Render the 6-card KPI strip at the top of the results."""
    if summary is None:
        cards = "".join(
            [
                _kpi_card("New", "—", "muted"),
                _kpi_card("Changed", "—", "muted"),
                _kpi_card("Unchanged", "—", "muted"),
                _kpi_card("Deleted", "—", "muted"),
                _kpi_card("Validation", "—", "muted"),
                _kpi_card("Exec Time", "—", "muted"),
            ]
        )
    else:
        val_label = "Pass (5/5)" if validation_passed else ("Fail" if validation_passed is False else "—")
        val_class = "success" if validation_passed else ("error" if validation_passed is False else "muted")
        time_str = f"{exec_time:.2f}s" if exec_time is not None else "—"

        cards = "".join(
            [
                _kpi_card("New", summary.get("new", 0), "success"),
                _kpi_card("Changed", summary.get("changed", 0), "accent"),
                _kpi_card("Unchanged", summary.get("unchanged", 0)),
                _kpi_card("Deleted", summary.get("deleted", 0), "error"),
                _kpi_card("Validation", val_label, val_class),
                _kpi_card("Exec Time", time_str, "info"),
            ]
        )

    st.markdown(f'<div class="kpi-strip">{cards}</div>', unsafe_allow_html=True)


# ── 3. Hero Result Summary ─────────────────────────────


def render_hero_summary(
    change_report: ChangeReport,
    validation_report: ValidationReport,
    explain_result: Optional[ExplainResult] = None,
    exec_time: Optional[float] = None,
    provider_used: str = "template",
) -> None:
    """Render the hero result summary card with plain-language 'What happened?' callout."""
    summary = change_report.summary
    total = summary.get("total", 0)
    new_c = summary.get("new", 0)
    chg_c = summary.get("changed", 0)
    unc_c = summary.get("unchanged", 0)
    del_c = summary.get("deleted", 0)

    val_passed = validation_report.passed
    val_badge = (
        '<span class="badge badge-pass">✓ SCD2 Invariants Passed (5/5 Rules)</span>'
        if val_passed
        else '<span class="badge badge-fail">✗ SCD2 Validation Failed</span>'
    )

    parts = []
    if new_c > 0:
        parts.append(f"<strong>{new_c}</strong> new record{'s' if new_c != 1 else ''} added")
    if chg_c > 0:
        parts.append(f"<strong>{chg_c}</strong> record{'s' if chg_c != 1 else ''} modified (previous active row closed, new version created)")
    if del_c > 0:
        parts.append(f"<strong>{del_c}</strong> record{'s' if del_c != 1 else ''} closed as inactive")
    if unc_c > 0:
        parts.append(f"<strong>{unc_c}</strong> record{'s' if unc_c != 1 else ''} preserved without changes")

    summary_text = ", ".join(parts) if parts else "No changes detected across snapshots"
    time_phrase = f" in <strong>{exec_time:.2f}s</strong>" if exec_time is not None else ""

    st.markdown(
        f"""
        <div class="hero-card">
            <div style="display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 12px;">
                <div>
                    <div class="hero-title">{_icon("check_circle")} Analysis Complete</div>
                    <div class="hero-subtitle">Analyzed {total} records across snapshots{time_phrase}</div>
                </div>
                <div>{val_badge}</div>
            </div>
            <div class="what-happened-card">
                <div class="what-happened-label">{_icon("file_text")} What happened?</div>
                <div>{summary_text}. All historical validity intervals <code>[effective_from, effective_to)</code> and point-in-time invariants are strictly preserved.</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ── 4. Input Workspace ─────────────────────────────────


def render_input_workspace(
    settings: Any,
    provider_options: list[str],
    provider_labels: dict[str, str],
    default_provider_idx: int,
    is_sample_active: bool = False,
    sample_file_names: tuple[str, str] = ("—", "—"),
) -> tuple:
    """Render intuitive data ingestion inputs with sample data toggle.

    Returns:
        (source_file, target_file, processing_date, snapshot_mode, delete_policy, sample_clicked, clear_sample_clicked)
    """
    st.markdown(
        f'<div class="section-title">{_icon("upload")} 1. Data Inputs</div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        """<div class="section-subtitle">Upload today's incoming snapshot and yesterday's SCD2 historical table, or load sample data for an instant demo.</div>""",
        unsafe_allow_html=True,
    )

    col_demo1, col_demo2 = st.columns([4, 1])
    sample_clicked = False
    clear_sample_clicked = False

    with col_demo1:
        if is_sample_active:
            st.info(
                f":material/check_circle: Loaded built-in sample demo: **{sample_file_names[0]}** (Source) & **{sample_file_names[1]}** (Target).",
                icon=":material/dataset:",
            )
        else:
            st.caption("New to SCD2 Copilot? Click **Try Sample Data** to run an instant demonstration with pre-configured datasets.")

    with col_demo2:
        if is_sample_active:
            clear_sample_clicked = st.button("Clear Sample", key="btn_clear_sample", use_container_width=True)
        else:
            sample_clicked = st.button("⚡ Try Sample Data", key="btn_try_sample", use_container_width=True)

    col_left, col_right = st.columns([3, 2], gap="large")

    with col_left:
        source_file = st.file_uploader(
            "Today's Data (Incoming Source CSV)",
            type=["csv"],
            key="source_upload",
            help="Upload today's source CSV containing current raw records.",
        )
        target_file = st.file_uploader(
            "Previous SCD2 Data (Yesterday's Target Table)",
            type=["csv"],
            key="target_upload",
            help="Upload yesterday's SCD2 table with effective_from, effective_to, and is_current columns.",
        )

    with col_right:
        processing_date = st.date_input(
            "Effective Date",
            value=date.today(),
            help="The date applied as effective_from for new/changed rows, and effective_to for closed rows.",
        )

        snapshot_mode = st.selectbox(
            "Snapshot Interpretation",
            options=["full", "incremental"],
            index=0,
            format_func=lambda x: "Full Universe Snapshot" if x == "full" else "Incremental Delta Feed",
            help="Full Universe: keys missing from today's data are eligible for deletion. Incremental Feed: missing keys are preserved unmodified.",
        )

        delete_policy = st.selectbox(
            "Missing Record Handling",
            options=["soft_delete", "ignore"],
            index=0,
            format_func=lambda x: "Soft Delete (Close inactive)" if x == "soft_delete" else "Ignore (Retain active)",
            help="How to handle records present in target but absent from source in a full snapshot: Soft Delete closes rows; Ignore keeps them active.",
        )

    return source_file, target_file, processing_date, snapshot_mode, delete_policy, sample_clicked, clear_sample_clicked


# ── 5. Comparison Settings (Schema Detection) ──────────


def render_schema_detection(
    source_df: pl.DataFrame,
    business_key: list[str],
    tracked_columns: list[str],
) -> tuple[list[str], list[str]]:
    """Render schema detection and comparison rules with user-friendly wording.

    Returns:
        (business_key, tracked_columns) — possibly overridden by user.
    """
    st.markdown(
        f'<div class="section-title">{_icon("key")} 2. Comparison Rules</div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="section-subtitle">Configure how records are matched across snapshots and which attributes trigger new versions.</div>',
        unsafe_allow_html=True,
    )

    col_a, col_b = st.columns(2, gap="large")
    with col_a:
        bk_override = st.multiselect(
            "How should records be matched? (Business Key)",
            options=source_df.columns,
            default=business_key,
            help="The unique business identifier(s) that identify an entity across snapshots (e.g., customer_id).",
        )
    with col_b:
        st.multiselect(
            "Which fields count as changes? (Tracked Attributes)",
            options=tracked_columns,
            default=tracked_columns,
            help="Columns monitored for changes. Any difference in these columns triggers an SCD2 version split.",
            disabled=True,
        )

    return bk_override if bk_override else business_key, tracked_columns


# ── 6. Primary Action & Run Controls ───────────────────


def render_run_controls(
    settings: Any,
    provider_options: list[str],
    provider_labels: dict[str, str],
    default_provider_idx: int,
) -> tuple[bool, bool, str, str, bool]:
    """Render the primary action CTA and collapsible Advanced Settings.

    Returns:
        (run_clicked, reset_clicked, exec_mode, llm_choice, force_recompute)
    """
    col_run, col_reset = st.columns([4, 1], gap="medium")
    with col_run:
        run_clicked = st.button(
            "Analyze Changes",
            type="primary",
            icon=":material/play_arrow:",
            use_container_width=True,
            key="run_pipeline_btn",
            help="Execute deterministic change detection, apply SCD2 transformations, validate invariants, and generate AI explanations.",
        )
    with col_reset:
        reset_clicked = st.button(
            "Reset",
            use_container_width=True,
            key="reset_btn",
            help="Reset session state and clear current results.",
        )

    # Progressive disclosure for technical execution settings
    with st.expander("⚙️ Advanced Execution Settings", expanded=False):
        c_adv1, c_adv2, c_adv3 = st.columns(3)
        with c_adv1:
            exec_mode = st.radio(
                "Execution Engine",
                options=["Interactive Flow (In-Process)", "Prefect Deployment (Background Runner)"],
                index=0,
                help="Interactive runs immediately in-process. Prefect Deployment runs on the local background runner with queue concurrency limits.",
                key="execution_mode_selection",
            )
        with c_adv2:
            llm_choice = st.selectbox(
                "AI Explanation Engine",
                options=provider_options,
                index=default_provider_idx,
                format_func=lambda x: provider_labels.get(x, x),
                help="Choose the model provider for change explanations. 'template' is fully offline.",
                key="llm_choice_select",
            )
        with c_adv3:
            force_recompute = st.checkbox(
                "⚡ Force Recompute",
                value=False,
                help="Bypass the M3.6 SHA-256 idempotency cache and force full recalculation even if an identical run fingerprint exists.",
                key="force_recompute_chk",
            )

    return run_clicked, reset_clicked, exec_mode, llm_choice, force_recompute


# ── 7. Tab: Overview ───────────────────────────────────


def render_overview_tab(
    change_report: ChangeReport,
    business_key: list[str],
    tracked_columns: list[str],
    processing_date: date,
    exec_time: float,
    validation_report: ValidationReport,
    provider_used: str,
    explain_result: Optional[ExplainResult] = None,
    deduplication_status: str = "new_execution",
    is_reused: bool = False,
) -> None:
    """Render the overview tab with truthful performance metrics and clear status cards."""
    summary = change_report.summary
    val_summary = compute_validation_summary(validation_report)
    ai_summary = compute_ai_status(explain_result, provider_used)

    snapshot_mode_val = getattr(change_report, "snapshot_mode", "full")
    snapshot_mode_label = (
        snapshot_mode_val.value.capitalize()
        if hasattr(snapshot_mode_val, "value")
        else str(snapshot_mode_val).capitalize()
    )

    delete_policy_val = getattr(change_report, "delete_policy", "soft_delete")
    delete_policy_label = (
        delete_policy_val.value.replace("_", " ").title()
        if hasattr(delete_policy_val, "value")
        else str(delete_policy_val).replace("_", " ").title()
    )

    total_records = summary.get("total", 0)
    throughput_str = f"{(total_records / exec_time):,.0f} rows/s" if exec_time > 0 and total_records > 0 else "Instant (Cached)"

    dedup_label = "Reused Cached Run" if is_reused else ("Forced Recompute" if deduplication_status == "forced_reexecution" else "Fresh Execution")

    items = [
        ("Effective Date", processing_date.isoformat()),
        ("Snapshot Mode", snapshot_mode_label),
        ("Delete Policy", delete_policy_label),
        ("Business Key", ", ".join(business_key)),
        ("Tracked Columns", ", ".join(tracked_columns)),
        ("Total Records", f"{total_records:,}"),
        ("New Records", f"{summary.get('new', 0):,}"),
        ("Changed Records", f"{summary.get('changed', 0):,}"),
        ("Unchanged Records", f"{summary.get('unchanged', 0):,}"),
        ("Deleted Records", f"{summary.get('deleted', 0):,}"),
        ("Execution Time", f"{exec_time:.3f}s"),
        ("Throughput", throughput_str),
        ("Execution Status", dedup_label),
        ("Validation", f"{val_summary['status']} ({val_summary['pass_count']}/{val_summary['total_rules']} Rules Passed)"),
        ("AI Status", f"{ai_summary['status']} ({ai_summary['provider']})"),
    ]

    grid_html = '<div class="summary-grid">'
    for label, value in items:
        grid_html += (
            f'<div class="summary-item">'
            f'  <span class="s-label">{html_mod.escape(label)}</span>'
            f'  <span class="s-value">{html_mod.escape(value)}</span>'
            f'</div>'
        )
    grid_html += "</div>"
    st.markdown(grid_html, unsafe_allow_html=True)

    # ── Strict Separation: Data Correctness vs AI Explanation ──
    val_status_color = "var(--success)" if val_summary["passed"] else "var(--error)"
    ai_status_color = (
        "var(--success)" if ai_summary["status"] == "SUCCESS"
        else "var(--info)" if ai_summary["status"] == "TEMPLATE"
        else "var(--warning)" if ai_summary["status"] == "FALLBACK"
        else "var(--error)"
    )

    fallback_info = (
        f'<div style="font-size:0.8rem; color:var(--warning); margin-top:4px;"><strong>Reason:</strong> {html_mod.escape(ai_summary["fallback_reason"])}</div>'
        if ai_summary["has_fallback"] and ai_summary["fallback_reason"]
        else ""
    )

    metrics_line = []
    if ai_summary["latency"]:
        metrics_line.append(f"<strong>Latency:</strong> {ai_summary['latency']}")
    if ai_summary["tokens"]:
        metrics_line.append(f"<strong>Tokens:</strong> {ai_summary['tokens']}")
    if ai_summary["cost"]:
        metrics_line.append(f"<strong>Cost:</strong> {ai_summary['cost']}")
    metrics_str = " &bull; ".join(metrics_line) if metrics_line else "Offline template generation"

    col_val, col_ai = st.columns(2)
    with col_val:
        viol_badge = (
            f" &bull; <span style='color:var(--error);'><strong>Violations:</strong> {val_summary['fail_count']}</span>"
            if val_summary["fail_count"]
            else ""
        )
        card_val_parts = [
            '<div style="background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius); padding: 16px; height: 100%;">',
            '  <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">',
            f'    <span style="font-size:0.85rem; font-weight: 600; color:var(--text-1);">{_icon("shield_check")} Data Correctness (SCD2 Validator)</span>',
            f'    <span style="font-size:0.85rem; font-weight: 700; color:{val_status_color};">{val_summary["status"]}</span>',
            '  </div>',
            '  <div style="font-size:0.82rem; color:var(--text-1); margin-bottom: 4px;">',
            f'    <strong>Invariants Passed:</strong> {val_summary["pass_count"]}/{val_summary["total_rules"]}{viol_badge}',
            '  </div>',
            '  <div style="font-size:0.82rem; color:var(--text-1); margin-bottom: 8px;">',
            f'    <strong>Integrity Issues:</strong> {val_summary["total_issues"]} detected',
            '  </div>',
            '  <div style="font-size:0.75rem; color:var(--text-2); line-height: 1.4;">',
            '    100% authoritative deterministic correctness based on 5 SCD2 mathematical invariants. Does not depend on LLMs.',
            '  </div>',
            '</div>',
        ]
        st.markdown("\n".join(card_val_parts), unsafe_allow_html=True)

    with col_ai:
        fallback_badge = (
            " &bull; <span style='color:var(--warning);'><strong>Fallback Active</strong></span>"
            if ai_summary["has_fallback"]
            else " &bull; <strong>Fallback:</strong> None"
        )
        card_ai_parts = [
            '<div style="background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius); padding: 16px; height: 100%;">',
            '  <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">',
            f'    <span style="font-size:0.85rem; font-weight: 600; color:var(--text-1);">{_icon("cpu")} AI Explanation Subsystem</span>',
            f'    <span style="font-size:0.85rem; font-weight: 700; color:{ai_status_color};">{ai_summary["status"]}</span>',
            '  </div>',
            '  <div style="font-size:0.82rem; color:var(--text-1); margin-bottom: 4px;">',
            f'    <strong>Active Provider:</strong> {ai_summary["provider"]}{fallback_badge}',
            '  </div>',
            f'  <div style="font-size:0.82rem; color:var(--text-1); margin-bottom: 4px;">{metrics_str}</div>',
        ]
        if fallback_info:
            card_ai_parts.append(f'  {fallback_info}')
        card_ai_parts.extend([
            '  <div style="font-size:0.75rem; color:var(--text-2); line-height: 1.4; margin-top: 6px;">',
            '    Natural language explanation layer. Evaluates validated evidence; does not alter SCD2 correctness.',
            '  </div>',
            '</div>',
        ])
        st.markdown("\n".join(card_ai_parts), unsafe_allow_html=True)

    # Measured Performance & Automation Stats
    render_performance_stats_panel(change_report, validation_report, exec_time, deduplication_status, is_reused)


def render_performance_stats_panel(
    change_report: ChangeReport,
    validation_report: ValidationReport,
    exec_time: float,
    deduplication_status: str,
    is_reused: bool,
) -> None:
    """Render truthful, measured automation statistics without ungrounded claims."""
    summary = change_report.summary
    total_records = summary.get("total", 0)
    changes = summary.get("new", 0) + summary.get("changed", 0) + summary.get("deleted", 0)
    unchanged = summary.get("unchanged", 0)
    val_passed = sum(1 for r in validation_report.rules if r.status == ValidationStatus.PASS)
    val_total = len(validation_report.rules)

    throughput_display = f"{(total_records / exec_time):,.0f} rows/s" if exec_time > 0 and total_records > 0 else "Cached"

    st.markdown(
        f"""
        <div class="section-card" style="margin-top: 20px;">
            <div class="section-title">
                {_icon("zap")} Execution Performance &amp; Operations
            </div>
            <div style="display: flex; flex-wrap: wrap; gap: 20px; padding: 12px 0;">
                <div style="flex: 1; min-width: 140px;">
                    <div style="font-size: 0.72rem; color: var(--text-2); text-transform: uppercase;">Engine Mode</div>
                    <div style="font-size: 1.15rem; font-weight: 600; color: var(--text-1); margin-top: 2px;">Vectorized Polars</div>
                </div>
                <div style="flex: 1; min-width: 140px;">
                    <div style="font-size: 0.72rem; color: var(--text-2); text-transform: uppercase;">Duration</div>
                    <div style="font-size: 1.15rem; font-weight: 600; color: var(--accent); margin-top: 2px;">{exec_time:.3f}s</div>
                </div>
                <div style="flex: 1; min-width: 140px;">
                    <div style="font-size: 0.72rem; color: var(--text-2); text-transform: uppercase;">Throughput</div>
                    <div style="font-size: 1.15rem; font-weight: 600; color: var(--success); margin-top: 2px;">{throughput_display}</div>
                </div>
                <div style="flex: 1; min-width: 140px;">
                    <div style="font-size: 0.72rem; color: var(--text-2); text-transform: uppercase;">Changes Classified</div>
                    <div style="font-size: 1.15rem; font-weight: 600; color: var(--accent); margin-top: 2px;">{changes}</div>
                </div>
                <div style="flex: 1; min-width: 140px;">
                    <div style="font-size: 0.72rem; color: var(--text-2); text-transform: uppercase;">History Preserved</div>
                    <div style="font-size: 1.15rem; font-weight: 600; color: var(--text-1); margin-top: 2px;">{unchanged}</div>
                </div>
                <div style="flex: 1; min-width: 140px;">
                    <div style="font-size: 0.72rem; color: var(--text-2); text-transform: uppercase;">Invariants Verified</div>
                    <div style="font-size: 1.15rem; font-weight: 600; color: var(--success); margin-top: 2px;">{val_passed} / {val_total}</div>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ── 8. Tab: Updated SCD2 Table ─────────────────────────


def render_table_tab(
    scd2_output: pl.DataFrame,
    business_key: list[str],
) -> None:
    """Render the resulting SCD2 table with status filtering and sorting."""
    row_count = scd2_output.height
    col_count = scd2_output.width
    current_count = 0
    historical_count = 0
    if "is_current" in scd2_output.columns:
        current_count = scd2_output.filter(pl.col("is_current") == True).height  # noqa: E712
        historical_count = row_count - current_count

    st.markdown(
        f'<div style="display:flex;gap:20px;align-items:center;margin-bottom:12px;">'
        f'  <span style="color:var(--text-2);font-size:0.82rem;">'
        f'    {_icon("table")} <strong>{row_count:,}</strong> total rows &middot; '
        f'    <strong>{col_count}</strong> columns &middot; '
        f'    <span style="color:var(--success);font-weight:600;">{current_count:,} active versions</span> &middot; '
        f'    <span style="color:var(--text-2);">{historical_count:,} historical versions</span>'
        f'  </span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    filter_col1, filter_col2 = st.columns(2)
    with filter_col1:
        current_filter = st.selectbox(
            "Filter by version state",
            options=["All records", "Active versions only (is_current=True)", "Historical versions only (is_current=False)"],
            key="table_filter_current",
        )
    with filter_col2:
        sort_by = st.selectbox(
            "Sort table by",
            options=["Default order"] + business_key + ["effective_from", "effective_to"],
            key="table_sort",
        )

    display_df = scd2_output

    if current_filter.startswith("Active") and "is_current" in scd2_output.columns:
        display_df = display_df.filter(pl.col("is_current") == True)  # noqa: E712
    elif current_filter.startswith("Historical") and "is_current" in scd2_output.columns:
        display_df = display_df.filter(pl.col("is_current") == False)  # noqa: E712

    if sort_by != "Default order" and sort_by in display_df.columns:
        display_df = display_df.sort(sort_by)

    st.dataframe(display_df.to_pandas(), width="stretch")


# ── 9. Tab: Validation ─────────────────────────────────


def render_validation_tab(validation_report: ValidationReport) -> None:
    """Render rule-by-rule SCD2 invariant validation results."""
    v_summary = validation_report.summary
    total_rules = len(validation_report.rules)
    pass_count = v_summary.get("pass", 0)
    fail_count = v_summary.get("fail", 0)
    warn_count = v_summary.get("warn", 0)

    st.markdown(
        f'<div style="margin-bottom: 16px; font-size:0.88rem; color:var(--text-1);">'
        f'  <strong>{pass_count} of {total_rules}</strong> rules passed &bull; '
        f'  <span style="color:var(--error);"><strong>{fail_count}</strong> violations</span> &bull; '
        f'  <span style="color:var(--warning);"><strong>{warn_count}</strong> warnings</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    for rule in validation_report.rules:
        if rule.status == ValidationStatus.PASS:
            badge = '<span class="badge badge-pass">PASS</span>'
        elif rule.status == ValidationStatus.FAIL:
            badge = '<span class="badge badge-fail">FAIL</span>'
        else:
            badge = '<span class="badge badge-warn">WARN</span>'

        st.markdown(
            f"""
            <div class="validation-rule">
                {badge}
                <span class="rule-name">{html_mod.escape(rule.name)}</span>
                <span class="rule-msg">{html_mod.escape(rule.message)}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )

        if rule.details:
            with st.expander(f"Inspect {len(rule.details)} details for {rule.name}"):
                for detail in rule.details:
                    st.caption(f"→ {detail}")


# ── 10. Tab: Explanations ──────────────────────────────


def render_explanations_tab(
    explain_result: ExplainResult,
) -> None:
    """Render change explanations with executive narrative first and metrics below."""
    explanations = explain_result.explanations

    # Show warnings first if any
    for w in explain_result.warnings:
        st.warning(w)

    if not explanations:
        st.markdown(
            '<div class="empty-state">'
            f'  <div class="empty-icon">{_icon("message")}</div>'
            '  <div class="empty-text">No changes were detected in this snapshot to explain.</div>'
            '</div>',
            unsafe_allow_html=True,
        )
    else:
        # Group by change type
        groups: dict[str, list[Explanation]] = {}
        for exp in explanations:
            key = exp.change_type.value.upper()
            groups.setdefault(key, []).append(exp)

        for change_type, exps in groups.items():
            badge_class = {
                "NEW": "badge-new",
                "CHANGED": "badge-changed",
                "DELETED": "badge-deleted",
            }.get(change_type, "badge-info")

            st.markdown(
                f'<span class="badge {badge_class}" style="margin:12px 0 8px 0;display:inline-block;">'
                f'{change_type} ({len(exps)})</span>',
                unsafe_allow_html=True,
            )

            for exp in exps:
                key_str = ", ".join(f"{k}={v}" for k, v in exp.business_key_values.items())
                label = f"Entity: {key_str}" if exp.provider != "template" else f"Template: {key_str}"
                with st.expander(label):
                    st.write(exp.text)
                    st.caption(f"Engine: {exp.provider.capitalize()}")

    # Secondary AI Usage & Efficiency panel underneath
    st.markdown("---")
    render_ai_usage_panel(explain_result.metrics)


def get_efficiency_badge(avg_tokens: float, provider: str) -> tuple[str, str]:
    """Return (badge_label, css_class) based on average tokens per change."""
    if provider == "template":
        return "Offline", "badge-info"
    if avg_tokens <= 150:
        return "Excellent", "badge-pass"
    elif avg_tokens <= 350:
        return "Good", "badge-new"
    elif avg_tokens <= 750:
        return "Moderate", "badge-warn"
    else:
        return "Expensive", "badge-fail"


def render_ai_usage_panel(metrics: Optional[LLMMetrics]) -> None:
    """Render the secondary AI Usage & Efficiency diagnostics card."""
    if metrics is None:
        st.caption("No AI Usage metrics available (offline template run).")
        return

    badge_label, badge_class = get_efficiency_badge(metrics.avg_tokens_per_change, metrics.provider)
    token_label = "Estimated Tokens" if metrics.is_estimated else "Exact Tokens"

    st.markdown(
        f"""
        <div class="section-card">
            <div class="section-title">
                {_icon("cpu")} AI Usage &amp; Cost Diagnostics
                <span class="badge {badge_class}" style="margin-left: auto;">{badge_label}</span>
            </div>
            <div style="display: flex; flex-wrap: wrap; gap: 24px;">
                <div style="flex: 1; min-width: 150px;">
                    <div style="font-size: 0.72rem; color: var(--text-2); text-transform: uppercase;">Provider &amp; Model</div>
                    <div style="font-size: 1.0rem; font-weight: 600; color: var(--text-1); margin-top: 4px;">
                        {metrics.provider.capitalize()} <span style="font-size: 0.8rem; color: var(--text-2);">({metrics.model})</span>
                    </div>
                </div>
                <div style="flex: 1; min-width: 120px;">
                    <div style="font-size: 0.72rem; color: var(--text-2); text-transform: uppercase;">{token_label}</div>
                    <div style="font-size: 1.0rem; font-weight: 600; color: var(--text-1); margin-top: 4px;">
                        {metrics.total_tokens:,} <span style="font-size: 0.8rem; color: var(--muted);">({metrics.prompt_tokens}p / {metrics.completion_tokens}c)</span>
                    </div>
                </div>
                <div style="flex: 1; min-width: 100px;">
                    <div style="font-size: 0.72rem; color: var(--text-2); text-transform: uppercase;">Estimated Cost</div>
                    <div style="font-size: 1.0rem; font-weight: 600; color: var(--success); margin-top: 4px;">
                        ${metrics.estimated_cost:.5f}
                    </div>
                </div>
                <div style="flex: 1; min-width: 120px;">
                    <div style="font-size: 0.72rem; color: var(--text-2); text-transform: uppercase;">Avg Tokens / Change</div>
                    <div style="font-size: 1.0rem; font-weight: 600; color: var(--text-1); margin-top: 4px;">
                        {metrics.avg_tokens_per_change:.1f}
                    </div>
                </div>
                <div style="flex: 1; min-width: 100px;">
                    <div style="font-size: 0.72rem; color: var(--text-2); text-transform: uppercase;">API Latency</div>
                    <div style="font-size: 1.0rem; font-weight: 600; color: var(--info); margin-top: 4px;">
                        {metrics.request_duration:.2f}s
                    </div>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ── 11. Tab: Data Explorer & Diffs ─────────────────────


def render_explorer_tab(
    source_df: Optional[pl.DataFrame],
    target_df: Optional[pl.DataFrame],
    scd2_output: pl.DataFrame,
    change_report: ChangeReport,
) -> None:
    """Render human-readable change diffs and dataset previews with honest archive notes."""
    explorer_sub = st.selectbox(
        "Select inspection view",
        options=[
            "What Changed? (Old → New)",
            "New Records",
            "Deleted Records",
            "Updated SCD2 Table Preview",
            "Today's Source Data Preview",
            "Previous SCD2 Target Preview",
            "Output Schema",
        ],
        key="explorer_view",
    )

    if explorer_sub == "What Changed? (Old → New)":
        changed = change_report.changed
        if changed:
            st.caption(f"**{len(changed)}** modified records detected:")
            for rec in changed:
                key_str = ", ".join(f"{k} = {v}" for k, v in rec.business_key_values.items())
                with st.expander(f"Key: {key_str} ({len(rec.field_changes)} field modifications)"):
                    table_rows = ""
                    for fc in rec.field_changes:
                        old_val = html_mod.escape(str(fc.old_value) if fc.old_value is not None else "NULL")
                        new_val = html_mod.escape(str(fc.new_value) if fc.new_value is not None else "NULL")
                        col_name = html_mod.escape(fc.column)
                        table_rows += (
                            f'<tr>'
                            f'  <td><strong>{col_name}</strong></td>'
                            f'  <td><span class="diff-cell-old">{old_val}</span></td>'
                            f'  <td>&rarr;</td>'
                            f'  <td><span class="diff-cell-new">{new_val}</span></td>'
                            f'</tr>'
                        )
                    st.markdown(
                        f"""
                        <table class="diff-table">
                            <thead>
                                <tr><th>Field</th><th>Old Value</th><th></th><th>New Value</th></tr>
                            </thead>
                            <tbody>{table_rows}</tbody>
                        </table>
                        """,
                        unsafe_allow_html=True,
                    )
        else:
            st.info("No modified records were found in this comparison.")

    elif explorer_sub == "New Records":
        new_recs = change_report.new
        if new_recs:
            rows = [dict(r.business_key_values) for r in new_recs]
            st.caption(f"**{len(rows)}** new records created:")
            st.dataframe(rows, width="stretch")
        else:
            st.info("No newly added records.")

    elif explorer_sub == "Deleted Records":
        deleted = change_report.deleted
        if deleted:
            rows = [dict(r.business_key_values) for r in deleted]
            st.caption(f"**{len(rows)}** closed/deleted records:")
            st.dataframe(rows, width="stretch")
        else:
            st.info("No deleted records.")

    elif explorer_sub == "Today's Source Data Preview":
        if source_df is not None and not source_df.is_empty():
            st.caption(f"Source: **{source_df.height}** rows × **{source_df.width}** columns")
            st.dataframe(source_df.head(200).to_pandas(), width="stretch")
        else:
            st.info("ℹ️ Raw input snapshot files ('today' and 'yesterday') are not stored in disk run archives to conserve storage. Upload CSV files to inspect raw inputs.")

    elif explorer_sub == "Previous SCD2 Target Preview":
        if target_df is not None and not target_df.is_empty():
            st.caption(f"Target: **{target_df.height}** rows × **{target_df.width}** columns")
            st.dataframe(target_df.head(200).to_pandas(), width="stretch")
        else:
            st.info("ℹ️ Raw input snapshot files ('today' and 'yesterday') are not stored in disk run archives to conserve storage. Upload CSV files to inspect raw inputs.")

    elif explorer_sub == "Updated SCD2 Table Preview":
        st.caption(f"Output: **{scd2_output.height}** rows × **{scd2_output.width}** columns")
        st.dataframe(scd2_output.head(200).to_pandas(), width="stretch")

    elif explorer_sub == "Output Schema":
        schema_data = [
            {"Column Name": col, "Polars DataType": str(dtype)}
            for col, dtype in zip(scd2_output.columns, scd2_output.dtypes)
        ]
        st.dataframe(schema_data, width="stretch")


# ── 12. Tab: Single Canonical Run History ──────────────


def render_history_tab(run_history: Optional[list[dict]] = None) -> None:
    """Render the single canonical persisted-run history experience with one-click restore."""
    from src.scd2_copilot.artifacts import list_runs, read_run_artifacts

    persisted_runs = list_runs()

    st.markdown("#### Execution History (`data/runs`)")
    st.caption("Each entry is a persisted run archive with verifiable Parquet data, change records, and invariant reports.")

    if not persisted_runs:
        st.info("No persisted runs found in `data/runs/`. Execute a pipeline run to generate your first audit archive.")
        return

    for r in persisted_runs:
        ts = r.started_at or r.created_at or "—"
        status_icon = "♻️" if r.is_reused else ("⚡" if r.deduplication_status == "forced_reexecution" else "📦")
        status_label = "Reused Cache" if r.is_reused else ("Forced Run" if r.deduplication_status == "forced_reexecution" else "New Run")
        val_pass = r.validation_summary.get("pass", 0)
        val_fail = r.validation_summary.get("fail", 0)
        val_label = "Pass (5/5)" if val_fail == 0 else f"Fail ({val_fail} violations)"

        expander_title = (
            f"{status_icon} [{r.run_id}] {ts} "
            f"· +{r.change_counts.get('new', 0)} / ~{r.change_counts.get('changed', 0)} / -{r.change_counts.get('deleted', 0)} "
            f"· {val_label}"
        )

        with st.expander(expander_title):
            col1, col2, col3 = st.columns([2, 2, 1], gap="medium")
            with col1:
                st.write(f"**Run ID:** `{r.run_id}`")
                if r.execution_fingerprint:
                    st.write(f"**Fingerprint:** `{r.execution_fingerprint[:16]}...`")
                st.write(f"**Status:** `{status_label}`")
                if r.is_reused and r.reused_from_run_id:
                    st.write(f"**Reused From:** `{r.reused_from_run_id}`")
                st.write(f"**Trigger:** `{r.trigger_type}`")
                st.write(f"**Effective Date:** `{r.processing_date or '—'}`")
                st.write(f"**AI Engine:** `{r.ai_status}` (`{r.ai_provider or 'template'}`)")
                if getattr(r, "created_by", None):
                    creator = r.created_by.get("name") or r.created_by.get("email") or r.created_by.get("subject")
                    st.write(f"**Executed By:** `{creator}`")

            with col2:
                st.write(f"**Output Rows:** {r.row_counts.get('output', '—')}")
                st.write(
                    f"**Changes:** New: {r.change_counts.get('new', 0)} | "
                    f"Changed: {r.change_counts.get('changed', 0)} | "
                    f"Deleted: {r.change_counts.get('deleted', 0)}"
                )
                st.write(f"**Validation:** {val_label}")
                st.write(f"**Duration:** {r.total_duration_seconds:.3f}s")

            with col3:
                st.write("")
                if st.button("Load Run", key=f"hist_load_{r.run_id}", use_container_width=True):
                    loaded = read_run_artifacts(r.run_id)
                    st.session_state.update({
                        "pipeline_status": "completed",
                        "scd2_output": loaded.scd2_output,
                        "change_report": loaded.change_report,
                        "validation_report": loaded.validation_report,
                        "explain_result": loaded.explain_result,
                        "execution_time": loaded.metadata.total_duration_seconds,
                        "business_key": loaded.metadata.business_key,
                        "tracked_columns": loaded.metadata.tracked_columns,
                        "provider_used": loaded.metadata.ai_provider,
                        "persisted_run_id": loaded.metadata.run_id,
                        "persisted_processing_date": loaded.metadata.processing_date,
                    })
                    st.rerun()


# ── 13. Downloads Section ──────────────────────────────


def render_downloads(
    scd2_output: pl.DataFrame,
    validation_report: ValidationReport,
    explanations: list[Explanation],
) -> None:
    """Render clean download action buttons."""
    st.markdown(
        f'<div class="section-title">{_icon("download")} Exports &amp; Deliverables</div>',
        unsafe_allow_html=True,
    )

    col1, col2, col3 = st.columns(3, gap="medium")

    with col1:
        csv_data = scd2_output.write_csv()
        st.download_button(
            label="SCD2 Output (CSV)",
            data=csv_data,
            file_name="scd2_output.csv",
            mime="text/csv",
            use_container_width=True,
            icon=":material/download:",
        )

    with col2:
        val_lines = []
        for rule in validation_report.rules:
            status_str = rule.status.value.upper()
            val_lines.append(f"[{status_str}] {rule.name}: {rule.message}")
            for d in rule.details:
                val_lines.append(f"  → {d}")
        val_text = "\n".join(val_lines)
        st.download_button(
            label="Validation Report (TXT)",
            data=val_text,
            file_name="validation_report.txt",
            mime="text/plain",
            use_container_width=True,
            icon=":material/description:",
        )

    with col3:
        exp_lines = []
        for exp in explanations:
            key_str = ", ".join(f"{k}={v}" for k, v in exp.business_key_values.items())
            exp_lines.append(f"[{exp.change_type.value.upper()}] {key_str}")
            exp_lines.append(f"  {exp.text}")
            exp_lines.append(f"  Provider: {exp.provider}")
            exp_lines.append("")
        exp_text = "\n".join(exp_lines) if exp_lines else "No changes to explain."
        st.download_button(
            label="AI Explanations (TXT)",
            data=exp_text,
            file_name="explanations.txt",
            mime="text/plain",
            use_container_width=True,
            icon=":material/chat:",
        )


# ── 14. Advanced Details Panel ─────────────────────────


def render_advanced_panel(
    explain_result: Optional[ExplainResult] = None,
    provider_used: str = "—",
    exec_time: Optional[float] = None,
    settings: Any = None,
    deployed_run_info: Optional[dict] = None,
    fingerprint: Optional[str] = None,
) -> None:
    """Render the collapsible advanced details panel."""
    with st.expander("🛠️ Advanced Technical Diagnostics", expanded=False):
        st.caption("Internal diagnostics and orchestration metadata for debugging and audit trail verification.")

        adv_col1, adv_col2 = st.columns(2, gap="large")
        with adv_col1:
            st.write("**Provider chain:**", provider_used)
            st.write("**Execution time:**", f"{exec_time:.3f}s" if exec_time else "—")
            if fingerprint:
                st.write("**Execution Fingerprint:**", f"`{fingerprint}`")
            if deployed_run_info:
                st.write("**Prefect Flow Run ID:**", f"`{deployed_run_info.get('flow_run_id')}`")
                st.write("**Prefect Deployment:**", f"`{deployed_run_info.get('deployment')}`")

        with adv_col2:
            if settings:
                g_status = "Configured" if settings.has_gemini_key else "Not configured"
                q_status = "Configured" if settings.has_groq_key else "Not configured"
                st.write(f"**Gemini API Key:** {g_status}")
                st.write(f"**Groq API Key:** {q_status}")

            if explain_result and explain_result.warnings:
                st.write("**Provider Warnings:**")
                for w in explain_result.warnings:
                    st.caption(w)
            else:
                st.write("**Provider Warnings:** None")

        st.divider()
        st.caption("SCD2 logic is strictly deterministic. The LLM only explains — it never decides.")


# ── Helper: Compute Summaries ──────────────────────────


def compute_validation_summary(validation_report: ValidationReport) -> dict[str, Any]:
    """Compute truthful deterministic data quality summary from ValidationReport."""
    total_rules = len(validation_report.rules)
    pass_count = sum(1 for r in validation_report.rules if r.status == ValidationStatus.PASS)
    fail_count = sum(1 for r in validation_report.rules if r.status == ValidationStatus.FAIL)
    warn_count = sum(1 for r in validation_report.rules if r.status == ValidationStatus.WARN)
    total_issues = sum(len(getattr(r, "details", [])) for r in validation_report.rules)

    status = "PASS" if validation_report.passed else "FAIL"

    return {
        "status": status,
        "passed": validation_report.passed,
        "total_rules": total_rules,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "warn_count": warn_count,
        "total_issues": total_issues,
    }


def compute_ai_status(
    explain_result: Optional[ExplainResult] = None,
    provider_used: str = "template",
) -> dict[str, Any]:
    """Compute truthful AI explanation subsystem status."""
    if explain_result is None:
        return {
            "status": "UNAVAILABLE",
            "provider": provider_used.capitalize(),
            "has_fallback": False,
            "fallback_reason": None,
            "latency": None,
            "tokens": None,
            "cost": None,
        }

    has_warnings = bool(explain_result.warnings)
    provider = (explain_result.provider_used or provider_used).lower()

    if has_warnings:
        status = "FALLBACK"
        fallback_reason = "; ".join(explain_result.warnings)
    elif provider == "template":
        status = "TEMPLATE"
        fallback_reason = None
    elif explain_result.explanations:
        status = "SUCCESS"
        fallback_reason = None
    else:
        status = "UNAVAILABLE"
        fallback_reason = "No explanations generated"

    metrics = explain_result.metrics
    latency_str = f"{metrics.request_duration:.2f}s" if metrics and metrics.request_duration else None
    tokens_str = f"{metrics.total_tokens:,}" if metrics and metrics.total_tokens else None
    cost_str = f"${metrics.estimated_cost:.4f}" if metrics and metrics.estimated_cost else None

    return {
        "status": status,
        "provider": provider.capitalize(),
        "has_fallback": has_warnings,
        "fallback_reason": fallback_reason,
        "latency": latency_str,
        "tokens": tokens_str,
        "cost": cost_str,
    }
