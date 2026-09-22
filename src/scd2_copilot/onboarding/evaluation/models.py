"""Evaluation data models and report schemas for M10 — Evaluation, Security & Freeze."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Optional, Union
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class DataQualityEvaluationMetrics(BaseModel):
    """Evaluation metrics for deterministic data quality and validation coverage."""

    total_injected_violations: int = Field(..., description="Total injected data violations in test fixtures.")
    detected_violations: int = Field(..., description="Violations successfully caught by deterministic validation.")
    coverage_rate: float = Field(..., description="Ratio of detected to total injected violations.")
    clean_records_evaluated: int = Field(..., description="Total known-clean records evaluated.")
    false_positive_count: int = Field(0, description="Clean records incorrectly marked as invalid.")
    false_positive_rate: float = Field(0.0, description="False positive rate on clean records.")
    violations_by_category: dict[str, int] = Field(default_factory=dict, description="Counts by error rule ID.")


class MappingEvaluationMetrics(BaseModel):
    """Evaluation metrics for candidate mapping and proposal generation."""

    total_labeled_fields: int = Field(..., description="Total ground-truth source fields evaluated.")
    correct_mappings: int = Field(..., description="Correctly proposed target mappings against ground truth.")
    accuracy: float = Field(..., description="Ratio of correct mappings to total labeled fields.")
    review_required_count: int = Field(..., description="Proposals correctly flagged for human review.")
    review_required_rate: float = Field(..., description="Ratio of proposals requiring human review.")
    unmapped_count: int = Field(..., description="Ground-truth unmapped or rejected source fields.")
    unsupported_rejections: int = Field(..., description="Illegal/unsupported transformations rejected.")


class ExceptionEvaluationMetrics(BaseModel):
    """Evaluation metrics for exception enqueuing, correction, and reprocessing."""

    exceptions_enqueued: int = Field(..., description="Total validation failures converted to exceptions.")
    corrections_attempted: int = Field(..., description="Total operator corrections submitted.")
    reprocessing_succeeded: int = Field(..., description="Corrections verified valid through reprocessing.")
    resolution_rate: float = Field(..., description="Ratio of successful resolutions to attempted corrections.")
    audit_entries_verified: int = Field(..., description="Full audit lineage entries confirmed.")


class IdempotencyEvaluationMetrics(BaseModel):
    """Evaluation metrics for onboarding runs and idempotency invariants."""

    total_submissions: int = Field(..., description="Total run submission requests tested.")
    identical_replays_detected: int = Field(..., description="Exact replays recognized with zero duplicate side effects.")
    conflicts_detected: int = Field(..., description="Same key with different payload producing 409 Conflict.")
    side_effect_violations: int = Field(0, description="Duplicate run rows or unintended state mutations (must be 0).")
    idempotency_correctness_rate: float = Field(..., description="Ratio of correct idempotency decisions.")


class SchemaDriftEvaluationMetrics(BaseModel):
    """Evaluation metrics for schema drift detection and mapping impact analysis."""

    total_drift_events: int = Field(..., description="Total ground-truth drift events injected.")
    detected_drift_events: int = Field(..., description="Drift events accurately identified.")
    detection_accuracy: float = Field(..., description="Ratio of detected drift events.")
    compatibility_states_verified: int = Field(..., description="COMPATIBLE, REVIEW_REQUIRED, BROKEN states correctly classified.")
    broken_mappings_prevented: int = Field(..., description="Executions blocked against incompatible schema.")


class SCD2EvaluationMetrics(BaseModel):
    """Evaluation metrics for SCD2 historical integration and temporal correctness."""

    total_evaluated_transitions: int = Field(..., description="Total entity transitions processed.")
    new_records_correct: int = Field(..., description="NEW entities creating initial active version.")
    changed_records_correct: int = Field(..., description="CHANGED entities closing old and opening new version.")
    unchanged_records_correct: int = Field(..., description="UNCHANGED entities preserving history without mutation.")
    temporal_invariant_violations: int = Field(0, description="Overlaps, inverted dates, or duplicate current rows (must be 0).")
    point_in_time_queries_correct: int = Field(..., description="Correct point-in-time historical version lookups.")


class GuardrailEvaluationMetrics(BaseModel):
    """Evaluation metrics for pre-commit guardrail boundary and containment recovery."""

    total_batches_evaluated: int = Field(..., description="Total candidate batches evaluated.")
    normal_batches_passed: int = Field(..., description="NORMAL batches committed downstream.")
    suspicious_batches_held: int = Field(..., description="SUSPICIOUS batches contained without downstream mutation.")
    decision_accuracy: float = Field(..., description="Ratio of correct NORMAL vs SUSPICIOUS decisions.")
    pre_commit_holds_verified: int = Field(..., description="Holds verified to preserve historical state unchanged.")
    history_leakage_violations: int = Field(0, description="Historical mutations occurring on held batches (must be 0).")
    recovery_transitions_verified: int = Field(..., description="RELEASE, REPROCESS, and DISCARD verified.")


class AIEvaluationMetrics(BaseModel):
    """Evaluation metrics for AI advisory boundary, safety, and failure isolation."""

    total_evaluations: int = Field(..., description="Total AI invocations or fallback evaluations.")
    structured_output_validity_rate: float = Field(..., description="Proportion of model outputs adhering to Pydantic schema.")
    prompt_pii_suppression_rate: float = Field(1.0, description="Proportion of prompts strictly omitting customer PII (must be 1.0).")
    fallback_success_rate: float = Field(..., description="Graceful deterministic fallback upon model failure.")
    authority_boundary_violations: int = Field(0, description="AI attempting direct mutation or code execution (must be 0).")


class PerformanceBenchmarkMetrics(BaseModel):
    """Performance measurements for a specific synthetic dataset scale."""

    dataset_scale: str = Field(..., description="Dataset scale identifier (e.g. '100_rows', '1000_rows', '5000_rows').")
    row_count: int = Field(..., description="Number of source records evaluated.")
    profiling_duration_ms: float = Field(..., description="Time taken to profile schema and statistics.")
    candidate_generation_duration_ms: float = Field(..., description="Time taken to generate mapping candidates.")
    transformation_throughput_rows_per_sec: float = Field(..., description="Records transformed per second.")
    validation_throughput_rows_per_sec: float = Field(..., description="Records validated per second.")
    scd2_throughput_rows_per_sec: float = Field(..., description="Records processed through SCD2 per second.")
    guardrail_evaluation_ms: float = Field(..., description="Pre-commit guardrail decision latency.")
    end_to_end_duration_ms: float = Field(..., description="Total execution time for the complete onboarding pass.")


class SecurityVerificationSummary(BaseModel):
    """Summary of security and privacy invariant verification."""

    secrets_contained: bool = Field(True, description="No secrets committed, logged, or exposed.")
    pii_suppressed_in_metadata: bool = Field(True, description="Raw PII suppressed in logs, profiles, drift, and hold evidence.")
    arbitrary_code_execution_blocked: bool = Field(True, description="Pure DSL execution; zero eval/exec calls.")
    path_traversal_blocked: bool = Field(True, description="Filesystem access strictly bounded; traversal rejected.")
    sql_parameterized: bool = Field(True, description="All SQL queries parameterized; zero raw interpolation.")
    recovery_auth_enforced: bool = Field(True, description="Operator authentication and authorization enforced.")
    findings: list[dict[str, Any]] = Field(default_factory=list, description="Specific security check details.")


class M10EvaluationReport(BaseModel):
    """Comprehensive evaluation and freeze artifact for Customer Onboarding."""

    report_id: str = Field(default_factory=lambda: f"m10-eval-{uuid4().hex[:12]}")
    canonical_schema_version: Union[str, int] = Field(default="customer.v1")

    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    data_quality: DataQualityEvaluationMetrics
    mapping: MappingEvaluationMetrics
    exception_workflow: ExceptionEvaluationMetrics
    idempotency: IdempotencyEvaluationMetrics
    schema_drift: SchemaDriftEvaluationMetrics
    scd2: SCD2EvaluationMetrics
    guardrail: GuardrailEvaluationMetrics
    ai_advisory: AIEvaluationMetrics
    security: SecurityVerificationSummary
    performance: list[PerformanceBenchmarkMetrics]
    end_to_end_demo_passed: bool = Field(..., description="Whether the complete 18-step demo path passed.")
    overall_verdict: str = Field(default="PASS", description="Overall milestone freeze verdict: PASS or FAIL.")
    limitations: list[str] = Field(default_factory=list, description="Documented architectural limitations.")

    def save_artifact(self, path: Union[str, Path]) -> Path:
        """Persist evaluation report as formatted JSON artifact."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(self.model_dump_json(indent=2))
        return target
