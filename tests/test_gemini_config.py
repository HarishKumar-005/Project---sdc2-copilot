"""Tests for Gemini configuration-driven model fallback chain, runtime routing, and metrics tracking.

Verifies:
1. Default primary model is gemini-3.8-flash; default fallbacks are 3.5-flash-lite, 3.1-flash-lite, 2.5-flash.
2. Configuration overrides (Settings and env vars for GEMINI_MODEL and GEMINI_FALLBACK_MODELS).
3. Blank primary model safely resolves to default; blank fallbacks resolve to [].
4. GeminiProvider respects configured model chain and attempts primary model first.
5. Transient errors retry the same model with exponential backoff.
6. Non-recoverable errors (404, daily quota 429) advance immediately to the next model in chain.
7. Successful fallback accurately records the actual model used in metrics and explanations.
8. Complete chain exhaustion falls back to Groq, then Template.
9. Structured output schema (ExplanationItem) remains preserved.
10. Anti-regression test ensures old hard-coded MODEL_CHAIN is absent while legitimate models are supported.
"""

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, call, patch
import pytest

from src.scd2_copilot.config import LLMProvider, Settings, get_settings
from src.scd2_copilot.explain import ExplainResult, explain_changes, get_provider
from src.scd2_copilot.models import (
    ChangeRecord,
    ChangeReport,
    ChangeType,
    FieldChange,
    ValidationReport,
    ValidationRule,
    ValidationStatus,
)
from src.scd2_copilot.providers.gemini import (
    DEFAULT_GEMINI_FALLBACK_MODELS,
    DEFAULT_GEMINI_MODEL,
    ExplanationItem,
    GeminiProvider,
)
from src.scd2_copilot.providers.groq import GroqProvider
from src.scd2_copilot.providers.template import TemplateProvider


# ── 1. Settings & Configuration Tests ──────────────────────────────────────────


def test_default_primary_and_fallback_models():
    """Default primary should be gemini-3.8-flash; default fallbacks should match account availability."""
    assert DEFAULT_GEMINI_MODEL == "gemini-3.8-flash"
    assert DEFAULT_GEMINI_FALLBACK_MODELS == [
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-2.5-flash",
    ]

    settings = Settings(_env_file=None)
    assert settings.gemini_model == "gemini-3.8-flash"
    assert settings.gemini_fallback_models == [
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-2.5-flash",
    ]
    assert settings.get_gemini_model_chain() == [
        "gemini-3.8-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-2.5-flash",
    ]


def test_env_override_gemini_model(monkeypatch):
    """GEMINI_MODEL environment variable should override the primary model."""
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.7-flash")
    settings = Settings(_env_file=None)
    assert settings.gemini_model == "gemini-3.7-flash"


def test_env_override_gemini_fallback_models(monkeypatch):
    """GEMINI_FALLBACK_MODELS comma-separated environment variable should override fallbacks."""
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash-lite, gemini-2.5-flash")
    settings = Settings(_env_file=None)
    assert settings.gemini_fallback_models == ["gemini-3.5-flash-lite", "gemini-2.5-flash"]
    assert settings.get_gemini_model_chain() == [
        "gemini-3.8-flash",
        "gemini-3.5-flash-lite",
        "gemini-2.5-flash",
    ]


def test_blank_primary_model_safely_resolves_to_default():
    """Empty or whitespace-only primary model should default safely to gemini-3.8-flash."""
    s_empty = Settings(_env_file=None, gemini_model="")
    assert s_empty.gemini_model == "gemini-3.8-flash"

    s_spaces = Settings(_env_file=None, gemini_model="   ")
    assert s_spaces.gemini_model == "gemini-3.8-flash"


