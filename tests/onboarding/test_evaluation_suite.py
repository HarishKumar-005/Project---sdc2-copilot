"""Comprehensive test suite for M10 — Evaluation, Security & Freeze."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import pytest

from src.scd2_copilot.onboarding.canonical import CANONICAL_CUSTOMER_V1
from src.scd2_copilot.onboarding.evaluation import (
    AIEvaluationMetrics,
    DataQualityEvaluationMetrics,
    ExceptionEvaluationMetrics,
    GroundTruthDataset,
    GuardrailEvaluationMetrics,
    IdempotencyEvaluationMetrics,
    M10EvaluationReport,
    MappingEvaluationMetrics,
    OnboardingSystemEvaluator,
    PerformanceBenchmarkMetrics,
    SCD2EvaluationMetrics,
    SchemaDriftEvaluationMetrics,
    SecurityVerificationSummary,
    SecurityVerifier,
    create_approved_mapping_for_dataset,
    get_dataset_a_clean,
    get_dataset_b_moderately_messy,
    get_dataset_c_highly_messy,
)


def test_ground_truth_datasets_integrity() -> None:
    """Verify Dataset A, B, and C satisfy contract specifications and ground truth invariants."""
    ds_a = get_dataset_a_clean(row_count=20)
    assert ds_a.dataset_id == "dataset_a_clean"
    assert len(ds_a.raw_records) == 20
    assert ds_a.injected_violation_count == 0
    assert ds_a.expected_clean_count == 20
    assert len(ds_a.source_schema.columns) == 7

    ds_b = get_dataset_b_moderately_messy(row_count=30)
    assert ds_b.dataset_id == "dataset_b_moderately_messy"
    assert len(ds_b.raw_records) == 30
    assert ds_b.injected_violation_count == 5
    assert ds_b.expected_clean_count == 25
    assert "cust_no" in ds_b.ground_truth_mappings
    assert ds_b.ground_truth_mappings["cust_no"] == "customer_id"

    ds_c = get_dataset_c_highly_messy(row_count=35)
    assert ds_c.dataset_id == "dataset_c_highly_messy"
    assert len(ds_c.raw_records) == 35
    assert ds_c.injected_violation_count == 5
    assert "audit_tag" not in ds_c.ground_truth_mappings  # Unmapped column


def test_data_quality_evaluation() -> None:
    """Verify validation detection coverage and zero false positives on clean data."""
    evaluator = OnboardingSystemEvaluator()
    metrics = evaluator.evaluate_data_quality()

    assert isinstance(metrics, DataQualityEvaluationMetrics)
    assert metrics.coverage_rate >= 0.99, f"Expected 100% coverage, got {metrics.coverage_rate}"
    assert metrics.false_positive_count == 0, f"False positives on clean records: {metrics.false_positive_count}"
    assert metrics.false_positive_rate == 0.0
    assert "EMAIL_FORMAT" in metrics.violations_by_category
    assert "CUSTOMER_ID_REQUIRED" in metrics.violations_by_category
    assert "STATUS_ENUM" in metrics.violations_by_category



def test_mapping_evaluation() -> None:
    """Verify candidate mapping accuracy, review-required flags, and unsupported op rejection."""
    evaluator = OnboardingSystemEvaluator()
    metrics = evaluator.evaluate_mapping_accuracy()

    assert isinstance(metrics, MappingEvaluationMetrics)
    assert metrics.accuracy >= 0.95
    assert metrics.review_required_count >= 1
    assert metrics.unmapped_count >= 1
    assert metrics.unsupported_rejections >= 1


def test_exception_workflow_evaluation() -> None:
    """Verify exception enqueuing, structured correction, reprocessing, and resolution."""
    evaluator = OnboardingSystemEvaluator()
    metrics = evaluator.evaluate_exception_workflow()

    assert isinstance(metrics, ExceptionEvaluationMetrics)
    assert metrics.exceptions_enqueued >= 5
    assert metrics.corrections_attempted >= 5
    assert metrics.reprocessing_succeeded >= 5
    assert metrics.resolution_rate == 1.0
    assert metrics.audit_entries_verified >= 10


def test_idempotency_and_runs_evaluation() -> None:
    """Verify exact replay recognition, conflict rejection, and side-effect prevention."""
    evaluator = OnboardingSystemEvaluator()
    metrics = evaluator.evaluate_idempotency_and_runs()

    assert isinstance(metrics, IdempotencyEvaluationMetrics)
    assert metrics.identical_replays_detected == 1
    assert metrics.conflicts_detected == 1
    assert metrics.side_effect_violations == 0
    assert metrics.idempotency_correctness_rate == 1.0


def test_schema_drift_evaluation() -> None:
    """Verify drift detection accuracy and impact categorization on approved mappings."""
    evaluator = OnboardingSystemEvaluator()
    metrics = evaluator.evaluate_schema_drift()

    assert isinstance(metrics, SchemaDriftEvaluationMetrics)
    assert metrics.detection_accuracy == 1.0
    assert metrics.compatibility_states_verified >= 1
    assert metrics.broken_mappings_prevented >= 1


def test_scd2_temporal_evaluation() -> None:
    """Verify version creation, closure, interval consistency, and point-in-time accuracy."""
    evaluator = OnboardingSystemEvaluator()
    metrics = evaluator.evaluate_scd2_integration()

    assert isinstance(metrics, SCD2EvaluationMetrics)
    assert metrics.new_records_correct == 2
    assert metrics.changed_records_correct == 1
    assert metrics.unchanged_records_correct == 1
    assert metrics.temporal_invariant_violations == 0
    assert metrics.point_in_time_queries_correct == 2


def test_guardrail_pre_commit_evaluation() -> None:
    """Verify pre-commit decision boundary, suspicious hold, zero history leakage, and recovery."""
    evaluator = OnboardingSystemEvaluator()
    metrics = evaluator.evaluate_guardrail_integration()

    assert isinstance(metrics, GuardrailEvaluationMetrics)
    assert metrics.normal_batches_passed == 1
    assert metrics.suspicious_batches_held == 1
    assert metrics.decision_accuracy == 1.0
    assert metrics.pre_commit_holds_verified == 1
    assert metrics.history_leakage_violations == 0
    assert metrics.recovery_transitions_verified >= 1


def test_ai_advisory_boundary_evaluation() -> None:
    """Verify AI prompt privacy, failure isolation, structured validity, and authority boundaries."""
    evaluator = OnboardingSystemEvaluator()
    metrics = evaluator.evaluate_ai_advisory_boundary()

    assert isinstance(metrics, AIEvaluationMetrics)
    assert metrics.total_evaluations >= 5
    assert metrics.structured_output_validity_rate == 1.0
    assert metrics.prompt_pii_suppression_rate == 1.0
    assert metrics.fallback_success_rate == 1.0
    assert metrics.authority_boundary_violations == 0


def test_security_verifier() -> None:
    """Verify credential masking, PII suppression, code injection resistance, and SQL safety."""
    verifier = SecurityVerifier()
    summary = verifier.verify_all()

    assert isinstance(summary, SecurityVerificationSummary)
    assert summary.secrets_contained is True
    assert summary.pii_suppressed_in_metadata is True
    assert summary.arbitrary_code_execution_blocked is True
    assert summary.path_traversal_blocked is True
    assert summary.sql_parameterized is True
    assert summary.recovery_auth_enforced is True
    assert len(summary.findings) >= 6


def test_performance_benchmarks() -> None:
    """Verify performance measurements across 100, 1000, 5000 synthetic rows."""
    evaluator = OnboardingSystemEvaluator()
    metrics = evaluator.evaluate_performance()

    assert len(metrics) == 3
    for m in metrics:
        assert isinstance(m, PerformanceBenchmarkMetrics)
        assert m.row_count in (100, 1000, 5000)
        assert m.transformation_throughput_rows_per_sec > 1000.0
        assert m.validation_throughput_rows_per_sec > 1000.0
        assert m.end_to_end_duration_ms > 0.0


def test_end_to_end_18_step_demo() -> None:
    """Verify complete 18-step demonstration path from ingestion to hold and recovery."""
    evaluator = OnboardingSystemEvaluator()
    success = evaluator.run_end_to_end_demo()
    assert success is True


def test_full_m10_report_compilation_and_artifact_save() -> None:
    """Verify compilation of complete M10 report and JSON artifact serialization."""
    evaluator = OnboardingSystemEvaluator()
    report = evaluator.evaluate_all()

    assert isinstance(report, M10EvaluationReport)
    assert report.canonical_schema_version == CANONICAL_CUSTOMER_V1.version
    assert report.overall_verdict == "PASS"
    assert report.end_to_end_demo_passed is True
    assert len(report.limitations) >= 3

    with tempfile.TemporaryDirectory() as tmpdir:
        report_path = Path(tmpdir) / "reports" / "m10_evaluation_report.json"
        saved = report.save_artifact(report_path)
        assert saved.exists()

        # Reload and parse
        with open(saved, "r", encoding="utf-8") as f:
            data = json.load(f)

        assert data["overall_verdict"] == "PASS"
        assert data["data_quality"]["coverage_rate"] >= 0.99
        assert data["idempotency"]["idempotency_correctness_rate"] == 1.0
        assert data["scd2"]["temporal_invariant_violations"] == 0
        assert data["guardrail"]["history_leakage_violations"] == 0
