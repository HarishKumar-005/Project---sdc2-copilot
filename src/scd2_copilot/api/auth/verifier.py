"""Cryptographic verification of Supabase access JWTs via asymmetric JWKS."""

from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import UUID

import jwt
from jwt import PyJWKClient, PyJWKClientError

from ...config import Settings, get_settings
from .exceptions import (
    AuthError,
    InvalidClaimsError,
    InvalidTokenError,
    JWKSError,
    TokenExpiredError,
)
from .models import AuthenticatedOperator

logger = logging.getLogger("scd2_copilot.api.auth.verifier")


class SupabaseJWTVerifier:
    """Verifies Supabase Auth access JWTs against the project's public JWKS endpoint.

    Adheres strictly to the asymmetric signing key architecture (ES256 / P-256).
    Validates signature, issuer, audience, expiration, and extracts typed operator identity.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        jwks_client: Optional[PyJWKClient] = None,
        jwks_url: Optional[str] = None,
        expected_issuer: Optional[str] = None,
        expected_audience: Optional[str] = None,
        allowed_algorithms: Optional[list[str]] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.jwks_url = jwks_url or self.settings.resolved_supabase_jwks_url
        self.expected_issuer = expected_issuer or self.settings.resolved_supabase_issuer
        self.expected_audience = expected_audience or self.settings.supabase_auth_audience
        self.allowed_algorithms = allowed_algorithms or ["ES256", "RS256"]

        if jwks_client is not None:
            self.jwks_client = jwks_client
        elif self.jwks_url:
            self.jwks_client = PyJWKClient(self.jwks_url, cache_keys=True, max_cached_keys=16)
        else:
            self.jwks_client = None

    def verify_token(self, token: str) -> AuthenticatedOperator:
        """Verify the given Supabase access JWT and return a validated AuthenticatedOperator.

        Raises:
            TokenExpiredError: If token expiration has passed.
            InvalidClaimsError: If issuer, audience, or sub is invalid.
            InvalidTokenError: If signature is invalid or token is malformed.
            JWKSError: If JWKS lookup fails.
        """
        if not token or not isinstance(token, str):
            raise InvalidTokenError("Authentication token must be a non-empty string.")

        clean_token = token.strip()
        if not clean_token:
            raise InvalidTokenError("Authentication token cannot be whitespace.")

        # 1. Inspect unverified header to resolve key algorithm & kid
        try:
            unverified_header = jwt.get_unverified_header(clean_token)
        except Exception as exc:
            logger.warning("Failed to parse JWT header: %s", exc)
            raise InvalidTokenError(f"Malformed JWT header: {exc}") from exc

        alg = unverified_header.get("alg")
        if alg not in self.allowed_algorithms:
            logger.warning("Disallowed JWT algorithm '%s'. Permitted: %s", alg, self.allowed_algorithms)
            raise InvalidTokenError(f"Unsupported signing algorithm '{alg}'. Expected one of: {self.allowed_algorithms}")

        # 2. Resolve public signing key from JWKS
        if self.jwks_client is None:
            raise JWKSError("Supabase JWKS URL is not configured; cannot verify asymmetric JWT.")

        try:
            signing_key = self.jwks_client.get_signing_key_from_jwt(clean_token)
        except PyJWKClientError as exc:
            logger.warning("JWKS key resolution failed: %s", exc)
            raise JWKSError(f"Unable to resolve signing key from Supabase JWKS: {exc}") from exc
        except Exception as exc:
            logger.warning("Unexpected error during JWKS key retrieval: %s", exc)
            raise JWKSError(f"Key retrieval failed: {exc}") from exc

        # 3. Cryptographically verify signature and standard claims
        try:
            decode_options = {
                "verify_signature": True,
                "verify_exp": True,
                "verify_aud": bool(self.expected_audience),
                "verify_iss": bool(self.expected_issuer),
                "require": ["sub", "exp"],
            }
            payload = jwt.decode(
                clean_token,
                key=signing_key.key,
                algorithms=[alg],
                audience=self.expected_audience if self.expected_audience else None,
                issuer=self.expected_issuer if self.expected_issuer else None,
                options=decode_options,
                leeway=10,  # 10 seconds clock skew tolerance
            )
        except jwt.ExpiredSignatureError as exc:
            logger.info("Supabase JWT has expired")
            raise TokenExpiredError("Supabase access token has expired.") from exc
        except jwt.InvalidIssuerError as exc:
            logger.warning("Supabase JWT issuer mismatch: %s", exc)
            raise InvalidClaimsError("Invalid token issuer.") from exc
        except jwt.InvalidAudienceError as exc:
            logger.warning("Supabase JWT audience mismatch: %s", exc)
            raise InvalidClaimsError("Invalid token audience.") from exc
        except jwt.InvalidSignatureError as exc:
            logger.warning("Supabase JWT signature verification failed: %s", exc)
            raise InvalidTokenError("Token cryptographic signature verification failed.") from exc
        except jwt.DecodeError as exc:
            logger.warning("Supabase JWT decode error: %s", exc)
            raise InvalidTokenError(f"Could not decode token: {exc}") from exc
        except Exception as exc:
            logger.warning("Unexpected error during JWT verification: %s", exc)
            raise InvalidTokenError(f"Token validation failed: {exc}") from exc

        # 4. Extract and validate claims
        sub_raw = payload.get("sub")
        try:
            user_id = UUID(str(sub_raw))
        except (ValueError, TypeError) as exc:
            logger.warning("Invalid sub UUID in JWT: %s", sub_raw)
            raise InvalidClaimsError(f"Subject 'sub' claim must be a valid UUID: {sub_raw}") from exc

        email = payload.get("email")
        role = payload.get("role", "authenticated")
        app_metadata = payload.get("app_metadata") or {}
        user_metadata = payload.get("user_metadata") or {}

        session_id_raw = payload.get("session_id")
        session_id: Optional[UUID] = None
        if session_id_raw:
            try:
                session_id = UUID(str(session_id_raw))
            except (ValueError, TypeError):
                pass

        # 5. Evaluate authorization for operational recovery
        is_authorized = self._evaluate_operator_authorization(
            email=email,
            app_metadata=app_metadata,
        )

        operator = AuthenticatedOperator(
            user_id=user_id,
            email=str(email) if email else None,
            role=str(role),
            app_metadata=app_metadata if isinstance(app_metadata, dict) else {},
            user_metadata=user_metadata if isinstance(user_metadata, dict) else {},
            session_id=session_id,
            is_authorized_operator=is_authorized,
        )
        logger.debug(
            "Successfully verified Supabase operator %s (authorized=%s)",
            operator.user_id,
            operator.is_authorized_operator,
        )
        return operator

    def _evaluate_operator_authorization(
        self,
        email: Optional[str],
        app_metadata: dict[str, Any],
    ) -> bool:
        """Determine whether the authenticated identity is authorized for recovery actions.

        Rules:
        1. If recovery_operator_emails contains '*', any authenticated Supabase user is permitted.
        2. If operator's email is explicitly listed in recovery_operator_emails, permitted.
        3. If trusted app_metadata contains role 'operator' or 'admin', permitted.
        """
        allowlist = self.settings.recovery_operator_emails

        # 1. Wildcard allowlist (common in development / single-tenant demo)
        if "*" in allowlist:
            return True

        # 2. Explicit email match (case-insensitive)
        if email:
            clean_email = email.strip().lower()
            if any(clean_email == allowed.strip().lower() for allowed in allowlist):
                return True

        # 3. Trusted app_metadata claim check (server-managed only)
        if isinstance(app_metadata, dict):
            roles = app_metadata.get("roles")
            if isinstance(roles, list) and any(r in ("operator", "admin", "service_role") for r in roles):
                return True
            if app_metadata.get("role") in ("operator", "admin", "service_role"):
                return True
            if app_metadata.get("operator") is True or app_metadata.get("is_operator") is True:
                return True

        return False
