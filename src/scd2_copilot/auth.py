"""Supabase Auth service for Streamlit user authentication and session management.

Provides:
- Immutable AuthenticatedUser identity model for audit and ownership tracking
- Session persistence and auto-refresh in st.session_state
- PKCE Google OAuth authorization URL generation and callback code exchange
- Supabase session logout and token revocation
- Dynamic retrieval of Supabase access JWT for ApiClient requests
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import secrets
import threading
import time
from typing import Any, Optional
from urllib.parse import urlparse
from uuid import UUID

import streamlit as st

from .auth_supabase import SupabaseAuthService, SupabaseSession, generate_pkce_pair
from .config import get_settings

logger = logging.getLogger("scd2_copilot.auth")


@dataclass(frozen=True)
class AuthenticatedUser:
    """Immutable identity of an authenticated user.

    Stores strictly minimal identity claims needed for audit and ownership.
    Constructed exclusively from verified Supabase session attributes.
    """

    provider: str = "supabase"
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
            provider=data.get("provider", "supabase"),
            subject=str(data.get("subject", "")),
            email=data.get("email"),
            name=data.get("name"),
        )


def get_current_supabase_session() -> Optional[SupabaseSession]:

    """Retrieve active Supabase session from Streamlit session state, auto-refreshing if needed."""
    try:
        if not hasattr(st, "session_state"):
            return None
        session: Optional[SupabaseSession] = st.session_state.get("supabase_session")
        if session is None:
            return None

        # Check for auto-refresh
        if session.needs_refresh and session.refresh_token:
            auth_svc = SupabaseAuthService()
            refreshed = auth_svc.refresh_session(session.refresh_token)
            if refreshed:
                st.session_state["supabase_session"] = refreshed
                return refreshed
            elif session.is_expired:
                # Refresh failed and token is expired
                st.session_state.pop("supabase_session", None)
                return None

        return session
    except Exception as exc:
        logger.debug("Could not resolve Supabase session: %s", exc)
        return None


def set_current_supabase_session(session: Optional[SupabaseSession]) -> None:
    """Store active Supabase session in Streamlit session state."""
    try:
        if hasattr(st, "session_state"):
            if session is not None:
                st.session_state["supabase_session"] = session
            else:
                st.session_state.pop("supabase_session", None)
    except Exception:
        pass


def get_supabase_access_token() -> Optional[str]:
    """Return active Supabase access token (JWT) for Bearer authentication."""
    session = get_current_supabase_session()
    if session and not session.is_expired:
        return session.access_token
    return None


def is_user_logged_in() -> bool:
    """Check whether the current user is authenticated via active Supabase session."""
    session = get_current_supabase_session()
    return session is not None and not session.is_expired


def get_current_user() -> Optional[AuthenticatedUser]:
    """Extract authenticated user identity from the active Supabase session.

    Returns:
        AuthenticatedUser if logged in, None otherwise.
    """
    session = get_current_supabase_session()
    if session is not None and not session.is_expired:
        return AuthenticatedUser(
            provider="supabase",
            subject=str(session.user_id),
            email=session.email,
            name=session.display_name,
        )
    return None



def get_current_request_origin() -> Optional[str]:
    """Extract the current web origin (scheme + host) from Streamlit context or configuration."""
    try:
        # 1. Explicit configuration in Settings (from .env or env var STREAMLIT_APP_URL)
        settings = get_settings()
        if getattr(settings, "streamlit_app_url", None) and settings.streamlit_app_url.strip():
            return settings.streamlit_app_url.strip().rstrip("/")

        # 2. Render cloud automatic environment variable (RENDER_EXTERNAL_URL)
        render_url = os.environ.get("RENDER_EXTERNAL_URL")
        if render_url and render_url.strip():
            return render_url.strip().rstrip("/")

        # 3. Railway automatic environment variable (RAILWAY_PUBLIC_DOMAIN)
        railway_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
        if railway_domain and railway_domain.strip():
            domain = railway_domain.strip().rstrip("/")
            if not domain.startswith("http"):
                domain = f"https://{domain}"
            return domain

        # 4. Explicit configuration in st.secrets
        if hasattr(st, "secrets") and st.secrets:
            app_url = st.secrets.get("STREAMLIT_APP_URL") or st.secrets.get("APP_URL")
            if app_url and str(app_url).strip():
                return str(app_url).strip().rstrip("/")

        # 3. Streamlit context inspection (url or headers)
        if hasattr(st, "context"):
            ctx_url = getattr(st.context, "url", None)
            if ctx_url:
                parsed = urlparse(ctx_url)
                if parsed.scheme and parsed.netloc:
                    return f"{parsed.scheme}://{parsed.netloc}"

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


@dataclass(frozen=True)
class _OAuthTransaction:
    """Ephemeral server-side transaction record for binding PKCE verifier to OAuth state."""

    code_verifier: str
    created_at: float
    origin: str


_TRANSACTION_LOCK = threading.Lock()
_TRANSACTION_STORE: dict[str, _OAuthTransaction] = {}
_MAX_STORE_CAPACITY = 500
_TRANSACTION_TTL_SECONDS = 600.0  # 10 minutes maximum lifetime


def store_oauth_transaction(state: str, code_verifier: str, origin: str) -> None:
    """Store an ephemeral OAuth PKCE transaction keyed by high-entropy state nonce.

    Applies capacity bounding and automatic TTL eviction of stale transactions.
    """
    now = time.time()
    with _TRANSACTION_LOCK:
        # Purge expired entries
        expired_keys = [
            s for s, tx in _TRANSACTION_STORE.items()
            if (now - tx.created_at) > _TRANSACTION_TTL_SECONDS
        ]
        for s in expired_keys:
            _TRANSACTION_STORE.pop(s, None)

        # Enforce maximum store capacity
        if len(_TRANSACTION_STORE) >= _MAX_STORE_CAPACITY:
            oldest_key = min(_TRANSACTION_STORE.keys(), key=lambda k: _TRANSACTION_STORE[k].created_at)
            _TRANSACTION_STORE.pop(oldest_key, None)

        _TRANSACTION_STORE[state] = _OAuthTransaction(
            code_verifier=code_verifier,
            created_at=now,
            origin=origin,
        )


def consume_oauth_transaction(state: Optional[str]) -> Optional[str]:
    """Atomically retrieve and remove the code_verifier for a valid, unexpired OAuth transaction.

    Fails closed: returns None if state is absent, unknown, expired, or already consumed.
    """
    if not state or not isinstance(state, str):
        return None

    now = time.time()
    with _TRANSACTION_LOCK:
        tx = _TRANSACTION_STORE.pop(state, None)
        if tx is None:
            return None
        if (now - tx.created_at) > _TRANSACTION_TTL_SECONDS:
            return None
        return tx.code_verifier


def consume_latest_oauth_transaction(
    origin: Optional[str] = None,
    max_age_seconds: float = _TRANSACTION_TTL_SECONDS,
) -> Optional[str]:
    """Atomically retrieve and remove the code_verifier for the most recent valid, unexpired OAuth transaction.

    Used as a graceful fallback when external OAuth providers (such as Supabase Auth)
    omit the state parameter on callback and the browser session was reset across redirects
    or opened in a new tab by st.link_button.
    """
    now = time.time()
    with _TRANSACTION_LOCK:
        # Purge expired entries
        expired_keys = [
            s for s, tx in _TRANSACTION_STORE.items()
            if (now - tx.created_at) > _TRANSACTION_TTL_SECONDS
        ]
        for s in expired_keys:
            _TRANSACTION_STORE.pop(s, None)

        if not _TRANSACTION_STORE:
            return None

        candidates = list(_TRANSACTION_STORE.keys())
        if origin:
            clean_origin = origin.rstrip("/")
            origin_candidates = [
                k for k in candidates
                if _TRANSACTION_STORE[k].origin.rstrip("/") == clean_origin
            ]
            if origin_candidates:
                candidates = origin_candidates

        latest_state = max(candidates, key=lambda k: _TRANSACTION_STORE[k].created_at)
        tx = _TRANSACTION_STORE.pop(latest_state)
        if (now - tx.created_at) > max_age_seconds:
            return None
        return tx.code_verifier


def get_pending_transactions_count() -> int:
    """Return count of active pending transactions (used for diagnostics and tests)."""
    with _TRANSACTION_LOCK:
        return len(_TRANSACTION_STORE)


def clear_all_transactions_for_testing() -> None:
    """Clear transaction store strictly for test isolation."""
    with _TRANSACTION_LOCK:
        _TRANSACTION_STORE.clear()


def is_auth_configured() -> bool:
    """Determine whether Supabase Auth is configured with a valid base URL and publishable key."""
    auth_svc = SupabaseAuthService()
    return auth_svc.is_configured


def build_google_login_url(redirect_to: Optional[str] = None) -> str:
    """Generate PKCE pair, register server-side transaction nonce, and return clean OAuth URL.

    The PKCE code_verifier remains strictly server-side and is NEVER attached to redirect URLs.
    """
    auth_svc = SupabaseAuthService()
    if not auth_svc.is_configured:
        return ""

    origin = redirect_to or get_current_request_origin() or "http://localhost:8501"
    base_origin = origin.rstrip("/")

    # Generate high-entropy PKCE pair and unguessable state nonce
    code_verifier, code_challenge = generate_pkce_pair()
    oauth_state = secrets.token_urlsafe(32)

    # Store transaction server-side keyed strictly by state nonce
    store_oauth_transaction(state=oauth_state, code_verifier=code_verifier, origin=base_origin)

    # Also record in session_state for same-session binding check
    try:
        if hasattr(st, "session_state"):
            st.session_state["pending_oauth_state"] = oauth_state
    except Exception:
        pass

    # Notice: target_redirect is clean base origin (NO spkce parameter!).
    # State is passed via standard OAuth 2.0 state parameter.
    return auth_svc.build_google_oauth_url(
        redirect_to=base_origin,
        code_challenge=code_challenge,
        state=oauth_state,
    )


def handle_auth_callback() -> Optional[SupabaseSession]:
    """Inspect query parameters for Supabase OAuth callback (code, state, or error).

    Validates state binding, atomically consumes code_verifier, exchanges authorization code
    for session, updates session state, clears query params, and returns the session.
    """
    try:
        if not hasattr(st, "query_params"):
            return None

        # Check for OAuth error callback
        if "error" in st.query_params:
            err_code = str(st.query_params.get("error", "auth_error"))
            err_desc = str(st.query_params.get("error_description", "Authentication failed"))
            logger.warning("Supabase OAuth error callback: %s", err_code)
            if hasattr(st, "session_state"):
                st.session_state["auth_error"] = f"{err_code}: {err_desc}"
            st.query_params.clear()
            return None

        # Check for authorization code callback
        if "code" in st.query_params:
            auth_code = str(st.query_params["code"])
            state = st.query_params.get("state") or st.query_params.get("oauth_state")

            # Atomically consume matching code_verifier from server-side transaction store
            code_verifier: Optional[str] = None
            if state:
                code_verifier = consume_oauth_transaction(str(state))

            # Fall back to session_state pending_oauth_state if query param state was omitted
            if not code_verifier and hasattr(st, "session_state") and "pending_oauth_state" in st.session_state:
                saved_state = st.session_state.pop("pending_oauth_state", None)
                if saved_state:
                    code_verifier = consume_oauth_transaction(str(saved_state))

            # If state was omitted by the identity provider and session was reset (e.g. new tab from st.link_button),
            # fall back to the most recent unexpired server-side transaction matching the request origin.
            if not code_verifier and not state:
                req_origin = get_current_request_origin()
                code_verifier = consume_latest_oauth_transaction(origin=req_origin)

            # Clear temporary state and query parameters immediately
            st.query_params.clear()
            if hasattr(st, "session_state"):
                st.session_state.pop("pending_oauth_state", None)

            # Fail closed if transaction state could not be validated
            if not code_verifier:
                logger.warning("OAuth callback rejected: missing, invalid, expired, or replayed state")
                if hasattr(st, "session_state"):
                    st.session_state["auth_error"] = (
                        "Invalid or expired authentication transaction. Please try signing in again."
                    )
                return None

            auth_svc = SupabaseAuthService()
            session = auth_svc.exchange_code_for_session(auth_code=auth_code, code_verifier=code_verifier)

            if session:
                logger.info("Successfully established Supabase session for user %s", session.user_id)
                set_current_supabase_session(session)
                if hasattr(st, "session_state"):
                    st.session_state.pop("auth_error", None)
                st.rerun()
                return session
            else:
                reason = auth_svc.last_error or "Failed to complete authentication. Please try signing in again."
                logger.warning("Failed to exchange Supabase authorization code: %s", reason)
                if hasattr(st, "session_state"):
                    st.session_state["auth_error"] = reason
                return None

    except Exception as exc:
        logger.error("Error processing auth callback: %s", exc)
        if hasattr(st, "session_state"):
            st.session_state["auth_error"] = f"Error processing login callback: {exc}"
    return None


def trigger_logout() -> None:
    """Log out the current user by revoking the Supabase session and clearing state."""
    try:
        session = get_current_supabase_session()
        if session and session.access_token:
            auth_svc = SupabaseAuthService()
            auth_svc.sign_out(session.access_token)
    except Exception as exc:
        logger.debug("Supabase sign_out call error: %s", exc)

    try:
        set_current_supabase_session(None)
        if hasattr(st, "session_state"):
            st.session_state.pop("supabase_session", None)
            st.session_state.pop("pending_oauth_state", None)
            st.session_state.pop("auth_error", None)
    except Exception:
        pass

    try:
        st.rerun()
    except Exception:
        pass

