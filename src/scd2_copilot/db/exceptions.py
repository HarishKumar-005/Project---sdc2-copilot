"""Database exceptions for SCD2 Copilot V2 data-access layer."""

from __future__ import annotations

from typing import Optional

from ..exceptions import SCD2Error


class DatabaseError(SCD2Error):
    """Base exception for all database-related operations."""

    def __init__(self, message: str, *, cause: Optional[Exception] = None) -> None:
        super().__init__(message)
        self.cause = cause


class DatabaseConfigurationError(DatabaseError):
    """Raised when database configuration (e.g. DATABASE_URL) is missing, malformed, or invalid."""


class DatabaseConnectionError(DatabaseError):
    """Raised when connecting to the database fails (DNS, timeout, refusal, auth)."""


class DatabaseQueryError(DatabaseError):
    """Raised when a SQL query execution fails."""

    def __init__(
        self,
        message: str,
        *,
        query: Optional[str] = None,
        table: Optional[str] = None,
        cause: Optional[Exception] = None,
    ) -> None:
        super().__init__(message, cause=cause)
        self.query = query
        self.table = table


class DatabaseTransactionError(DatabaseError):
    """Raised when a transaction block fails to commit or roll back."""


class DatabaseIntegrityError(DatabaseQueryError):
    """Raised when a unique, foreign key, or check constraint is violated."""


class EntityNotFoundError(DatabaseQueryError):
    """Raised when an expected row (e.g. run_id or checkpoint) is not found."""
