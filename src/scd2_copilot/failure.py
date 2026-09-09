"""Failure classification and semantics for the SCD2 Copilot pipeline.

Categorizes pipeline errors into deterministic, validation, transient AI,
permanent AI, and unexpected internal errors to guide fail-fast, retry,
and flow-state behavior.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from .exceptions import DuplicateBusinessKeyError, InvalidTemporalValueError


class FailureCategory(str, Enum):
    """Authoritative failure classifications for pipeline tasks and flows."""

    DETERMINISTIC_INPUT_ERROR = "deterministic_input_error"
    DETERMINISTIC_SCHEMA_ERROR = "deterministic_schema_error"
    DETERMINISTIC_SCD2_ERROR = "deterministic_scd2_error"
    VALIDATION_FAILURE = "validation_failure"
    TRANSIENT_AI_ERROR = "transient_ai_error"
    PERMANENT_AI_ERROR = "permanent_ai_error"
    PERSISTENCE_ERROR = "persistence_error"
    UNEXPECTED_INTERNAL_ERROR = "unexpected_internal_error"


def classify_pipeline_exception(
    exc: Exception, task_name: Optional[str] = None
) -> FailureCategory:
    """Classify an exception into a FailureCategory based on type, task, and metadata.

    Args:
        exc: The exception encountered.
        task_name: Optional name of the task where the exception occurred.

    Returns:
        FailureCategory identifying the nature of the failure.
    """
    # 1. SCD2 domain invariants (duplicate keys, invalid dates)
    if isinstance(exc, (DuplicateBusinessKeyError, InvalidTemporalValueError)):
        return FailureCategory.DETERMINISTIC_SCD2_ERROR

    msg = str(exc).lower()

    # 2. Ingestion errors (missing file, bad CSV format, schema mismatch)
    if task_name in ("ingest_csvs", "ingest_task"):
        if isinstance(exc, (FileNotFoundError, IsADirectoryError, PermissionError)):
            return FailureCategory.DETERMINISTIC_INPUT_ERROR
        if isinstance(exc, ValueError) or "csv" in msg:
            return FailureCategory.DETERMINISTIC_INPUT_ERROR

    # 3. Schema detection errors (missing keys, invalid tracked columns)
    if task_name in ("detect_schema", "schema_task"):
        if isinstance(exc, ValueError) or "key" in msg or "column" in msg:
            return FailureCategory.DETERMINISTIC_SCHEMA_ERROR

    # 4. Persistence errors (disk full, permissions, artifact write failure)
    if task_name in ("persist_artifacts", "persist_task", "write_run_artifacts") or "persistence" in msg or "artifact" in msg:
        return FailureCategory.PERSISTENCE_ERROR

    # 5. AI explanation errors (transient vs permanent vs internal)
    if task_name in ("explain_changes", "explain_task") or "ai" in msg or "gemini" in msg or "groq" in msg:
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        status = getattr(exc, "status", None)

        # Permanent authentication or permission errors
        if code in (401, 403) or status in ("UNAUTHENTICATED", "PERMISSION_DENIED"):
            return FailureCategory.PERMANENT_AI_ERROR
        if "unauthenticated" in msg or "permission_denied" in msg or "api key not valid" in msg or "invalid api key" in msg:
            return FailureCategory.PERMANENT_AI_ERROR

        # Permanent model not found or invalid endpoint
        if code == 404 or status == "NOT_FOUND" or "not found" in msg:
            return FailureCategory.PERMANENT_AI_ERROR

        # Permanent daily quota exhaustion
        if "perday" in msg or "daily" in msg or ("quota exceeded" in msg and "day" in msg):
            return FailureCategory.PERMANENT_AI_ERROR

        # Client-side prompt/schema rejection (permanent)
        if code == 400 or "invalid_argument" in msg:
            return FailureCategory.PERMANENT_AI_ERROR

        # Transient timeouts and network drops
        if isinstance(exc, (TimeoutError, ConnectionError)):
            return FailureCategory.TRANSIENT_AI_ERROR
        if any(tok in msg for tok in ("timeout", "timed out", "connection reset", "temporarily unavailable", "deadline exceeded", "rate limit", "resource exhausted")):
            return FailureCategory.TRANSIENT_AI_ERROR

        # Transient rate limits (per-minute) and server 5xx errors
        if code in (429, 500, 502, 503, 504) or status in ("RESOURCE_EXHAUSTED", "INTERNAL", "UNAVAILABLE", "DEADLINE_EXCEEDED"):
            return FailureCategory.TRANSIENT_AI_ERROR

        # Deterministic programming / input errors within AI task
        if isinstance(exc, (ValueError, TypeError, KeyError, AttributeError, IndexError)):
            return FailureCategory.UNEXPECTED_INTERNAL_ERROR

        # Other unexpected exceptions
        return FailureCategory.UNEXPECTED_INTERNAL_ERROR

    # 5. Generic check for duplicate keys in message if not wrapped in DuplicateBusinessKeyError
    if "duplicate business key" in msg or "duplicate" in msg and "key" in msg:
        return FailureCategory.DETERMINISTIC_SCD2_ERROR

    # 6. Fallback
    return FailureCategory.UNEXPECTED_INTERNAL_ERROR
