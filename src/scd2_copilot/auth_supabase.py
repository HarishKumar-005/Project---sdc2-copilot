"""Supabase Auth service for Streamlit user authentication and session management."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import logging
import secrets
import time
from typing import Any, Optional
from urllib.parse import urlencode
from uuid import UUID

import httpx

from .config import Settings, get_settings

logger = logging.getLogger("scd2_copilot.auth_supabase")


def generate_pkce_pair() -> tuple[str, str]:
    """Generate a high-entropy PKCE code_verifier and code_challenge pair (S256).

    Returns:
        tuple[str, str]: (code_verifier, code_challenge)
    """
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return code_verifier, code_challenge


@dataclass(frozen=True)
class SupabaseSession:
    """Represents an active, authenticated Supabase Auth session."""

    access_token: str = field(repr=False)
    user_id: UUID
    email: Optional[str] = None
    role: str = "authenticated"
    refresh_token: Optional[str] = field(default=None, repr=False)
    expires_at: Optional[int] = None
    app_metadata: dict[str, Any] = field(default_factory=dict)
    user_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        """User-friendly display name."""
        if self.user_metadata and self.user_metadata.get("full_name"):
            return str(self.user_metadata["full_name"])
        if self.email:
            return self.email
        return str(self.user_id)

    @property
    def is_expired(self) -> bool:
        """True if the access token has expired."""
        if not self.expires_at:
            return False
        return time.time() >= self.expires_at

    @property
    def needs_refresh(self) -> bool:
        """True if token expires in less than 60 seconds."""
        if not self.expires_at:
            return False
        return (self.expires_at - time.time()) < 60

    def to_audit_dict(self) -> dict[str, Any]:
        """Safe dictionary for audit logging without tokens."""
        return {
            "user_id": str(self.user_id),
            "email": self.email,
            "role": self.role,
            "display_name": self.display_name,
        }


class SupabaseAuthService:
    """Client for executing Supabase Auth flows against the configured Supabase project."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self.base_url = (self.settings.supabase_url or "").rstrip("/")
        self.anon_key = self.settings.supabase_publishable_key or ""
        self.last_error: Optional[str] = None

    @property
    def is_configured(self) -> bool:
        """True if Supabase URL and publishable key are set."""
        return bool(self.base_url and self.anon_key)

    def _get_headers(self) -> dict[str, str]:
        return {
            "apikey": self.anon_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def build_google_oauth_url(
        self,
        redirect_to: str,
        code_challenge: Optional[str] = None,
        state: Optional[str] = None,
    ) -> str:
        """Construct the Supabase Auth Google OAuth redirection URL.

        Args:
            redirect_to: The callback URL after Google authentication.
            code_challenge: Optional S256 PKCE challenge.
            state: Optional random OAuth state nonce.
        """
        if not self.base_url:
            return ""
        params: dict[str, str] = {
            "provider": "google",
            "redirect_to": redirect_to,
        }
        if code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "s256"
        if state:
            params["state"] = state
        return f"{self.base_url}/auth/v1/authorize?{urlencode(params)}"

    def exchange_code_for_session(
        self,
        auth_code: str,
        code_verifier: Optional[str] = None,
    ) -> Optional[SupabaseSession]:
        """Exchange PKCE authorization code for a Supabase session."""
        if not self.is_configured or not auth_code:
            self.last_error = "Supabase Auth is not configured or authorization code is missing."
            return None

        url = f"{self.base_url}/auth/v1/token?grant_type=pkce"
        payload: dict[str, Any] = {"auth_code": auth_code}
        if code_verifier:
            payload["code_verifier"] = code_verifier

        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(url, headers=self._get_headers(), json=payload)
                if resp.is_success:
                    self.last_error = None
                    return self._parse_session(resp.json())
                try:
                    err_json = resp.json()
                    err_msg = (
                        err_json.get("msg")
                        or err_json.get("error_description")
                        or err_json.get("message")
                        or resp.text
                    )
                except Exception:
                    err_msg = resp.text
                self.last_error = f"{err_msg}"
                logger.warning("Code exchange failed: %s %s", resp.status_code, err_msg)
        except Exception as exc:
            self.last_error = f"Network connection error: {exc}"
            logger.error("Error exchanging code for Supabase session: %s", exc)
        return None

    def sign_in_with_id_token(
        self,
        id_token: str,
        provider: str = "google",
        nonce: Optional[str] = None,
    ) -> Optional[SupabaseSession]:
        """Exchange a third-party Google ID token for a Supabase access session."""
        if not self.is_configured or not id_token:
            self.last_error = "Supabase Auth is not configured or ID token is missing."
            return None

        url = f"{self.base_url}/auth/v1/token?grant_type=id_token"
        payload: dict[str, Any] = {
            "provider": provider,
            "id_token": id_token,
        }
        if nonce:
            payload["nonce"] = nonce

        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(url, headers=self._get_headers(), json=payload)
                if resp.is_success:
                    self.last_error = None
                    return self._parse_session(resp.json())
                try:
                    err_json = resp.json()
                    err_msg = (
                        err_json.get("msg")
                        or err_json.get("error_description")
                        or err_json.get("message")
                        or resp.text
                    )
                except Exception:
                    err_msg = resp.text
                self.last_error = f"{err_msg}"
                logger.warning("Sign-in with ID token failed: %s %s", resp.status_code, err_msg)
        except Exception as exc:
            self.last_error = f"Network connection error: {exc}"
            logger.error("Error signing in with ID token: %s", exc)
        return None

    def sign_in_with_password(
        self,
        email: str,
        password: str,
    ) -> Optional[SupabaseSession]:
        """Sign in using email and password (useful for operator accounts or demo)."""
        if not self.is_configured or not email or not password:
            self.last_error = "Email and password are required."
            return None

        url = f"{self.base_url}/auth/v1/token?grant_type=password"
        payload = {"email": email.strip(), "password": password}

        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(url, headers=self._get_headers(), json=payload)
                if resp.is_success:
                    self.last_error = None
                    return self._parse_session(resp.json())
                try:
                    err_json = resp.json()
                    err_msg = (
                        err_json.get("msg")
                        or err_json.get("error_description")
                        or err_json.get("message")
                        or resp.text
                    )
                except Exception:
                    err_msg = resp.text
                self.last_error = f"{err_msg}"
                logger.warning("Password sign-in failed: %s %s", resp.status_code, err_msg)
        except Exception as exc:
            self.last_error = f"Network connection error: {exc}"
            logger.error("Error signing in with password: %s", exc)
        return None

    def refresh_session(self, refresh_token: str) -> Optional[SupabaseSession]:
        """Obtain a new access token using a valid refresh token."""
        if not self.is_configured or not refresh_token:
            return None

        url = f"{self.base_url}/auth/v1/token?grant_type=refresh_token"
        payload = {"refresh_token": refresh_token}

        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(url, headers=self._get_headers(), json=payload)
                if resp.is_success:
                    return self._parse_session(resp.json())
                try:
                    err_json = resp.json()
                    err_msg = err_json.get("msg") or err_json.get("error_description") or err_json.get("message") or "Refresh failed"
                except Exception:
                    err_msg = "Refresh failed"
                logger.warning("Session refresh failed: %s %s", resp.status_code, err_msg)
        except Exception as exc:
            logger.error("Error refreshing Supabase session: %s", exc)
        return None

    def sign_out(self, access_token: str) -> bool:
        """Revoke session on Supabase Auth server."""
        if not self.is_configured or not access_token:
            return True

        url = f"{self.base_url}/auth/v1/logout"
        headers = self._get_headers()
        headers["Authorization"] = f"Bearer {access_token}"

        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.post(url, headers=headers)
                return resp.is_success or resp.status_code == 204
        except Exception as exc:
            logger.warning("Supabase logout call encountered error: %s", exc)
            return False

    def _parse_session(self, data: dict[str, Any]) -> Optional[SupabaseSession]:
        """Construct typed SupabaseSession from token response."""
        access_token = data.get("access_token")
        user_info = data.get("user") or {}
        user_id_raw = user_info.get("id")

        if not access_token or not user_id_raw:
            return None

        try:
            user_id = UUID(str(user_id_raw))
        except (ValueError, TypeError):
            return None

        email = user_info.get("email")
        role = user_info.get("role", "authenticated")
        refresh_token = data.get("refresh_token")

        expires_at = data.get("expires_at")
        if not expires_at and "expires_in" in data:
            expires_at = int(time.time()) + int(data["expires_in"])

        app_meta = user_info.get("app_metadata") or {}
        user_meta = user_info.get("user_metadata") or {}

        return SupabaseSession(
            access_token=access_token,
            user_id=user_id,
            email=email,
            role=role,
            refresh_token=refresh_token,
            expires_at=expires_at,
            app_metadata=app_meta if isinstance(app_meta, dict) else {},
            user_metadata=user_meta if isinstance(user_meta, dict) else {},
        )
