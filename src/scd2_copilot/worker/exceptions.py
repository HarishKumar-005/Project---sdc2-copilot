"""Worker domain exception hierarchy for incremental ingestion."""

from __future__ import annotations

from typing import Any, Optional


class WorkerError(Exception):
    """Base exception for all ingestion worker errors."""

    def __init__(self, message: str, cause: Optional[Exception] = None) -> None:
        super().__init__(message)
        self.cause = cause


class BatchProcessingError(WorkerError):
    """Raised when micro-batch processing or transformation fails."""


class WorkerValidationFailureError(WorkerError):
    """Raised when SCD2 invariant validation fails on the processed micro-batch."""

    def __init__(self, message: str, failures: Optional[list[str]] = None) -> None:
        super().__init__(message)
        self.failures = failures or []


class CheckpointAdvancementError(WorkerError):
    """Raised when the stream checkpoint cannot be updated or verified."""


class WorkerShutdownException(WorkerError):
    """Raised to cleanly interrupt long-running loops upon graceful shutdown request."""
