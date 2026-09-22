"""New Customer Onboarding view supporting manual CSV upload, REST API ingestion, and statistical profiling."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional
import polars as pl
import streamlit as st

from ..components import (
    render_metric_card,
    render_onboarding_header,
    render_status_badge,
)
from ..state import AVAILABLE_FIXTURES, OnboardingUIState


def render_new_onboarding_view(state: OnboardingUIState) -> None:
    """Render the primary entry point for starting a new customer data onboarding workflow."""
    render_onboarding_header(
        title="Start New Onboarding",
        subtitle="Ingest heterogeneous customer data from CSV uploads or REST endpoints, discover schemas, and perform statistical profiling with PII masking.",
    )

    # ── STEP 1: Select Source Type ──────────────────────────────
    st.markdown("### 📥 Step 1 — Select Source Type & Ingest Data")
    source_type_tab, rest_type_tab = st.tabs([
        "📁 CSV File Upload (Drag & Drop)",
        "🌐 Live REST API Source",
    ])

    with source_type_tab:
        st.markdown(
            "Upload any comma-separated values (CSV) customer export. "
            "The engine discovers delimiters, infers column datatypes, and computes an immutable SHA-256 schema hash."
        )
        uploaded_file = st.file_uploader(
            "Upload Customer CSV",
            type=["csv"],
            key="new_onb_csv_uploader",
            help="Select or drag and drop a customer export CSV file.",
        )
        if uploaded_file is not None:
            file_sig = f"{uploaded_file.name}_{uploaded_file.size}"
            if st.session_state.get("onb_custom_file_sig") != file_sig:
                bytes_data = uploaded_file.getvalue()
                with st.spinner(f"Parsing and discovering schema for `{uploaded_file.name}`..."):
                    try:
                        df, schema = state.load_custom_csv(bytes_data, uploaded_file.name)
                        st.session_state["onb_custom_file_sig"] = file_sig
                        st.session_state["onb_custom_filename"] = uploaded_file.name
                        st.success(f"✓ Ingested `{uploaded_file.name}` ({df.height:,} rows, {len(schema.columns)} columns)")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Failed to ingest CSV `{uploaded_file.name}`: {exc}")
            else:
                c_col1, c_col2 = st.columns([3, 1])
                with c_col1:
                    st.info(f"Active uploaded file: `{uploaded_file.name}` ({uploaded_file.size:,} bytes)")
                with c_col2:
                    if st.button("🔄 Re-read CSV", key="btn_reingest_csv", use_container_width=True):
                        st.session_state.pop("onb_custom_file_sig", None)
                        st.rerun()

    with rest_type_tab:
        st.markdown(
            "Connect to an operational HTTP/REST customer endpoint. "
            "Supports standard JSON arrays or nested payload keys (`data`, `records`, `results`, `customers`)."
        )
        r_col1, r_col2 = st.columns(2)
        with r_col1:
            base_url = st.text_input("Base URL", value="http://localhost:8000", help="Root host and port of the REST service.")
            endpoint = st.text_input("Endpoint Path", value="api/v1/customers", help="Resource endpoint path.")
            auth_header = st.text_input("Authorization Header (Optional)", value="", type="password", help="Bearer token or API key header.")

        with r_col2:
            paginated = st.checkbox("Enable Pagination", value=False, help="Iterate across pages until max records or empty page.")
            page_size = st.number_input("Page Size", min_value=10, max_value=1000, value=100)
            max_records = st.number_input("Max Records to Ingest", min_value=100, max_value=50000, value=5000)

        if st.button("🔗 Connect & Ingest REST Endpoint", type="primary", key="btn_connect_rest"):
            headers = {"Authorization": auth_header} if auth_header.strip() else {}
            try:
                with st.spinner(f"Connecting to {base_url.rstrip('/')}/{endpoint.lstrip('/')}..."):
                    df, schema = state.load_rest_source(
                        base_url=base_url,
                        endpoint=endpoint,
                        headers=headers,
                        paginated=paginated,
                        page_size=int(page_size),
                        max_records=int(max_records),
                    )
                    st.session_state.pop("onb_custom_filename", None)
                    st.session_state.pop("onb_custom_file_sig", None)
                    st.success(f"✓ Successfully fetched {df.height:,} records from REST endpoint!")
                    st.rerun()
            except Exception as exc:
                st.error(f"Failed to ingest from REST source: {exc}")

    # ── Secondary: Optional Example Sources ─────────────────────
    with st.expander("Need an example source? (Optional sample inputs for quick inspection)", expanded=False):
        st.caption("Quickly test the pipeline using authoritative example customer feeds:")
        ex_col1, ex_col2, ex_col3 = st.columns(3)
        with ex_col1:
            if st.button("Load CRM Example Feed", key="btn_ex_crm", use_container_width=True):
                state.load_fixture("CRM Customers")
                st.session_state.pop("onb_custom_filename", None)
                st.session_state.pop("onb_custom_file_sig", None)
                st.rerun()
        with ex_col2:
            if st.button("Load Billing Example Feed", key="btn_ex_billing", use_container_width=True):
                state.load_fixture("Billing Accounts")
                st.session_state.pop("onb_custom_filename", None)
                st.session_state.pop("onb_custom_file_sig", None)
                st.rerun()
        with ex_col3:
            if st.button("Load Support Example Feed", key="btn_ex_support", use_container_width=True):
                state.load_fixture("Support Tickets")
                st.session_state.pop("onb_custom_filename", None)
                st.session_state.pop("onb_custom_file_sig", None)
                st.rerun()

    df = st.session_state.get("onb_raw_df")
    schema = st.session_state.get("onb_source_schema")

    if df is None or schema is None:
        st.markdown("<div style='height: 16px;'></div>", unsafe_allow_html=True)
        st.info("👆 **Get Started:** Upload a customer CSV file or connect a REST source above to begin schema discovery and profiling.")
        return

    st.markdown("<div style='height: 12px;'></div>", unsafe_allow_html=True)

    # ── STEP 2: Source Validation & Schema Metadata ─────────────
    st.markdown("### 📋 Step 2 — Source Schema & Cryptographic Fingerprint")
    m_col1, m_col2, m_col3, m_col4 = st.columns(4)

    render_metric_card(m_col1, "Total Records", f"{df.height:,}", "Ingested records", "info")
    render_metric_card(m_col2, "Attributes Detected", len(schema.columns), "Source columns", "info")
    render_metric_card(m_col3, "Schema Version", f"v{schema.schema_version}", "Autodetected", "neutral")
    render_metric_card(m_col4, "SHA-256 Fingerprint", schema.schema_fingerprint[:10] + "...", "Cryptographic seal", "success")

    # Data Preview (first 5 rows)
    with st.expander("Preview Ingested Source Records (First 5 Rows)", expanded=False):
        st.dataframe(df.head(5).to_pandas(), use_container_width=True)

    st.markdown("<div style='height: 12px;'></div>", unsafe_allow_html=True)

    # ── STEP 3: Statistical Data Profiling ──────────────────────
    st.markdown("### 🔍 Step 3 — Statistical Data Profiler")
    st.markdown(
        "Execute native Polars statistical profiling. "
        "Calculates exact null ratios, distinct cardinality, string length distributions, and date observations, suppressing PII."
    )

    btn_col, _ = st.columns([1.5, 3.5])
    with btn_col:
        profile_btn = st.button("▶ Run Statistical Profiler", type="primary", use_container_width=True)

    if profile_btn or st.session_state.get("onb_profile") is not None:
        if profile_btn or st.session_state.get("onb_profile") is None:
            with st.spinner("Computing deterministic statistics with Polars expressions..."):
                profile = state.profile_active_source()
        else:
            profile = st.session_state["onb_profile"]

        # Profiling Table
        prof_rows = []
        for col in profile.columns:
            null_pct = f"{(col.null_count / profile.total_rows * 100):.1f}%" if profile.total_rows else "0.0%"
            uniq_pct = f"{(col.distinct_count / profile.total_rows * 100):.1f}%" if profile.total_rows else "0.0%"
            col_samples = getattr(col, "samples", getattr(col, "sample_values", []))
            samples_str = ", ".join(f"'{s}'" for s in col_samples[:3]) if col_samples else "—"
            inf_type = col.inferred_type.value if hasattr(col.inferred_type, "value") else str(col.inferred_type)

            prof_rows.append({
                "Column Name": col.column_name,
                "Inferred Type": inf_type,
                "Null Count": f"{col.null_count:,} ({null_pct})",
                "Distinct Values": f"{col.distinct_count:,} ({uniq_pct})",
                "Min Length": col.min_length if col.min_length is not None else "—",
                "Max Length": col.max_length if col.max_length is not None else "—",
                "Masked Samples": samples_str,
            })

        st.dataframe(prof_rows, use_container_width=True)
        st.caption("🔒 **Privacy Guarantee:** PII fields (emails, phones, personal names) are automatically masked before presentation.")

    # ── STEP 4: Continue ────────────────────────────────────────
    st.markdown("---")
    n_col1, n_col2 = st.columns([3, 1])
    with n_col1:
        st.markdown("**Next Step:** Review AI-proposed semantic mappings, apply explicit overrides, and sign off the immutable version.")
    with n_col2:
        if st.button("Continue to Mapping Review ➔", type="primary", use_container_width=True):
            st.session_state["onb_nav_selection"] = "3. Mapping Review"
            st.rerun()
