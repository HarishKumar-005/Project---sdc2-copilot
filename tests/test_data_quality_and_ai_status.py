"""Tests for M1.6: Remove Fake Confidence Score and Separate Data Quality from AI Status.

Verifies that:
1. Validation PASS/FAIL is 100% deterministic and unaffected by AI availability.
2. AI explanation status (SUCCESS, FALLBACK, TEMPLATE, UNAVAILABLE) is truthful and orthogonal to validation.
3. No artificial confidence percentages or provider bonuses exist.
4. compute_validation_summary accurately extracts rule-level pass/fail/issue counts.
5. compute_ai_status accurately extracts provider, fallback warnings, latency, tokens, and cost.
"""

from datetime import date
import polars as pl
import pytest

from app.ui_components import compute_ai_status, compute_validation_summary
from src.scd2_copilot.explain import ExplainResult
from src.scd2_copilot.models import (
    ChangeRecord,
    ChangeType,
    Explanation,
    LLMMetrics,
    ValidationReport,
    ValidationRule,
    ValidationStatus,
)
from src.scd2_copilot.validate import validate_scd2


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def passing_validation_report() -> ValidationReport:
    """A valid SCD2 table passing all 5 validation rules."""
    df = pl.DataFrame(
        {
            "id": [101, 102],
            "effective_from": [date(2026, 1, 1), date(2026, 1, 1)],
            "effective_to": [None, None],
            "is_current": [True, True],
            "salary": [100000, 120000],
        }
    )
    return validate_scd2(df, business_key=["id"])


@pytest.fixture
def failing_validation_report() -> ValidationReport:
    """An invalid SCD2 table failing date consistency (effective_from > effective_to)."""
    df = pl.DataFrame(
        {
            "id": [101],
            "effective_from": [date(2026, 5, 1)],
            "effective_to": [date(2026, 1, 1)],
            "is_current": [False],
            "salary": [100000],
        }
    )
    return validate_scd2(df, business_key=["id"])


# ── 1. Deterministic Validation Independence ─────────────────────────────────


def test_validation_pass_remains_pass_regardless_of_ai_provider(passing_validation_report):
    """Validation PASS must remain PASS whether AI is Gemini, Groq, Template, or None."""
    val_summary = compute_validation_summary(passing_validation_report)
    assert val_summary["status"] == "PASS"
    assert val_summary["passed"] is True
    assert val_summary["pass_count"] == 5
    assert val_summary["fail_count"] == 0
    assert val_summary["total_issues"] == 0

    # Across different AI states, the validation summary is completely unaffected
    for provider in ["gemini", "groq", "template"]:
        ai_stat = compute_ai_status(
            ExplainResult(provider_used=provider, explanations=[Explanation({"id": 1}, ChangeType.NEW, "text", provider)]),
            provider_used=provider,
        )
        assert val_summary["status"] == "PASS"
        assert val_summary["passed"] is True


def test_validation_fail_remains_fail_even_when_gemini_succeeds(failing_validation_report):
    """Successful AI explanation generation must NEVER upgrade a failed validation result."""
    val_summary = compute_validation_summary(failing_validation_report)
    assert val_summary["status"] == "FAIL"
    assert val_summary["passed"] is False
    assert val_summary["fail_count"] >= 1
    assert val_summary["total_issues"] >= 1

    # Gemini online success
    gemini_result = ExplainResult(
        provider_used="gemini",
        explanations=[Explanation({"id": 101}, ChangeType.NEW, "New hire added", "gemini")],
        metrics=LLMMetrics(
            provider="gemini",
            model="gemini-2.5-flash",
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            estimated_cost=0.0001,
            request_duration=0.5,
        ),
    )
    ai_stat = compute_ai_status(gemini_result, provider_used="gemini")
    assert ai_stat["status"] == "SUCCESS"

    # Crucial guarantee: validation is still FAIL
    assert val_summary["status"] == "FAIL"
    assert val_summary["passed"] is False


def test_validation_pass_remains_pass_when_gemini_fails(passing_validation_report):
    """LLM failure must NEVER downgrade a passing validation result."""
    val_summary = compute_validation_summary(passing_validation_report)
    assert val_summary["status"] == "PASS"

    # AI completely unavailable
    ai_stat = compute_ai_status(None, provider_used="gemini")
    assert ai_stat["status"] == "UNAVAILABLE"

    # Validation remains PASS
    assert val_summary["status"] == "PASS"


