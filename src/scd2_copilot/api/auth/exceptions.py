"""Authentication and authorization exceptions for Supabase JWT verification."""

from __future__ import annotations

from typing import Optional


class AuthError(Exception):
    """Base exception for authentication and authorization failures."""

    def __init__(self, message: str, code: str = "AUTH_ERROR", status_code: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


class MissingTokenError(AuthError):
    """Raised when Authorization Bearer token is missing."""

    def __init__(self, message: str = "Missing Authorization header with Bearer token.") -> None:
        super().__init__(message=message, code="MISSING_TOKEN", status_code=401)


class InvalidTokenError(AuthError):
    """Raised when JWT is malformed, has invalid signature, or unknown kid."""

    def __init__(self, message: str = "Invalid or tampered authentication token.") -> None:
        super().__init__(message=message, code="INVALID_TOKEN", status_code=401)


class TokenExpiredError(AuthError):
    """Raised when JWT has expired."""

    def __init__(self, message: str = "Authentication token has expired.") -> None:
        super().__init__(message=message, code="TOKEN_EXPIRED", status_code=401)


class InvalidClaimsError(AuthError):
    """Raised when required claims (issuer, audience, sub) are invalid or missing."""

    def __init__(self, message: str = "Token claims validation failed.") -> None:
        super().__init__(message=message, code="INVALID_CLAIMS", status_code=401)


class JWKSError(AuthError):
    """Raised when public signing keys cannot be retrieved or parsed from JWKS."""

    def __init__(self, message: str = "Failed to resolve signing key from Supabase JWKS.") -> None:
        super().__init__(message=message, code="JWKS_UNAVAILABLE", status_code=401)


class UnauthorizedOperatorError(AuthError):
    """Raised when an authenticated user lacks permission for operational recovery actions."""

    def __init__(
        self,
        message: str = "Authenticated user is not authorized to execute operational recovery actions.",
        user_id: Optional[str] = None,
    ) -> None:
        super().__init__(message=message, code="FORBIDDEN_OPERATOR", status_code=403)
        self.user_id = user_id
