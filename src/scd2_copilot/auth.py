"""Native Streamlit Google OpenID Connect (OIDC) authentication service.

Provides:
- Immutable AuthenticatedUser identity model for audit and ownership tracking
- Safe extraction of authenticated claims from st.user
- Graceful configuration verification without exposing secrets
- Streamlit native login / logout triggers (st.login, st.logout)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import logging
from typing import Any, Optional
import warnings

try:
    from authlib.deprecate import AuthlibDeprecationWarning
    warnings.filterwarnings("ignore", category=AuthlibDeprecationWarning)
except ImportError:
    pass

import streamlit as st

logger = logging.getLogger("scd2_copilot.auth")


@dataclass(frozen=True)
class AuthenticatedUser:
    """Immutable identity of an authenticated user.

    Stores strictly minimal identity claims needed for audit and ownership.
    Deliberately excludes tokens, passwords, cookies, and raw Google profile payloads.
    """

    provider: str = "google"
    subject: str = ""
    email: Optional[str] = None
    name: Optional[str] = None

    def to_audit_dict(self) -> dict[str, Any]:
        """Return JSON-serializable dictionary for audit metadata in run artifacts."""
        return {
            "provider": self.provider,
            "subject": self.subject,
            "email": self.email,
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> Optional[AuthenticatedUser]:
        """Construct AuthenticatedUser from dictionary representation."""
        if not data or not isinstance(data, dict):
            return None
        return cls(
            provider=data.get("provider", "google"),
            subject=str(data.get("subject", "")),
            email=data.get("email"),
            name=data.get("name"),
        )


def is_user_logged_in() -> bool:
    """Check whether the current user is authenticated via st.user.

    Safely handles both Streamlit script context and non-interactive/test environments.
    """
    try:
        # Check attribute first
        if hasattr(st, "user"):
            val = getattr(st.user, "is_logged_in", None)
            if val is not None:
                return bool(val)
            # Fall back to dict access
            return bool(st.user.get("is_logged_in", False))
        return False
    except Exception:
        return False


def get_current_user() -> Optional[AuthenticatedUser]:
    """Extract authenticated user identity from st.user.

    Returns:
        AuthenticatedUser if logged in, None otherwise.
    """
    if not is_user_logged_in():
        return None

    try:
        user_proxy = getattr(st, "user", None)
        if user_proxy is None:
            return None

        # Extract stable subject identifier (sub is standard OIDC claim)
        sub = (
            getattr(user_proxy, "sub", None)
            or user_proxy.get("sub")
            or getattr(user_proxy, "id", None)
            or user_proxy.get("id")
            or getattr(user_proxy, "email", None)
            or user_proxy.get("email")
            or "unknown_user"
        )

        email = getattr(user_proxy, "email", None) or user_proxy.get("email")
        name = getattr(user_proxy, "name", None) or user_proxy.get("name")

        return AuthenticatedUser(
            provider="google",
            subject=str(sub),
            email=str(email) if email else None,
            name=str(name) if name else None,
        )
    except Exception as exc:
        logger.warning("Could not extract authenticated user claims: %s", exc)
        return None


def is_auth_configured() -> bool:
    """Determine whether Streamlit OIDC authentication credentials are configured.

    Checks for the presence of the [auth] section in st.secrets.
    """
    try:
        if not hasattr(st, "secrets"):
            return False
        if "auth" not in st.secrets:
            return False
        auth_sec = st.secrets["auth"]
        # Valid if client_id is directly in [auth] or nested under [auth.google]
        has_direct = bool(auth_sec.get("client_id") and auth_sec.get("client_secret"))
        has_named = "google" in auth_sec and bool(
            auth_sec["google"].get("client_id") and auth_sec["google"].get("client_secret")
        )
        return has_direct or has_named
    except Exception:
        return False


def trigger_google_login() -> None:
    """Initiate native Streamlit OIDC authentication flow.

    Directs the user to Google OAuth2 consent screen.
    """
    try:
        if hasattr(st, "secrets") and "auth" in st.secrets and "google" in st.secrets["auth"]:
            st.login("google")
        else:
            st.login()
    except Exception as exc:
        # Avoid leaking client secrets or stack traces
        logger.error("Authentication initiation failed: %s", type(exc).__name__)
        st.error(
            "Authentication service is currently unavailable. "
            "Please ensure Google OIDC credentials are configured in your Streamlit secrets."
        )


def trigger_logout() -> None:
    """Log out the current user by clearing the identity cookie."""
    try:
        st.logout()
    except Exception as exc:
        logger.error("Logout failed: %s", type(exc).__name__)
        st.error("Could not complete logout. Please refresh your browser.")