def test_validation_pass_remains_pass_when_fallback_used(passing_validation_report):
    """Fallback to Groq or Template must NEVER downgrade a passing validation result."""
    val_summary = compute_validation_summary(passing_validation_report)
    assert val_summary["status"] == "PASS"

    # Fallback to template with warnings
    fallback_result = ExplainResult(
        provider_used="template",
        warnings=["Gemini quota exceeded. Falling back to template provider."],
        explanations=[Explanation({"id": 101}, ChangeType.NEW, "New hire added", "template")],
    )
    ai_stat = compute_ai_status(fallback_result, provider_used="template")
    assert ai_stat["status"] == "FALLBACK"
    assert ai_stat["has_fallback"] is True

    # Validation remains PASS
    assert val_summary["status"] == "PASS"


# ── 2. Truthful AI Status Representation ─────────────────────────────────────


def test_ai_status_primary_provider_success():
    """Online primary provider without warnings is SUCCESS with metrics."""
    result = ExplainResult(
        provider_used="gemini",
        explanations=[Explanation({"id": 101}, ChangeType.NEW, "Created", "gemini")],
        warnings=[],
        metrics=LLMMetrics(
            provider="gemini",
            model="gemini-2.5-flash",
            prompt_tokens=200,
            completion_tokens=80,
            total_tokens=280,
            estimated_cost=0.00015,
            request_duration=0.85,
        ),
    )
    ai_stat = compute_ai_status(result, provider_used="gemini")
    assert ai_stat["status"] == "SUCCESS"
    assert ai_stat["provider"] == "Gemini"
    assert ai_stat["has_fallback"] is False
    assert ai_stat["fallback_reason"] is None
    assert ai_stat["latency"] == "0.85s"
    assert ai_stat["tokens"] == "280"
    assert ai_stat["cost"] == "$0.0001"


def test_ai_status_provider_fallback():
    """Fallback from primary to secondary or template is labeled FALLBACK with warning reason."""
    result = ExplainResult(
        provider_used="groq",
        warnings=["Gemini 429 ResourceExhausted: falling back to groq"],
        explanations=[Explanation({"id": 101}, ChangeType.NEW, "Created", "groq")],
        metrics=LLMMetrics(
            provider="groq",
            model="llama-3.3-70b-versatile",
            prompt_tokens=180,
            completion_tokens=60,
            total_tokens=240,
            estimated_cost=0.0,
            request_duration=0.45,
        ),
    )
    ai_stat = compute_ai_status(result, provider_used="groq")
    assert ai_stat["status"] == "FALLBACK"
    assert ai_stat["provider"] == "Groq"
    assert ai_stat["has_fallback"] is True
    assert "Gemini 429" in ai_stat["fallback_reason"]


def test_ai_status_template_mode():
    """Template provider run intentionally without warnings is labeled TEMPLATE."""
    result = ExplainResult(
        provider_used="template",
        warnings=[],
        explanations=[Explanation({"id": 101}, ChangeType.NEW, "Record 101 was added", "template")],
    )
    ai_stat = compute_ai_status(result, provider_used="template")
    assert ai_stat["status"] == "TEMPLATE"
    assert ai_stat["provider"] == "Template"
    assert ai_stat["has_fallback"] is False
    assert ai_stat["fallback_reason"] is None


def test_ai_status_unavailable():
    """When no explanations are generated, status is UNAVAILABLE."""
    # None result
    ai_stat_none = compute_ai_status(None, provider_used="template")
    assert ai_stat_none["status"] == "UNAVAILABLE"

    # Empty explanations without warnings
    empty_result = ExplainResult(provider_used="gemini", explanations=[])
    ai_stat_empty = compute_ai_status(empty_result, provider_used="gemini")
    assert ai_stat_empty["status"] == "UNAVAILABLE"


# ── 3. Absence of Fake Confidence Score ──────────────────────────────────────


def test_no_confidence_assessment_in_ui_components():
    """Verify that compute_confidence_assessment has been completely removed."""
    import app.ui_components as ui
    assert not hasattr(ui, "compute_confidence_assessment"), (
        "compute_confidence_assessment was found in app.ui_components. It must be removed."
    )


def test_validation_summary_metrics_precision():
    """Validation summary contains exact rule counts and issue counts."""
    # Create custom report with 3 PASS, 1 WARN, 1 FAIL
    report = ValidationReport(
        rules=[
            ValidationRule("rule1", ValidationStatus.PASS, "ok"),
            ValidationRule("rule2", ValidationStatus.PASS, "ok"),
            ValidationRule("rule3", ValidationStatus.PASS, "ok"),
            ValidationRule("rule4", ValidationStatus.WARN, "warning", details=["id=1 warning"]),
            ValidationRule("rule5", ValidationStatus.FAIL, "fail", details=["id=2 invalid", "id=3 invalid"]),
        ]
    )
    summary = compute_validation_summary(report)
    assert summary["status"] == "FAIL"
    assert summary["passed"] is False
    assert summary["total_rules"] == 5
    assert summary["pass_count"] == 3
    assert summary["warn_count"] == 1
    assert summary["fail_count"] == 1
    assert summary["total_issues"] == 3
