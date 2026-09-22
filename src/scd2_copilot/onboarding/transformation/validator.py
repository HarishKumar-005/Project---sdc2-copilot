"""Deterministic validation engine verifying canonical customer records against customer.v1."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
import logging
import re
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field

from ..canonical import CanonicalSchema, get_canonical_customer_v1
from ..models.transformation import (
    CanonicalCustomerRecord,
    RecordValidationError,
    TransformedRecord,
    ValidationCategory,
    ValidationErrorSeverity,
)

logger = logging.getLogger("scd2_copilot.onboarding.validation")

# Strict standard email format regex
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")


class ValidationConfig(BaseModel):
    """Configurable validation flags and business rules."""

    model_config = ConfigDict(frozen=True)

    validate_email_uniqueness: bool = Field(default=False, description="Whether to enforce unique email addresses in batch")
    disallow_future_created_at: bool = Field(default=True, description="Whether created_at in the future is rejected")
    disallow_future_dob: bool = Field(default=True, description="Whether date_of_birth in the future is rejected")
    reference_datasets: dict[str, set[str]] = Field(default_factory=dict, description="Reference sets for referential integrity")


class DeterministicValidator:
    """Deterministic validation engine enforcing canonical customer contract invariants."""

    def __init__(
        self,
        canonical_schema: Optional[CanonicalSchema] = None,
        config: Optional[ValidationConfig] = None,
    ) -> None:
        self.canonical_schema = canonical_schema or get_canonical_customer_v1()
        self.config = config or ValidationConfig()
        self._status_field = self.canonical_schema.get_field("status")
        self._allowed_statuses = (
            set(self._status_field.allowed_values)
            if self._status_field and self._status_field.allowed_values
            else {"ACTIVE", "INACTIVE"}
        )

    def validate_records(
        self,
        transformed_records: list[TransformedRecord],
        reference_time: Optional[datetime] = None,
    ) -> tuple[list[CanonicalCustomerRecord], list[TransformedRecord], list[RecordValidationError]]:
        """Validate all transformed records and separate into valid canonical records and invalid records."""
        now_utc = reference_time or datetime.now(timezone.utc)
        today_utc = now_utc.date()

        # Step 1: Batch-wide duplicate identification
        customer_id_counts: Counter[str] = Counter()
        email_counts: Counter[str] = Counter()

        for rec in transformed_records:
            cid = rec.canonical_values.get("customer_id")
            if cid is not None and str(cid).strip():
                customer_id_counts[str(cid).strip()] += 1

            if self.config.validate_email_uniqueness:
                em = rec.canonical_values.get("email")
                if em is not None and str(em).strip():
                    email_counts[str(em).strip().lower()] += 1

        duplicate_customer_ids = {cid for cid, cnt in customer_id_counts.items() if cnt > 1}
        duplicate_emails = {em for em, cnt in email_counts.items() if cnt > 1}

        # Step 2: Record-level validation
        valid_records: list[CanonicalCustomerRecord] = []
        invalid_records: list[TransformedRecord] = []
        all_errors: list[RecordValidationError] = []

        for rec in transformed_records:
            rec_errors: list[RecordValidationError] = list(rec.errors)
            canonical = rec.canonical_values
            cid_val = canonical.get("customer_id")
            rec_id = str(cid_val) if cid_val is not None else rec.source_record_id

            # Rule 1: REQUIRED fields
            for req_field in self.canonical_schema.get_required_fields():
                f_name = req_field.name
                val = canonical.get(f_name)
                if val is None or (isinstance(val, str) and val.strip() == ""):
                    rec_errors.append(
                        RecordValidationError(
                            row_index=rec.row_index,
                            record_id=rec_id,
                            rule_id=f"{f_name.upper()}_REQUIRED",
                            category=ValidationCategory.REQUIRED,
                            field=f_name,
                            observed_value=None if val is None else repr(val),
                            reason=f"Mandatory canonical field '{f_name}' is missing or blank.",
                        )
                    )

            # Rule 2: TYPE validations
            # customer_id
            if cid_val is not None and not isinstance(cid_val, str):
                rec_errors.append(
                    RecordValidationError(
                        row_index=rec.row_index,
                        record_id=rec_id,
                        rule_id="CUSTOMER_ID_TYPE",
                        category=ValidationCategory.TYPE,
                        field="customer_id",
                        observed_value=str(cid_val),
                        reason=f"Field 'customer_id' must be a string, got {type(cid_val).__name__}.",
                    )
                )

            # first_name
            fn_val = canonical.get("first_name")
            if fn_val is not None and not isinstance(fn_val, str):
                rec_errors.append(
                    RecordValidationError(
                        row_index=rec.row_index,
                        record_id=rec_id,
                        rule_id="FIRST_NAME_TYPE",
                        category=ValidationCategory.TYPE,
                        field="first_name",
                        observed_value=str(fn_val),
                        reason=f"Field 'first_name' must be a string, got {type(fn_val).__name__}.",
                    )
                )

            # last_name
            ln_val = canonical.get("last_name")
            if ln_val is not None and not isinstance(ln_val, str):
                rec_errors.append(
                    RecordValidationError(
                        row_index=rec.row_index,
                        record_id=rec_id,
                        rule_id="LAST_NAME_TYPE",
                        category=ValidationCategory.TYPE,
                        field="last_name",
                        observed_value=str(ln_val),
                        reason=f"Field 'last_name' must be a string, got {type(ln_val).__name__}.",
                    )
                )

            # date_of_birth
            dob_val = canonical.get("date_of_birth")
            if dob_val is not None:
                if not isinstance(dob_val, date) or isinstance(dob_val, datetime):
                    rec_errors.append(
                        RecordValidationError(
                            row_index=rec.row_index,
                            record_id=rec_id,
                            rule_id="DATE_OF_BIRTH_TYPE",
                            category=ValidationCategory.TYPE,
                            field="date_of_birth",
                            observed_value=str(dob_val),
                            reason=f"Field 'date_of_birth' must be a date, got {type(dob_val).__name__}.",
                        )
                    )
                elif self.config.disallow_future_dob and dob_val > today_utc:
                    rec_errors.append(
                        RecordValidationError(
                            row_index=rec.row_index,
                            record_id=rec_id,
                            rule_id="DATE_OF_BIRTH_NOT_IN_PAST",
                            category=ValidationCategory.BUSINESS_RULE,
                            field="date_of_birth",
                            observed_value=str(dob_val),
                            reason=f"Field 'date_of_birth' ({dob_val}) cannot be in the future (after {today_utc}).",
                        )
                    )

            # created_at
            ca_val = canonical.get("created_at")
            clean_created_at: Optional[datetime] = None
            if ca_val is not None:
                if isinstance(ca_val, datetime):
                    # Ensure timezone-aware UTC
                    clean_created_at = ca_val if ca_val.tzinfo else ca_val.replace(tzinfo=timezone.utc)
                elif isinstance(ca_val, date):
                    clean_created_at = datetime(ca_val.year, ca_val.month, ca_val.day, 0, 0, 0, tzinfo=timezone.utc)
                elif isinstance(ca_val, str):
                    try:
                        parsed_dt = datetime.fromisoformat(ca_val.replace("Z", "+00:00"))
                        clean_created_at = parsed_dt if parsed_dt.tzinfo else parsed_dt.replace(tzinfo=timezone.utc)
                    except ValueError:
                        rec_errors.append(
                            RecordValidationError(
                                row_index=rec.row_index,
                                record_id=rec_id,
                                rule_id="CREATED_AT_TYPE",
                                category=ValidationCategory.TYPE,
                                field="created_at",
                                observed_value=str(ca_val),
                                reason=f"Field 'created_at' must be a valid ISO datetime string or datetime object.",
                            )
                        )
                else:
                    rec_errors.append(
                        RecordValidationError(
                            row_index=rec.row_index,
                            record_id=rec_id,
                            rule_id="CREATED_AT_TYPE",
                            category=ValidationCategory.TYPE,
                            field="created_at",
                            observed_value=str(ca_val),
                            reason=f"Field 'created_at' must be a datetime, got {type(ca_val).__name__}.",
                        )
                    )

                if clean_created_at and self.config.disallow_future_created_at:
                    if clean_created_at > now_utc:
                        rec_errors.append(
                            RecordValidationError(
                                row_index=rec.row_index,
                                record_id=rec_id,
                                rule_id="CREATED_AT_NOT_IN_FUTURE",
                                category=ValidationCategory.BUSINESS_RULE,
                                field="created_at",
                                observed_value=str(clean_created_at),
                                reason=f"Field 'created_at' ({clean_created_at.isoformat()}) cannot be in the future (after {now_utc.isoformat()}).",
                            )
                        )

            # Rule 3: FORMAT - Email
            em_val = canonical.get("email")
            if em_val is not None and isinstance(em_val, str) and em_val.strip() != "":
                em_clean = em_val.strip()
                if not EMAIL_REGEX.match(em_clean):
                    rec_errors.append(
                        RecordValidationError(
                            row_index=rec.row_index,
                            record_id=rec_id,
                            rule_id="EMAIL_FORMAT",
                            category=ValidationCategory.FORMAT,
                            field="email",
                            observed_value=em_clean,
                            reason=f"Email '{em_clean}' does not match canonical email format (name@domain.tld).",
                        )
                    )

            # Rule 4: ENUM - status
            st_val = canonical.get("status")
            if st_val is not None and isinstance(st_val, str) and st_val.strip() != "":
                st_clean = st_val.strip().upper()
                if st_clean not in self._allowed_statuses:
                    rec_errors.append(
                        RecordValidationError(
                            row_index=rec.row_index,
                            record_id=rec_id,
                            rule_id="STATUS_ENUM",
                            category=ValidationCategory.ENUM,
                            field="status",
                            observed_value=str(st_val),
                            reason=f"Status value '{st_val}' is not in allowed canonical enum values: {sorted(self._allowed_statuses)}.",
                        )
                    )

            # Rule 5: UNIQUE / DUPLICATE - customer_id
            if cid_val is not None and str(cid_val).strip() in duplicate_customer_ids:
                dup_key = str(cid_val).strip()
                rec_errors.append(
                    RecordValidationError(
                        row_index=rec.row_index,
                        record_id=rec_id,
                        rule_id="CUSTOMER_ID_DUPLICATE",
                        category=ValidationCategory.DUPLICATE,
                        field="customer_id",
                        observed_value=dup_key,
                        reason=f"Duplicate customer_id '{dup_key}' detected across {customer_id_counts[dup_key]} records in batch.",
                    )
                )

            # Rule 5b: Configurable email uniqueness
            if self.config.validate_email_uniqueness and em_val is not None:
                em_key = str(em_val).strip().lower()
                if em_key in duplicate_emails:
                    rec_errors.append(
                        RecordValidationError(
                            row_index=rec.row_index,
                            record_id=rec_id,
                            rule_id="EMAIL_DUPLICATE",
                            category=ValidationCategory.DUPLICATE,
                            field="email",
                            observed_value=em_key,
                            reason=f"Duplicate email address '{em_key}' detected across {email_counts[em_key]} records in batch.",
                        )
                    )

            # Rule 6: Referential Integrity where configured
            for ref_field, valid_set in self.config.reference_datasets.items():
                ref_val = canonical.get(ref_field)
                if ref_val is not None and str(ref_val).strip() not in valid_set:
                    rec_errors.append(
                        RecordValidationError(
                            row_index=rec.row_index,
                            record_id=rec_id,
                            rule_id=f"{ref_field.upper()}_REFERENTIAL_INTEGRITY",
                            category=ValidationCategory.REFERENTIAL_INTEGRITY,
                            field=ref_field,
                            observed_value=str(ref_val),
                            reason=f"Value '{ref_val}' in field '{ref_field}' does not exist in reference dataset.",
                        )
                    )

            # Separate blocking vs warning
            blocking_errors = [e for e in rec_errors if e.severity == ValidationErrorSeverity.BLOCKING]

            all_errors.extend(rec_errors)

            if not blocking_errors and clean_created_at:
                # Valid record
                canonical_record = CanonicalCustomerRecord(
                    customer_id=str(cid_val).strip(),
                    first_name=str(canonical["first_name"]).strip(),
                    last_name=str(canonical["last_name"]).strip(),
                    email=str(canonical["email"]).strip().lower(),
                    date_of_birth=dob_val,
                    status=str(canonical["status"]).strip().upper(),
                    created_at=clean_created_at,
                )
                valid_records.append(canonical_record)
            else:
                # Invalid record
                updated_rec = rec.model_copy(
                    update={
                        "is_valid": False,
                        "errors": rec_errors,
                    }
                )
                invalid_records.append(updated_rec)

        return valid_records, invalid_records, all_errors