def test_blank_fallback_models_resolves_to_empty_list():
    """Empty or whitespace fallback string should resolve to an empty list."""
    s_empty_str = Settings(_env_file=None, gemini_fallback_models="")
    assert s_empty_str.gemini_fallback_models == []
    assert s_empty_str.get_gemini_model_chain() == ["gemini-3.8-flash"]

    s_spaces = Settings(_env_file=None, gemini_fallback_models="   ")
    assert s_spaces.gemini_fallback_models == []

    s_none = Settings(_env_file=None, gemini_fallback_models=None)
    assert s_none.gemini_fallback_models == []


# ── 2. Provider Model Chain & Routing Tests ────────────────────────────────────


def test_gemini_provider_default_model_chain():
    """GeminiProvider initialized without explicit args should configure default model chain."""
    with patch("src.scd2_copilot.providers.gemini.genai.Client"):
        provider = GeminiProvider(api_key="mock_key")
        assert provider.primary_model == "gemini-3.8-flash"
        assert provider.model == "gemini-3.8-flash"
        assert provider.fallback_models == [
            "gemini-3.5-flash-lite",
            "gemini-3.1-flash-lite",
            "gemini-2.5-flash",
        ]
        assert provider.model_chain == [
            "gemini-3.8-flash",
            "gemini-3.5-flash-lite",
            "gemini-3.1-flash-lite",
            "gemini-2.5-flash",
        ]


def test_gemini_provider_custom_chain():
    """GeminiProvider should accept explicit primary and fallback models."""
    with patch("src.scd2_copilot.providers.gemini.genai.Client"):
        provider = GeminiProvider(
            api_key="mock_key",
            model="gemini-3.7-flash",
            fallback_models=["gemini-2.5-flash"],
        )
        assert provider.primary_model == "gemini-3.7-flash"
        assert provider.fallback_models == ["gemini-2.5-flash"]
        assert provider.model_chain == ["gemini-3.7-flash", "gemini-2.5-flash"]


def test_get_provider_passes_configured_chain():
    """get_provider(settings) should pass configured primary and fallback models."""
    with patch("src.scd2_copilot.providers.gemini.genai.Client"):
        settings = Settings(
            _env_file=None,
            gemini_api_key="mock_gemini_key",
            llm_provider=LLMProvider.GEMINI,
            gemini_model="gemini-3.7-flash",
            gemini_fallback_models=["gemini-2.5-flash"],
        )
        provider = get_provider(settings)
        assert isinstance(provider, GeminiProvider)
        assert provider.primary_model == "gemini-3.7-flash"
        assert provider.fallback_models == ["gemini-2.5-flash"]


# ── 3. Per-Model Retry & Transient vs Non-Recoverable Errors ───────────────────


@patch("src.scd2_copilot.providers.gemini.time.sleep")
@patch("src.scd2_copilot.providers.gemini.genai.Client")
def test_transient_primary_failure_retries_same_model(mock_client_cls, mock_sleep):
    """Transient per-minute rate limits (429 without PerDay) should retry the same model with backoff."""
    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client

    # Fail on attempt 1 with 429 rate limit, succeed on attempt 2
    mock_response = MagicMock()
    mock_response.text = "Valid explanation after retry."
    mock_response.usage_metadata = MagicMock(prompt_token_count=50, candidates_token_count=20, total_token_count=70)

    mock_client.models.generate_content.side_effect = [
        Exception("ResourceExhausted: 429 rate limit exceeded"),
        mock_response,
    ]

    provider = GeminiProvider(api_key="mock_key", model="gemini-3.8-flash")
    record = ChangeRecord(business_key_values={"id": 1}, change_type=ChangeType.NEW)
    explanation = provider.explain_change(record)

    # Assertions
    assert explanation.provider == "gemini (gemini-3.8-flash)"
    assert explanation.text == "Valid explanation after retry."
    assert mock_sleep.call_count == 1
    assert mock_sleep.call_args[0][0] == 3  # base retry delay
    assert provider.last_metrics.model == "gemini-3.8-flash"
    assert provider.last_attempted_models == ["gemini-3.8-flash"]


