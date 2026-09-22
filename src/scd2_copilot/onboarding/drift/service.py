"""Coordinator service for schema drift evaluation and mapping impact persistence."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Union

from ..models.approval import ApprovedMappingVersion
from ..models.drift import SchemaDiffResult, SchemaDriftReport
from ..models.schema_snapshot import SourceSchemaSnapshot
from .engine import DeterministicSchemaDiffEngine
from .impact import MappingImpactAnalyzer
from .repository import SchemaDriftRepository

logger = logging.getLogger("scd2_copilot.onboarding.drift.service")


class SchemaDriftService:
    """Orchestrates schema diffing, mapping impact analysis, and report persistence."""

    def __init__(
        self,
        repository: Optional[SchemaDriftRepository] = None,
        diff_engine: Optional[DeterministicSchemaDiffEngine] = None,
        impact_analyzer: Optional[MappingImpactAnalyzer] = None,
        artifact_dir: Optional[Union[str, Path]] = None,
    ) -> None:
        self.repository = repository or SchemaDriftRepository()
        self.diff_engine = diff_engine or DeterministicSchemaDiffEngine()
        self.impact_analyzer = impact_analyzer or MappingImpactAnalyzer()
        self.artifact_dir = Path(artifact_dir) if artifact_dir else None

    def evaluate_drift(
        self,
        prior_schema: SourceSchemaSnapshot,
        current_schema: SourceSchemaSnapshot,
        approved_mapping: ApprovedMappingVersion,
        persist: bool = True,
    ) -> SchemaDriftReport:
        """Compute schema diff, analyze mapping impact, optionally persist artifact and record.

        Guarantees:
        1. Fully deterministic execution.
        2. Approved mapping is untouched.
        3. Report is persisted to repository and optional artifact directory.
        """
        # 1. Deterministic schema diff
        diff_result = self.diff_engine.diff(
            prior_schema=prior_schema,
            current_schema=current_schema,
        )

        # 2. Deterministic mapping impact analysis
        report = self.impact_analyzer.analyze_impact(
            approved_mapping=approved_mapping,
            diff_result=diff_result,
        )

        # 3. Optional artifact persistence
        if self.artifact_dir is not None:
            artifact_file = self.artifact_dir / f"drift_{report.report_id}.json"
            report.save_artifact(artifact_file)
            logger.info("Saved schema drift report artifact to %s", artifact_file)

        # 4. Save to repository if requested
        if persist:
            self.repository.save(report)

        return report

    def get_report(self, report_id: str) -> Optional[SchemaDriftReport]:
        """Fetch a drift report by ID, or None if not found."""
        return self.repository.get_by_id(report_id)

    def get_report_required(self, report_id: str) -> SchemaDriftReport:
        """Fetch a drift report by ID, raising DriftReportNotFoundError if missing."""
        return self.repository.get_by_id_required(report_id)

    def list_reports(
        self,
        source_id: Optional[str] = None,
        mapping_version_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[SchemaDriftReport], int]:
        """List drift reports with optional filtering."""
        return self.repository.list_reports(
            source_id=source_id,
            mapping_version_id=mapping_version_id,
            limit=limit,
            offset=offset,
        )
