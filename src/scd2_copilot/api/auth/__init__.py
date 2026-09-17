"""Authentication and authorization package for Supabase JWT verification."""

from .dependencies import (
    get_current_operator,
    get_jwt_verifier,
    get_optional_operator,
    require_authorized_operator,
    set_jwt_verifier,
)
from .exceptions import (
    AuthError,
    InvalidClaimsError,
    InvalidTokenError,
    JWKSError,
    MissingTokenError,
    TokenExpiredError,
    UnauthorizedOperatorError,
)
from .models import AuthenticatedOperator
from .verifier import SupabaseJWTVerifier

__all__ = [
    "AuthenticatedOperator",
    "AuthError",
    "InvalidClaimsError",
    "InvalidTokenError",
    "JWKSError",
    "MissingTokenError",
    "SupabaseJWTVerifier",
    "TokenExpiredError",
    "UnauthorizedOperatorError",
    "get_current_operator",
    "get_jwt_verifier",
    "get_optional_operator",
    "require_authorized_operator",
    "set_jwt_verifier",
]