@patch("src.scd2_copilot.providers.gemini.time.sleep")
@patch("src.scd2_copilot.providers.gemini.genai.Client")
def test_non_recoverable_primary_failure_advances_immediately(mock_client_cls, mock_sleep):
    """Non-recoverable errors (404, daily quota exhausted) should immediately advance to the next model without retrying."""
    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client

    mock_response = MagicMock()
    mock_response.text = "Generated by fallback model."
    mock_response.usage_metadata = MagicMock(prompt_token_count=60, candidates_token_count=25, total_token_count=85)

    def side_effect(model, contents, config=None):
        if model == "gemini-3.8-flash":
            # Non-recoverable daily quota exhaustion
            raise Exception("ResourceExhausted: 429 Quota exceeded: PerDay limit reached")
        elif model == "gemini-3.5-flash-lite":
            return mock_response
        raise RuntimeError("Unexpected call")

    mock_client.models.generate_content.side_effect = side_effect

    provider = GeminiProvider(
        api_key="mock_key",
        model="gemini-3.8-flash",
        fallback_models=["gemini-3.5-flash-lite", "gemini-2.5-flash"],
    )
    record = ChangeRecord(business_key_values={"id": 10}, change_type=ChangeType.NEW)
    explanation = provider.explain_change(record)

    # Assertions: No sleep was wasted on the exhausted model!
    assert mock_sleep.call_count == 0
    assert explanation.provider == "gemini (gemini-3.5-flash-lite)"
    assert explanation.text == "Generated by fallback model."
    assert provider.last_metrics.model == "gemini-3.5-flash-lite"
    assert provider.last_attempted_models == ["gemini-3.8-flash", "gemini-3.5-flash-lite"]


# ── 4. Structured Output & Accurate Metric Attribution ────────────────────────


def test_structured_output_schema_preserved():
    """ExplanationItem Pydantic schema must preserve id and explanation fields."""
    item = ExplanationItem(id=0, explanation="Customer address updated from NY to CA.")
    assert item.id == 0
    assert item.explanation == "Customer address updated from NY to CA."

    schema = ExplanationItem.model_json_schema()
    assert "id" in schema["properties"]
    assert "explanation" in schema["properties"]
    assert schema["properties"]["id"]["type"] == "integer"
    assert schema["properties"]["explanation"]["type"] == "string"


@patch("src.scd2_copilot.providers.gemini.genai.Client")
def test_successful_fallback_records_actual_model_in_metrics(mock_client_cls):
    """When fallback occurs, LLMMetrics must record the actual successful model, not the primary."""
    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client

    mock_response = MagicMock()
    mock_response.parsed = [ExplanationItem(id=0, explanation="Handled by 3.1-flash-lite.")]
    mock_response.usage_metadata = MagicMock(prompt_token_count=100, candidates_token_count=30, total_token_count=130)

    def side_effect(model, contents, config=None):
        if model in ("gemini-3.8-flash", "gemini-3.5-flash-lite"):
            raise Exception("Model 404 not found")
        elif model == "gemini-3.1-flash-lite":
            return mock_response
        raise RuntimeError("Unexpected model call")

    mock_client.models.generate_content.side_effect = side_effect

    provider = GeminiProvider(
        api_key="mock_key",
        model="gemini-3.8-flash",
        fallback_models=["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-2.5-flash"],
    )

    records = [
        ChangeRecord(
            business_key_values={"id": 101},
            change_type=ChangeType.CHANGED,
            field_changes=[FieldChange("city", "Old", "New")],
        )
    ]

    explanations = provider.explain_changes_batch(records)
    assert len(explanations) == 1
    assert explanations[0].provider == "gemini (gemini-3.1-flash-lite)"
    assert explanations[0].text == "Handled by 3.1-flash-lite."

    # Metrics must report the actual model that succeeded
    metrics = provider.last_metrics
    assert metrics is not None
    assert metrics.provider == "gemini"
    assert metrics.model == "gemini-3.1-flash-lite"
    assert metrics.prompt_tokens == 100
    assert metrics.completion_tokens == 30
    # For gemini-3.1-flash-lite: $0.25/1M input, $1.50/1M output
    # (100 * 0.25 / 1_000_000) + (30 * 1.50 / 1_000_000) = 0.000025 + 0.000045 = 0.000070
    assert pytest.approx(metrics.estimated_cost, 1e-8) == 0.000070
    assert provider.last_attempted_models == [
        "gemini-3.8-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
    ]


