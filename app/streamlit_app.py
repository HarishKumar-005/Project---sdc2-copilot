"""SCD2 Copilot — Enterprise Data Change Intelligence Dashboard.

Deterministic historical change detection & AI-assisted explanations.
"""

from __future__ import annotations

import html as html_mod
import logging
import sys
import time
import warnings
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

try:
    from authlib.deprecate import AuthlibDeprecationWarning
    warnings.filterwarnings("ignore", category=AuthlibDeprecationWarning)
except ImportError:
    pass

import polars as pl
import streamlit as st

logger = logging.getLogger("scd2_copilot.streamlit")

# Add project root to path so src package is importable
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from src.scd2_copilot.auth import get_current_user, handle_auth_callback, is_user_logged_in
from src.scd2_copilot.config import get_settings, LLMProvider
from src.scd2_copilot.exceptions import ContractValidationError, DuplicateBusinessKeyError
from src.scd2_copilot.deployment import (
    FULL_DEPLOYMENT_NAME,
    DeploymentParameters,
    apply_deployment,
    configure_prefect_api_url,
    load_deployment_run_artifacts,
    poll_deployment_run_state,
    stage_dataset,
    trigger_pipeline_run,
)
from src.scd2_copilot.ingestion import load_csv, validate_csv_columns
from src.scd2_copilot.schema import detect_business_key, detect_tracked_columns
from src.scd2_copilot.workflow import run_pipeline

from v2_monitor import render_v2_live_monitor
try:
    from onboarding_ui import render_customer_onboarding_app
except ImportError:
    from app.onboarding_ui import render_customer_onboarding_app
from ui_components import (
    _icon,
    inject_theme,
    render_header,
    render_hero_summary,
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
    render_login_gate,
    render_user_badge,
)

# ── Page Config ────────────────────────────────────────
st.set_page_config(
    page_title="SCD2 Copilot",
    page_icon=":material/table_chart:",
    layout="wide",
    initial_sidebar_state="expanded",
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
    "deployed_run_info": None,
    "error_message": None,
    "persisted_run_id": None,
    "persisted_processing_date": None,
    "is_sample_active": False,
    "selected_processing_date": date.today(),
}
for key, val in _defaults.items():
    if key not in st.session_state:
        st.session_state[key] = val


def populate_session_state_from_persisted_run(
    persisted: Any,
    source_df: Optional[pl.DataFrame] = None,
    target_df: Optional[pl.DataFrame] = None,
    source_file_name: str = "—",
    target_file_name: str = "—",
) -> None:
    """Populate Streamlit session state from a persisted M3.5 run without re-running the engine."""
    meta = persisted.metadata
    st.session_state.update({
        "pipeline_status": "completed",
        "change_report": persisted.change_report,
        "scd2_output": persisted.scd2_output,
        "validation_report": persisted.validation_report,
        "explain_result": persisted.explain_result,
        "execution_time": meta.total_duration_seconds,
        "source_df": source_df if source_df is not None else st.session_state.get("source_df"),
        "target_df": target_df if target_df is not None else st.session_state.get("target_df"),
        "business_key": meta.business_key or st.session_state.get("business_key"),
        "tracked_columns": meta.tracked_columns or st.session_state.get("tracked_columns"),
        "provider_used": meta.ai_provider or "template",
        "persisted_run_id": meta.run_id,
        "persisted_processing_date": meta.processing_date,
        "error_message": None,
    })

    hist_entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_name": source_file_name,
        "target_name": target_file_name,
        "provider": meta.ai_provider or "template",
        "new": persisted.change_report.summary["new"],
        "changed": persisted.change_report.summary["changed"],
        "unchanged": persisted.change_report.summary["unchanged"],
        "deleted": persisted.change_report.summary["deleted"],
        "validation_passed": persisted.validation_report.passed,
        "exec_time": f"{meta.total_duration_seconds:.2f}",
        "is_reused": meta.is_reused,
        "deduplication_status": meta.deduplication_status,
        "execution_fingerprint": meta.execution_fingerprint,
    }
    curr_hist = st.session_state.get("run_history", [])
    if not (curr_hist and curr_hist[-1].get("execution_fingerprint") == meta.execution_fingerprint and curr_hist[-1].get("timestamp") == hist_entry["timestamp"]):
        curr_hist.append(hist_entry)
        st.session_state["run_history"] = curr_hist


