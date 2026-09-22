"""Transformation pipeline coordinating deterministic transformation, validation, and result packaging."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Optional
import polars as pl

from ..canonical import CanonicalSchema, get_canonical_customer_v1
from ..models.approval import ApprovedMappingVersion
from ..models.schema_snapshot import SourceSchemaSnapshot
from ..models.transformation import TransformationResult
from .engine import DeterministicTransformationEngine
from .validator import DeterministicValidator, ValidationConfig

logger = logging.getLogger("scd2_copilot.onboarding.transformation.pipeline")


class TransformationPipeline:
    """Coordinates deterministic transformation and validation for customer data onboarding."""

    def __init__(
        self,
        canonical_schema: Optional[CanonicalSchema] = None,
        validation_config: Optional[ValidationConfig] = None,
    ) -> None:
        self.canonical_schema = canonical_schema or get_canonical_customer_v1()
        self.validation_config = validation_config or ValidationConfig()
        self.validator = DeterministicValidator(
            canonical_schema=self.canonical_schema,
            config=self.validation_config,
        )

    def execute(
        self,
        df: pl.DataFrame,
        approved_version: ApprovedMappingVersion,
        source_schema: Optional[SourceSchemaSnapshot] = None,
        reference_time: Optional[datetime] = None,
    ) -> TransformationResult:
        """Execute end-to-end transformation and validation pipeline on incoming source data."""
        # 1. Execute transformation engine
        engine = DeterministicTransformationEngine(
            approved_version=approved_version,
            canonical_schema=self.canonical_schema,
        )
        transformed_records = engine.transform_dataframe(df, source_schema=source_schema)

        # 2. Execute deterministic validation
        valid_recs, invalid_recs, all_errors = self.validator.validate_records(
            transformed_records=transformed_records,
            reference_time=reference_time,
        )

        # 3. Assemble and return frozen result
        return TransformationResult(
            source_id=approved_version.source_id,
            mapping_version_id=approved_version.mapping_version_id,
            canonical_schema_name=self.canonical_schema.schema_name,
            canonical_schema_version=self.canonical_schema.version,
            total_records=len(transformed_records),
            valid_record_count=len(valid_recs),
            invalid_record_count=len(invalid_recs),
            valid_records=valid_recs,
            invalid_records=invalid_recs,
            all_errors=all_errors,
            executed_at=datetime.now(timezone.utc),
        )
