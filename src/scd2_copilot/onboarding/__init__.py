"""Customer Data Onboarding & Integration Guardrail bounded context."""

from .adapters.base import SourceAdapter
from .adapters.csv_adapter import CSVSourceAdapter
from .adapters.mock_rest_adapter import MockRESTSourceAdapter
from .approval.service import MappingReviewService, MappingReviewSession
from .exceptions import (
    CanonicalSchemaMismatchError,
    DuplicateTargetMappingError,
    ExceptionNotFoundError,
    ExceptionQueueError,
    FatalConfigurationFailureError,
    IncompleteMappingError,
    InvalidCorrectionError,
    InvalidExceptionStateTransitionError,
    InvalidSourceFieldError,
    InvalidTargetFieldError,
    InvalidTransformationError,
    MappingApprovalError,
    MappingNotApprovedError,
    MissingSourceColumnError,
    NonDismissibleExceptionError,
    OnboardingError,
    SourceCorruptedError,
    SourceEmptyError,
    SourceSchemaError,
    SourceSchemaMismatchError,
    SourceTransportError,
    TransformationConfigurationError,
    TransformationError,
    UnresolvedAmbiguityError,
    RunError,
    IdempotencyConflictError,
    InvalidRunStateTransitionError,
    RunNotFoundError,
    RunExecutionError,
    SchemaDriftError,
    DriftReportNotFoundError,
    SCD2IntegrationError,
    SCD2InvariantValidationError,
    SchemaCompatibilityError,
    NoValidRecordsError,
)

from .scd2 import (
    CustomerPointInTimeState,
    CustomerSCD2Adapter,
    CustomerSCD2Config,
    CustomerSCD2ExecutionResult,
    CustomerSCD2Service,
)

from .guardrail import (
    CustomerGuardrailAdapter,
    CustomerGuardrailConfig,
    CustomerGuardrailEvaluationResult,
    CustomerHistoricalGuardrailService,
)

from .models.drift import (
    DriftType,
    FieldMappingImpact,
    MappingCompatibilityState,
    SchemaDiffResult,
    SchemaDriftEvent,
    SchemaDriftReport,
)
from .drift.engine import DeterministicSchemaDiffEngine
from .drift.impact import MappingImpactAnalyzer
from .drift.repository import SchemaDriftRepository
from .drift.service import SchemaDriftService
from .models.run import (

    OnboardingRun,
    RunArtifactPaths,
    RunCancelRequest,
    RunCreateRequest,
    RunListResponse,
    RunMetrics,
    RunResponse,
    RunRetryRequest,
    RunStatus,
    VALID_RUN_TRANSITIONS,
    validate_run_transition,
)
from .runs.fingerprint import compute_input_fingerprint, compute_request_fingerprint
from .runs.repository import OnboardingRunRepository
from .runs.service import OnboardingRunService
from .models.approval import (
    ApprovedMappingDefinition,
    ApprovedMappingVersion,
    FieldReviewDecision,
    ReviewDecisionType,
)
from .models.exception import (
    CorrectionType,
    CustomerRecordException,
    DEFAULT_NON_DISMISSIBLE_RULES,
    ExceptionBatch,
    ExceptionCategory,
    ExceptionStatus,
    RecordCorrection,
    ReprocessingResult,
)
from .exception_queue.repository import ExceptionQueueRepository
from .exception_queue.service import ExceptionQueueService
from .models.profile import (
    ColumnProfile,
    DataProfile,
    DatePatternReport,
    SamplingConfig,
    SamplingPolicy,
    ValueFrequency,
)
from .models.schema_snapshot import (
    ColumnSnapshot,
    SchemaFingerprint,
    SourceSchemaSnapshot,
)
from .models.source import SourceDefinition, SourceType
from .models.transformation import (
    CanonicalCustomerRecord,
    RecordValidationError,
    TransformationResult,
    TransformedRecord,
    ValidationCategory,
    ValidationErrorSeverity,
)
from .profiler.engine import DataProfiler
from .profiler.fingerprint import compute_schema_fingerprint, normalize_column_name

from .canonical import CanonicalField, CanonicalSchema, get_canonical_customer_v1
from .mapping.candidates import DeterministicCandidateGenerator
from .mapping.engine import SemanticMappingEngine
from .mapping.prompt import build_mapping_prompt
from .models.mapping import (
    CandidateMapping,
    DeterministicEvidence,
    FieldMappingProposal,
    LLMFieldProposal,
    LLMMappingBatchResponse,
    MappingProposalBatch,
    MappingType,
    TransformationOpType,
    TransformationStep,
)
from .transformation.engine import DeterministicTransformationEngine
from .transformation.service import TransformationPipeline
from .transformation.validator import DeterministicValidator, ValidationConfig

