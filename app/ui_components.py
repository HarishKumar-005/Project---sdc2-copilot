"""Reusable UI components for the SCD2 Copilot dashboard.

All rendering functions accept data models and return nothing (they
write directly to the Streamlit page).  No business logic lives here.
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

# ── Inline SVG icon library ────────────────────────────
# Monoline 16×16 icons, stroke-based.  Keeps the bundle self-contained
# with zero external dependencies.  Every icon uses currentColor so it
# inherits the surrounding text/CSS color automatically.

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
    "search": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"><circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5L14 14"/></svg>',
    "clock": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6.5"/><path d="M8 4v4l2.5 1.5"/></svg>',
    "message": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 3h11a1 1 0 011 1v7a1 1 0 01-1 1H5l-3 2.5V4a1 1 0 011-1z"/></svg>',
    "table": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"><rect x="2" y="2" width="12" height="12" rx="1.5"/><path d="M2 6h12M2 10h12M6 2v12M10 2v12"/></svg>',
    "chart": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="12" height="12" rx="1.5"/><path d="M5 10V7M8 10V5M11 10V8"/></svg>',
    "history": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6.5"/><path d="M8 4v4l-2 2"/><path d="M2 8h1M13 8h1"/></svg>',
    "play": '<svg class="icon" viewBox="0 0 16 16" fill="currentColor" stroke="none"><path d="M5 3l8 5-8 5V3z"/></svg>',
    "refresh": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 7A5.5 5.5 0 0113 5.5"/><path d="M13.5 2v4h-4"/><path d="M13.5 9A5.5 5.5 0 013 10.5"/><path d="M2.5 14v-4h4"/></svg>',
    "plus_circle": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"><circle cx="8" cy="8" r="6.5"/><path d="M8 5v6M5 8h6"/></svg>',
    "minus_circle": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"><circle cx="8" cy="8" r="6.5"/><path d="M5 8h6"/></svg>',
    "edit": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 13.5h11M9.5 3l3 3-7 7H2.5v-3l7-7z"/></svg>',
    "trash": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M3 4h10M6 4V2.5h4V4M4.5 4v8.5a1 1 0 001 1h5a1 1 0 001-1V4"/></svg>',
    "file_text": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M4 1.5h5.5L13 5v8.5a1 1 0 01-1 1H4a1 1 0 01-1-1v-12a1 1 0 011-1z"/><path d="M9 1.5V5h3.5"/><path d="M5.5 8h5M5.5 10.5h5"/></svg>',
    "zap": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M9 1.5L3.5 9H8l-1 5.5L12.5 7H8l1-5.5z"/></svg>',
    "database": '<svg class="icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"><ellipse cx="8" cy="4" rx="5.5" ry="2.5"/><path d="M2.5 4v8c0 1.38 2.46 2.5 5.5 2.5s5.5-1.12 5.5-2.5V4"/><path d="M2.5 8c0 1.38 2.46 2.5 5.5 2.5s5.5-1.12 5.5-2.5"/></svg>',
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


# ── Header ─────────────────────────────────────────────


def render_header(
    processing_date: date,
    provider_name: str = "template",
    provider_ready: bool = True,
    pipeline_status: str = "idle",
) -> None:
    """Top-of-page dashboard header with status badges."""
    provider_dot = "dot-green" if provider_ready else "dot-red"
    provider_label = f"{provider_name.capitalize()}"

    status_map = {
        "idle": ("dot-gray", "Idle"),
        "running": ("dot-yellow", "Running"),
        "completed": ("dot-green", "Completed"),
        "error": ("dot-red", "Error"),
    }
    status_dot, status_text = status_map.get(pipeline_status, ("dot-gray", "Idle"))

    st.markdown(
        f"""
        <div class="dashboard-header">
            <div class="app-title">{_icon("database")} SCD2 Copilot</div>
            <div class="app-subtitle">AI-assisted Slowly Changing Dimension Type 2 Builder</div>
            <div class="header-meta">
                <span class="header-badge">{_icon("calendar")} {processing_date.isoformat()}</span>
                <span class="header-badge"><span class="dot {provider_dot}"></span> {provider_label}</span>
                <span class="header-badge"><span class="dot {status_dot}"></span> {status_text}</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ── KPI Strip ──────────────────────────────────────────


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
    """Render the 6-card KPI strip at the top of the page."""
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
        val_label = "Pass" if validation_passed else ("Fail" if validation_passed is False else "—")
        val_class = "success" if validation_passed else ("error" if validation_passed is False else "muted")
        time_str = f"{exec_time:.1f}s" if exec_time is not None else "—"

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


