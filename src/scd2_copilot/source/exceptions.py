"""Typed exceptions for PostgreSQL source configuration and adapter boundary."""

from __future__ import annotations

from typing import Optional


class SourceConfigurationError(Exception):
    """Base exception for source configuration and validation errors."""

    def __init__(self, message: str, details: Optional[dict] = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class SourceConnectionError(SourceConfigurationError):
    """Raised when the PostgreSQL source database cannot be reached."""


class SourceSchemaNotFoundError(SourceConfigurationError):
    """Raised when the configured database schema does not exist."""


class SourceTableNotFoundError(SourceConfigurationError):
    """Raised when the configured source table does not exist in the target schema."""


class SourceColumnNotFoundError(SourceConfigurationError):
    """Raised when one or more configured columns (keys, timestamp, or tracked) do not exist."""


class SourceTypeMismatchError(SourceConfigurationError):
    """Raised when a column's data type is incompatible (e.g. non-temporal change timestamp)."""
