"""Reusable UI components for the Customer Data Onboarding & Integration Guardrail platform."""

from __future__ import annotations

import html as html_mod
from typing import Any, Optional
import streamlit as st


def render_onboarding_header(title: str, subtitle: str) -> None:
    """Render a clean, high-contrast page header with descriptive subtitle."""
    safe_title = html_mod.escape(title)
    safe_subtitle = html_mod.escape(subtitle)
    st.markdown(
        f"""<div style="margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid var(--onb-border, #e2e8f0);">
<div style="font-size: 1.65rem; font-weight: 800; color: var(--onb-text-1, #0f172a); line-height: 1.25; letter-spacing: -0.02em;">
{safe_title}
</div>
<div style="font-size: 0.92rem; color: var(--onb-text-2, #475569); margin-top: 6px; line-height: 1.4;">
{safe_subtitle}
</div>
</div>""",
        unsafe_allow_html=True,
    )


def render_status_badge(text: str, status_type: str = "neutral") -> str:
    """Return HTML markup for a styled status pill badge.

    status_type options: 'success', 'warning', 'error', 'held', 'info', 'neutral'
    """
    safe_text = html_mod.escape(text.upper())
    class_name = f"onb-badge onb-badge-{status_type.lower()}"
    return f'<span class="{class_name}">{safe_text}</span>'


def render_metric_card(
    col: Any,
    title: str,
    value: Any,
    subtitle: str = "",
    status_type: str = "neutral",
) -> None:
    """Render a clean light-themed operational KPI card inside a Streamlit column."""
    badge_html = f'<div style="margin-top: 4px;">{render_status_badge(subtitle, status_type)}</div>' if subtitle else ""
    html_content = (
        f'<div class="onb-metric-card">'
        f'<div class="onb-metric-title">{html_mod.escape(title)}</div>'
        f'<div class="onb-metric-value">{html_mod.escape(str(value))}</div>'
        f'{badge_html}'
        f'</div>'
    )
    col.markdown(html_content, unsafe_allow_html=True)


def render_pipeline_stepper(active_stage: str) -> None:
    """Render an interactive, horizontal 11-stage pipeline progress stepper."""
    stages = [
        ("SOURCE", "Source Feed"),
        ("PROFILE", "Profiling"),
        ("MAP", "AI Proposal"),
        ("APPROVE", "Human Approval"),
        ("TRANSFORM", "Transformation"),
        ("VALIDATE", "Validation"),
        ("EXCEPTIONS", "Exceptions"),
        ("DRIFT", "Schema Drift"),
        ("SCD2", "SCD2 History"),
        ("GUARDRAIL", "Guardrail"),
        ("RECOVERY", "Recovery"),
    ]

    active_upper = active_stage.upper()
    stage_keys = [s[0] for s in stages]
    active_idx = stage_keys.index(active_upper) if active_upper in stage_keys else 0

    items_html = []
    for idx, (code, label) in enumerate(stages):
        if idx < active_idx:
            cls = "onb-step-item onb-step-completed"
            icon = "✓"
        elif idx == active_idx:
            cls = "onb-step-item onb-step-current"
            icon = str(idx + 1)
        else:
            cls = "onb-step-item onb-step-pending"
            icon = str(idx + 1)

        items_html.append(
            f'<div class="{cls}"><div class="onb-step-circle">{icon}</div><span>{html_mod.escape(label)}</span></div>'
        )

    full_stepper = "".join(items_html)
    st.markdown(
        f'<div class="onb-stepper-container">{full_stepper}</div>',
        unsafe_allow_html=True,
    )


def render_ai_advisory_box(
    explanation_text: str,
    title: str = "AI Root-Cause Advisory",
    provider: str = "Gemini 3.8 Flash",
) -> None:
    """Render an explicit advisory AI box reinforcing 'AI explains, AI does NOT decide'."""
    safe_exp = html_mod.escape(explanation_text)
    html_content = (
        f'<div class="onb-ai-box">'
        f'<div class="onb-ai-box-header">'
        f'<div style="display: flex; align-items: center; gap: 8px;">'
        f'<span style="font-size: 1.1rem;">🤖</span>'
        f'<span style="font-weight: 700; font-size: 0.95rem; color: #166534;">{html_mod.escape(title)}</span>'
        f'<span class="onb-ai-tag">{html_mod.escape(provider)}</span>'
        f'</div>'
        f'<div class="onb-ai-disclaimer">Advisory only • Non-authoritative</div>'
        f'</div>'
        f'<div style="font-size: 0.88rem; color: #14532d; line-height: 1.5; margin-top: 6px;">{safe_exp}</div>'
        f'</div>'
    )
    st.markdown(html_content, unsafe_allow_html=True)


def render_lineage_panel(
    run_id: Optional[str] = None,
    source_id: Optional[str] = None,
    schema_fingerprint: Optional[str] = None,
    mapping_version: Optional[str] = None,
    canonical_schema: str = "customer.v1",
    input_fingerprint: Optional[str] = None,
) -> None:
    """Render a cryptographic lineage panel for traceability and auditability."""
    html_content = (
        f'<div class="onb-lineage-panel">'
        f'<strong>🔗 RUN LINEAGE &amp; GOVERNANCE AUDIT</strong><br>'
        f'• Run ID: <code>{html_mod.escape(str(run_id or "—"))}</code><br>'
        f'• Source ID: <code>{html_mod.escape(str(source_id or "—"))}</code><br>'
        f'• Schema Fingerprint: <code>{html_mod.escape(str(schema_fingerprint or "—"))[:16]}...</code><br>'
        f'• Mapping Version: <code>{html_mod.escape(str(mapping_version or "—"))}</code><br>'
        f'• Canonical Target: <code>{html_mod.escape(canonical_schema)}</code><br>'
        f'• Input Fingerprint: <code>{html_mod.escape(str(input_fingerprint or "—"))[:16]}...</code>'
        f'</div>'
    )
    st.markdown(html_content, unsafe_allow_html=True)