# ── Input Workspace ────────────────────────────────────


def render_input_workspace(
    settings: Any,
    provider_options: list[str],
    provider_labels: dict[str, str],
    default_provider_idx: int,
) -> tuple:
    """Render file uploaders and settings.

    Returns:
        (source_file, target_file, processing_date, llm_choice, delete_policy)
    """
    st.markdown(
        f'<div class="section-title">{_icon("upload")} Input &amp; Settings</div>',
        unsafe_allow_html=True,
    )

    col_left, col_right = st.columns([3, 2], gap="medium")

    with col_left:
        source_file = st.file_uploader(
            "Source CSV (today's data)",
            type=["csv"],
            key="source_upload",
            help="Upload today's full snapshot CSV.",
        )
        target_file = st.file_uploader(
            "Target CSV (yesterday's SCD2 table)",
            type=["csv"],
            key="target_upload",
            help="Upload yesterday's SCD2 table with effective_from, effective_to, is_current columns.",
        )

    with col_right:
        processing_date = st.date_input(
            "Processing Date",
            value=date.today(),
            help="The date to stamp on new/changed rows (effective_from).",
        )

        llm_choice = st.selectbox(
            "LLM Provider",
            options=provider_options,
            index=default_provider_idx,
            format_func=lambda x: provider_labels.get(x, x),
            help="Choose the explanation engine. 'template' works without API keys.",
        )

        delete_policy = st.selectbox(
            "Delete Policy",
            options=["soft_delete", "ignore"],
            index=0,
            help="How to handle records present in target but absent from source.",
        )

    return source_file, target_file, processing_date, llm_choice, delete_policy


# ── Schema Display ─────────────────────────────────────


def render_schema_detection(
    source_df: pl.DataFrame,
    business_key: list[str],
    tracked_columns: list[str],
) -> tuple[list[str], list[str]]:
    """Render detected schema and allow overrides.

    Returns:
        (business_key, tracked_columns) — possibly overridden by user.
    """
    st.markdown(
        f'<div class="section-title">{_icon("key")} Schema Detection</div>',
        unsafe_allow_html=True,
    )

    col_a, col_b = st.columns(2)
    with col_a:
        bk_override = st.multiselect(
            "Business Key",
            options=source_df.columns,
            default=business_key,
            help="Auto-detected business key. Override if needed.",
        )
    with col_b:
        tc_display = st.multiselect(
            "Tracked Columns",
            options=tracked_columns,
            default=tracked_columns,
            help="Columns monitored for changes. Auto-detected.",
            disabled=True,
        )

    return bk_override if bk_override else business_key, tracked_columns


# ── Run Controls ───────────────────────────────────────


def render_run_controls() -> tuple[bool, bool]:
    """Render Run Pipeline and Reset buttons.

    Returns:
        (run_clicked, reset_clicked)
    """
    col_run, col_reset = st.columns([4, 1])
    with col_run:
        run_clicked = st.button(
            "Run SCD2 Pipeline",
            type="primary",
            use_container_width=True,
            key="run_pipeline_btn",
        )
    with col_reset:
        reset_clicked = st.button(
            "Reset",
            use_container_width=True,
            key="reset_btn",
        )
    return run_clicked, reset_clicked


# ── Tab: Overview ──────────────────────────────────────


