"""Gemini LLM provider via google-genai SDK.

Uses the new unified ``google.genai`` SDK (v2.x) to call Gemini models
for generating human-readable change explanations.

Includes a model fallback chain and retry logic for transient errors.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from google import genai
from pydantic import BaseModel, Field

from ..config import DEFAULT_GEMINI_FALLBACK_MODELS, DEFAULT_GEMINI_MODEL
from ..models import ChangeRecord, ChangeType, Explanation, LLMMetrics
from .base import LLMProvider
from .template import TemplateProvider

logger = logging.getLogger(__name__)

MAX_RETRIES = 2  # 1 initial attempt + 2 retries = 3 total attempts
RETRY_BASE_DELAY = 3  # seconds

# Model-aware pricing registry: model_name -> (prompt_price_per_1m, completion_price_per_1m) in USD
# Based on current official Google Gemini API pricing documentation:
# - gemini-3.8-flash: $0.75/1M input, $3.75/1M output (introductory through Dec 31, 2026; $1.50/$7.50 from Jan 1, 2027)
# - gemini-3.5-flash-lite: $0.30/1M input, $2.50/1M output
# - gemini-3.1-flash-lite: $0.25/1M input, $1.50/1M output
# - gemini-2.5-flash: $0.30/1M input, $2.50/1M output
# - gemini-2.0-flash: $0.10/1M input, $0.40/1M output
# - gemini-1.5-flash: $0.075/1M input, $0.30/1M output
GEMINI_MODEL_PRICING: dict[str, tuple[float, float]] = {
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-1.5-flash": (0.075, 0.30),
}


def calculate_gemini_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    token_count_estimated: bool = False,
) -> tuple[float, bool]:
    """Calculate the estimated cost in USD for a given Gemini model.

    Returns:
        tuple[float, bool]: (estimated_cost, is_estimated)
        If official current pricing cannot be safely determined for a model,
        marks cost as estimated (0.0, True) rather than inventing a value.
    """
    from datetime import date

    pricing = GEMINI_MODEL_PRICING.get(model)
    if pricing is not None:
        input_rate, output_rate = pricing
        # For gemini-3.8-flash: introductory rates ($0.75/$3.75) through Dec 31, 2026;
        # standard pricing ($1.50/$7.50) from Jan 1, 2027 onwards.
        if model == "gemini-3.8-flash" and date.today() >= date(2027, 1, 1):
            input_rate, output_rate = 1.50, 7.50

        cost = (prompt_tokens * input_rate / 1_000_000) + (
            completion_tokens * output_rate / 1_000_000
        )
        return cost, token_count_estimated
    return 0.0, True


def _classify_gemini_error(e: Exception) -> str:
    """Classify an exception from Google GenAI SDK into a structured category.

    Inspects structured SDK exception properties (code, status, ServerError, ClientError)
    first, and falls back to string parsing only for backward-compatibility with unstructured exceptions.

    Categories:
    - "transient_rate_limit": Per-minute rate limits (HTTP 429 without daily quota exhaustion).
    - "transient_server": Server-side transient issues (HTTP 5xx, ServerError, UNAVAILABLE).
    - "exhausted_daily_quota": Daily quota exhausted (HTTP 429 with PerDay quota indicator).
    - "invalid_model": Model not found or unsupported endpoint (HTTP 404, NOT_FOUND).
    - "auth_failure": Authentication or permission errors (HTTP 401, 403, UNAUTHENTICATED, PERMISSION_DENIED).
    - "unknown": Any other unclassified error.
    """
    code = getattr(e, "code", None)
    if code is None:
        code = getattr(e, "status_code", None)
    status = getattr(e, "status", None)
    msg = str(getattr(e, "message", None) or str(e))
    msg_lower = msg.lower()

    # 1. Authentication / Permission failure
    if code in (401, 403) or status in ("UNAUTHENTICATED", "PERMISSION_DENIED"):
        return "auth_failure"
    if "unauthenticated" in msg_lower or "permission_denied" in msg_lower or "api key not valid" in msg_lower:
        return "auth_failure"

    # 2. Model not found / Unsupported model
    if code == 404 or status == "NOT_FOUND":
        return "invalid_model"
    if "not found" in msg_lower or "unsupported" in msg_lower:
        return "invalid_model"

    # 3. Rate limits & Quotas (429 / RESOURCE_EXHAUSTED)
    if code == 429 or status == "RESOURCE_EXHAUSTED" or "429" in msg or "resourceexhausted" in msg_lower:
        if "perday" in msg_lower or "daily" in msg_lower:
            return "exhausted_daily_quota"
        return "transient_rate_limit"

    # 4. Transient Server errors (5xx / ServerError)
    if (isinstance(code, int) and 500 <= code < 600) or status in ("INTERNAL", "UNAVAILABLE", "DEADLINE_EXCEEDED"):
        return "transient_server"
    if any(s in msg for s in ["500", "502", "503", "504", "ServerError", "InternalServerError", "ServiceUnavailable"]):
        return "transient_server"

    return "unknown"


class ExplanationItem(BaseModel):
    """Pydantic model for a single structured explanation item."""

    id: int = Field(description="The Record ID (index) from the input list.")
    explanation: str = Field(
        description="The clear, concise 1-2 sentence business explanation of the change."
    )


class GeminiProvider(LLMProvider):
    """Generates explanations using the Gemini API with configuration-driven model fallback."""

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        fallback_models: list[str] | None = None,
    ) -> None:
        clean_model = (model or "").strip()
        self.primary_model = clean_model if clean_model else DEFAULT_GEMINI_MODEL
        self.model = self.primary_model  # for backward compatibility

        if fallback_models is not None:
            cleaned = [str(m).strip() for m in fallback_models if str(m).strip()]
            deduped: list[str] = []
            seen = {self.primary_model}
            for m in cleaned:
                if m not in seen:
                    deduped.append(m)
                    seen.add(m)
            self.fallback_models = deduped
        else:
            self.fallback_models = list(DEFAULT_GEMINI_FALLBACK_MODELS)

        self._client = genai.Client(api_key=api_key)
        self.last_attempted_models: list[str] = []
        self.last_metrics: LLMMetrics | None = None

    @property
    def model_chain(self) -> list[str]:
        """Ordered list of Gemini models to attempt (primary followed by unique fallbacks)."""
        return [self.primary_model] + self.fallback_models

    @property
    def name(self) -> str:
        return "gemini"

    def explain_change(self, record: ChangeRecord) -> Explanation:
        prompt = _build_prompt(record)
        text, model_used = self._call_model(prompt)

        return Explanation(
            business_key_values=record.business_key_values,
            change_type=record.change_type,
            text=text,
            provider=f"gemini ({model_used})",
        )

    def explain_changes_batch(self, records: list[ChangeRecord]) -> list[Explanation]:
        """Generate human-readable explanations for a batch of change records using structured output."""
        if not records:
            return []

        prompt = _build_batch_prompt(records)

        # Setup structured output config using Pydantic model list
        config = {
            "response_mime_type": "application/json",
            "response_schema": list[ExplanationItem],
        }

        try:
            parsed_items, model_used = self._call_model(prompt, config=config)
        except Exception as e:
            logger.warning("Gemini batch API call failed across all models: %s", e)
            raise

        # Map parsed results back to input records
        explanation_map = {}
        if isinstance(parsed_items, list):
            for item in parsed_items:
                if isinstance(item, ExplanationItem):
                    explanation_map[item.id] = item.explanation
                elif isinstance(item, dict):
                    explanation_map[item.get("id")] = item.get("explanation")

        explanations = []
        template = TemplateProvider()
        for idx, record in enumerate(records):
            text = explanation_map.get(idx)
            if text:
                explanations.append(
                    Explanation(
                        business_key_values=record.business_key_values,
                        change_type=record.change_type,
                        text=text,
                        provider=f"gemini ({model_used})",
                    )
                )
            else:
                # Fallback to local template explanation for missing items in the parsed response
                explanations.append(template.explain_change(record))

        return explanations

    def _call_model(
        self, prompt: str, config: dict | None = None
    ) -> tuple[Any, str]:
        """Call Gemini models in sequence with per-model retry for transient errors.

        Returns:
            Tuple of (response_text_or_parsed_object, successful_model_name).

        Raises:
            Exception if all models in chain fail.
        """
        attempted_models: list[str] = []
        last_error: Exception | None = None

        # Explicitly disable automatic function calling (AFC) to prevent google-genai
        # from logging spurious AFC warnings when generating plain/structured content without tools.
        call_config: dict[str, Any] = dict(config) if config else {}
        if "automatic_function_calling" not in call_config and "tools" not in call_config:
            call_config["automatic_function_calling"] = {"disable": True}

        for model in self.model_chain:
            attempted_models.append(model)
            for attempt in range(MAX_RETRIES + 1):  # 1 initial + 2 retries = 3 total attempts
                try:
                    response = self._client.models.generate_content(
                        model=model,
                        contents=prompt,
                        config=call_config,
                    )

                    # Extract text or parsed schema depending on configuration
                    if "response_schema" in call_config:
                        result = response.parsed
                    else:
                        result = response.text.strip() if response.text else ""

                    # Extract usage metadata
                    usage = getattr(response, "usage_metadata", None)
                    if usage:
                        prompt_tokens = getattr(usage, "prompt_token_count", 0) or 0
                        completion_tokens = getattr(usage, "candidates_token_count", 0) or 0
                        total_tokens = getattr(usage, "total_token_count", 0) or 0
                        token_count_estimated = False
                    else:
                        # Character-based estimation: 4 chars per token roughly
                        prompt_tokens = max(1, len(prompt) // 4)
                        completion_tokens = max(1, len(str(result)) // 4)
                        total_tokens = prompt_tokens + completion_tokens
                        token_count_estimated = True

                    cost, is_estimated = calculate_gemini_cost(
                        model=model,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        token_count_estimated=token_count_estimated,
                    )

                    self.last_metrics = LLMMetrics(
                        provider="gemini",
                        model=model,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=total_tokens,
                        estimated_cost=cost,
                        is_estimated=is_estimated,
                    )
                    self.last_attempted_models = attempted_models

                    return result, model

                except Exception as e:
                    last_error = e
                    category = _classify_gemini_error(e)

                    # Handle transient recoverable errors
                    if category in ("transient_rate_limit", "transient_server"):
                        if attempt < MAX_RETRIES:
                            delay = RETRY_BASE_DELAY * (2 ** attempt)
                            logger.info(
                                "Model %s %s (attempt %d/%d), retrying in %ds...",
                                model,
                                category,
                                attempt + 1,
                                MAX_RETRIES + 1,
                                delay,
                            )
                            time.sleep(delay)
                            continue
                        else:
                            # Retries exhausted for this model: DO NOT sleep after the final attempt!
                            logger.info(
                                "Model %s exhausted %d retries for %s. Advancing to next model.",
                                model,
                                MAX_RETRIES,
                                category,
                            )
                            break

                    # Non-recoverable errors (exhausted_daily_quota, invalid_model, auth_failure, unknown)
                    logger.info(
                        "Model %s failed with non-recoverable %s (%s). Advancing to next model.",
                        model,
                        category,
                        type(e).__name__,
                    )
                    break

        self.last_attempted_models = attempted_models
        raise last_error or RuntimeError(f"All Gemini models failed: {attempted_models}")

    def _call_with_fallback(
        self, prompt: str, config: dict | None = None
    ) -> tuple[Any, str]:
        """Backward-compatible wrapper returning (result, model_name)."""
        return self._call_model(prompt, config=config)


def _build_prompt(record: ChangeRecord) -> str:
    """Build a structured prompt for the LLM from a ChangeRecord."""
    key_str = ", ".join(f"{k}={v}" for k, v in record.business_key_values.items())

    lines = [
        "You are a data engineering assistant explaining SCD2 (Slowly Changing Dimension Type 2) changes.",
        "Explain the following change in one or two clear sentences for a business user.",
        "",
        f"Record: {key_str}",
        f"Change type: {record.change_type.value}",
    ]

    if record.change_type == ChangeType.CHANGED and record.field_changes:
        lines.append("Field changes:")
        for fc in record.field_changes:
            lines.append(f"  - {fc.column}: '{fc.old_value}' → '{fc.new_value}'")

    lines.append("")
    lines.append("Write a clear, concise explanation. Do not use markdown.")

    return "\n".join(lines)


def _build_batch_prompt(records: list[ChangeRecord]) -> str:
    """Build a structured prompt for explaining a batch of changes."""
    lines = [
        "You are a data engineering assistant explaining SCD2 (Slowly Changing Dimension Type 2) changes.",
        "For each of the input records, generate a clear, concise explanation of the SCD2 changes in one or two sentences for a business user.",
        "Do not use markdown.",
        "",
        "Input records to explain:",
    ]
    for idx, record in enumerate(records):
        key_str = ", ".join(f"{k}={v}" for k, v in record.business_key_values.items())
        lines.append(f"--- Record ID: {idx} ---")
        lines.append(f"Business key: {key_str}")
        lines.append(f"Change type: {record.change_type.value}")
        if record.change_type == ChangeType.CHANGED and record.field_changes:
            lines.append("Field changes:")
            for fc in record.field_changes:
                lines.append(f"  - {fc.column}: '{fc.old_value}' → '{fc.new_value}'")
        lines.append("")

    return "\n".join(lines)