# ── 5. Provider-Level Fallbacks (Gemini -> Groq -> Template) ───────────────────


@patch("src.scd2_copilot.providers.gemini.time.sleep")
@patch("src.scd2_copilot.providers.gemini.genai.Client")
@patch("src.scd2_copilot.providers.groq.groq_sdk.Groq")
def test_gemini_exhaustion_falls_back_to_groq(mock_groq_cls, mock_gemini_cls, mock_sleep):
    """When all Gemini models fail and Groq is configured, execution falls back to Groq."""
    mock_gemini = MagicMock()
    mock_gemini_cls.return_value = mock_gemini
    mock_gemini.models.generate_content.side_effect = Exception("404 Model not found")

    mock_groq = MagicMock()
    mock_groq_cls.return_value = mock_groq
    mock_chat = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = '{"explanations": [{"id": 0, "explanation": "Handled by Groq"}]}'
    mock_chat.choices = [mock_choice]
    mock_chat.usage = MagicMock(prompt_tokens=80, completion_tokens=20, total_tokens=100)
    mock_groq.chat.completions.create.return_value = mock_chat

    settings = Settings(
        _env_file=None,
        gemini_api_key="mock_gemini",
        groq_api_key="mock_groq",
        llm_provider=LLMProvider.GEMINI,
        gemini_model="gemini-3.8-flash",
        gemini_fallback_models=["gemini-2.5-flash"],
    )

    report = ChangeReport(
        changed=[
            ChangeRecord(
                business_key_values={"id": 101},
                change_type=ChangeType.CHANGED,
                field_changes=[FieldChange("city", "Old", "New")],
            )
        ],
        processing_date=date(2026, 6, 8),
    )

    result = explain_changes(report, settings=settings)

    assert result.provider_used == "groq"
    assert len(result.explanations) == 1
    assert result.explanations[0].provider == "groq"
    assert result.explanations[0].text == "Handled by Groq"
    assert any("Fell back to Groq" in w for w in result.warnings)
    assert result.metrics.provider == "groq"


@patch("src.scd2_copilot.providers.gemini.time.sleep")
@patch("src.scd2_copilot.providers.gemini.genai.Client")
@patch("src.scd2_copilot.providers.groq.groq_sdk.Groq")
def test_gemini_and_groq_exhaustion_falls_back_to_template(mock_groq_cls, mock_gemini_cls, mock_sleep):
    """When both Gemini chain and Groq fail, execution falls back cleanly to deterministic template."""
    mock_gemini = MagicMock()
    mock_gemini_cls.return_value = mock_gemini
    mock_gemini.models.generate_content.side_effect = Exception("All Gemini models down")

    mock_groq = MagicMock()
    mock_groq_cls.return_value = mock_groq
    mock_groq.chat.completions.create.side_effect = Exception("Groq connection timeout")

    settings = Settings(
        _env_file=None,
        gemini_api_key="mock_gemini",
        groq_api_key="mock_groq",
        llm_provider=LLMProvider.GEMINI,
    )

    report = ChangeReport(
        new=[ChangeRecord(business_key_values={"id": 300}, change_type=ChangeType.NEW)],
        processing_date=date(2026, 6, 8),
    )

    result = explain_changes(report, settings=settings)

    assert result.provider_used == "template"
    assert len(result.explanations) == 1
    assert result.explanations[0].provider == "template"
    assert result.metrics.provider == "template"
    assert any("Groq fallback failed" in w for w in result.warnings)


# ── 6. Deterministic Validation Independence ───────────────────────────────────