def render_overview_tab(
    change_report: ChangeReport,
    business_key: list[str],
    tracked_columns: list[str],
    processing_date: date,
    exec_time: float,
    validation_report: ValidationReport,
    provider_used: str,
) -> None:
    """Render the overview/summary tab."""
    summary = change_report.summary
    confidence_label, confidence_explanation, confidence_score = compute_confidence_assessment(
        validation_report, provider_used
    )

    items = [
        ("Processing Date", processing_date.isoformat()),
        ("Business Key", ", ".join(business_key)),
        ("Tracked Columns", ", ".join(tracked_columns)),
        ("Total Records", str(summary["total"])),
        ("New", str(summary["new"])),
        ("Changed", str(summary["changed"])),
        ("Unchanged", str(summary["unchanged"])),
        ("Deleted", str(summary["deleted"])),
        ("Execution Time", f"{exec_time:.2f}s"),
        ("Provider", provider_used.capitalize()),
        ("Validation", "Passed" if validation_report.passed else "Failed"),
        ("Confidence Assessment", f"{confidence_label} ({confidence_score:.0f}%)"),
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

    # Confidence score card
    bar_color = (
        "var(--success)" if confidence_score >= 80
        else "var(--warning)" if confidence_score >= 50
        else "var(--error)"
    )
    st.markdown(
        f"""
        <div style="margin-top:20px; background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius); padding: 16px;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                <span style="font-size:0.85rem; font-weight: 600; color:var(--text-1);">{_icon("shield")} Confidence Assessment</span>
                <span style="font-size:0.85rem; font-weight: 700; color:{bar_color};">{confidence_label} ({confidence_score:.0f}%)</span>
            </div>
            <div class="trust-bar" style="margin-bottom: 12px;">
                <div class="trust-fill" style="width:{confidence_score}%;background:{bar_color};"></div>
            </div>
            <div style="font-size:0.8rem; color:var(--text-2); line-height: 1.5;">
                {confidence_explanation}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Business Impact panel
    render_business_impact_panel(change_report, validation_report)


# ── Tab: Updated SCD2 Table ───────────────────────────


def render_table_tab(
    scd2_output: pl.DataFrame,
    business_key: list[str],
) -> None:
    """Render the SCD2 output table with filters and context."""
    row_count = scd2_output.height
    col_count = scd2_output.width
    current_count = 0
    historical_count = 0
    if "is_current" in scd2_output.columns:
        current_count = scd2_output.filter(pl.col("is_current") == True).height  # noqa: E712
        historical_count = row_count - current_count

    # Context bar
    st.markdown(
        f'<div style="display:flex;gap:24px;align-items:center;margin-bottom:12px;">'
        f'  <span style="color:var(--text-2);font-size:0.82rem;">'
        f'    {_icon("table")} <strong>{row_count}</strong> rows &middot; '
        f'    <strong>{col_count}</strong> columns &middot; '
        f'    <span style="color:var(--success);">{current_count} current</span> &middot; '
        f'    <span style="color:var(--text-2);">{historical_count} historical</span>'
        f'  </span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # Filter controls
    filter_col1, filter_col2 = st.columns(2)
    with filter_col1:
        current_filter = st.selectbox(
            "Filter by status",
            options=["All", "Current only", "Historical only"],
            key="table_filter_current",
        )
    with filter_col2:
        sort_by = st.selectbox(
            "Sort by",
            options=["Default"] + business_key + ["effective_from"],
            key="table_sort",
        )

    display_df = scd2_output

    if current_filter == "Current only" and "is_current" in scd2_output.columns:
        display_df = display_df.filter(pl.col("is_current") == True)  # noqa: E712
    elif current_filter == "Historical only" and "is_current" in scd2_output.columns:
        display_df = display_df.filter(pl.col("is_current") == False)  # noqa: E712

    if sort_by != "Default" and sort_by in display_df.columns:
        display_df = display_df.sort(sort_by)

    st.dataframe(display_df.to_pandas(), width="stretch")


# ── Tab: Validation ────────────────────────────────────


def render_validation_tab(validation_report: ValidationReport) -> None:
    """Render rule-by-rule validation results."""
    v_summary = validation_report.summary
    st.caption(
        f"**{v_summary['pass']}** passed · "
        f"**{v_summary.get('fail', 0)}** failed · "
        f"**{v_summary.get('warn', 0)}** warnings"
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
            for detail in rule.details:
                st.caption(f"  → {detail}")


# ── Tab: Explanations ─────────────────────────────────


def render_explanations_tab(
    explain_result: ExplainResult,
) -> None:
    """Render change explanations grouped by type."""
    # Render AI Usage & Efficiency panel first
    render_ai_usage_panel(explain_result.metrics)

    explanations = explain_result.explanations

    # Show any warnings
    for w in explain_result.warnings:
        st.warning(w)

    if not explanations:
        st.markdown(
            '<div class="empty-state">'
            f'  <div class="empty-icon">{_icon("message")}</div>'
            '  <div class="empty-text">No changes to explain.</div>'
            '</div>',
            unsafe_allow_html=True,
        )
        return

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
            f'<span class="badge {badge_class}" style="margin:8px 0;display:inline-block;">'
            f'{change_type} ({len(exps)})</span>',
            unsafe_allow_html=True,
        )

        for exp in exps:
            key_str = ", ".join(f"{k}={v}" for k, v in exp.business_key_values.items())
            label = f"AI: {key_str}" if exp.provider != "template" else f"Template: {key_str}"
            with st.expander(label):
                st.write(exp.text)
                st.caption(f"Provider: {exp.provider}")


# ── Tab: Data Explorer ─────────────────────────────────


def render_explorer_tab(
    source_df: pl.DataFrame,
    target_df: pl.DataFrame,
    scd2_output: pl.DataFrame,
    change_report: ChangeReport,
) -> None:
    """Render data previews for source, target, and diff."""
    explorer_sub = st.selectbox(
        "View",
        options=[
            "Source Preview",
            "Target Preview",
            "Output Preview",
            "Changed Records Only",
            "Deleted Records Only",
            "Schema",
        ],
        key="explorer_view",
    )

    if explorer_sub == "Source Preview":
        st.caption(f"Source: **{source_df.height}** rows × **{source_df.width}** columns")
        st.dataframe(source_df.head(200).to_pandas(), width="stretch")

    elif explorer_sub == "Target Preview":
        st.caption(f"Target: **{target_df.height}** rows × **{target_df.width}** columns")
        st.dataframe(target_df.head(200).to_pandas(), width="stretch")

    elif explorer_sub == "Output Preview":
        st.caption(f"Output: **{scd2_output.height}** rows × **{scd2_output.width}** columns")
        st.dataframe(scd2_output.head(200).to_pandas(), width="stretch")

    elif explorer_sub == "Changed Records Only":
        changed = change_report.changed
        if changed:
            rows = []
            for rec in changed:
                row = dict(rec.business_key_values)
                for fc in rec.field_changes:
                    row[f"{fc.column} (old)"] = fc.old_value
                    row[f"{fc.column} (new)"] = fc.new_value
                rows.append(row)
            st.caption(f"**{len(rows)}** changed records")
            st.dataframe(rows, width="stretch")
        else:
            st.info("No changed records.")

    elif explorer_sub == "Deleted Records Only":
        deleted = change_report.deleted
        if deleted:
            rows = [dict(rec.business_key_values) for rec in deleted]
            st.caption(f"**{len(rows)}** deleted records")
            st.dataframe(rows, width="stretch")
        else:
            st.info("No deleted records.")

    elif explorer_sub == "Schema":
        schema_data = [
            {"Column": col, "Type": str(dtype)}
            for col, dtype in zip(scd2_output.columns, scd2_output.dtypes)
        ]
        st.dataframe(schema_data, width="stretch")


# ── Tab: Run History ───────────────────────────────────


def render_history_tab(run_history: list[dict]) -> None:
    """Render run history / audit log."""
    if not run_history:
        st.markdown(
            '<div class="empty-state">'
            f'  <div class="empty-icon">{_icon("history")}</div>'
            '  <div class="empty-text">No runs recorded yet.</div>'
            '</div>',
            unsafe_allow_html=True,
        )
        return

    for i, run in enumerate(reversed(run_history), 1):
        run_num = len(run_history) - i + 1
        ts = run.get('timestamp', '—')
        with st.expander(f"Run #{run_num} — {ts}"):
            col1, col2 = st.columns(2)
            with col1:
                st.write(f"**Source:** {run.get('source_name', '—')}")
                st.write(f"**Target:** {run.get('target_name', '—')}")
                st.write(f"**Provider:** {run.get('provider', '—')}")
            with col2:
                st.write(f"**New:** {run.get('new', 0)} | **Changed:** {run.get('changed', 0)}")
                st.write(f"**Unchanged:** {run.get('unchanged', 0)} | **Deleted:** {run.get('deleted', 0)}")
                v_status = "Passed" if run.get('validation_passed') else "Failed"
                st.write(f"**Validation:** {v_status}")
                st.write(f"**Exec Time:** {run.get('exec_time', '—')}s")


# ── Downloads Section ──────────────────────────────────


def render_downloads(
    scd2_output: pl.DataFrame,
    validation_report: ValidationReport,
    explanations: list[Explanation],
) -> None:
    """Render download buttons for outputs."""
    st.markdown(
        f'<div class="section-title">{_icon("download")} Downloads</div>',
        unsafe_allow_html=True,
    )

    col1, col2, col3 = st.columns(3)

    with col1:
        csv_data = scd2_output.write_csv()
        st.download_button(
            label="SCD2 Output (CSV)",
            data=csv_data,
            file_name="scd2_output.csv",
            mime="text/csv",
            use_container_width=True,
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
            label="Validation Report",
            data=val_text,
            file_name="validation_report.txt",
            mime="text/plain",
            use_container_width=True,
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
            label="Explanations",
            data=exp_text,
            file_name="explanations.txt",
            mime="text/plain",
            use_container_width=True,
        )


# ── Advanced Panel ─────────────────────────────────────


def render_advanced_panel(
    explain_result: Optional[ExplainResult] = None,
    provider_used: str = "—",
    exec_time: Optional[float] = None,
    settings: Any = None,
) -> None:
    """Render the collapsible advanced details panel."""
    with st.expander("Advanced Details", expanded=False):
        st.caption("Internal diagnostics for debugging. Not required for normal operation.")

        adv_col1, adv_col2 = st.columns(2)
        with adv_col1:
            st.write("**Provider chain:**", provider_used)
            st.write("**Execution time:**", f"{exec_time:.3f}s" if exec_time else "—")
            if settings:
                g_status = "Set" if settings.has_gemini_key else "Not set"
                q_status = "Set" if settings.has_groq_key else "Not set"
                st.write(f"**Gemini key:** {g_status}")
                st.write(f"**Groq key:** {q_status}")
                st.write("**Effective provider:**", provider_used)

        with adv_col2:
            if explain_result and explain_result.warnings:
                st.write("**Warnings:**")
                for w in explain_result.warnings:
                    st.caption(w)
            else:
                st.write("**Warnings:** None")

        st.divider()
        st.caption("SCD2 logic is deterministic. The LLM only explains — it never decides.")


# ── Confidence Assessment & Business ROI Panels ────────


def compute_confidence_assessment(
    validation_report: ValidationReport,
    provider_used: str,
) -> tuple[str, str, float]:
    """Compute an honest confidence assessment (0-100) and qualitative label.

    Calculated from validation rule outcomes and provider runtime modes.
    """
    total = len(validation_report.rules) or 1
    pass_count = sum(
        1 for r in validation_report.rules if r.status == ValidationStatus.PASS
    )
    val_score = (pass_count / total) * 80

    provider_bonus = 20 if provider_used in ("gemini", "groq") else 10
    score = min(val_score + provider_bonus, 100.0)

    has_fail = any(r.status == ValidationStatus.FAIL for r in validation_report.rules)
    has_warn = any(r.status == ValidationStatus.WARN for r in validation_report.rules)

    if has_fail:
        label = "Low"
        explanation = (
            "One or more critical validation rules failed! This indicates overlapping date intervals, "
            "duplicate active records, or missing business keys. Immediate correction is required."
        )
    elif score >= 90:
        label = "Very High"
        explanation = (
            "All 5 deterministic validation rules passed. Business key auto-detected successfully. "
            "Online LLM provider completed batch explanations with structured schema validation."
        )
    elif score >= 75:
        label = "High"
        explanation = (
            "All deterministic validation rules passed. However, the system is running in offline template "
            "fallback mode for natural-language explanations."
        )
    elif has_warn or score >= 50:
        label = "Medium"
        explanation = (
            "SCD2 transformation completed, but validation warnings were triggered. "
            "Review date boundaries and tracked attributes for potential data quality issues."
        )
    else:
        label = "Low"
        explanation = "Validation integrity check failed or critical configurations are missing."

    return label, explanation, score


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
    """Render the AI Usage & Efficiency panel showing token counts and costs."""
    if metrics is None:
        st.info("No AI Usage metrics available (Pipeline was run without AI explanations).")
        return

    badge_label, badge_class = get_efficiency_badge(metrics.avg_tokens_per_change, metrics.provider)
    token_label = "Estimated Tokens" if metrics.is_estimated else "Exact Tokens"

    st.markdown(
        f"""
        <div class="section-card" style="margin-bottom: 20px;">
            <div class="section-title">
                {_icon("cpu")} AI Usage &amp; Efficiency
                <span class="badge {badge_class}" style="margin-left: auto;">{badge_label}</span>
            </div>
            <div style="display: flex; flex-wrap: wrap; gap: 24px;">
                <div style="flex: 1; min-width: 150px;">
                    <div style="font-size: 0.72rem; color: var(--text-2); text-transform: uppercase;">Provider / Model</div>
                    <div style="font-size: 1.05rem; font-weight: 600; color: var(--text-1); margin-top: 4px;">
                        {metrics.provider.capitalize()} <span style="font-size: 0.8rem; color: var(--text-2);">({metrics.model})</span>
                    </div>
                </div>
                <div style="flex: 1; min-width: 120px;">
                    <div style="font-size: 0.72rem; color: var(--text-2); text-transform: uppercase;">{token_label}</div>
                    <div style="font-size: 1.05rem; font-weight: 600; color: var(--text-1); margin-top: 4px;">
                        {metrics.total_tokens} <span style="font-size: 0.8rem; color: var(--muted);">({metrics.prompt_tokens}p / {metrics.completion_tokens}c)</span>
                    </div>
                </div>
                <div style="flex: 1; min-width: 100px;">
                    <div style="font-size: 0.72rem; color: var(--text-2); text-transform: uppercase;">Estimated Cost</div>
                    <div style="font-size: 1.05rem; font-weight: 600; color: var(--success); margin-top: 4px;">
                        ${metrics.estimated_cost:.5f}
                    </div>
                </div>
                <div style="flex: 1; min-width: 120px;">
                    <div style="font-size: 0.72rem; color: var(--text-2); text-transform: uppercase;">Avg Tokens / Change</div>
                    <div style="font-size: 1.05rem; font-weight: 600; color: var(--text-1); margin-top: 4px;">
                        {metrics.avg_tokens_per_change:.1f}
                    </div>
                </div>
                <div style="flex: 1; min-width: 100px;">
                    <div style="font-size: 0.72rem; color: var(--text-2); text-transform: uppercase;">API Latency</div>
                    <div style="font-size: 1.05rem; font-weight: 600; color: var(--info); margin-top: 4px;">
                        {metrics.request_duration:.2f}s
                    </div>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_business_impact_panel(
    change_report: ChangeReport,
    validation_report: ValidationReport,
) -> None:
    """Render the executive Business Impact dashboard panel."""
    summary = change_report.summary
    total_records = summary.get("total", 0)
    changes = summary.get("new", 0) + summary.get("changed", 0) + summary.get("deleted", 0)
    unchanged = summary.get("unchanged", 0)
    val_passed = sum(1 for r in validation_report.rules if r.status == ValidationStatus.PASS)
    val_total = len(validation_report.rules)

    st.markdown(
        f"""<div class="section-card" style="margin-top: 20px;">
<div class="section-title">
{_icon("shield_check")} Business Impact
</div>
<div style="display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin-bottom: 20px;">
<!-- Process Comparison -->
<div style="background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius); padding: 16px;">
<div style="font-weight: 600; font-size: 0.9rem; color: var(--error); margin-bottom: 10px; display: flex; align-items: center; gap: 6px;">
{_icon("minus_circle")} Traditional Process (Manual)
</div>
<ul style="margin: 0; padding-left: 20px; font-size: 0.82rem; color: var(--text-2); line-height: 1.6;">
<li>Manual SQL development</li>
<li>Manual validation</li>
<li>Manual documentation</li>
<li>Manual change analysis</li>
</ul>
</div>
<div style="background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius); padding: 16px;">
<div style="font-weight: 600; font-size: 0.9rem; color: var(--success); margin-bottom: 10px; display: flex; align-items: center; gap: 6px;">
{_icon("plus_circle")} Copilot Process (Automated)
</div>
<ul style="margin: 0; padding-left: 20px; font-size: 0.82rem; color: var(--text-2); line-height: 1.6;">
<li>Automated SCD2 generation</li>
<li>Automated validation</li>
<li>Automated change detection</li>
<li>AI-assisted explanations</li>
</ul>
</div>
</div>
<!-- Time Saved & Operational stats -->
<div style="display: flex; flex-wrap: wrap; gap: 20px; margin-bottom: 20px; border-top: 1px solid var(--border); border-bottom: 1px solid var(--border); padding: 16px 0;">
<div style="flex: 1; min-width: 180px; border-right: 1px solid var(--border); padding-right: 12px;">
<div style="font-size: 0.72rem; color: var(--text-2); text-transform: uppercase; letter-spacing: 0.05em;">Traditional Time</div>
<div style="font-size: 1.35rem; font-weight: 700; color: var(--error); margin-top: 2px;">30–60 minutes</div>
</div>
<div style="flex: 1; min-width: 180px; border-right: 1px solid var(--border); padding-right: 12px;">
<div style="font-size: 0.72rem; color: var(--text-2); text-transform: uppercase; letter-spacing: 0.05em;">Copilot Time</div>
<div style="font-size: 1.35rem; font-weight: 700; color: var(--success); margin-top: 2px;">&lt; 1 minute</div>
</div>
<div style="flex: 1; min-width: 140px;">
<div style="font-size: 0.72rem; color: var(--text-2); text-transform: uppercase;">Records Processed</div>
<div style="font-size: 1.15rem; font-weight: 600; color: var(--text-1); margin-top: 2px;">{total_records}</div>
</div>
<div style="flex: 1; min-width: 140px;">
<div style="font-size: 0.72rem; color: var(--text-2); text-transform: uppercase;">Changes Detected</div>
<div style="font-size: 1.15rem; font-weight: 600; color: var(--accent); margin-top: 2px;">{changes}</div>
</div>
<div style="flex: 1; min-width: 140px;">
<div style="font-size: 0.72rem; color: var(--text-2); text-transform: uppercase;">Historical Preserved</div>
<div style="font-size: 1.15rem; font-weight: 600; color: var(--text-1); margin-top: 2px;">{unchanged}</div>
</div>
<div style="flex: 1; min-width: 140px;">
<div style="font-size: 0.72rem; color: var(--text-2); text-transform: uppercase;">Validation Passed</div>
<div style="font-size: 1.15rem; font-weight: 600; color: var(--success); margin-top: 2px;">{val_passed} / {val_total}</div>
</div>
</div>
<!-- Why This Matters -->
<div>
<div style="font-size: 0.72rem; color: var(--text-2); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 4px;">Why This Matters</div>
<div style="font-size: 0.85rem; color: var(--text-1); line-height: 1.5; font-style: italic;">
"Reduces repetitive SCD2 implementation effort while improving auditability and consistency."
</div>
</div>
</div>""",
        unsafe_allow_html=True,
    )