# ── Inject Theme ──────────────────────────────────────
inject_theme()

# ── Handle OAuth Callback (Supabase PKCE code exchange) ─
handle_auth_callback()

# ── Local / Dev Authentication Bypass ─────────────────
if hasattr(st, "query_params") and st.query_params.get("skip_auth") in ("true", "1", "yes"):
    st.session_state["dev_bypass_authenticated"] = True

# ── Authentication Gate (Supabase Auth) ────────────────
if not is_user_logged_in():
    render_login_gate()
    st.stop()

current_user = get_current_user()

# ── Settings & AI Options ─────────────────────────────
settings = get_settings()

provider_options = ["template"]
provider_labels = {"template": "Template (offline, rule-based)"}

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

# ── Sidebar: Mode & Clean Overview ────────────────────
with st.sidebar:
    render_user_badge(current_user)
    st.markdown("### Platform Mode")
    app_mode = st.radio(
        "Select Operating Mode",
        [
            "🚀 Customer Data Onboarding & Guardrail",
            "⚡ Live Guardrail Monitor (V2)",
            "📁 Batch CSV Analysis (V1)",
        ],
        index=0,
        key="platform_mode_selector",
        label_visibility="collapsed",
    )
    if app_mode != "🚀 Customer Data Onboarding & Guardrail":
        st.divider()
        st.markdown("### SCD2 Copilot")
        st.caption("AI-Assisted Data Change & Historical Analytics Platform")
        st.markdown(
            """
            - **Engine:** Polars (Vectorized)
            - **Validator:** 5 Invariant Rules
            - **Orchestration:** Prefect 3
            - **Idempotency:** SHA-256 Fingerprint
            - **Persistence:** Parquet & JSON / Supabase
            """
        )
        st.divider()
        st.caption("Switch modes anytime via the selector above.")