def test_deterministic_validation_remains_independent_from_ai_status():
    """Failed SCD2 validation cannot be upgraded or altered by AI status."""
    from app.ui_components import compute_ai_status, compute_validation_summary

    failing_report = ValidationReport(
        rules=[
            ValidationRule(
                name="date_consistency",
                status=ValidationStatus.FAIL,
                message="effective_from > effective_to",
            )
        ]
    )
    val_summary = compute_validation_summary(failing_report)
    assert val_summary["status"] == "FAIL"
    assert val_summary["passed"] is False

    ai_result = ExplainResult(
        provider_used="gemini",
        explanations=[ChangeRecord({"id": 1}, ChangeType.NEW)],
    )
    ai_stat = compute_ai_status(ai_result, provider_used="gemini")
    assert ai_stat["status"] == "SUCCESS"
    assert val_summary["status"] == "FAIL"  # Remains FAIL!


# ── 7. Anti-Regression & Model Hygiene ─────────────────────────────────────────


def test_regression_no_old_hardcoded_model_chain_in_gemini_provider():
    """Verify that the old hard-coded MODEL_CHAIN = [...] loop no longer exists in provider source."""
    gemini_file = Path(__file__).resolve().parent.parent / "src" / "scd2_copilot" / "providers" / "gemini.py"
    content = gemini_file.read_text(encoding="utf-8")

    assert "MODEL_CHAIN =" not in content, (
        "Hard-coded 'MODEL_CHAIN =' must not exist in gemini.py; model routing must be configuration-driven."
    )
    # Check that unversioned preview endpoint gemini-3-flash is not present
    assert '"gemini-3-flash"' not in content, (
        "Preview endpoint 'gemini-3-flash' must not be hardcoded in gemini.py."
    )


# ── 8. M1.8 Production-Readiness Hardening Verification ───────────────────────


def test_single_source_of_truth_configuration():
    """Settings in config.py must own model defaults, and gemini.py must import them."""
    import src.scd2_copilot.config as config_mod
    import src.scd2_copilot.providers.gemini as gemini_mod

    assert hasattr(config_mod, "DEFAULT_GEMINI_MODEL")
    assert hasattr(config_mod, "DEFAULT_GEMINI_FALLBACK_MODELS")
    assert gemini_mod.DEFAULT_GEMINI_MODEL is config_mod.DEFAULT_GEMINI_MODEL
    assert gemini_mod.DEFAULT_GEMINI_FALLBACK_MODELS is config_mod.DEFAULT_GEMINI_FALLBACK_MODELS


def test_model_normalization_and_deduplication():
    """Settings must normalize model IDs, strip whitespace, remove empty entries, and remove duplicates."""
    settings = Settings(
        _env_file=None,
        gemini_model="  gemini-3.8-flash  ",
        gemini_fallback_models=[
            "gemini-3.8-flash",  # duplicate of primary -> must be removed from fallbacks
            "  gemini-3.5-flash-lite  ",
            "   ",  # empty -> must be removed
            "gemini-3.5-flash-lite",  # duplicate fallback -> must be removed
            "gemini-2.5-flash",
        ],
    )
    assert settings.gemini_model == "gemini-3.8-flash"
    assert settings.gemini_fallback_models == ["gemini-3.5-flash-lite", "gemini-2.5-flash"]
    assert settings.get_gemini_model_chain() == [
        "gemini-3.8-flash",
        "gemini-3.5-flash-lite",
        "gemini-2.5-flash",
    ]


