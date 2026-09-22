"""Typed deterministic exceptions for the Customer Data Onboarding & Integration Guardrail."""

from __future__ import annotations

from typing import Any, Optional


class OnboardingError(Exception):
    """Base exception for all customer data onboarding operations."""

    def __init__(self, message: str, details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        """Convert exception to serializable dictionary."""
        return {
            "error_type": self.__class__.__name__,
            "message": self.message,
            "details": self.details,
        }


class SourceEmptyError(OnboardingError):
    """Raised when an ingested source contains zero bytes, zero records, or header-only data."""


class SourceCorruptedError(OnboardingError):
    """Raised when a source contains unparseable or malformed content (e.g. invalid delimiter, syntax error)."""


class SourceTransportError(OnboardingError):
    """Raised when connection or transport fails (e.g. HTTP 4xx/5xx, connection timeout, network failure)."""


class SourceSchemaError(OnboardingError):
    """Raised when source schema cannot be discovered, has duplicate headers, or violates structural constraints."""


class MappingApprovalError(OnboardingError):
    """Base exception for mapping review, approval, and versioning invariant violations."""


class UnresolvedAmbiguityError(MappingApprovalError):
    """Raised when attempting to approve or finalize a mapping that has unresolved ambiguous targets."""


class DuplicateTargetMappingError(MappingApprovalError):
    """Raised when multiple source fields are mapped to the same canonical target field."""


class IncompleteMappingError(MappingApprovalError):
    """Raised when mandatory canonical fields have no active approved mapping."""


class InvalidTargetFieldError(MappingApprovalError):
    """Raised when a mapping references a target field not defined in the canonical schema."""


class InvalidSourceFieldError(MappingApprovalError):
    """Raised when a review decision references a source field not present in the source schema."""


class InvalidTransformationError(MappingApprovalError):
    """Raised when an override specifies an unsupported or unapproved transformation operation."""


class TransformationError(OnboardingError):
    """Base exception for deterministic transformation execution issues."""


class MappingNotApprovedError(TransformationError):
    """Raised when attempting to execute transformation on an unapproved or draft mapping."""


class SourceSchemaMismatchError(TransformationError):
    """Raised when the source schema or fingerprint does not match the approved mapping's expected source."""


class MissingSourceColumnError(TransformationError):
    """Raised when an active mapped source column is missing from the input dataset."""


class CanonicalSchemaMismatchError(TransformationError):
    """Raised when the canonical schema version does not match the approved mapping version."""


class TransformationConfigurationError(TransformationError):
    """Raised when a transformation step configuration is structurally invalid or unparseable."""


class ExceptionQueueError(OnboardingError):
    """Base exception for exception queue, correction, and reprocessing operations."""


class InvalidExceptionStateTransitionError(ExceptionQueueError):
    """Raised when attempting an illegal lifecycle transition on an exception."""


class ExceptionNotFoundError(ExceptionQueueError):
    """Raised when an exception ID is not found in the queue or repository."""


class InvalidCorrectionError(ExceptionQueueError):
    """Raised when a proposed correction is malformed, targets an invalid field, or uses unsupported operations."""


class NonDismissibleExceptionError(ExceptionQueueError):
    """Raised when attempting to dismiss an exception that violates non-dismissible policy."""


class FatalConfigurationFailureError(ExceptionQueueError):
    """Raised when a failure is a batch/mapping/schema fatal configuration issue, not a record-level exception."""


class RunError(OnboardingError):
    """Base exception for onboarding run and idempotency operations."""


class IdempotencyConflictError(RunError):
    """Raised when the same idempotency key is submitted with a different request fingerprint."""


class InvalidRunStateTransitionError(RunError):
    """Raised when an illegal run lifecycle transition is attempted."""


class RunNotFoundError(RunError):
    """Raised when an onboarding run ID is not found."""


class RunExecutionError(RunError):
    """Raised when an onboarding run execution fails."""


class SchemaDriftError(OnboardingError):
    """Base exception for schema drift and mapping impact operations."""


class DriftReportNotFoundError(SchemaDriftError):
    """Raised when a schema drift report ID is not found in the repository."""


class SCD2IntegrationError(OnboardingError):
    """Base exception for all customer data onboarding SCD2 integration boundary operations."""


class SCD2InvariantValidationError(SCD2IntegrationError):
    """Raised when SCD2 output table violates one or more deterministic SCD2 invariant rules."""


class SchemaCompatibilityError(SCD2IntegrationError):
    """Raised when source schema drift compatibility is BROKEN and blocks SCD2 historical processing."""


class NoValidRecordsError(SCD2IntegrationError):
    """Raised when an attempt is made to feed zero valid records into SCD2 processing."""
