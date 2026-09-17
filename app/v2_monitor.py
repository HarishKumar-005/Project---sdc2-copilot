"""SCD2 Copilot — V2 Live Guardrail & Operational Monitoring Interface.

Connects strictly to the headless FastAPI operational boundary via typed ApiClient.
Never opens direct database connections or executes raw SQL from Streamlit.
"""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any, Optional
from uuid import UUID

import polars as pl
import streamlit as st

from src.scd2_copilot.api.client import (
    ApiClient,
    ApiClientError,
    ApiConflictError,
    ApiConnectionError,
    ApiForbiddenError,
    ApiNotFoundError,
    ApiUnauthorizedError,
)
from src.scd2_copilot.api.schemas import (
    HoldResponse,
    OperationalMetricsResponse,
)
from src.scd2_copilot.auth import AuthenticatedUser, get_supabase_access_token
from src.scd2_copilot.config import Settings
try:
    from ui_components import render_v2_hold_card, render_v2_system_health_strip
except ImportError:
    from app.ui_components import render_v2_hold_card, render_v2_system_health_strip

logger = logging.getLogger("scd2_copilot.ui.v2_monitor")


def render_v2_live_monitor(current_user: Optional[AuthenticatedUser], settings: Settings) -> None:
    """Render the operational live monitoring interface (V2.6)."""
    st.markdown(
        """
        <div style="margin-bottom: 20px;">
            <div style="display: flex; align-items: center; gap: 10px;">
                <span style="font-size: 1.8rem;">⚡</span>
                <div>
                    <div style="font-size: 1.5rem; font-weight: 700; color: var(--text-1, #ffffff); line-height: 1.2;">
                        Operational Stream Monitor
                    </div>
                    <div style="font-size: 0.85rem; color: var(--text-2, #8b949e); margin-top: 2px;">
                        Continuous micro-batch ingestion, deterministic guardrail containment &amp; authorized recovery console
                    </div>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── Initialize ApiClient ──────────────────────────────────
    api_client = ApiClient(
        base_url=settings.api_base_url,
        token_provider=get_supabase_access_token,
        timeout=settings.api_request_timeout_seconds,
    )

    # ── Telemetry Controls (Manual & Auto-refresh) ───────────
    col_ctrl1, col_ctrl2 = st.columns([1, 4])
    with col_ctrl1:
        if st.button("🔄 Refresh Telemetry", width="stretch", type="secondary"):
            st.rerun()

    # ── API Liveness & Readiness Probing ─────────────────────
    health_status = "offline"
    db_connected = False
    error_detail = None
    metrics: Optional[OperationalMetricsResponse] = None

    try:
        health = api_client.check_health()
        health_status = health.status
    except ApiConnectionError as exc:
        health_status = "offline"
        error_detail = str(exc)
    except Exception as exc:
        health_status = "error"
        error_detail = str(exc)

    if health_status == "healthy":
        try:
            readiness = api_client.check_readiness()
            db_connected = getattr(readiness, "database_connected", getattr(readiness, "db_connected", False))
        except Exception as exc:
            logger.warning("Readiness probe check failed: %s", exc)
            db_connected = False

        try:
            metrics = api_client.get_metrics()
        except Exception as exc:
            logger.warning("Failed to fetch operational metrics: %s", exc)
            metrics = None

    # ── System Health Strip ──────────────────────────────────
    render_v2_system_health_strip(
        health_status=health_status,
        db_connected=db_connected,
        metrics=metrics,
        last_refresh_time=datetime.now(),
    )

    if health_status != "healthy":
        st.warning(
            f"⚠️ **Operational API Service Unreachable**\n\n"
            f"The monitoring dashboard could not connect to the headless API at `{settings.api_base_url}`.\n\n"
            f"Start the FastAPI service in your terminal to stream live telemetry:\n\n"
            f"```powershell\n"
            f".venv\\Scripts\\uvicorn src.scd2_copilot.api:app --host 127.0.0.1 --port 8000\n"
            f"```\n\n"
            f"*Detail: {error_detail or 'Connection refused'}*"
        )
        return

    st.markdown("<div style='height: 12px;'></div>", unsafe_allow_html=True)

    # ── Main Functional Tabs ─────────────────────────────────
    tab_holds, tab_runs, tab_history, tab_inventory, tab_config = st.tabs(
        [
            "🛡️ Containment Queue",
            "⚡ Processing Runs",
            "📜 SCD2 History Explorer",
            "📦 Operational Source Inventory",
            "⚙️ Source Configuration",
        ]
    )

    # ── Tab 1: Held Batches & Containment ─────────────────────
    with tab_holds:
        _render_containment_tab(api_client=api_client)

    # ── Tab 2: Processing Runs ───────────────────────────────
    with tab_runs:
        _render_runs_tab(api_client=api_client)

    # ── Tab 3: SCD2 History Explorer ─────────────────────────
    with tab_history:
        _render_history_tab(api_client=api_client)

    # ── Tab 4: Operational Source Inventory ───────────────────
    with tab_inventory:
        _render_inventory_tab(api_client=api_client)

    # ── Tab 5: Source Configuration (V3 Phase 1) ──────────────
    with tab_config:
        _render_source_config_tab(api_client=api_client, settings=settings)


def _render_containment_tab(api_client: ApiClient) -> None:
    """Render the active holds list, detail card, and recovery action console."""
    # ── Latest Processing Run Banner ─────────────────────────
    try:
        latest_run_resp = api_client.list_runs(limit=1)
        if latest_run_resp.runs:
            lr = latest_run_resp.runs[0]
            status_color = "#238636" if lr.status == "COMPLETED" else ("#da3633" if lr.status == "FAILED" else "#1f6feb")
            st.markdown(
                f"""
                <div style="border: 1px solid var(--border, #30363d); border-radius: 8px; padding: 10px 14px; margin-bottom: 14px; background: rgba(31, 111, 235, 0.05); display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
                    <div>
                        <span style="font-weight: 700; font-size: 0.85rem; color: #58a6ff;">⚡ LATEST PROCESSING RUN</span>
                        <span style="margin-left: 8px; font-family: monospace; font-size: 0.82rem; color: var(--text-2, #8b949e);">{str(lr.run_id)[:8]}...</span>
                    </div>
                    <div style="font-size: 0.82rem; color: var(--text-2, #8b949e); display: flex; gap: 12px; align-items: center;">
                        <span>Status: <strong style="color: {status_color};">{lr.status}</strong></span>
                        <span>Seen: <strong>{lr.records_seen}</strong></span>
                        <span>Changed: <strong>{lr.records_changed}</strong></span>
                        <span>Held: <strong style="color: {'#da3633' if lr.records_held > 0 else '#8b949e'};">{lr.records_held}</strong></span>
                        <span>Started: <strong>{str(lr.started_at)[:19] if lr.started_at else '—'}</strong></span>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
    except Exception as exc:
        logger.debug("Could not fetch latest run for containment header: %s", exc)

    col_f1, col_f2 = st.columns([1, 2])
    with col_f1:
        status_filter = st.selectbox(
            "Hold Status Filter",
            options=["HELD", "ALL", "RELEASED", "REPROCESSED", "DISCARDED"],
            index=0,
            key="v2_hold_status_filter",
        )

    fetch_status = None if status_filter == "ALL" else status_filter

    try:
        holds_resp = api_client.list_holds(limit=50, status=fetch_status)
        holds = holds_resp.holds
    except Exception as exc:
        st.error(f"Failed to fetch held batches: {exc}")
        return

    if not holds:
        st.success(
            f"No batches found with status **{status_filter}**. "
            "Deterministic guardrail containment queue is clear!"
        )
        return

    # ── Latest Detected Hold Highlight Banner ────────────────
    newest_hold = holds[0]
    sev_color = {
        "CRITICAL": "#da3633",
        "HIGH": "#e3b341",
        "MEDIUM": "#58a6ff",
        "LOW": "#8b949e",
    }.get(newest_hold.severity.upper(), "#8b949e")
    st.markdown(
        f"""
        <div style="border: 1px solid {sev_color}44; border-radius: 8px; padding: 10px 14px; margin-bottom: 14px; background: {sev_color}0d; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
            <div>
                <span style="font-weight: 700; font-size: 0.85rem; color: {sev_color};">🛡️ LATEST DETECTED HOLD</span>
                <span style="margin-left: 8px; font-family: monospace; font-size: 0.82rem;">Hold {str(newest_hold.hold_id)[:8]}...</span>
                <span style="margin-left: 8px; font-size: 0.82rem; color: var(--text-1, #ffffff); font-weight: 500;">{newest_hold.reason}</span>
            </div>
            <div style="font-size: 0.82rem; color: var(--text-2, #8b949e);">
                <span>[{newest_hold.status}]</span>
                <span style="margin-left: 6px;">{newest_hold.records_affected} recs</span>
                <span style="margin-left: 6px;">{str(newest_hold.created_at)[:19]}</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(f"**Found {len(holds)} batch(es)** matching filter:")

    # Map options uniquely by hold_id to prevent key collisions
    hold_id_map: dict[str, HoldResponse] = {str(h.hold_id): h for h in holds}
    newest_hold_id_str = str(holds[0].hold_id)

    # Auto-sync selection if a fresh newest hold appeared since last render
    last_newest = st.session_state.get("v2_last_seen_newest_hold_id")
    if last_newest != newest_hold_id_str:
        st.session_state["v2_last_seen_newest_hold_id"] = newest_hold_id_str
        st.session_state["v2_selected_hold_id"] = newest_hold_id_str

    # If current selected hold is not in the filtered options, reset to newest
    if st.session_state.get("v2_selected_hold_id") not in hold_id_map:
        st.session_state["v2_selected_hold_id"] = newest_hold_id_str

    def _format_hold_option(hid: str) -> str:
        h = hold_id_map[hid]
        is_latest_tag = " (LATEST)" if hid == newest_hold_id_str else ""
        return (
            f"Hold {hid[:8]}...{is_latest_tag} | [{h.status}] {h.severity} — "
            f"{h.reason} ({h.records_affected} recs, {str(h.created_at)[:19]})"
        )

    selected_hold_id = st.selectbox(
        "Select Batch to Inspect & Recover",
        options=list(hold_id_map.keys()),
        format_func=_format_hold_option,
        key="v2_selected_hold_id",
    )
    selected_hold = hold_id_map[selected_hold_id]

    # Display flash message from previous action if present
    if "v2_flash_message" in st.session_state:
        flash_msg, flash_type = st.session_state.pop("v2_flash_message")
        if flash_type == "success":
            st.success(flash_msg)
        elif flash_type == "warning":
            st.warning(flash_msg)
        elif flash_type == "error":
            st.error(flash_msg)

    # Define Recovery Action Callbacks
    def on_release(hold_id: UUID, reason: Optional[str]) -> None:
        try:
            res = api_client.release_hold(hold_id=hold_id, operator_reason=reason)
            st.session_state["v2_flash_message"] = (
                f"✅ **Hold Released Successfully!** Committed {res.records_affected} records "
                f"downstream. Watermark advanced to {res.checkpoint_advanced_to or 'now'}.",
                "success",
            )
            st.rerun()
        except ApiUnauthorizedError:
            st.error("Authentication required. Please sign in with authorized operator credentials.")
        except ApiForbiddenError:
            st.error("Access Forbidden: Your user account is not authorized for operational recovery actions.")
        except ApiConflictError as exc:
            st.warning(f"State Conflict: {exc.message}")
        except Exception as exc:
            st.error(f"Release failed: {exc}")

    def on_reprocess(hold_id: UUID, force: bool) -> None:
        try:
            res = api_client.reprocess_hold(hold_id=hold_id, force_normal=force)
            st.session_state["v2_flash_message"] = (
                f"🔄 **Hold Reprocessed Successfully!** Status: {res.status}. "
                f"Records committed: {res.records_affected}.",
                "success",
            )
            st.rerun()
        except ApiUnauthorizedError:
            st.error("Authentication required. Please sign in with authorized operator credentials.")
        except ApiForbiddenError:
            st.error("Access Forbidden: Your user account is not authorized for operational recovery actions.")
        except ApiConflictError as exc:
            st.warning(f"State Conflict: {exc.message}")
        except Exception as exc:
            st.error(f"Reprocess failed: {exc}")

    def on_discard(hold_id: UUID, reason: Optional[str], advance: bool) -> None:
        try:
            res = api_client.discard_hold(
                hold_id=hold_id,
                operator_reason=reason,
                advance_checkpoint=advance,
            )
            st.session_state["v2_flash_message"] = (
                f"🗑️ **Hold Discarded Successfully!** Status: {res.status}. "
                f"Checkpoint advanced: {advance}.",
                "success",
            )
            st.rerun()
        except ApiUnauthorizedError:
            st.error("Authentication required. Please sign in with authorized operator credentials.")
        except ApiForbiddenError:
            st.error("Access Forbidden: Your user account is not authorized for operational recovery actions.")
        except ApiConflictError as exc:
            st.warning(f"State Conflict: {exc.message}")
        except Exception as exc:
            st.error(f"Discard failed: {exc}")

    render_v2_hold_card(
        hold=selected_hold,
        on_release_callback=on_release,
        on_reprocess_callback=on_reprocess,
        on_discard_callback=on_discard,
    )


def _render_runs_tab(api_client: ApiClient) -> None:
    """Render the stream processing micro-batch audit runs."""
    col_l1, col_l2 = st.columns([1, 3])
    with col_l1:
        run_limit = st.selectbox("Max Runs to Display", [10, 20, 50, 100], index=1, key="v2_run_limit")

    try:
        runs_resp = api_client.list_runs(limit=run_limit)
        runs = runs_resp.runs
    except Exception as exc:
        st.error(f"Failed to fetch runs: {exc}")
        return

    if not runs:
        st.info("No processing runs recorded yet.")
        return

    # ── Latest Processing Run Highlight Banner ────────────────
    latest_run = runs[0]
    status_color = "#238636" if latest_run.status == "COMPLETED" else ("#da3633" if latest_run.status == "FAILED" else "#1f6feb")
    st.markdown(
        f"""
        <div style="border: 1px solid var(--border, #30363d); border-radius: 8px; padding: 10px 14px; margin-bottom: 14px; background: rgba(31, 111, 235, 0.05); display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
            <div>
                <span style="font-weight: 700; font-size: 0.85rem; color: #58a6ff;">⚡ LATEST PROCESSING RUN</span>
                <span style="margin-left: 8px; font-family: monospace; font-size: 0.82rem; color: var(--text-2, #8b949e);">{str(latest_run.run_id)}</span>
            </div>
            <div style="font-size: 0.82rem; color: var(--text-2, #8b949e); display: flex; gap: 12px; align-items: center;">
                <span>Status: <strong style="color: {status_color};">{latest_run.status}</strong></span>
                <span>Seen: <strong>{latest_run.records_seen}</strong></span>
                <span>Changed: <strong>{latest_run.records_changed}</strong></span>
                <span>Held: <strong style="color: {'#da3633' if latest_run.records_held > 0 else '#8b949e'};">{latest_run.records_held}</strong></span>
                <span>Started: <strong>{str(latest_run.started_at)[:19] if latest_run.started_at else '—'}</strong></span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Build summary table
    table_data = []
    for r in runs:
        table_data.append({
            "Run ID": str(r.run_id)[:8] + "...",
            "Status": r.status,
            "Records Seen": r.records_seen,
            "Records Changed": r.records_changed,
            "Records Held": r.records_held,
            "Started": str(r.started_at)[:19] if r.started_at else "—",
            "Completed": str(r.completed_at)[:19] if r.completed_at else "—",
            "Full Run ID": str(r.run_id),
        })

    df = pl.DataFrame(table_data)
    st.dataframe(df.drop("Full Run ID"), width="stretch")

    with st.expander("🔍 Inspect Full Run Metadata", expanded=False):
        selected_run_id_str = st.selectbox(
            "Select Run ID",
            options=[str(r.run_id) for r in runs],
            key="v2_inspect_run_id",
        )
        selected_run = next((r for r in runs if str(r.run_id) == selected_run_id_str), runs[0])
        st.json(selected_run.model_dump(mode="json"))


def _render_history_tab(api_client: ApiClient) -> None:
    """Render the interactive SCD2 History lookup by business key."""
    st.markdown("#### Point-in-Time SCD2 History Lookup")
    st.caption(
        "Search chronological versions for an entity. Validity intervals follow half-open "
        "`[effective_from, effective_to)` semantics. An active record has `is_current = True` "
        "and `effective_to = None`."
    )

    try:
        monitors_resp = api_client.list_monitors()
        monitors = monitors_resp.monitors
    except Exception as exc:
        logger.warning("Could not list monitors for history tab: %s", exc)
        monitors = []

    if not monitors:
        # Fallback to default inventory search
        _render_inventory_history_lookup(api_client)
        return

    # If only 1 monitor and it is the canonical inventory monitor
    default_mon = monitors[0]
    if len(monitors) == 1 and default_mon.name in ("inventory", "default"):
        _render_inventory_history_lookup(api_client)
        return

    # Multiple monitors or non-inventory active monitor: provide selector
    mon_names = [m.name for m in monitors]
    selected_name = st.selectbox(
        "Select Active Monitor",
        options=mon_names,
        index=0,
        key="v2_hist_mon_selector",
    )
    selected_mon = next((m for m in monitors if m.name == selected_name), default_mon)

    if selected_mon.name in ("inventory", "default") and set(selected_mon.keys) == {"sku_id", "warehouse_id"}:
        _render_inventory_history_lookup(api_client)
    else:
        _render_generic_history_lookup(api_client, selected_mon)


def _render_inventory_history_lookup(api_client: ApiClient) -> None:
    """Render the canonical inventory-specific SKU / Warehouse lookup."""
    col1, col2, col3 = st.columns([2, 2, 1])
    with col1:
        sku_input = st.text_input("SKU ID", value="SKU-1001", key="v2_hist_sku")
    with col2:
        wh_input = st.text_input("Warehouse ID", value="WH-01", key="v2_hist_wh")
    with col3:
        st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
        search_clicked = st.button("Search History", width="stretch", type="primary", key="v2_hist_inv_search")

    if search_clicked or (sku_input and wh_input):
        try:
            with st.spinner("Searching SCD2 history..."):
                history_resp = api_client.get_history(sku_id=sku_input.strip(), warehouse_id=wh_input.strip())
            versions = history_resp.versions

            if not versions:
                st.info(f"No SCD2 history found for business key `({sku_input}, {wh_input})`.")
                return

            st.markdown(f"**Found {len(versions)} chronological version(s):**")

            rows = []
            for idx, v in enumerate(versions, start=1):
                rows.append({
                    "Ver #": idx,
                    "Current?": "🟢 ACTIVE" if v.is_current else "⚪ Historical",
                    "Qty on Hand": v.quantity_on_hand,
                    "Reorder Level": v.reorder_level,
                    "Status": v.status,
                    "Effective From (inc)": str(v.effective_from)[:19],
                    "Effective To (exc)": str(v.effective_to)[:19] if v.effective_to else "OPEN (Current)",
                })

            hist_df = pl.DataFrame(rows)
            st.dataframe(hist_df, width="stretch")

        except Exception as exc:
            st.error(f"Error querying history: {exc}")


def _render_generic_history_lookup(api_client: ApiClient, monitor_config: Any) -> None:
    """Render dynamic structured input fields per business-key column from monitor config."""
    business_keys = getattr(monitor_config, "keys", [])
    if not business_keys:
        st.warning("No business keys defined for the selected monitor.")
        return

    st.markdown(f"##### Entity History: `{monitor_config.name}`")
    table_desc = f"{getattr(monitor_config.source, 'schema_name', getattr(monitor_config.source, 'schema', 'public'))}.{monitor_config.source.table_name}"
    st.caption(f"Source table: `{table_desc}`")

    # One structured input field per business key column
    num_cols = len(business_keys)
    cols = st.columns(num_cols + 1)
    key_values: dict[str, str] = {}

    for idx, k in enumerate(business_keys):
        with cols[idx]:
            val = st.text_input(f"Key: {k}", key=f"v2_gen_hist_{monitor_config.name}_{k}")
            key_values[k] = val

    with cols[-1]:
        st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
        search_clicked = st.button("Search History", width="stretch", type="primary", key=f"v2_gen_hist_btn_{monitor_config.name}")

    if search_clicked:
        missing_keys = [k for k, v in key_values.items() if not v or not v.strip()]
        if missing_keys:
            st.warning(f"Please provide all required business keys: {', '.join(missing_keys)}")
            return

        clean_key = {k: v.strip() for k, v in key_values.items()}
        try:
            with st.spinner("Searching entity history..."):
                resp = api_client.get_entity_history(source_name=monitor_config.name, entity_key=clean_key)
            versions = resp.versions

            if not versions:
                st.info(f"No SCD2 history found for entity key `{clean_key}`.")
                return

            st.markdown(f"**Found {len(versions)} chronological version(s):**")

            rows = []
            for idx, v in enumerate(versions, start=1):
                row_dict: dict[str, Any] = {
                    "Ver #": idx,
                    "Current?": "🟢 ACTIVE" if v.is_current else "⚪ Historical",
                }
                # Add key columns
                for k, kv in v.entity_key.items():
                    row_dict[k] = kv
                # Add tracked attributes
                for a, av in v.attributes.items():
                    row_dict[a] = av
                row_dict["Effective From (inc)"] = str(v.effective_from)[:19]
                row_dict["Effective To (exc)"] = str(v.effective_to)[:19] if v.effective_to else "OPEN (Current)"
                rows.append(row_dict)

            hist_df = pl.DataFrame(rows)
            st.dataframe(hist_df, width="stretch")

        except Exception as exc:
            st.error(f"Error querying generic entity history: {exc}")


def _render_inventory_tab(api_client: ApiClient) -> None:
    """Render real-time snapshot of the upstream inventory_source table."""
    st.markdown("#### Upstream Source Inventory (`inventory_source`)")
    st.caption(
        "Live view of the source records currently residing in Supabase PostgreSQL before incremental ingestion."
    )

    try:
        inv_resp = api_client.list_inventory(limit=100)
        records = inv_resp.records
    except Exception as exc:
        st.error(f"Failed to fetch source inventory: {exc}")
        return

    if not records:
        st.info("No records in source inventory.")
        return

    rows = []
    for r in records:
        rows.append({
            "SKU ID": r.sku_id,
            "Warehouse ID": r.warehouse_id,
            "Qty on Hand": r.quantity_on_hand,
            "Reorder Level": r.reorder_level,
            "Status": r.status,
            "Updated At": str(r.updated_at)[:19] if r.updated_at else "—",
        })

    df = pl.DataFrame(rows)
    st.dataframe(df, width="stretch")


def _render_source_config_tab(api_client: ApiClient, settings: Settings) -> None:
    """Render the active monitor configuration and provide on-demand schema validation."""
    st.markdown("#### ⚙️ PostgreSQL Monitor Configuration")
    st.caption(
        "Active source configuration defining the monitored PostgreSQL table, "
        "business keys, change timestamp, and tracked attributes for incremental SCD2 processing."
    )

    # 1. Fetch monitor configuration
    monitor_name = "warehouse_inventory"
    schema_name = "public"
    table_name = settings.ingestion_table_name or "inventory_source"
    business_keys = ["sku_id", "warehouse_id"]
    change_ts = "updated_at"
    tracked_cols = ["quantity_on_hand", "reorder_level", "status"]

    try:
        cfg_resp = api_client.get_monitor("default")
        monitor_name = cfg_resp.name
        schema_name = cfg_resp.source.schema_name
        table_name = cfg_resp.source.table_name
        business_keys = cfg_resp.business_keys
        change_ts = cfg_resp.change_timestamp.column
        tracked_cols = cfg_resp.tracked_columns
    except Exception as exc:
        logger.debug("Failed to fetch monitor config via API: %s", exc)

    # 2. Display Configuration Cards
    col_c1, col_c2 = st.columns(2)
    with col_c1:
        st.markdown(
            f"""
            <div style="background: var(--bg-card, #161b22); padding: 14px 18px; border-radius: 8px; border: 1px solid var(--border-color, #30363d); margin-bottom: 12px;">
                <div style="font-size: 0.75rem; text-transform: uppercase; color: var(--text-2, #8b949e); font-weight: 600;">Monitor Identifier</div>
                <div style="font-size: 1.15rem; font-weight: 700; color: #58a6ff; margin-top: 4px;"><code>{monitor_name}</code></div>
                <div style="margin-top: 10px; font-size: 0.75rem; text-transform: uppercase; color: var(--text-2, #8b949e); font-weight: 600;">Source Location</div>
                <div style="font-size: 1.0rem; font-weight: 600; color: var(--text-1, #c9d1d9); margin-top: 2px;">
                    PostgreSQL ➔ <code>{schema_name}.{table_name}</code>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with col_c2:
        st.markdown(
            f"""
            <div style="background: var(--bg-card, #161b22); padding: 14px 18px; border-radius: 8px; border: 1px solid var(--border-color, #30363d); margin-bottom: 12px;">
                <div style="font-size: 0.75rem; text-transform: uppercase; color: var(--text-2, #8b949e); font-weight: 600;">Change Timestamp Watermark</div>
                <div style="font-size: 1.05rem; font-weight: 600; color: #d29922; margin-top: 4px;"><code>{change_ts}</code></div>
                <div style="margin-top: 10px; font-size: 0.75rem; text-transform: uppercase; color: var(--text-2, #8b949e); font-weight: 600;">Business Keys</div>
                <div style="font-size: 0.95rem; font-weight: 600; color: #3fb950; margin-top: 2px;">
                    <code>{', '.join(business_keys)}</code>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.markdown("**Tracked Attributes (SCD2 Version-Triggering Columns)**:")
    st.code(", ".join(tracked_cols), language="text")

    st.markdown("<div style='height: 10px;'></div>", unsafe_allow_html=True)

    # 3. Interactive Validation Action
    col_v1, col_v2 = st.columns([1, 3])
    with col_v1:
        validate_clicked = st.button("🔍 Validate Source Configuration", type="primary", width="stretch")

    if validate_clicked:
        with st.spinner("Validating source against PostgreSQL database..."):
            try:
                val_resp = api_client.validate_monitor_config()
                if val_resp.is_valid:
                    st.success("✅ **Source Configuration is Valid & Reachable!**")
                    if val_resp.warnings:
                        for w in val_resp.warnings:
                            st.warning(f"⚠️ {w}")

                    if val_resp.discovered_columns:
                        st.markdown("**Discovered Table Schema in PostgreSQL:**")
                        col_rows = []
                        for col_name, c_meta in val_resp.discovered_columns.items():
                            col_rows.append({
                                "Column Name": col_name,
                                "Data Type": c_meta.data_type,
                                "Nullable": "YES" if c_meta.is_nullable else "NO",
                                "Position": c_meta.ordinal_position,
                            })
                        st.dataframe(pl.DataFrame(col_rows), width="stretch")

                    if val_resp.primary_keys:
                        st.caption(f"Discovered Primary Key(s): `{', '.join(val_resp.primary_keys)}`")
                else:
                    st.error(f"❌ **Source Configuration Validation Failed** ({len(val_resp.errors)} error(s)):")
                    for err in val_resp.errors:
                        st.markdown(f"- 🔴 {err}")
            except Exception as exc:
                st.error(f"Validation request failed: {exc}")


