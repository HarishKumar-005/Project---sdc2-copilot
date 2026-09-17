"""Data model for verified authenticated operator identity derived from Supabase JWT claims."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional
from uuid import UUID


@dataclass(frozen=True)
class AuthenticatedOperator:
    """Immutable representation of an authenticated operator.

    Constructed strictly from verified Supabase JWT claims.
    Never constructed from untrusted client-supplied headers.
    """

    user_id: UUID
    email: Optional[str] = None
    role: str = "authenticated"
    app_metadata: dict[str, Any] = field(default_factory=dict)
    user_metadata: dict[str, Any] = field(default_factory=dict)
    session_id: Optional[UUID] = None
    is_authorized_operator: bool = False

    @property
    def display_name(self) -> str:
        """Friendly name for logging and auditing."""
        if self.user_metadata and self.user_metadata.get("full_name"):
            return str(self.user_metadata["full_name"])
        if self.email:
            return self.email
        return str(self.user_id)

    def to_audit_dict(self) -> dict[str, Any]:
        """Sanitized dictionary suitable for audit logging and execution metadata."""
        return {
            "user_id": str(self.user_id),
            "email": self.email,
            "role": self.role,
            "session_id": str(self.session_id) if self.session_id else None,
            "is_authorized_operator": self.is_authorized_operator,
        }