def render_v1_batch_mode(
    current_user: Optional[AuthenticatedUser],
    settings: Settings,
    provider_options: list[str],
    provider_labels: dict[str, str],
    default_idx: int,
) -> None:
    """Render the standard V1 batch CSV ingestion and analysis interface."""
    # ── Header Date Synchronization ───────────────────────
    if st.session_state.get("persisted_processing_date"):
        try:
            header_date = date.fromisoformat(str(st.session_state["persisted_processing_date"]))
        except Exception:
            header_date = date.today()
    elif st.session_state.get("selected_processing_date"):
        header_date = st.session_state["selected_processing_date"]
    else:
        header_date = date.today()

    header_provider = st.session_state.get(
        "provider_used",
        settings.get_effective_provider().value,
    )

    render_header(
        processing_date=header_date,
        provider_name=header_provider,
        provider_ready=settings.has_gemini_key or settings.has_groq_key,
        pipeline_status=st.session_state["pipeline_status"],
        persisted_run_id=st.session_state.get("persisted_run_id"),
        user=current_user,
    )

    # ── 1. Data Ingestion & Workspace ─────────────────────
    (
        source_file,
        target_file,
        processing_date,
        snapshot_mode,
        delete_policy,
        sample_clicked,
        clear_sample_clicked,
    ) = render_input_workspace(
        settings=settings,
        provider_options=provider_options,
        provider_labels=provider_labels,
        default_provider_idx=default_idx,
        is_sample_active=st.session_state.get("is_sample_active", False),
        sample_file_names=("source_today.csv", "target_yesterday.csv"),
    )

    st.session_state["selected_processing_date"] = processing_date

    # Handle sample data toggles
    if sample_clicked:
        st.session_state["is_sample_active"] = True
        st.session_state["pipeline_status"] = "idle"
        st.session_state["persisted_run_id"] = None
        st.session_state["persisted_processing_date"] = None
        st.rerun()

    if clear_sample_clicked:
        st.session_state["is_sample_active"] = False
        for key in _defaults:
            st.session_state[key] = _defaults[key]
        st.rerun()

    # Resolve data inputs (uploaded files vs built-in sample demo)
    source_input = None
    target_input = None
    source_name = "—"
    target_name = "—"

    if source_file is not None and target_file is not None:
        source_input = source_file
        target_input = target_file
        source_name = source_file.name
        target_name = target_file.name
        st.session_state["is_sample_active"] = False
    elif st.session_state.get("is_sample_active"):
        sample_src = _project_root / "sample-data" / "source_today.csv"
        sample_tgt = _project_root / "sample-data" / "target_yesterday.csv"
        if sample_src.is_file() and sample_tgt.is_file():
            source_input = sample_src
            target_input = sample_tgt
            source_name = "source_today.csv"
            target_name = "target_yesterday.csv"

    # ── 2. Comparison Rules & Primary Action ───────────────
    if source_input is not None and target_input is not None:
        try:
            source_df = load_csv(source_input, dataset_name="source")
            target_df = load_csv(target_input, dataset_name="target")
            st.session_state["source_df"] = source_df
            st.session_state["target_df"] = target_df
        except Exception as e:
            st.error(f"Error loading CSV inputs: {e}")
            st.stop()

        # Validate compatibility
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

        # Render comparison rules
        business_key, tracked_columns = render_schema_detection(
            source_df, business_key, tracked_columns
        )
        tracked_columns = detect_tracked_columns(source_df, business_key)

        # Render primary action CTA and advanced settings
        (
            run_clicked,
            reset_clicked,
            exec_mode,
            llm_choice,
            force_recompute,
        ) = render_run_controls(
            settings=settings,
            provider_options=provider_options,
            provider_labels=provider_labels,
            default_provider_idx=default_idx,
        )

        if reset_clicked:
            for key in _defaults:
                st.session_state[key] = _defaults[key]
            st.rerun()

        # ── 3. Pipeline Execution ─────────────────────────
        if run_clicked:
            st.session_state["pipeline_status"] = "running"
            t_start = time.perf_counter()

            try:
                settings.llm_provider = LLMProvider(llm_choice)

                if exec_mode == "Prefect Deployment (Background Runner)":
                    configure_prefect_api_url()
                    ts = int(time.time())
                    staged_src = stage_dataset(source_df, f"staged_source_{ts}")
                    staged_tgt = stage_dataset(target_df, f"staged_target_{ts}")
                    apply_deployment()
                    params = DeploymentParameters(
                        source=staged_src,
                        target=staged_tgt,
                        processing_date=str(processing_date),
                        business_key_override=business_key,
                        tracked_columns_override=tracked_columns,
                        snapshot_mode=snapshot_mode,
                        delete_policy=delete_policy,
                        llm_provider=llm_choice,
                        force_recompute=force_recompute,
                    )
                    flow_run = trigger_pipeline_run(params, timeout=0)
                    flow_run_id = str(flow_run.id)
                    flow_run_name = flow_run.name

                    st.session_state["deployed_run_info"] = {
                        "flow_run_id": flow_run_id,
                        "flow_run_name": flow_run_name,
                        "state": flow_run.state.name,
                        "deployment": FULL_DEPLOYMENT_NAME,
                    }

                    status_box = st.status(
                        f"Prefect Deployment: Flow run '{flow_run_name}' submitted...",
                        expanded=True,
                    )
                    with status_box:
                        st.write(
                            f":material/schedule: Submitted flow run **{flow_run_name}** (`{flow_run_id}`). "
                            f"Initial state: **{flow_run.state.name}**"
                        )

                        last_reported = [flow_run.state.name]
                        def _on_status_change(state_name: str, msg: Optional[str]) -> None:
                            if state_name != last_reported[0]:
                                last_reported[0] = state_name
                                if state_name in ("Scheduled", "Pending"):
                                    st.write(f":material/schedule: State: **{state_name}** (waiting for runner)...")
                                elif state_name == "Running":
                                    st.write(f":material/sync: State: **Running** (executing vectorized SCD2 transformation)...")
                                elif state_name == "Completed":
                                    st.write(f":material/check_circle: State: **Completed**! Loading persisted artifacts...")
                                elif state_name in ("Failed", "Cancelled", "Crashed"):
                                    st.write(f":material/error: State: **{state_name}** ({msg or 'No details'}).")

                        term_state, err_msg, persisted = poll_deployment_run_state(
                            flow_run_id=flow_run_id,
                            timeout=120.0,
                            poll_interval=1.5,
                            status_callback=_on_status_change,
                        )

                    if term_state == "Completed" and persisted is not None:
                        status_box.update(
                            label=f"Flow run '{flow_run_name}' completed successfully!",
                            state="complete",
                            expanded=False,
                        )
                        populate_session_state_from_persisted_run(
                            persisted=persisted,
                            source_df=source_df,
                            target_df=target_df,
                            source_file_name=source_name,
                            target_file_name=target_name,
                        )
                        st.session_state["deployed_run_info"]["state"] = "Completed"
                        st.rerun()

                    elif term_state == "Failed":
                        status_box.update(
                            label=f"Flow run '{flow_run_name}' failed!",
                            state="error",
                            expanded=True,
                        )
                        st.session_state["pipeline_status"] = "error"
                        st.session_state["deployed_run_info"]["state"] = "Failed"
                        st.session_state["error_message"] = err_msg or "Flow run failed"
                        st.error(f"Deployment Flow Run '{flow_run_name}' Failed: {err_msg}")

                    elif term_state in ("Cancelled", "Crashed"):
                        status_box.update(
                            label=f"Flow run '{flow_run_name}' {term_state.lower()}!",
                            state="error",
                            expanded=True,
                        )
                        st.session_state["pipeline_status"] = "cancelled"
                        st.session_state["deployed_run_info"]["state"] = term_state
                        st.session_state["error_message"] = err_msg or f"Flow run was {term_state.lower()}"
                        st.warning(f"Deployment Flow Run '{flow_run_name}' {term_state}: {err_msg}")

                    else:
                        status_box.update(
                            label=f"Flow run '{flow_run_name}' is {term_state} (waiting for runner)",
                            state="running",
                            expanded=True,
                        )
                        st.session_state["pipeline_status"] = "deployed"
                        st.session_state["deployed_run_info"]["state"] = term_state
                        st.info(
                            f"Flow Run **{flow_run_name}** is currently **{term_state}**. "
                            f"Ensure local runner is running with `python -m src.scd2_copilot.deployment --serve`. "
                            f"Click **Check Status** below to refresh."
                        )

                else:
                    # Interactive In-Process Execution through Prefect Orchestration Boundary
                    with st.spinner("Analyzing changes and generating SCD2 history…"):
                        user_audit = current_user.to_audit_dict() if current_user else None
                        result = run_pipeline(
                            source=source_df,
                            target=target_df,
                            processing_date=processing_date,
                            business_key_override=business_key,
                            tracked_columns_override=tracked_columns,
                            snapshot_mode=snapshot_mode,
                            delete_policy=delete_policy,
                            settings=settings,
                            force_recompute=force_recompute,
                            created_by=user_audit,
                        )

                        st.session_state.update({
                            "pipeline_status": "completed",
                            "change_report": result.change_report,
                            "scd2_output": result.scd2_output,
                            "validation_report": result.validation_report,
                            "explain_result": result.explain_result,
                            "execution_time": result.execution_time,
                            "source_df": result.source_df,
                            "target_df": result.target_df,
                            "business_key": result.business_key,
                            "tracked_columns": result.tracked_columns,
                            "provider_used": result.provider_used,
                            "persisted_run_id": result.orchestration_summary.run_id if result.orchestration_summary else None,
                            "persisted_processing_date": processing_date,
                            "error_message": None,
                        })

                        st.session_state["run_history"].append({
                            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "source_name": source_name,
                            "target_name": target_name,
                            "provider": result.provider_used,
                            "new": result.change_report.summary["new"],
                            "changed": result.change_report.summary["changed"],
                            "unchanged": result.change_report.summary["unchanged"],
                            "deleted": result.change_report.summary["deleted"],
                            "validation_passed": result.validation_report.passed,
                            "exec_time": f"{result.execution_time:.2f}",
                            "is_reused": result.orchestration_summary.is_reused if result.orchestration_summary else False,
                            "deduplication_status": result.orchestration_summary.deduplication_status if result.orchestration_summary else "new_execution",
                            "execution_fingerprint": result.orchestration_summary.execution_fingerprint if result.orchestration_summary else None,
                            "created_by": (current_user.name or current_user.email) if current_user else "—",
                        })

                        st.rerun()

            except ContractValidationError as e:
                st.session_state["pipeline_status"] = "error"
                st.session_state["error_message"] = str(e)
                st.error("Input rejected by data contract: No SCD2 changes were applied to protect data integrity.")
                quarantine = e.quarantine_result
                if quarantine is not None:
                    st.markdown(
                        f"""
                        <div class="section-card" style="border-left: 4px solid var(--error); margin-top: 12px;">
                            <div style="font-weight: 600; color: var(--error); margin-bottom: 6px;">
                                {_icon("alert_triangle")} Quarantine Record: <code>{quarantine.quarantine_id}</code>
                            </div>
                            <div style="font-size: 0.88rem; color: var(--text-1); margin-bottom: 6px;">
                                <strong>Reason:</strong> {html_mod.escape(quarantine.reason)}
                            </div>
                            {f'<div style="font-size: 0.82rem; color: var(--text-2);"><strong>Affected columns:</strong> {", ".join(quarantine.affected_columns)}</div>' if quarantine.affected_columns else ''}
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
            except DuplicateBusinessKeyError as e:
                st.session_state["pipeline_status"] = "error"
                st.session_state["error_message"] = str(e)
                st.error(f"Data Quality Error: {e}")
            except Exception as e:
                st.session_state["pipeline_status"] = "error"
                st.session_state["error_message"] = str(e)
                st.error(f"Pipeline failed: {e}")

        # Background deployment status banner
        if st.session_state.get("pipeline_status") == "deployed":
            info = st.session_state.get("deployed_run_info") or {}
            flow_run_id = info.get("flow_run_id")
            flow_run_name = info.get("flow_run_name")

            if flow_run_id:
                try:
                    curr_state, err_msg, persisted = load_deployment_run_artifacts(flow_run_id)
                    if curr_state == "Completed" and persisted is not None:
                        populate_session_state_from_persisted_run(
                            persisted=persisted,
                            source_df=source_df if "source_df" in locals() else None,
                            target_df=target_df if "target_df" in locals() else None,
                            source_file_name=source_name,
                            target_file_name=target_name,
                        )
                        st.session_state["deployed_run_info"]["state"] = "Completed"
                        st.rerun()
                    elif curr_state == "Failed":
                        st.session_state["pipeline_status"] = "error"
                        st.session_state["deployed_run_info"]["state"] = "Failed"
                        st.session_state["error_message"] = err_msg or "Flow run failed"
                    elif curr_state in ("Cancelled", "Crashed"):
                        st.session_state["pipeline_status"] = "cancelled"
                        st.session_state["deployed_run_info"]["state"] = curr_state
                        st.session_state["error_message"] = err_msg or f"Flow run was {curr_state.lower()}"
                    else:
                        info["state"] = curr_state
                except Exception as exc:
                    logger.debug("Could not check background flow run status: %s", exc)

            if st.session_state.get("pipeline_status") == "deployed":
                col_b1, col_b2 = st.columns([5, 1])
                with col_b1:
                    st.info(
                        f"Flow Run **{flow_run_name}** is submitted to deployment **{info.get('deployment')}** "
                        f"(ID: `{flow_run_id}`, State: **{info.get('state')}**). "
                        f"Runs under concurrency limit 1 (ENQUEUE). Ensure local runner is running with `python -m src.scd2_copilot.deployment --serve`."
                    )
                with col_b2:
                    if st.button("Check Status", key="btn_check_deployed_status"):
                        st.rerun()

    # ── 4. Unified Results & Intelligence ──────────────────
    if st.session_state.get("change_report") is not None:
        st.markdown("---")
        st.markdown(
            f'<div class="section-title">{_icon("chart")} 3. Results &amp; Change Intelligence</div>',
            unsafe_allow_html=True,
        )

        cr = st.session_state["change_report"]
        so = st.session_state["scd2_output"]
        vr = st.session_state["validation_report"]
        er = st.session_state["explain_result"]
        et = st.session_state["execution_time"]
        bk = st.session_state["business_key"] or []
        tc = st.session_state["tracked_columns"] or []
        pu = st.session_state["provider_used"]
        cur_date = st.session_state.get("selected_processing_date", date.today())
        if st.session_state.get("persisted_processing_date"):
            try:
                cur_date = date.fromisoformat(str(st.session_state["persisted_processing_date"]))
            except Exception:
                pass

        # Dominant Hero Result Summary Card
        render_hero_summary(
            change_report=cr,
            validation_report=vr,
            explain_result=er,
            exec_time=et,
            provider_used=pu,
        )

        # Secondary KPI Strip
        render_kpi_strip(
            summary=cr.summary,
            validation_passed=vr.passed if vr else None,
            exec_time=et,
            provider=pu,
        )

        # 6 Focused Tabs
        tab_overview, tab_changes, tab_explain, tab_val, tab_table, tab_history = st.tabs(
            [
                "Overview",
                "What Changed?",
                "Why It Changed (AI)",
                "SCD2 Validation",
                "Updated SCD2 Table",
                "History",
            ]
        )

        with tab_overview:
            is_reused_val = False
            dedup_val = "new_execution"
            if st.session_state.get("run_history"):
                is_reused_val = st.session_state["run_history"][-1].get("is_reused", False)
                dedup_val = st.session_state["run_history"][-1].get("deduplication_status", "new_execution")

            render_overview_tab(
                change_report=cr,
                business_key=bk,
                tracked_columns=tc,
                processing_date=cur_date,
                exec_time=et,
                validation_report=vr,
                provider_used=pu,
                explain_result=er,
                deduplication_status=dedup_val,
                is_reused=is_reused_val,
            )

        with tab_changes:
            src_preview = st.session_state.get("source_df")
            tgt_preview = st.session_state.get("target_df")
            render_explorer_tab(src_preview, tgt_preview, so, cr)

        with tab_explain:
            if er:
                render_explanations_tab(er)
            else:
                st.info("No AI explanation result available.")

        with tab_val:
            render_validation_tab(vr)

        with tab_table:
            render_table_tab(so, bk)

        with tab_history:
            render_history_tab(st.session_state.get("run_history"))

        # Exports
        render_downloads(so, vr, er.explanations if er else [])

        # Advanced Diagnostics Panel
        render_advanced_panel(
            explain_result=er,
            provider_used=pu,
            exec_time=et,
            settings=settings,
            deployed_run_info=st.session_state.get("deployed_run_info"),
            fingerprint=(
                st.session_state["run_history"][-1].get("execution_fingerprint")
                if st.session_state.get("run_history")
                else None
            ),
        )

    elif source_input is None or target_input is None:
        # Empty State with Clear Guidance
        st.markdown(
            f"""
            <div class="empty-state" style="margin-top:30px;">
                <div class="empty-icon">{_icon("folder")}</div>
                <div class="empty-text">
                    Upload today's source CSV and yesterday's SCD2 table above, or click <strong>⚡ Try Sample Data</strong> to explore a complete demonstration.
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )



# ── Mode Routing ──────────────────────────────────────
if app_mode == "🚀 Customer Data Onboarding & Guardrail":
    render_customer_onboarding_app()
elif app_mode == "⚡ Live Guardrail Monitor (V2)":
    render_v2_live_monitor(current_user=current_user, settings=settings)
else:
    render_v1_batch_mode(
        current_user=current_user,
        settings=settings,
        provider_options=provider_options,
        provider_labels=provider_labels,
        default_idx=default_idx,
    )

# ── Footer ─────────────────────────────────────────────
st.divider()
st.caption(
    "SCD2 Copilot · Deterministic historical change detection · AI-assisted explanations"
)
