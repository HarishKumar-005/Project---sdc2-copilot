"""Deterministic transformation engine executing approved mapping versions on source datasets."""

from __future__ import annotations

from datetime import date, datetime, timezone
import logging
from typing import Any, Optional
import polars as pl

from ..canonical import CanonicalSchema, get_canonical_customer_v1
from ..exceptions import (
    CanonicalSchemaMismatchError,
    MappingNotApprovedError,
    MissingSourceColumnError,
    SourceSchemaMismatchError,
    TransformationConfigurationError,
)
from ..models.approval import ApprovedMappingDefinition, ApprovedMappingVersion
from ..models.mapping import MappingType, TransformationOpType, TransformationStep
from ..models.schema_snapshot import SourceSchemaSnapshot
from ..models.transformation import (
    RecordValidationError,
    TransformedRecord,
    ValidationCategory,
    ValidationErrorSeverity,
)

logger = logging.getLogger("scd2_copilot.onboarding.transformation")


class DeterministicTransformationEngine:
    """Executes frozen ApprovedMappingVersion configurations on source Polars DataFrames."""

    def __init__(
        self,
        approved_version: ApprovedMappingVersion,
        canonical_schema: Optional[CanonicalSchema] = None,
    ) -> None:
        if not isinstance(approved_version, ApprovedMappingVersion):
            raise MappingNotApprovedError(
                f"Transformation requires an ApprovedMappingVersion instance, got: {type(approved_version).__name__}."
            )

        self.approved_version = approved_version
        self.canonical_schema = canonical_schema or get_canonical_customer_v1()

        # Validate canonical schema compatibility
        if self.approved_version.canonical_schema_name != self.canonical_schema.schema_name:
            raise CanonicalSchemaMismatchError(
                f"Mapping canonical schema name '{self.approved_version.canonical_schema_name}' "
                f"does not match expected '{self.canonical_schema.schema_name}'."
            )
        if self.approved_version.canonical_schema_version != self.canonical_schema.version:
            raise CanonicalSchemaMismatchError(
                f"Mapping canonical schema version '{self.approved_version.canonical_schema_version}' "
                f"does not match expected '{self.canonical_schema.version}'."
            )

        # Validate that all active mappings target valid canonical fields
        valid_targets = set(self.canonical_schema.field_names)
        for m in self.approved_version.get_active_mappings():
            if m.target_field not in valid_targets:
                raise TransformationConfigurationError(
                    f"Mapping targets unknown canonical field '{m.target_field}'. Valid: {valid_targets}."
                )

        # Index active mappings by source field and target field
        self._active_mappings_by_source: dict[str, ApprovedMappingDefinition] = {
            m.source_field: m for m in self.approved_version.get_active_mappings()
        }

        # Find which source field maps to customer_id for correlation
        self._id_source_field: Optional[str] = None
        for m in self.approved_version.get_active_mappings():
            if m.target_field == "customer_id":
                self._id_source_field = m.source_field
                break

    def transform_dataframe(
        self,
        df: pl.DataFrame,
        source_schema: Optional[SourceSchemaSnapshot] = None,
    ) -> list[TransformedRecord]:
        """Execute approved transformation definitions across all rows in the source DataFrame."""
        # 1. Validate Source Schema Fingerprint if provided
        if source_schema is not None:
            if source_schema.fingerprint.fingerprint_hash != self.approved_version.source_fingerprint:
                raise SourceSchemaMismatchError(
                    f"Source schema fingerprint '{source_schema.fingerprint.fingerprint_hash}' "
                    f"does not match approved mapping fingerprint '{self.approved_version.source_fingerprint}'.",
                    details={
                        "observed_fingerprint": source_schema.fingerprint.fingerprint_hash,
                        "expected_fingerprint": self.approved_version.source_fingerprint,
                    },
                )

        # 2. Verify all actively mapped source columns exist in DataFrame
        df_cols = set(df.columns)
        active_mappings = self.approved_version.get_active_mappings()
        for m in active_mappings:
            if m.source_field not in df_cols:
                raise MissingSourceColumnError(
                    f"Mapped source column '{m.source_field}' is missing from the input dataset. Available: {df.columns}.",
                    details={"missing_column": m.source_field, "available_columns": df.columns},
                )

        # 3. Transform records deterministically
        records: list[TransformedRecord] = []
        rows_iter = df.iter_rows(named=True)

        for row_idx, row_dict in enumerate(rows_iter):
            source_rec_id = str(row_dict[self._id_source_field]) if self._id_source_field and row_dict.get(self._id_source_field) is not None else None
            canonical_values: dict[str, Any] = {}
            transformation_errors: list[RecordValidationError] = []

            for mapping_def in active_mappings:
                source_field = mapping_def.source_field
                target_field = mapping_def.target_field
                if not target_field:
                    continue

                raw_val = row_dict.get(source_field)

                # Execute transformation pipeline for this field
                transformed_val, field_errors = self._apply_transformations(
                    val=raw_val,
                    mapping_def=mapping_def,
                    row_dict=row_dict,
                    row_index=row_idx,
                    record_id=source_rec_id,
                )
                canonical_values[target_field] = transformed_val
                transformation_errors.extend(field_errors)


            records.append(
                TransformedRecord(
                    row_index=row_idx,
                    source_record_id=source_rec_id,
                    raw_values=row_dict,
                    canonical_values=canonical_values,
                    is_valid=len(transformation_errors) == 0,
                    errors=transformation_errors,
                )
            )

        return records

    def _apply_transformations(
        self,
        val: Any,
        mapping_def: ApprovedMappingDefinition,
        row_dict: dict[str, Any],
        row_index: int,
        record_id: Optional[str],
    ) -> tuple[Any, list[RecordValidationError]]:
        """Execute sequential transformation steps on a single field value."""
        target_field = mapping_def.target_field or mapping_def.source_field
        errors: list[RecordValidationError] = []
        current = val

        for step in mapping_def.transformations:
            op = step.op
            params = step.params or {}

            if op == TransformationOpType.TRIM:
                if current is not None:
                    current = str(current).strip()

            elif op == TransformationOpType.LOWERCASE:
                if current is not None:
                    current = str(current).lower()

            elif op == TransformationOpType.UPPERCASE:
                if current is not None:
                    current = str(current).upper()

            elif op == TransformationOpType.NORMALIZE_EMAIL:
                if current is not None:
                    current = str(current).strip().lower()

            elif op == TransformationOpType.CAST:
                target_type = params.get("target_type", "string").lower()
                current, err = self._execute_cast(current, target_type, target_field, row_index, record_id)
                if err:
                    errors.append(err)

            elif op == TransformationOpType.PARSE_DATE:
                fmt = params.get("format")
                current, err = self._execute_parse_date(current, fmt, target_field, row_index, record_id)
                if err:
                    errors.append(err)

            elif op == TransformationOpType.MAP_ENUM:
                enum_map = params.get("enum_map") or params.get("mapping") or {}
                current = self._execute_map_enum(current, enum_map)


            elif op == TransformationOpType.CONCAT:
                fields = params.get("fields", [])
                separator = params.get("separator", " ")
                null_handling = params.get("null_handling", "skip")
                current, err = self._execute_concat(row_dict, fields, separator, null_handling, target_field, row_index, record_id)
                if err:
                    errors.append(err)

            else:
                errors.append(
                    RecordValidationError(
                        row_index=row_index,
                        record_id=record_id,
                        rule_id="UNSUPPORTED_TRANSFORMATION_OPERATION",
                        category=ValidationCategory.TRANSFORMATION,
                        field=target_field,
                        observed_value=str(op),
                        reason=f"Unsupported transformation operation: '{op}'.",
                    )
                )

        return current, errors

    @staticmethod
    def _execute_cast(
        val: Any,
        target_type: str,
        target_field: str,
        row_index: int,
        record_id: Optional[str],
    ) -> tuple[Any, Optional[RecordValidationError]]:
        """Deterministically cast a value to a supported primitive target type."""
        if val is None or (isinstance(val, str) and val.strip() == ""):
            return None, None

        try:
            if target_type == "string":
                return str(val), None
            elif target_type == "integer":
                return int(float(val)), None
            elif target_type == "float":
                return float(val), None
            elif target_type == "boolean":
                s = str(val).strip().lower()
                if s in ("true", "1", "t", "yes", "y"):
                    return True, None
                elif s in ("false", "0", "f", "no", "n"):
                    return False, None
                raise ValueError(f"Cannot cast '{val}' to boolean.")
            else:
                raise ValueError(f"Unsupported cast target type '{target_type}'.")
        except Exception as e:
            return None, RecordValidationError(
                row_index=row_index,
                record_id=record_id,
                rule_id=f"{target_field.upper()}_CAST_ERROR",
                category=ValidationCategory.TRANSFORMATION,
                field=target_field,
                observed_value=str(val),
                reason=f"Failed to cast value '{val}' to {target_type}: {e}.",
            )

    @staticmethod
    def _execute_parse_date(
        val: Any,
        fmt: Optional[str],
        target_field: str,
        row_index: int,
        record_id: Optional[str],
    ) -> tuple[Any, Optional[RecordValidationError]]:
        """Parse date value using an explicit format string or standard ISO fallback."""
        if val is None or (isinstance(val, str) and val.strip() == ""):
            return None, None

        if isinstance(val, date) and not isinstance(val, datetime):
            return val, None

        if isinstance(val, datetime):
            return val.date(), None

        s = str(val).strip()

        # If explicit format is specified, use strptime strictly
        if fmt:
            try:
                dt = datetime.strptime(s, fmt)
                return dt.date(), None
            except ValueError as e:
                return None, RecordValidationError(
                    row_index=row_index,
                    record_id=record_id,
                    rule_id="INVALID_DATE",
                    category=ValidationCategory.TRANSFORMATION,
                    field=target_field,
                    observed_value=s,
                    reason=f"Date '{s}' does not match configured format '{fmt}': {e}.",
                )

        # Standard ISO formats
        for iso_fmt in ("%Y-%m-%d", "%Y/%m/%d"):
            try:
                dt = datetime.strptime(s, iso_fmt)
                return dt.date(), None
            except ValueError:
                continue

        # Try ISO timestamp
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.date(), None
        except ValueError:
            pass

        return None, RecordValidationError(
            row_index=row_index,
            record_id=record_id,
            rule_id="INVALID_DATE",
            category=ValidationCategory.TRANSFORMATION,
            field=target_field,
            observed_value=s,
            reason=f"Date '{s}' cannot be parsed with standard ISO formats and no format was configured.",
        )

    @staticmethod
    def _execute_map_enum(val: Any, enum_map: dict[str, str]) -> Any:
        """Translate source categorical token to canonical enum using explicit dictionary."""
        if val is None:
            return None

        s = str(val).strip()
        # Direct lookup
        if s in enum_map:
            return enum_map[s]

        # Case-insensitive lookup
        upper_map = {k.upper(): v for k, v in enum_map.items()}
        if s.upper() in upper_map:
            return upper_map[s.upper()]

        # Return original value so that ENUM validator flags the exact invalid observed value
        return s

    @staticmethod
    def _execute_concat(
        row_dict: dict[str, Any],
        fields: list[str],
        separator: str,
        null_handling: str,
        target_field: str,
        row_index: int,
        record_id: Optional[str],
    ) -> tuple[Any, Optional[RecordValidationError]]:
        """Deterministically concatenate multiple fields with explicit ordering and null handling."""
        parts: list[str] = []
        for f in fields:
            val = row_dict.get(f)
            if val is None or (isinstance(val, str) and val.strip() == ""):
                if null_handling == "fail":
                    return None, RecordValidationError(
                        row_index=row_index,
                        record_id=record_id,
                        rule_id=f"{target_field.upper()}_CONCAT_NULL_ERROR",
                        category=ValidationCategory.TRANSFORMATION,
                        field=target_field,
                        observed_value=None,
                        reason=f"Field '{f}' is null during CONCAT with null_handling='fail'.",
                    )
                elif null_handling == "skip":
                    continue
                else:  # empty string
                    parts.append("")
            else:
                parts.append(str(val).strip())

        return separator.join(parts), None
