"""SCD2 Copilot — Enterprise Dashboard.

Upload source and target CSVs, run the SCD2 pipeline, inspect results
across dedicated tabs, and download outputs.
"""

from __future__ import annotations

import sys
import time
from datetime import date, datetime
from pathlib import Path

import streamlit as st
import polars as pl

# Add project root to path so src package is importable
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from src.scd2_copilot.config import get_settings, LLMProvider
from src.scd2_copilot.ingestion import load_csv, validate_csv_columns
from src.scd2_copilot.schema import detect_business_key, detect_tracked_columns
from src.scd2_copilot.detect_changes import detect_changes
from src.scd2_copilot.transform_scd2 import apply_scd2
from src.scd2_copilot.validate import validate_scd2
from src.scd2_copilot.explain import explain_changes

from ui_components import (
    inject_theme,
    render_header,
    render_kpi_strip,
    render_input_workspace,
    render_schema_detection,
    render_run_controls,
    render_overview_tab,
    render_table_tab,
    render_validation_tab,
    render_explanations_tab,
    render_explorer_tab,
    render_history_tab,
    render_downloads,
    render_advanced_panel,
)

# ── Page Config ────────────────────────────────────────
st.set_page_config(
    page_title="SCD2 Copilot",
    page_icon=":material/table_chart:",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Initialize session state ──────────────────────────
_defaults = {
    "pipeline_status": "idle",
    "change_report": None,
    "scd2_output": None,
    "validation_report": None,
    "explain_result": None,
    "execution_time": None,
    "source_df": None,
    "target_df": None,
    "business_key": None,
    "tracked_columns": None,
    "run_history": [],
    "provider_used": "template",
}
for key, val in _defaults.items():
    if key not in st.session_state:
        st.session_state[key] = val

# ── Inject Theme ──────────────────────────────────────
inject_theme()

# ── Settings ──────────────────────────────────────────
settings = get_settings()

# Build provider options with availability hints
provider_options = ["template"]
provider_labels = {"template": "Template (offline, no API key)"}

if settings.has_gemini_key:
    provider_options.insert(0, "gemini")
    provider_labels["gemini"] = "Gemini (API key configured)"
else:
    provider_options.append("gemini")
    provider_labels["gemini"] = "Gemini (no API key)"

if settings.has_groq_key:
    provider_options.insert(1 if "gemini" in provider_options[:1] else 0, "groq")
    provider_labels["groq"] = "Groq (API key configured)"
else:
    provider_options.append("groq")
    provider_labels["groq"] = "Groq (no API key)"

default_idx = 0 if settings.has_gemini_key else provider_options.index("template")

# ── 1. Header ─────────────────────────────────────────
# Show the actually-used provider after a run, fall back to config default
header_provider = st.session_state.get(
    "provider_used",
    settings.get_effective_provider().value,
)
render_header(
    processing_date=date.today(),
    provider_name=header_provider,
    provider_ready=settings.has_gemini_key or settings.has_groq_key,
    pipeline_status=st.session_state["pipeline_status"],
)

# ── 2. KPI Strip ──────────────────────────────────────
if st.session_state["change_report"] is not None:
    render_kpi_strip(
        summary=st.session_state["change_report"].summary,
        validation_passed=(
            st.session_state["validation_report"].passed
            if st.session_state["validation_report"]
            else None
        ),
        exec_time=st.session_state["execution_time"],
        provider=st.session_state["provider_used"],
    )
else:
    render_kpi_strip()

# ── 3. Input Workspace ────────────────────────────────
source_file, target_file, processing_date, llm_choice, delete_policy = render_input_workspace(
    settings=settings,
    provider_options=provider_options,
    provider_labels=provider_labels,
    default_provider_idx=default_idx,
)

# ── 4. Schema Detection ──────────────────────────────
if source_file and target_file:
    try:
        source_df = load_csv(source_file)
        target_df = load_csv(target_file)
    except Exception as e:
        st.error(f"Error loading CSVs: {e}")
        st.stop()

    # Validate CSV compatibility
    errors = validate_csv_columns(source_df, target_df)
    if errors:
        for err in errors:
            st.error(f"{err}")
        st.stop()

    # Detect schema
    try:
        business_key = detect_business_key(source_df, target_df)
        tracked_columns = detect_tracked_columns(source_df, business_key)
    except ValueError as e:
        st.error(f"Schema detection error: {e}")
        st.stop()

    # Show detected schema with overrides
    business_key, tracked_columns = render_schema_detection(
        source_df, business_key, tracked_columns
    )

    # Recompute tracked columns if business key was overridden
    tracked_columns = detect_tracked_columns(source_df, business_key)

    # ── 5. Run Controls ───────────────────────────────
    run_clicked, reset_clicked = render_run_controls()

    # Handle reset
    if reset_clicked:
        for key in _defaults:
            st.session_state[key] = _defaults[key]
        st.rerun()

    # ── 6. Pipeline Execution ─────────────────────────
    if run_clicked:
        st.session_state["pipeline_status"] = "running"
        t_start = time.perf_counter()

        with st.spinner("Running SCD2 pipeline…"):
            try:
                # Override LLM provider from selection
                settings.llm_provider = LLMProvider(llm_choice)

                # Step 1: Detect changes
                change_report = detect_changes(
                    source_df, target_df, business_key, tracked_columns, processing_date
                )

                # Step 2: Transform
                scd2_output = apply_scd2(
                    source_df, target_df, change_report,
                    business_key, tracked_columns, processing_date
                )

                # Step 3: Validate
                validation_report = validate_scd2(scd2_output, business_key)

                # Step 4: Explain
                explain_result = explain_changes(change_report, settings=settings)

                exec_time = time.perf_counter() - t_start

                # Store results in session state
                st.session_state.update({
                    "pipeline_status": "completed",
                    "change_report": change_report,
                    "scd2_output": scd2_output,
                    "validation_report": validation_report,
                    "explain_result": explain_result,
                    "execution_time": exec_time,
                    "source_df": source_df,
                    "target_df": target_df,
                    "business_key": business_key,
                    "tracked_columns": tracked_columns,
                    "provider_used": explain_result.provider_used,
                })

                # Append to run history
                st.session_state["run_history"].append({
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "source_name": source_file.name if source_file else "—",
                    "target_name": target_file.name if target_file else "—",
                    "provider": explain_result.provider_used,
                    "new": change_report.summary["new"],
                    "changed": change_report.summary["changed"],
                    "unchanged": change_report.summary["unchanged"],
                    "deleted": change_report.summary["deleted"],
                    "validation_passed": validation_report.passed,
                    "exec_time": f"{exec_time:.2f}",
                })

                st.rerun()

            except Exception as e:
                st.session_state["pipeline_status"] = "error"
                st.error(f"Pipeline failed: {e}")

    # ── 7. Status Banner ──────────────────────────────
    if st.session_state["pipeline_status"] == "completed":
        st.success("Pipeline completed successfully.")

        # Show LLM fallback warnings
        if st.session_state["explain_result"]:
            for warning in st.session_state["explain_result"].warnings:
                st.warning(warning)

    elif st.session_state["pipeline_status"] == "error":
        st.error("Pipeline encountered an error. Check details below.")

    # ── 8. Results Tabs ───────────────────────────────
    if st.session_state["change_report"] is not None:
        cr = st.session_state["change_report"]
        so = st.session_state["scd2_output"]
        vr = st.session_state["validation_report"]
        er = st.session_state["explain_result"]
        et = st.session_state["execution_time"]
        bk = st.session_state["business_key"]
        tc = st.session_state["tracked_columns"]
        pu = st.session_state["provider_used"]

        tab_overview, tab_table, tab_validation, tab_explain, tab_explorer, tab_history = st.tabs(
            ["Overview", "Updated Table", "Validation", "Explanations", "Explorer", "History"]
        )

        with tab_overview:
            render_overview_tab(cr, bk, tc, processing_date, et, vr, pu)

        with tab_table:
            render_table_tab(so, bk)

        with tab_validation:
            render_validation_tab(vr)

        with tab_explain:
            render_explanations_tab(er)

        with tab_explorer:
            src = st.session_state.get("source_df", source_df)
            tgt = st.session_state.get("target_df", target_df)
            render_explorer_tab(src, tgt, so, cr)

        with tab_history:
            render_history_tab(st.session_state["run_history"])

        # ── 9. Downloads ──────────────────────────────
        render_downloads(so, vr, er.explanations)

        # ── 10. Advanced Panel ────────────────────────
        render_advanced_panel(
            explain_result=er,
            provider_used=pu,
            exec_time=et,
            settings=settings,
        )

else:
    # No files uploaded
    from ui_components import _icon
    st.markdown(
        f"""
        <div class="empty-state" style="margin-top:40px;">
            <div class="empty-icon">{_icon("folder")}</div>
            <div class="empty-text">Upload both source and target CSV files to begin.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

# ── Footer ─────────────────────────────────────────────
st.divider()
st.caption(
    "SCD2 Copilot · Deterministic change detection · AI-powered explanations · "
    "Built for Infinite Computer Solutions"
)
