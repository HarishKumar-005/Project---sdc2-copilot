"""Unit tests for Milestone V2.5 — Evidence-Grounded AI Explanation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from src.scd2_copilot.config import Settings
from src.scd2_copilot.db.models import HeldChangeBatchRow, InventorySourceRow
from src.scd2_copilot.explanation.grounding import validate_explanation_grounding
from src.scd2_copilot.explanation.models import (
    BatchExplanationResult,
    ExplanationContext,
    GroundingViolation,
)
from src.scd2_copilot.explanation.prompt import build_explanation_prompt
from src.scd2_copilot.explanation.providers import (
    BaseExplanationProvider,
    DeterministicExplanationProvider,
    GeminiExplanationProvider,
    GroqExplanationProvider,
)
from src.scd2_copilot.explanation.service import ExplanationService
from src.scd2_copilot.guardrail.models import (
    GuardrailDecision,
    GuardrailDecisionType,
    GuardrailEvidence,
    GuardrailSeverity,
    RuleId,
    TriggeredRule,
)
from src.scd2_copilot.worker.batch import MicroBatch
from src.scd2_copilot.worker.worker import IngestionWorker, WorkerCycleResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_evidence() -> GuardrailEvidence:
    return GuardrailEvidence(
        evaluated_records=10,
        changed_records=8,
        new_records=1,
        unchanged_records=1,
        affected_population_ratio=0.8,
        warehouses_affected=2,
        skus_affected=5,
        max_quantity_relative_change=1.25,
        max_quantity_absolute_change=50,
        total_quantity_absolute_change=200,
        status_deactivations_count=3,
        event_window_seconds=10.0,
        velocity_changes_per_second=2.5,
        validation_passed=True,
        validation_failures=[],
    )


@pytest.fixture
def normal_guardrail_decision() -> GuardrailDecision:
    ev = GuardrailEvidence(
        evaluated_records=5,
        changed_records=1,
        new_records=0,
        unchanged_records=4,
        affected_population_ratio=0.2,
        warehouses_affected=1,
        skus_affected=1,
        max_quantity_relative_change=0.05,
        max_quantity_absolute_change=2,
        total_quantity_absolute_change=2,
        status_deactivations_count=0,
        event_window_seconds=1.0,
        velocity_changes_per_second=1.0,
        validation_passed=True,
        validation_failures=[],
    )
    return GuardrailDecision(
        decision=GuardrailDecisionType.NORMAL,
        severity=GuardrailSeverity.LOW,
        triggered_rules=[],
        reasons=[],
        evidence=ev,
    )


@pytest.fixture
def suspicious_guardrail_decision(sample_evidence: GuardrailEvidence) -> GuardrailDecision:
    rules = [
        TriggeredRule(
            rule_id=RuleId.LARGE_QUANTITY_SWING.value,
            rule_name="Large Quantity Swing",
            description="Quantity swing exceeded threshold",
            threshold=1.0,
            observed_value=1.25,
            severity=GuardrailSeverity.HIGH,
            message="Quantity swing of 125% exceeded threshold of 100%",
        ),
        TriggeredRule(
            rule_id=RuleId.MASS_DEACTIVATION.value,
            rule_name="Mass Deactivation",
            description="Status deactivations exceeded threshold",
            threshold=2,
            observed_value=3,
            severity=GuardrailSeverity.HIGH,
            message="3 deactivations observed (threshold 2)",
        ),
    ]
    return GuardrailDecision(
        decision=GuardrailDecisionType.SUSPICIOUS,
        severity=GuardrailSeverity.HIGH,
        triggered_rules=rules,
        reasons=["Rule LARGE_QUANTITY_SWING triggered", "Rule MASS_DEACTIVATION triggered"],
        evidence=sample_evidence,
    )


# ---------------------------------------------------------------------------
# 1. ExplanationContext Tests
# ---------------------------------------------------------------------------


def test_context_from_normal_guardrail_decision(normal_guardrail_decision: GuardrailDecision) -> None:
    batch_id = uuid4()
    ctx = ExplanationContext.from_guardrail_decision(
        decision=normal_guardrail_decision,
        batch_id=batch_id,
        source_name="test_source",
        watermark_start=datetime(2026, 9, 1, tzinfo=timezone.utc),
        watermark_end=datetime(2026, 9, 2, tzinfo=timezone.utc),
        validation_passed=True,
        processing_status="COMMITTED",
    )
    assert ctx.decision == "NORMAL"
    assert ctx.severity == "LOW"
    assert len(ctx.triggered_rules) == 0
    assert ctx.validation_passed is True
    assert ctx.processing_status == "COMMITTED"
    assert ctx.source_name == "test_source"
    assert ctx.batch_id == batch_id


def test_context_from_suspicious_guardrail_decision(
    suspicious_guardrail_decision: GuardrailDecision,
) -> None:
    batch_id = uuid4()
    ctx = ExplanationContext.from_guardrail_decision(
        decision=suspicious_guardrail_decision,
        batch_id=batch_id,
        source_name="inventory_stream",
        validation_passed=True,
        processing_status="HELD",
        containment_status="HELD",
    )
    assert ctx.decision == "SUSPICIOUS"
    assert ctx.severity == "HIGH"
    assert len(ctx.triggered_rules) == 2
    assert ctx.triggered_rules[0].rule_id == RuleId.LARGE_QUANTITY_SWING.value
    assert ctx.containment_status == "HELD"


def test_context_from_held_batch_row(suspicious_guardrail_decision: GuardrailDecision) -> None:
    hold_id = uuid4()
    run_id = uuid4()
    now_utc = datetime.now(timezone.utc)
    evidence_dict = {
        "guardrail_evidence": suspicious_guardrail_decision.evidence.to_dict(),
        "triggered_rules": [r.to_dict() for r in suspicious_guardrail_decision.triggered_rules],
        "batch_records": [{"sku_id": "SKU-1", "quantity_on_hand": 500}],
        "batch_fingerprint": "abc123hash",
    }
    held_row = HeldChangeBatchRow(
        hold_id=hold_id,
        run_id=run_id,
        source_name="inventory",
        severity="HIGH",
        reason="Suspicious batch",
        records_affected=1,
        evidence=evidence_dict,
        status="HELD",
        created_at=now_utc,
    )
    ctx = ExplanationContext.from_held_batch(held_row)
    assert ctx.decision == "SUSPICIOUS"
    assert ctx.severity == "HIGH"
    assert len(ctx.triggered_rules) == 2
    assert ctx.containment_status == "HELD"
    assert ctx.evidence.max_quantity_relative_change == 1.25
    assert len(ctx.records_sample) == 1


# ---------------------------------------------------------------------------
# 2. Prompt Construction Tests
# ---------------------------------------------------------------------------


def test_build_explanation_prompt_contains_strict_rules(
    suspicious_guardrail_decision: GuardrailDecision,
) -> None:
    ctx = ExplanationContext.from_guardrail_decision(
        decision=suspicious_guardrail_decision,
        source_name="inventory_source",
    )
    prompt = build_explanation_prompt(ctx)
    assert "authoritative_decision" in prompt
    assert "SUSPICIOUS" in prompt
    assert "LARGE_QUANTITY_SWING" in prompt
    assert "MASS_DEACTIVATION" in prompt
    assert "JSON OUTPUT CONTRACT" in prompt
    assert "context_version" in prompt

    from src.scd2_copilot.explanation.prompt import SYSTEM_INSTRUCTIONS

    assert "CANNOT change the decision" in SYSTEM_INSTRUCTIONS


def test_build_explanation_prompt_sanitizes_credentials(sample_evidence: GuardrailEvidence) -> None:
    # Ensure sensitive fields are never included in prompts
    ctx = ExplanationContext(
        decision="SUSPICIOUS",
        severity="MEDIUM",
        triggered_rules=[],
        evidence=sample_evidence,
        source_name="auth_stream",
        records_sample=[{"password": "secret_password", "token": "api_key_12345", "val": 42}],
    )
    prompt = build_explanation_prompt(ctx)
    assert "secret_password" not in prompt
    assert "api_key_12345" not in prompt
    assert "42" in prompt


# ---------------------------------------------------------------------------
# 3. Grounding Verification Tests
# ---------------------------------------------------------------------------


def test_grounding_valid_explanation(suspicious_guardrail_decision: GuardrailDecision) -> None:
    ctx = ExplanationContext.from_guardrail_decision(
        decision=suspicious_guardrail_decision,
        source_name="inventory",
        containment_status="HELD",
    )
    provider = DeterministicExplanationProvider()
    res = provider.generate_explanation(ctx)

    is_grounded, violations = validate_explanation_grounding(res, ctx)
    assert is_grounded is True
    assert len(violations) == 0


def test_grounding_rejects_altered_decision(
    suspicious_guardrail_decision: GuardrailDecision,
) -> None:
    ctx = ExplanationContext.from_guardrail_decision(
        decision=suspicious_guardrail_decision,
        source_name="inventory",
    )
    # Tamper with decision: claim NORMAL when context is SUSPICIOUS
    tampered_res = BatchExplanationResult(
        summary="All normal.",
        what_changed="None",
        why_flagged="None",
        evidence_points=["10 records"],
        validation_summary="Valid",
        containment_summary="Held",
        decision="NORMAL",  # Tampered!
        severity="HIGH",
        provider="test",
        explanation_version="v1",
        generated_at=datetime.now(timezone.utc),
    )
    is_grounded, violations = validate_explanation_grounding(tampered_res, ctx)
    assert is_grounded is False
    assert any(v.rule_name == "DECISION_IMMUTABILITY" for v in violations)


def test_grounding_rejects_altered_severity(
    suspicious_guardrail_decision: GuardrailDecision,
) -> None:
    ctx = ExplanationContext.from_guardrail_decision(
        decision=suspicious_guardrail_decision,
        source_name="inventory",
    )
    tampered_res = BatchExplanationResult(
        summary="Flagged batch.",
        what_changed="None",
        why_flagged="None",
        evidence_points=["10 records"],
        validation_summary="Valid",
        containment_summary="Held",
        decision="SUSPICIOUS",
        severity="LOW",  # Tampered! Was HIGH
        provider="test",
        explanation_version="v1",
        generated_at=datetime.now(timezone.utc),
    )
    is_grounded, violations = validate_explanation_grounding(tampered_res, ctx)
    assert is_grounded is False
    assert any(v.rule_name == "SEVERITY_IMMUTABILITY" for v in violations)


def test_grounding_rejects_untriggered_rule_hallucination(
    suspicious_guardrail_decision: GuardrailDecision,
) -> None:
    ctx = ExplanationContext.from_guardrail_decision(
        decision=suspicious_guardrail_decision,
        source_name="inventory",
    )
    # Result cites HIGH_CHANGE_VOLUME which was NOT triggered
    hallucinated_res = BatchExplanationResult(
        summary="Flagged due to HIGH_CHANGE_VOLUME rule.",
        what_changed="None",
        why_flagged="Rule HIGH_CHANGE_VOLUME was triggered and caused this hold.",
        evidence_points=["10 records"],
        validation_summary="Valid",
        containment_summary="Held",
        decision="SUSPICIOUS",
        severity="HIGH",
        provider="test",
        explanation_version="v1",
        generated_at=datetime.now(timezone.utc),
    )
    is_grounded, violations = validate_explanation_grounding(hallucinated_res, ctx)
    assert is_grounded is False
    assert any(v.rule_name == "UNTRIGGERED_RULE_CLAIMED" for v in violations)


def test_grounding_rejects_validation_contradiction(sample_evidence: GuardrailEvidence) -> None:
    ctx = ExplanationContext(
        decision="SUSPICIOUS",
        severity="HIGH",
        triggered_rules=[],
        evidence=sample_evidence,
        source_name="test",
        validation_passed=True,
    )
    contradicting_res = BatchExplanationResult(
        summary="Validation failed with invalid dates.",
        what_changed="None",
        why_flagged="None",
        evidence_points=[],
        validation_summary="SCD2 validation failed and invariants were violated.",
        containment_summary="Held",
        decision="SUSPICIOUS",
        severity="HIGH",
        provider="test",
        explanation_version="v1",
        generated_at=datetime.now(timezone.utc),
    )
    is_grounded, violations = validate_explanation_grounding(contradicting_res, ctx)
    assert is_grounded is False
    assert any(v.rule_name == "CONTRADICTED_VALIDATION_STATUS" for v in violations)


def test_grounding_rejects_containment_contradiction(sample_evidence: GuardrailEvidence) -> None:
    ctx = ExplanationContext(
        decision="SUSPICIOUS",
        severity="HIGH",
        triggered_rules=[],
        evidence=sample_evidence,
        source_name="test",
        validation_passed=True,
        processing_status="HELD",
        containment_status="HELD",
    )
    contradicting_res = BatchExplanationResult(
        summary="Batch was committed to history.",
        what_changed="None",
        why_flagged="None",
        evidence_points=[],
        validation_summary="Valid",
        containment_summary="Batch successfully committed to history and watermark advanced.",
        decision="SUSPICIOUS",
        severity="HIGH",
        provider="test",
        explanation_version="v1",
        generated_at=datetime.now(timezone.utc),
    )
    is_grounded, violations = validate_explanation_grounding(contradicting_res, ctx)
    assert is_grounded is False
    assert any(v.rule_name == "CONTRADICTED_CONTAINMENT_STATUS" for v in violations)


# ---------------------------------------------------------------------------
# 4. DeterministicExplanationProvider Tests
# ---------------------------------------------------------------------------


def test_deterministic_provider_offline_and_complete(
    suspicious_guardrail_decision: GuardrailDecision,
) -> None:
    provider = DeterministicExplanationProvider()
    ctx = ExplanationContext.from_guardrail_decision(
        decision=suspicious_guardrail_decision,
        source_name="inventory_stream",
        containment_status="HELD",
    )
    res = provider.generate_explanation(ctx)

    assert res.decision == "SUSPICIOUS"
    assert res.severity == "HIGH"
    assert res.provider == "deterministic"
    assert res.model is None
    assert res.is_fallback is True
    assert res.grounding_passed is True
    assert len(res.summary) > 0
    assert len(res.what_changed) > 0
    assert len(res.why_flagged) > 0
    assert len(res.evidence_points) > 0
    assert "LARGE_QUANTITY_SWING" in res.why_flagged
    assert "MASS_DEACTIVATION" in res.why_flagged


def test_deterministic_provider_reproducibility(
    suspicious_guardrail_decision: GuardrailDecision,
) -> None:
    provider = DeterministicExplanationProvider()
    ctx = ExplanationContext.from_guardrail_decision(
        decision=suspicious_guardrail_decision,
        source_name="inventory_stream",
        containment_status="HELD",
    )
    res1 = provider.generate_explanation(ctx)
    res2 = provider.generate_explanation(ctx)

    assert res1.summary == res2.summary
    assert res1.what_changed == res2.what_changed
    assert res1.why_flagged == res2.why_flagged
    assert res1.evidence_points == res2.evidence_points


# ---------------------------------------------------------------------------
# 5. Remote Providers Mock Tests
# ---------------------------------------------------------------------------


def test_gemini_provider_success(suspicious_guardrail_decision: GuardrailDecision) -> None:
    ctx = ExplanationContext.from_guardrail_decision(
        decision=suspicious_guardrail_decision,
        source_name="inventory",
    )
    mock_json_response = """
    {
        "summary": "Suspicious inventory volume change detected.",
        "what_changed": "8 of 10 records modified.",
        "why_flagged": "Quantity swing of 125% breached 100% threshold.",
        "evidence_points": ["records_changed=8", "swing=125%"],
        "validation_summary": "SCD2 invariants valid.",
        "containment_summary": "Held in quarantine.",
        "decision": "SUSPICIOUS",
        "severity": "HIGH",
        "provider": "gemini",
        "model": "gemini-2.5-flash",
        "explanation_version": "v1"
    }
    """
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value.text = mock_json_response

    provider = GeminiExplanationProvider(api_key="test_key")
    provider._client = mock_client

    res = provider.generate_explanation(ctx)
    assert res.decision == "SUSPICIOUS"
    assert res.severity == "HIGH"
    assert res.provider == "gemini"
    assert res.summary == "Suspicious inventory volume change detected."
    assert res.is_fallback is False


def test_gemini_provider_transient_retry(suspicious_guardrail_decision: GuardrailDecision) -> None:
    ctx = ExplanationContext.from_guardrail_decision(
        decision=suspicious_guardrail_decision,
        source_name="inventory",
    )
    mock_json_response = """
    {
        "summary": "Recovered after retry.",
        "what_changed": "Changed",
        "why_flagged": "Flagged",
        "evidence_points": [],
        "validation_summary": "Valid",
        "containment_summary": "Held",
        "decision": "SUSPICIOUS",
        "severity": "HIGH",
        "provider": "gemini",
        "model": "gemini-2.5-flash",
        "explanation_version": "v1"
    }
    """
    mock_client = MagicMock()
    # 1st call fails with transient exception, 2nd call succeeds
    mock_client.models.generate_content.side_effect = [
        Exception("503 Server Unavailable"),
        MagicMock(text=mock_json_response),
    ]

    provider = GeminiExplanationProvider(api_key="test_key")
    provider._client = mock_client

    with patch("time.sleep"):  # Avoid sleeping in unit test
        res = provider.generate_explanation(ctx)
    assert res.summary == "Recovered after retry."


def test_groq_provider_success(suspicious_guardrail_decision: GuardrailDecision) -> None:
    ctx = ExplanationContext.from_guardrail_decision(
        decision=suspicious_guardrail_decision,
        source_name="inventory",
    )
    mock_json_response = """
    {
        "summary": "Groq generated summary.",
        "what_changed": "Changes",
        "why_flagged": "Breach",
        "evidence_points": [],
        "validation_summary": "Valid",
        "containment_summary": "Held",
        "decision": "SUSPICIOUS",
        "severity": "HIGH",
        "provider": "groq",
        "model": "openai/gpt-oss-120b",
        "explanation_version": "v1"
    }
    """
    mock_client = MagicMock()
    mock_completion = MagicMock()
    mock_completion.choices = [MagicMock(message=MagicMock(content=mock_json_response))]
    mock_client.chat.completions.create.return_value = mock_completion

    provider = GroqExplanationProvider(api_key="test_key")
    provider._client = mock_client

    res = provider.generate_explanation(ctx)
    assert res.decision == "SUSPICIOUS"
    assert res.provider == "groq"
    assert res.summary == "Groq generated summary."


# ---------------------------------------------------------------------------
# 6. ExplanationService Routing & Fallback Tests
# ---------------------------------------------------------------------------


def test_service_disabled_ai_uses_deterministic_template(
    suspicious_guardrail_decision: GuardrailDecision,
) -> None:
    settings = Settings(ai_explanation_enabled=False)
    service = ExplanationService(settings=settings)
    ctx = ExplanationContext.from_guardrail_decision(suspicious_guardrail_decision, source_name="test")

    res = service.explain_batch(ctx)
    assert res.provider == "deterministic"
    assert res.is_fallback is True


def test_service_normal_batch_uses_deterministic_by_default(
    normal_guardrail_decision: GuardrailDecision,
) -> None:
    settings = Settings(ai_explanation_enabled=True, ai_explanation_for_normal_batches=False)
    mock_gemini = MagicMock(spec=BaseExplanationProvider)
    service = ExplanationService(settings=settings, gemini_provider=mock_gemini)
    ctx = ExplanationContext.from_guardrail_decision(normal_guardrail_decision, source_name="test")

    res = service.explain_batch(ctx)
    assert res.provider == "deterministic"
    # Ensure Gemini was never called for normal batch
    mock_gemini.generate_explanation.assert_not_called()


def test_service_fallback_gemini_to_groq(suspicious_guardrail_decision: GuardrailDecision) -> None:
    mock_gemini = MagicMock(spec=BaseExplanationProvider)
    mock_gemini.generate_explanation.side_effect = RuntimeError("Gemini quota exceeded")

    mock_groq = MagicMock(spec=BaseExplanationProvider)
    groq_res = BatchExplanationResult(
        summary="Groq explanation.",
        what_changed="Changed",
        why_flagged="LARGE_QUANTITY_SWING",
        evidence_points=[],
        validation_summary="Valid",
        containment_summary="Held",
        decision="SUSPICIOUS",
        severity="HIGH",
        provider="groq",
        explanation_version="v1",
        generated_at=datetime.now(timezone.utc),
    )
    mock_groq.generate_explanation.return_value = groq_res

    service = ExplanationService(
        gemini_provider=mock_gemini,
        groq_provider=mock_groq,
    )
    ctx = ExplanationContext.from_guardrail_decision(suspicious_guardrail_decision, source_name="test")

    res = service.explain_batch(ctx)
    assert res.provider == "groq"
    assert res.summary == "Groq explanation."
    assert any("Gemini" in w for w in res.warnings)


def test_service_fallback_to_deterministic_when_all_fail(
    suspicious_guardrail_decision: GuardrailDecision,
) -> None:
    mock_gemini = MagicMock(spec=BaseExplanationProvider)
    mock_gemini.generate_explanation.side_effect = RuntimeError("Gemini error")

    mock_groq = MagicMock(spec=BaseExplanationProvider)
    mock_groq.generate_explanation.side_effect = RuntimeError("Groq error")

    service = ExplanationService(
        gemini_provider=mock_gemini,
        groq_provider=mock_groq,
    )
    ctx = ExplanationContext.from_guardrail_decision(suspicious_guardrail_decision, source_name="test")

    res = service.explain_batch(ctx)
    assert res.provider == "deterministic"
    assert res.is_fallback is True
    assert any("Gemini" in w for w in res.warnings)
    assert any("Groq" in w for w in res.warnings)


def test_service_rejects_ungrounded_output_and_falls_back(
    suspicious_guardrail_decision: GuardrailDecision,
) -> None:
    # Gemini produces output that alters the decision
    ungrounded_res = BatchExplanationResult(
        summary="Gemini claims batch is normal.",
        what_changed="Changed",
        why_flagged="None",
        evidence_points=[],
        validation_summary="Valid",
        containment_summary="Held",
        decision="NORMAL",  # Altered! Context is SUSPICIOUS
        severity="HIGH",
        provider="gemini",
        explanation_version="v1",
        generated_at=datetime.now(timezone.utc),
    )
    mock_gemini = MagicMock(spec=BaseExplanationProvider)
    mock_gemini.generate_explanation.return_value = ungrounded_res

    service = ExplanationService(gemini_provider=mock_gemini, groq_provider=None)
    ctx = ExplanationContext.from_guardrail_decision(suspicious_guardrail_decision, source_name="test")

    res = service.explain_batch(ctx)
    # Should reject Gemini due to DECISION_IMMUTABILITY and fallback to deterministic
    assert res.provider == "deterministic"
    assert res.is_fallback is True
    assert any("grounding failure" in w for w in res.warnings)


def test_service_explain_held_batch_additive_persistence(
    suspicious_guardrail_decision: GuardrailDecision,
) -> None:
    hold_id = uuid4()
    run_id = uuid4()
    now_utc = datetime.now(timezone.utc)
    held_row = HeldChangeBatchRow(
        hold_id=hold_id,
        run_id=run_id,
        source_name="inventory",
        severity="HIGH",
        reason="Suspicious batch",
        records_affected=1,
        evidence={
            "guardrail_evidence": suspicious_guardrail_decision.evidence.to_dict(),
            "triggered_rules": [r.to_dict() for r in suspicious_guardrail_decision.triggered_rules],
        },
        status="HELD",
        created_at=now_utc,
    )

    mock_hold_repo = MagicMock()
    mock_hold_repo.get_hold.return_value = held_row

    service = ExplanationService(
        hold_repo=mock_hold_repo,
        gemini_provider=None,
        groq_provider=None,
    )

    res = service.explain_held_batch(hold_id=hold_id, persist=True)
    assert res.decision == "SUSPICIOUS"
    mock_hold_repo.attach_explanation.assert_called_once()
    call_args = mock_hold_repo.attach_explanation.call_args
    assert call_args.kwargs["hold_id"] == hold_id
    assert call_args.kwargs["explanation"]["decision"] == "SUSPICIOUS"


# ---------------------------------------------------------------------------
# 7. Worker Integration & Isolation Tests
# ---------------------------------------------------------------------------


def test_worker_explanation_failure_isolation() -> None:
    """If ExplanationService fails or throws, the worker cycle must not crash.

    The batch must still be HELD and the cycle result must report HELD.
    """
    mock_containment = MagicMock()
    hold_row = HeldChangeBatchRow(
        hold_id=uuid4(),
        run_id=uuid4(),
        source_name="inventory",
        severity="HIGH",
        reason="Suspicious batch",
        records_affected=5,
        evidence={},
        status="HELD",
        created_at=datetime.now(timezone.utc),
    )
    mock_containment.contain_suspicious_batch.return_value = (hold_row, True)

    mock_explanation_service = MagicMock()
    mock_explanation_service.explain_held_batch.side_effect = Exception("LLM crash")

    mock_db = MagicMock()
    mock_guardrail = MagicMock()
    suspicious_decision = GuardrailDecision(
        decision=GuardrailDecisionType.SUSPICIOUS,
        severity=GuardrailSeverity.HIGH,
        triggered_rules=[
            TriggeredRule(
                rule_id=RuleId.LARGE_QUANTITY_SWING.value,
                rule_name="Large Quantity Swing",
                description="Swing",
                threshold=1.0,
                observed_value=1.5,
                severity=GuardrailSeverity.HIGH,
                message="Swing exceeded",
            )
        ],
        reasons=["Swing"],
        evidence=GuardrailEvidence(
            evaluated_records=5,
            changed_records=5,
            new_records=0,
            unchanged_records=0,
            affected_population_ratio=1.0,
            warehouses_affected=1,
            skus_affected=1,
            max_quantity_relative_change=1.5,
            max_quantity_absolute_change=50,
            total_quantity_absolute_change=50,
            status_deactivations_count=0,
            event_window_seconds=1.0,
            velocity_changes_per_second=5.0,
            validation_passed=True,
            validation_failures=[],
        ),
    )
    mock_guardrail.evaluate.return_value = suspicious_decision

    worker = IngestionWorker(
        db_manager=mock_db,
        guardrail=mock_guardrail,
        containment=mock_containment,
        explanation_service=mock_explanation_service,
    )

    t1 = datetime(2026, 9, 16, 10, 0, 0, tzinfo=timezone.utc)
    source_records = [
        InventorySourceRow(
            sku_id="SKU-1",
            warehouse_id="WH-1",
            quantity_on_hand=100,
            reorder_level=20,
            status="ACTIVE",
            updated_at=t1,
        )
    ]

    worker.checkpoint_repo = MagicMock()
    worker.checkpoint_repo.get_checkpoint.return_value = MagicMock(
        watermark_value=datetime(2026, 9, 16, 9, 0, 0, tzinfo=timezone.utc)
    )
    worker.inventory_repo = MagicMock()
    worker.inventory_repo.fetch_inventory_updated_after.return_value = source_records
    worker.history_repo = MagicMock()
    worker.history_repo.fetch_current_history_for_keys.return_value = []

    with patch("src.scd2_copilot.worker.worker.detect_changes") as mock_detect:
        with patch("src.scd2_copilot.worker.worker.apply_scd2") as mock_apply:
            with patch("src.scd2_copilot.worker.worker.validate_scd2") as mock_val:
                mock_detect.return_value = MagicMock(changed=[MagicMock()])
                mock_apply.return_value = MagicMock()
                mock_val.return_value = MagicMock(passed=True)

                cycle_result = worker.run_once()

    # The cycle MUST be HELD even though explanation generation threw an exception
    assert cycle_result.status == "HELD"
    assert cycle_result.records_held == 1
    assert cycle_result.explanation is None


def test_service_groq_as_primary_provider(
    suspicious_guardrail_decision: GuardrailDecision,
) -> None:
    """Ensure that when settings.llm_provider == 'groq', Groq is called first as primary."""
    from src.scd2_copilot.config import LLMProvider

    groq_res = BatchExplanationResult(
        summary="Groq primary explanation.",
        what_changed="Changed",
        why_flagged="Suspicious volume",
        evidence_points=["1 record changed"],
        validation_summary="Validation passed",
        containment_summary="Held in quarantine",
        decision="SUSPICIOUS",
        severity="HIGH",
        provider="groq",
        explanation_version="v1",
        generated_at=datetime.now(timezone.utc),
    )

    mock_gemini = MagicMock(spec=BaseExplanationProvider)
    mock_groq = MagicMock(spec=BaseExplanationProvider)
    mock_groq.generate_explanation.return_value = groq_res

    settings = Settings(llm_provider=LLMProvider.GROQ)
    svc = ExplanationService(
        settings=settings,
        gemini_provider=mock_gemini,
        groq_provider=mock_groq,
    )
    ctx = ExplanationContext.from_guardrail_decision(suspicious_guardrail_decision, source_name="test")

    res = svc.explain_batch(ctx)

    # Groq was called first and succeeded; Gemini was never called
    assert mock_groq.generate_explanation.called
    assert not mock_gemini.generate_explanation.called
    assert res.provider == "groq"
    assert res.summary == "Groq primary explanation."


