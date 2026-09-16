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
from urllib.parse import urlparse
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


def get_current_request_origin() -> Optional[str]:
    """Extract the current web origin (scheme + host) from Streamlit context if available."""
    try:
        if hasattr(st, "context"):
            # Check st.context.url first
            ctx_url = getattr(st.context, "url", None)
            if ctx_url:
                parsed = urlparse(ctx_url)
                if parsed.scheme and parsed.netloc:
                    return f"{parsed.scheme}://{parsed.netloc}"

            # Fall back to headers
            if hasattr(st.context, "headers"):
                headers = st.context.headers
                host = headers.get("x-forwarded-host") or headers.get("host")
                if host:
                    is_local = "localhost" in host or "127.0.0.1" in host
                    proto = headers.get("x-forwarded-proto") or ("http" if is_local else "https")
                    return f"{proto}://{host}"
    except Exception:
        pass
    return None


def sync_redirect_uri_with_host() -> Optional[str]:
    """Ensure redirect_uri in secrets matches the current deployment origin if running in cloud.

    When deployed to Streamlit Community Cloud (e.g. sdc2-copilot.streamlit.app),
    secrets might still contain 'http://localhost:8501/oauth2callback' copied from
    local dev. This function dynamically adapts redirect_uri in memory so the OIDC
    flow redirects back to the actual deployed application domain instead of localhost.

    Returns:
        The active redirect_uri (either existing or dynamically adjusted), or None.
    """
    try:
        if not hasattr(st, "secrets") or "auth" not in st.secrets:
            return None

        auth_sec = st.secrets["auth"]
        current_redirect = auth_sec.get("redirect_uri", "")

        origin = get_current_request_origin()
        if not origin:
            return current_redirect or None

        expected_redirect = f"{origin.rstrip('/')}/oauth2callback"
        if current_redirect == expected_redirect:
            return current_redirect

        is_local_config = "localhost" in current_redirect or "127.0.0.1" in current_redirect
        is_remote_origin = "localhost" not in origin and "127.0.0.1" not in origin

        # Only auto-sync if currently configured as localhost but accessed via remote cloud origin
        if is_local_config and is_remote_origin:
            from streamlit.runtime.secrets import AttrDict, secrets_singleton

            def _to_plain_dict(obj: Any) -> Any:
                if isinstance(obj, (dict, AttrDict)):
                    return {k: _to_plain_dict(v) for k, v in obj.items()}
                elif isinstance(obj, list):
                    return [_to_plain_dict(v) for v in obj]
                return obj

            auth_dict = _to_plain_dict(secrets_singleton.get("auth", {}))
            auth_dict["redirect_uri"] = expected_redirect
            secrets_singleton.merge_programmatic_secrets({"auth": auth_dict})
            logger.info("Automatically synced auth redirect_uri to: %s", expected_redirect)
            return expected_redirect

        return current_redirect or None
    except Exception as exc:
        logger.debug("Could not auto-sync redirect_uri with host: %s", exc)
        return None


def is_auth_configured() -> bool:
    """Determine whether Streamlit OIDC authentication credentials are configured.

    Checks for the presence of the [auth] section in st.secrets.
    """
    try:
        sync_redirect_uri_with_host()
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
        sync_redirect_uri_with_host()
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