__all__ = [
    # Canonical
    "CanonicalField",
    "CanonicalSchema",
    "get_canonical_customer_v1",
    # Adapters
    "SourceAdapter",
    "CSVSourceAdapter",
    "MockRESTSourceAdapter",
    # Exceptions
    "OnboardingError",
    "SourceEmptyError",
    "SourceCorruptedError",
    "SourceTransportError",
    "SourceSchemaError",
    "MappingApprovalError",
    "UnresolvedAmbiguityError",
    "DuplicateTargetMappingError",
    "IncompleteMappingError",
    "InvalidTargetFieldError",
    "InvalidSourceFieldError",
    "InvalidTransformationError",
    "TransformationError",
    "MappingNotApprovedError",
    "SourceSchemaMismatchError",
    "MissingSourceColumnError",
    "CanonicalSchemaMismatchError",
    "TransformationConfigurationError",
    # Models
    "SourceType",
    "SourceDefinition",
    "ColumnSnapshot",
    "SchemaFingerprint",
    "SourceSchemaSnapshot",
    "SamplingPolicy",
    "SamplingConfig",
    "ValueFrequency",
    "DatePatternReport",
    "ColumnProfile",
    "DataProfile",
    "TransformationOpType",
    "TransformationStep",
    "MappingType",
    "DeterministicEvidence",
    "CandidateMapping",
    "FieldMappingProposal",
    "MappingProposalBatch",
    "LLMFieldProposal",
    "LLMMappingBatchResponse",
    "ReviewDecisionType",
    "FieldReviewDecision",
    "ApprovedMappingDefinition",
    "ApprovedMappingVersion",
    "ValidationCategory",
    "ValidationErrorSeverity",
    "RecordValidationError",
    "CanonicalCustomerRecord",
    "TransformedRecord",
    "TransformationResult",
    # Profiler & Utils
    "DataProfiler",
    "compute_schema_fingerprint",
    "normalize_column_name",
    # Mapping Engine
    "DeterministicCandidateGenerator",
    "SemanticMappingEngine",
    "build_mapping_prompt",
    # Approval & Versioning
    "MappingReviewService",
    "MappingReviewSession",
    # Transformation & Validation
    "DeterministicTransformationEngine",
    "DeterministicValidator",
    "ValidationConfig",
    "TransformationPipeline",
    # M5 Exception Queue & Reprocessing
    "ExceptionQueueError",
    "InvalidExceptionStateTransitionError",
    "ExceptionNotFoundError",
    "InvalidCorrectionError",
    "NonDismissibleExceptionError",
    "FatalConfigurationFailureError",
    "ExceptionCategory",
    "ExceptionStatus",
    "CorrectionType",
    "RecordCorrection",
    "ReprocessingResult",
    "DEFAULT_NON_DISMISSIBLE_RULES",
    "CustomerRecordException",
    "ExceptionBatch",
    "ExceptionQueueService",
    "ExceptionQueueRepository",
    # M6 Onboarding Runs, API, and Idempotency
    "RunError",
    "IdempotencyConflictError",
    "InvalidRunStateTransitionError",
    "RunNotFoundError",
    "RunExecutionError",
    "RunStatus",
    "VALID_RUN_TRANSITIONS",
    "validate_run_transition",
    "RunMetrics",
    "RunArtifactPaths",
    "OnboardingRun",
    "RunCreateRequest",
    "RunResponse",
    "RunListResponse",
    "RunRetryRequest",
    "RunCancelRequest",
    "compute_input_fingerprint",
    "compute_request_fingerprint",
    "OnboardingRunRepository",
    "OnboardingRunService",
    # M7 Schema Drift and Mapping Impact
    "SchemaDriftError",
    "DriftReportNotFoundError",
    "DriftType",
    "MappingCompatibilityState",
    "SchemaDriftEvent",
    "FieldMappingImpact",
    "SchemaDiffResult",
    "SchemaDriftReport",
    "DeterministicSchemaDiffEngine",
    "MappingImpactAnalyzer",
    "SchemaDriftRepository",
    "SchemaDriftService",
    # M8 SCD2 Integration
    "SCD2IntegrationError",
    "SCD2InvariantValidationError",
    "SchemaCompatibilityError",
    "NoValidRecordsError",
    "CustomerSCD2Config",
    "CustomerSCD2ExecutionResult",
    "CustomerPointInTimeState",
    "CustomerSCD2Adapter",
    "CustomerSCD2Service",
    # M9 Historical Guardrail Integration
    "CustomerGuardrailConfig",
    "CustomerGuardrailEvaluationResult",
    "CustomerGuardrailAdapter",
    "CustomerHistoricalGuardrailService",
]



