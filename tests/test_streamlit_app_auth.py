"""Streamlit AppTest automated tests verifying authentication gate and authenticated workspace."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

APP_PATH = Path(__file__).resolve().parent.parent / "app" / "streamlit_app.py"


def test_apptest_unauthenticated_flow():
    """Unauthenticated access stops at the login gate and does not render the SCD2 workspace."""
    # When unauthenticated, _get_user_info returns is_logged_in=False
    with patch("streamlit.user_info._get_user_info", return_value={"is_logged_in": False}):
        at = AppTest.from_file(str(APP_PATH), default_timeout=30)
        at.run()

        # Verify login gate elements are present
        button_labels = [b.label for b in at.button]
        assert "Continue with Google" in button_labels

        # Verify that protected controls (like Analyze Changes) were not rendered
        assert "Analyze Changes" not in button_labels


def test_apptest_authenticated_flow():
    """Authenticated user passes the login gate and accesses the SCD2 workspace and user badge."""
    mock_claims = {
        "is_logged_in": True,
        "email": "analyst@example.com",
        "name": "Jane Analyst",
        "sub": "google_sub_888",
    }
    with patch("streamlit.user_info._get_user_info", return_value=mock_claims):
        at = AppTest.from_file(str(APP_PATH), default_timeout=30)
        at.run()

        # Verify that login button is absent
        button_labels = [b.label for b in at.button]
        assert "Continue with Google" not in button_labels

        # Verify that protected workspace controls and logout button are rendered
        assert "Log out" in button_labels
        assert "⚡ Try Sample Data" in button_labels

        # Clicking sample data loads sample data and renders the Analyze Changes run button
        at.button(key="btn_try_sample").click().run()
        updated_buttons = [b.label for b in at.button]
        assert "Analyze Changes" in updated_buttons