@patch("src.scd2_copilot.providers.gemini.time.sleep")
@patch("src.scd2_copilot.providers.gemini.genai.Client")
def test_retry_semantics_three_attempts_total(mock_client_cls, mock_sleep):
    """MAX_RETRIES=2 must mean 1 initial attempt + 2 retries = 3 total attempts per model."""
    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client

    mock_response = MagicMock()
    mock_response.text = "Success on attempt 3"
    mock_response.usage_metadata = MagicMock(prompt_token_count=10, candidates_token_count=5, total_token_count=15)

    # Fail on attempt 1 and 2, succeed on attempt 3
    mock_client.models.generate_content.side_effect = [
        Exception("503 Service Unavailable"),
        Exception("503 Service Unavailable"),
        mock_response,
    ]

    provider = GeminiProvider(api_key="mock", model="gemini-3.8-flash", fallback_models=[])
    res, model = provider._call_model("test prompt")

    assert res == "Success on attempt 3"
    assert model == "gemini-3.8-flash"
    assert mock_client.models.generate_content.call_count == 3
    assert mock_sleep.call_count == 2
    assert [c.args[0] for c in mock_sleep.call_args_list] == [3, 6]


@patch("src.scd2_copilot.providers.gemini.time.sleep")
@patch("src.scd2_copilot.providers.gemini.genai.Client")
def test_no_sleep_on_final_attempt(mock_client_cls, mock_sleep):
    """When retries are exhausted for a model, it must advance immediately without sleeping after the final attempt."""
    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client

    mock_response = MagicMock()
    mock_response.text = "Fallback succeeded"
    mock_response.usage_metadata = MagicMock(prompt_token_count=10, candidates_token_count=5, total_token_count=15)

    def side_effect(model, contents, config=None):
        if model == "gemini-3.8-flash":
            raise Exception("500 Internal Server Error")
        return mock_response

    mock_client.models.generate_content.side_effect = side_effect

    provider = GeminiProvider(
        api_key="mock",
        model="gemini-3.8-flash",
        fallback_models=["gemini-2.5-flash"],
    )
    res, model = provider._call_model("test prompt")

    assert model == "gemini-2.5-flash"
    # Primary model made 3 attempts (1 initial + 2 retries), slept twice (delays: 3, 6), and did NOT sleep after attempt 3
    assert mock_sleep.call_count == 2
    assert [c.args[0] for c in mock_sleep.call_args_list] == [3, 6]


def test_model_aware_cost_calculation():
    """Cost calculation must be model-aware and distinguish Gemini 3.8 vs 3.5 vs 3.1 vs 2.5 vs unknown models."""
    from src.scd2_copilot.providers.gemini import calculate_gemini_cost

    # gemini-3.8-flash: $0.75 / 1M prompt, $3.75 / 1M completion (introductory through Dec 31, 2026)
    cost_38, est_38 = calculate_gemini_cost("gemini-3.8-flash", prompt_tokens=1_000_000, completion_tokens=1_000_000)
    assert pytest.approx(cost_38, 1e-6) == 4.50
    assert est_38 is False

    # gemini-3.5-flash-lite: $0.30 / 1M prompt, $2.50 / 1M completion
    cost_35, est_35 = calculate_gemini_cost("gemini-3.5-flash-lite", prompt_tokens=1_000_000, completion_tokens=1_000_000)
    assert pytest.approx(cost_35, 1e-6) == 2.80
    assert est_35 is False

    # gemini-3.1-flash-lite: $0.25 / 1M prompt, $1.50 / 1M completion
    cost_31, est_31 = calculate_gemini_cost("gemini-3.1-flash-lite", prompt_tokens=1_000_000, completion_tokens=1_000_000)
    assert pytest.approx(cost_31, 1e-6) == 1.75
    assert est_31 is False

    # gemini-2.5-flash: $0.30 / 1M prompt, $2.50 / 1M completion
    cost_25, est_25 = calculate_gemini_cost("gemini-2.5-flash", prompt_tokens=1_000_000, completion_tokens=1_000_000)
    assert pytest.approx(cost_25, 1e-6) == 2.80
    assert est_25 is False

    # Unknown model: marks cost as unavailable/estimated (0.0, True)
    cost_unk, est_unk = calculate_gemini_cost("gemini-future-model", prompt_tokens=100, completion_tokens=50)
    assert cost_unk == 0.0
    assert est_unk is True


