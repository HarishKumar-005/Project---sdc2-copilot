"""Streamlit AppTest automated tests verifying authentication gate and authenticated workspace."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from uuid import UUID

import pytest
from streamlit.testing.v1 import AppTest

from src.scd2_copilot.auth_supabase import SupabaseSession

APP_PATH = Path(__file__).resolve().parent.parent / "app" / "streamlit_app.py"


def test_apptest_unauthenticated_flow():
    """Unauthenticated access stops at the login gate and does not render the SCD2 workspace."""
    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    at.run()

    # Verify login gate elements are present
    element_labels = [getattr(e, "label", None) for e in at.main]
    button_labels = [b.label for b in at.button]

    # "Continue with Google" link button is present in main container
    assert any(getattr(e, "label", None) == "Continue with Google" for e in at.main)
    # Password sign-in button is present
    assert "Sign In with Password" in button_labels

    # Verify that protected controls (like Log out and Analyze Changes) were not rendered
    assert "Log out" not in button_labels
    assert "Analyze Changes" not in button_labels


def test_apptest_authenticated_flow():
    """Authenticated user with SupabaseSession passes the login gate and accesses the SCD2 workspace."""
    from src.scd2_copilot.api.client import ApiConnectionError

    at = AppTest.from_file(str(APP_PATH), default_timeout=30)
    session = SupabaseSession(
        access_token="test.jwt.token",
        user_id=UUID("12345678-1234-5678-1234-567812345678"),
        email="analyst@example.com",
        user_metadata={"full_name": "Jane Analyst"},
    )
    at.session_state["supabase_session"] = session
    at.session_state["platform_mode_selector"] = "⚡ Live Guardrail Monitor (V2)"

    with patch("src.scd2_copilot.api.client.ApiClient.check_health", side_effect=ApiConnectionError("offline")):
        at.run()

    # Verify that login gate is absent
    assert not any(getattr(e, "label", None) == "Continue with Google" for e in at.main)
    assert "Sign In with Password" not in [b.label for b in at.button]

    # Verify that protected workspace controls and logout button are rendered
    button_labels = [b.label for b in at.button]
    assert "Log out" in button_labels
    assert any("Refresh Telemetry" in b for b in button_labels)

    # Switch to Batch CSV mode to test the batch sample data flow
    at.radio(key="platform_mode_selector").set_value("📁 Batch CSV Analysis (V1)").run()
    button_labels = [b.label for b in at.button]
    assert "⚡ Try Sample Data" in button_labels

    # Clicking sample data loads sample data and renders the Analyze Changes run button
    at.button(key="btn_try_sample").click().run()
    updated_buttons = [b.label for b in at.button]
    assert "Analyze Changes" in updated_buttons

