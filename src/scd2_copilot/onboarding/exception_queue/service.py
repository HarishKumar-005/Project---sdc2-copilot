"""Exception Queue and Reprocessing Service for M5."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Optional
import polars as pl

from ..canonical import get_canonical_customer_v1
from ..exceptions import (
    FatalConfigurationFailureError,
    InvalidCorrectionError,
    InvalidExceptionStateTransitionError,
    NonDismissibleExceptionError,
)
from ..models.approval import ApprovedMappingVersion
from ..models.exception import (
    CorrectionType,
    CustomerRecordException,
    DEFAULT_NON_DISMISSIBLE_RULES,
    ExceptionBatch,
    ExceptionCategory,
    ExceptionStatus,
    RecordCorrection,
    ReprocessingResult,
)
from ..models.mapping import TransformationOpType, TransformationStep
from ..models.transformation import (
    CanonicalCustomerRecord,
    RecordValidationError,
    TransformationResult,
    TransformedRecord,
    ValidationCategory,
)
from ..profiler.sampling import mask_email, mask_name, mask_phone
from ..transformation.engine import DeterministicTransformationEngine
from ..transformation.validator import DeterministicValidator, ValidationConfig
from .repository import ExceptionQueueRepository


class ExceptionQueueService:
    """Coordinates exception creation from M4 results, explicit corrections, and deterministic reprocessing."""

    def __init__(
        self,
        validation_config: Optional[ValidationConfig] = None,
        repository: Optional[ExceptionQueueRepository] = None,
    ) -> None:
        self.validation_config = validation_config or ValidationConfig()
        self.repository = repository or ExceptionQueueRepository()

    def enqueue_from_transformation_result(
        self,
        result: TransformationResult,
        run_id: Optional[str] = None,
        source_id: Optional[str] = None,
        mapping_version_id: Optional[str] = None,
    ) -> ExceptionBatch:
        """Create exceptions from transformation result, persist to repository, and return ExceptionBatch."""
        exceptions = self.create_exceptions_from_transformation_result(result, run_id=run_id)
        self.repository.add_batch(exceptions)
        batch = ExceptionBatch(
            batch_id=f"batch_{run_id or result.source_id}",
            source_id=source_id or result.source_id,
            run_id=run_id,
            mapping_version_id=mapping_version_id or result.mapping_version_id,
            exceptions=exceptions,
        )
        return batch

    def list_exceptions(
        self,
        run_id: Optional[str] = None,
        status: Optional[ExceptionStatus] = None,
        rule_id: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> list[CustomerRecordException]:
        """Filter and list exceptions matching criteria."""
        return self.repository.list_exceptions(
            status=status,
            rule_id=rule_id,
            source_id=source_id,
            run_id=run_id,
        )

    def create_exceptions_from_transformation_result(
        self,
        result: TransformationResult,
        run_id: Optional[str] = None,
    ) -> list[CustomerRecordException]:
        """Convert M4 invalid records and validation errors into durable CustomerRecordException objects."""
        # Fatal check: if mapping version was unapproved or schema mismatched, that's not a record exception
        if not result.mapping_version_id:
            raise FatalConfigurationFailureError("Cannot extract record exceptions without an approved mapping version ID.")

        exceptions: list[CustomerRecordException] = []
        seen_exception_ids: set[str] = set()

        for record in result.invalid_records:
            record_key = record.source_record_id or f"row_{record.row_index}"
            clean_key = re.sub(r"[^a-zA-Z0-9_-]", "_", str(record_key))

            for err in record.errors:
                clean_rule = re.sub(r"[^a-zA-Z0-9_-]", "_", str(err.rule_id))
                clean_field = re.sub(r"[^a-zA-Z0-9_-]", "_", str(err.field))
                exception_id = f"exc_{result.source_id}_row{record.row_index}_{clean_key}_{clean_rule}_{clean_field}"

                # Deterministic deduplication within the batch
                if exception_id in seen_exception_ids:
                    continue
                seen_exception_ids.add(exception_id)

                category = self._map_category(err.category)
                safe_observed = self._sanitize_observed_value(err.field, err.observed_value)
                suggested_fix = self._compute_suggested_fix(err.rule_id, err.field)

                exc = CustomerRecordException(
                    exception_id=exception_id,
                    run_id=run_id,
                    source_id=result.source_id,
                    source_record_id=record.source_record_id,
                    row_index=record.row_index,
                    rule_id=err.rule_id,
                    category=category,
                    field=err.field,
                    observed_value=safe_observed,
                    expected=self._compute_expected(err.rule_id, err.field),
                    reason=err.reason,
                    suggested_fix=suggested_fix,
                    status=ExceptionStatus.OPEN,
                    source_schema_version=None,
                    canonical_schema_version=result.canonical_schema_version,
                    mapping_version_id=result.mapping_version_id,
                    raw_record=dict(record.raw_values),
                    corrections=[],
                    reprocessing_history=[],
                )
                exceptions.append(exc)

        return exceptions

    def apply_correction(
        self,
        exception: CustomerRecordException,
        field: str,
        correction_type: CorrectionType,
        applied_by: str,
        reason: str,
        corrected_value: Optional[Any] = None,
        transformations: Optional[list[TransformationStep]] = None,
    ) -> CustomerRecordException:
        """Validate and record an explicit structured correction on an exception."""
        # 1. State check
        if exception.status in (ExceptionStatus.RESOLVED, ExceptionStatus.DISMISSED):
            raise InvalidExceptionStateTransitionError(
                f"Cannot apply correction to exception '{exception.exception_id}' in state '{exception.status}'.",
                details={"exception_id": exception.exception_id, "current_status": exception.status.value},
            )

        # 2. Field check: must exist in canonical schema or in raw record
        canonical_fields = set(get_canonical_customer_v1().field_names)
        raw_fields = set(exception.raw_record.keys())
        if field not in canonical_fields and field not in raw_fields:
            raise InvalidCorrectionError(
                f"Correction field '{field}' does not exist in canonical schema {sorted(canonical_fields)} "
                f"or source raw record fields {sorted(raw_fields)}.",
                details={"field": field, "canonical_fields": list(canonical_fields), "source_fields": list(raw_fields)},
            )

        # 3. Validation by correction type
        trans_list = transformations or []
        if correction_type == CorrectionType.APPLY_TRANSFORMATION:
            if not trans_list:
                raise InvalidCorrectionError(
                    "CorrectionType.APPLY_TRANSFORMATION requires at least one transformation step.",
                    details={"exception_id": exception.exception_id, "field": field},
                )
            for step in trans_list:
                if not isinstance(step.op, TransformationOpType):
                    raise InvalidCorrectionError(
                        f"Unsupported transformation operation: '{step.op}'. Must be an approved TransformationOpType.",
                        details={"op": str(step.op)},
                    )

        # 4. Construct correction
        correction_id = f"corr_{exception.exception_id}_{len(exception.corrections) + 1}"
        correction = RecordCorrection(
            correction_id=correction_id,
            exception_id=exception.exception_id,
            correction_type=correction_type,
            field=field,
            corrected_value=corrected_value,
            transformations=trans_list,
            applied_by=applied_by,
            reason=reason,
        )

        return exception.with_correction(correction)

    def reprocess_exception(
        self,
        exception: CustomerRecordException,
        approved_version: ApprovedMappingVersion,
        reprocessed_by: str = "system",
        validation_config: Optional[ValidationConfig] = None,
    ) -> tuple[CustomerRecordException, Optional[CanonicalCustomerRecord]]:
        """Deterministically reprocess a single exception using the existing M4 transformation and validation path."""
        # 1. State check
        if exception.status not in (ExceptionStatus.CORRECTED, ExceptionStatus.REPROCESSED, ExceptionStatus.OPEN):
            raise InvalidExceptionStateTransitionError(
                f"Cannot reprocess exception '{exception.exception_id}' in state '{exception.status}'.",
                details={"exception_id": exception.exception_id, "current_status": exception.status.value},
            )

        config = validation_config or self.validation_config
        engine = DeterministicTransformationEngine(approved_version)
        validator = DeterministicValidator(config=config)

        # 2. Build corrected source row dictionary
        corrected_raw = dict(exception.raw_record)
        canonical_overrides: dict[str, Any] = {}

        for corr in exception.corrections:
            if corr.correction_type == CorrectionType.VALUE_OVERRIDE:
                if corr.field in corrected_raw:
                    corrected_raw[corr.field] = corr.corrected_value
                else:
                    # Check if mapped to a source field
                    mapped_source = None
                    for m in approved_version.get_active_mappings():
                        if m.target_field == corr.field and m.source_field in corrected_raw:
                            mapped_source = m.source_field
                            break
                    if mapped_source:
                        corrected_raw[mapped_source] = corr.corrected_value
                    else:
                        canonical_overrides[corr.field] = corr.corrected_value

            elif corr.correction_type == CorrectionType.APPLY_TRANSFORMATION:
                if corr.field in corrected_raw:
                    val = corrected_raw[corr.field]
                    for step in corr.transformations:
                        val, _ = engine._execute_step(
                            val, step, corr.field, exception.row_index, exception.source_record_id, corrected_raw
                        )
                    corrected_raw[corr.field] = val

        # 3. Transform corrected row via M4 Engine
        df_single = pl.DataFrame([corrected_raw])
        transformed_records = engine.transform_dataframe(df_single)
        trans_rec = transformed_records[0]

        # Apply any direct canonical overrides if specified
        if canonical_overrides:
            merged_canonical = dict(trans_rec.canonical_values)
            merged_canonical.update(canonical_overrides)
            trans_rec = trans_rec.model_copy(update={"canonical_values": merged_canonical})

        # 4. Validate transformed record via M4 Validator
        valid_recs, invalid_recs, _ = validator.validate_records([trans_rec])

        attempt_id = f"rep_{exception.exception_id}_{len(exception.reprocessing_history) + 1}"
        now = datetime.now(timezone.utc)

        if valid_recs:
            canonical_rec = valid_recs[0]
            rep_result = ReprocessingResult(
                attempt_id=attempt_id,
                exception_id=exception.exception_id,
                attempted_at=now,
                reprocessed_by=reprocessed_by,
                success=True,
                errors=[],
                canonical_record=canonical_rec,
            )
            updated_exc = exception.with_reprocessing(rep_result)
            return updated_exc, canonical_rec
        else:
            rep_result = ReprocessingResult(
                attempt_id=attempt_id,
                exception_id=exception.exception_id,
                attempted_at=now,
                reprocessed_by=reprocessed_by,
                success=False,
                errors=invalid_recs[0].errors,
                canonical_record=None,
            )
            updated_exc = exception.with_reprocessing(rep_result)
            return updated_exc, None

    def reprocess_batch(
        self,
        exceptions: list[CustomerRecordException],
        approved_version: ApprovedMappingVersion,
        reprocessed_by: str = "system",
        validation_config: Optional[ValidationConfig] = None,
    ) -> tuple[list[CustomerRecordException], list[CanonicalCustomerRecord]]:
        """Deterministically reprocess a collection of exceptions together, evaluating cross-record rules."""
        if not exceptions:
            return [], []

        config = validation_config or self.validation_config
        engine = DeterministicTransformationEngine(approved_version)
        validator = DeterministicValidator(config=config)

        # 1. Prepare raw rows with all corrections applied
        transformed_list: list[TransformedRecord] = []
        canonical_overrides_by_idx: dict[int, dict[str, Any]] = {}

        for i, exc in enumerate(exceptions):
            corrected_raw = dict(exc.raw_record)
            c_overrides: dict[str, Any] = {}

            for corr in exc.corrections:
                if corr.correction_type == CorrectionType.VALUE_OVERRIDE:
                    if corr.field in corrected_raw:
                        corrected_raw[corr.field] = corr.corrected_value
                    else:
                        mapped_source = None
                        for m in approved_version.get_active_mappings():
                            if m.target_field == corr.field and m.source_field in corrected_raw:
                                mapped_source = m.source_field
                                break
                        if mapped_source:
                            corrected_raw[mapped_source] = corr.corrected_value
                        else:
                            c_overrides[corr.field] = corr.corrected_value
                elif corr.correction_type == CorrectionType.APPLY_TRANSFORMATION:
                    if corr.field in corrected_raw:
                        val = corrected_raw[corr.field]
                        for step in corr.transformations:
                            val, _ = engine._execute_step(
                                val, step, corr.field, exc.row_index, exc.source_record_id, corrected_raw
                            )
                        corrected_raw[corr.field] = val

            single_df = pl.DataFrame([corrected_raw])
            t_recs = engine.transform_dataframe(single_df)
            t_rec = t_recs[0].model_copy(update={"row_index": i})
            if c_overrides:
                merged = dict(t_rec.canonical_values)
                merged.update(c_overrides)
                t_rec = t_rec.model_copy(update={"canonical_values": merged})
            transformed_list.append(t_rec)

        # 2. Validate all records together in batch (enforces batch duplicate detection)
        valid_recs, invalid_recs, _ = validator.validate_records(transformed_list)

        invalid_rows = {inv.row_index: inv for inv in invalid_recs}
        valid_by_id = {v.customer_id: v for v in valid_recs}

        updated_exceptions: list[CustomerRecordException] = []
        valid_canonical_records: list[CanonicalCustomerRecord] = []
        now = datetime.now(timezone.utc)

        for i, (exc, t_rec) in enumerate(zip(exceptions, transformed_list)):
            attempt_id = f"rep_{exc.exception_id}_{len(exc.reprocessing_history) + 1}"
            cid = t_rec.canonical_values.get("customer_id")

            if i in invalid_rows:
                inv = invalid_rows[i]
                rep_result = ReprocessingResult(
                    attempt_id=attempt_id,
                    exception_id=exc.exception_id,
                    attempted_at=now,
                    reprocessed_by=reprocessed_by,
                    success=False,
                    errors=inv.errors,
                    canonical_record=None,
                )
                updated_exc = exc.with_reprocessing(rep_result)
                updated_exceptions.append(updated_exc)
            else:
                can_rec = valid_by_id.get(cid)
                if can_rec is None:
                    can_rec = CanonicalCustomerRecord(
                        customer_id=t_rec.canonical_values["customer_id"],
                        first_name=t_rec.canonical_values["first_name"],
                        last_name=t_rec.canonical_values["last_name"],
                        email=t_rec.canonical_values["email"],
                        date_of_birth=t_rec.canonical_values.get("date_of_birth"),
                        status=t_rec.canonical_values["status"],
                        created_at=t_rec.canonical_values["created_at"],
                    )
                rep_result = ReprocessingResult(
                    attempt_id=attempt_id,
                    exception_id=exc.exception_id,
                    attempted_at=now,
                    reprocessed_by=reprocessed_by,
                    success=True,
                    errors=[],
                    canonical_record=can_rec,
                )
                updated_exc = exc.with_reprocessing(rep_result)
                updated_exceptions.append(updated_exc)
                valid_canonical_records.append(can_rec)

        return updated_exceptions, valid_canonical_records

    def dismiss_exception(
        self,
        exception: CustomerRecordException,
        dismissed_by: str,
        reason: str,
        non_dismissible_rules: Optional[set[str]] = None,
    ) -> CustomerRecordException:
        """Dismiss an exception under explicit policy authorization."""
        return exception.with_dismissal(
            dismissed_by=dismissed_by,
            reason=reason,
            non_dismissible_rules=non_dismissible_rules,
        )

    @staticmethod
    def _map_category(cat: ValidationCategory) -> ExceptionCategory:
        if cat == ValidationCategory.REQUIRED:
            return ExceptionCategory.VALIDATION_ERROR
        elif cat in (ValidationCategory.TYPE, ValidationCategory.FORMAT, ValidationCategory.ENUM):
            return ExceptionCategory.VALIDATION_ERROR
        elif cat in (ValidationCategory.UNIQUE, ValidationCategory.DUPLICATE):
            return ExceptionCategory.DUPLICATE_ERROR
        elif cat == ValidationCategory.TRANSFORMATION:
            return ExceptionCategory.TRANSFORMATION_ERROR
        elif cat == ValidationCategory.BUSINESS_RULE:
            return ExceptionCategory.BUSINESS_RULE_ERROR
        elif cat == ValidationCategory.REFERENTIAL_INTEGRITY:
            return ExceptionCategory.REFERENTIAL_INTEGRITY_ERROR
        return ExceptionCategory.VALIDATION_ERROR

    @staticmethod
    def _sanitize_observed_value(field: str, observed_value: Optional[str]) -> Optional[str]:
        if observed_value is None:
            return None
        s = str(observed_value).strip()
        lower_field = field.lower()
        if "email" in lower_field:
            return mask_email(s)
        elif "phone" in lower_field:
            return mask_phone(s)
        elif "name" in lower_field and ("first" in lower_field or "last" in lower_field):
            return mask_name(s)
        return s

    @staticmethod
    def _compute_suggested_fix(rule_id: str, field: str) -> str:
        if "REQUIRED" in rule_id or rule_id == "REQUIRED_FIELD_MISSING":
            return f"Provide a non-null, non-empty value for mandatory canonical field '{field}'."
        elif rule_id == "INVALID_EMAIL_FORMAT":
            return "Correct email format to 'user@domain.tld' or normalize raw email value."
        elif rule_id == "INVALID_STATUS_ENUM":
            return "Map status to canonical 'ACTIVE' or 'INACTIVE' via enum mapping or explicit override."
        elif rule_id == "DUPLICATE_CUSTOMER_ID":
            return "Resolve customer_id identity collision by providing a unique identifier."
        elif rule_id == "INVALID_DATE_OF_BIRTH":
            return "Ensure date of birth is a valid historical date in the past."
        elif rule_id == "INVALID_CREATED_AT":
            return "Ensure created_at timestamp is not in the future."
        elif rule_id == "REFERENTIAL_INTEGRITY_VIOLATION":
            return f"Ensure referenced parent entity key for '{field}' exists in authoritative store."
        elif "CAST_ERROR" in rule_id:
            return f"Ensure source value for '{field}' can be converted to expected primitive type."
        elif rule_id == "INVALID_DATE":
            return f"Ensure date string for '{field}' matches configured date format pattern."
        return f"Review and correct value for field '{field}' to satisfy canonical contract."

    @staticmethod
    def _compute_expected(rule_id: str, field: str) -> str:
        if "REQUIRED" in rule_id:
            return f"Non-null, non-empty {field}"
        elif rule_id == "INVALID_EMAIL_FORMAT":
            return "Valid RFC-compliant email address format"
        elif rule_id == "INVALID_STATUS_ENUM":
            return "One of: ACTIVE, INACTIVE"
        elif rule_id == "DUPLICATE_CUSTOMER_ID":
            return "Unique customer_id across source batch"
        elif rule_id == "INVALID_DATE_OF_BIRTH":
            return "Historical date (<= today)"
        elif rule_id == "INVALID_CREATED_AT":
            return "Timestamp <= current execution time"
        elif rule_id == "REFERENTIAL_INTEGRITY_VIOLATION":
            return "Key present in reference dataset"
        return "Valid canonical customer value"
