"""FastAPI authentication and authorization dependencies for Supabase JWT verification."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ...config import Settings, get_settings
from .exceptions import AuthError, TokenExpiredError, UnauthorizedOperatorError
from .models import AuthenticatedOperator
from .verifier import SupabaseJWTVerifier

logger = logging.getLogger("scd2_copilot.api.auth.dependencies")

# HTTP Bearer scheme: auto_error=False allows custom structured JSON error responses
bearer_scheme = HTTPBearer(auto_error=False)

# Singleton verifier cache
_verifier_instance: Optional[SupabaseJWTVerifier] = None


def get_jwt_verifier(settings: Settings = Depends(get_settings)) -> SupabaseJWTVerifier:
    """Dependency provider returning the configured SupabaseJWTVerifier singleton."""
    global _verifier_instance
    if _verifier_instance is None:
        _verifier_instance = SupabaseJWTVerifier(settings=settings)
    return _verifier_instance


def set_jwt_verifier(verifier: Optional[SupabaseJWTVerifier]) -> None:
    """Allows test fixtures to override or mock the verifier instance."""
    global _verifier_instance
    _verifier_instance = verifier


def get_current_operator(
    auth: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme),
    verifier: SupabaseJWTVerifier = Depends(get_jwt_verifier),
) -> AuthenticatedOperator:
    """Authenticate request using the Supabase access JWT from Authorization: Bearer <token>.

    Raises:
        HTTPException(401): If token is missing, expired, invalid, or untrusted.
    """
    if auth is None or not auth.credentials:
        logger.debug("Request missing Bearer token in Authorization header")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "MISSING_TOKEN",
                "message": "Authorization header with Bearer token is required.",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        operator = verifier.verify_token(auth.credentials)
        return operator
    except TokenExpiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": exc.code, "message": exc.message},
            headers={"WWW-Authenticate": "Bearer error=\"invalid_token\", error_description=\"Token has expired\""},
        ) from exc
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": exc.code, "message": exc.message},
            headers={"WWW-Authenticate": f"Bearer error=\"invalid_token\", error_description=\"{exc.message}\""},
        ) from exc
    except Exception as exc:
        logger.error("Unexpected error during operator authentication: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTHENTICATION_FAILED", "message": "Authentication failed."},
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def get_optional_operator(
    auth: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme),
    verifier: SupabaseJWTVerifier = Depends(get_jwt_verifier),
    settings: Settings = Depends(get_settings),
) -> Optional[AuthenticatedOperator]:
    """Optionally verify the operator if token is present; enforce authentication if api_auth_required=True."""
    if auth is None or not auth.credentials:
        if settings.api_auth_required:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "MISSING_TOKEN", "message": "API authentication is required."},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return None

    try:
        return verifier.verify_token(auth.credentials)
    except Exception as exc:
        if settings.api_auth_required:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "INVALID_TOKEN", "message": "Invalid authentication token."},
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        logger.debug("Optional authentication failed; proceeding as unauthenticated: %s", exc)
        return None


def require_authorized_operator(
    operator: AuthenticatedOperator = Depends(get_current_operator),
) -> AuthenticatedOperator:
    """Authorize the authenticated operator specifically for operational recovery actions.

    Raises:
        HTTPException(403): If the authenticated user is not permitted to perform recovery actions.
    """
    if not operator.is_authorized_operator:
        logger.warning(
            "Forbidden recovery attempt by user %s (email=%s, role=%s)",
            operator.user_id,
            operator.email,
            operator.role,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "FORBIDDEN_OPERATOR",
                "message": "Authenticated user is not authorized to execute operational recovery actions.",
                "user_id": str(operator.user_id),
            },
        )
    return operator