def test_gemini_38_flash_post_2026_pricing():
    """Verify gemini-3.8-flash transitions to standard pricing ($1.50/$7.50) from Jan 1, 2027."""
    from datetime import date
    from unittest.mock import patch
    from src.scd2_copilot.providers.gemini import calculate_gemini_cost

    class MockDate(date):
        @classmethod
        def today(cls):
            return cls(2027, 1, 15)

    with patch("datetime.date", MockDate):
        cost, est = calculate_gemini_cost("gemini-3.8-flash", prompt_tokens=1_000_000, completion_tokens=1_000_000)
        assert pytest.approx(cost, 1e-6) == 9.00  # 1.50 + 7.50
        assert est is False


def test_structured_sdk_exception_classification():
    """_classify_gemini_error must inspect structured SDK properties (code, status) before string fallback."""
    from src.scd2_copilot.providers.gemini import _classify_gemini_error

    class FakeAPIError(Exception):
        def __init__(self, code, status, message):
            self.code = code
            self.status = status
            self.message = message
            super().__init__(f"{code} {status}: {message}")

    # 1. Invalid model / 404
    err_404 = FakeAPIError(404, "NOT_FOUND", "models/unknown is not found")
    assert _classify_gemini_error(err_404) == "invalid_model"

    # 2. Transient server error / 503
    err_503 = FakeAPIError(503, "UNAVAILABLE", "The service is temporarily unavailable")
    assert _classify_gemini_error(err_503) == "transient_server"

    # 3. Transient rate limit / 429 without PerDay
    err_rate = FakeAPIError(429, "RESOURCE_EXHAUSTED", "Rate limit exceeded. Please retry.")
    assert _classify_gemini_error(err_rate) == "transient_rate_limit"

    # 4. Exhausted daily quota / 429 with PerDay
    err_quota = FakeAPIError(429, "RESOURCE_EXHAUSTED", "Quota exceeded for quota metric PerDay limit reached")
    assert _classify_gemini_error(err_quota) == "exhausted_daily_quota"

    # 5. Authentication failure / 401
    err_auth = FakeAPIError(401, "UNAUTHENTICATED", "API key not valid")
    assert _classify_gemini_error(err_auth) == "auth_failure"

    # 6. Unknown failure
    err_unk = FakeAPIError(418, "TEAPOT", "I'm a teapot")
    assert _classify_gemini_error(err_unk) == "unknown"


@patch("src.scd2_copilot.providers.gemini.genai.Client")
def test_partial_batch_fallback_observable(mock_client_cls):
    """When a batch call has missing items, partial template fallback must be observable and truthful."""
    from app.ui_components import compute_ai_status

    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client

    # Batch response only returns record 0, record 1 is missing!
    mock_response = MagicMock()
    mock_response.parsed = [ExplanationItem(id=0, explanation="Explained record 0")]
    mock_response.usage_metadata = MagicMock(prompt_token_count=50, candidates_token_count=20, total_token_count=70)
    mock_client.models.generate_content.return_value = mock_response

    settings = Settings(
        _env_file=None,
        gemini_api_key="mock_key",
        llm_provider=LLMProvider.GEMINI,
        gemini_model="gemini-3.8-flash",
    )

    report = ChangeReport(
        new=[
            ChangeRecord({"id": 101}, ChangeType.NEW),
            ChangeRecord({"id": 102}, ChangeType.NEW),
        ],
        processing_date=date(2026, 6, 8),
    )

    result = explain_changes(report, settings=settings)

    # Assertions
    assert len(result.explanations) == 2
    assert result.fallback_count == 1
    assert result.provider_used == "gemini (partial template fallback)"
    assert result.explanations[0].provider == "gemini (gemini-3.8-flash)"
    assert result.explanations[1].provider == "template"
    assert any("fell back to template for 1 explanation(s)" in w for w in result.warnings)

    # compute_ai_status must evaluate to FALLBACK, not false SUCCESS
    ai_stat = compute_ai_status(result, provider_used="gemini")
    assert ai_stat["status"] == "FALLBACK"
    assert ai_stat["has_fallback"] is True


