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

from ..models import ChangeRecord, ChangeType, Explanation, LLMMetrics
from .base import LLMProvider
from .template import TemplateProvider

logger = logging.getLogger(__name__)

# Model fallback chain: try each in order until one works.
# Each model has a separate daily free-tier quota, so if one is
# exhausted we can try the next.
MODEL_CHAIN = [
    "gemini-3.1-flash-lite",
    "gemini-3-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-3.5-flash",
]

MAX_RETRIES_PER_MODEL = 2
RETRY_BASE_DELAY = 3  # seconds


class ExplanationItem(BaseModel):
    """Pydantic model for a single structured explanation item."""

    id: int = Field(description="The Record ID (index) from the input list.")
    explanation: str = Field(
        description="The clear, concise 1-2 sentence business explanation of the change."
    )


class GeminiProvider(LLMProvider):
    """Generates explanations using the Gemini API with model fallback."""

    def __init__(self, api_key: str) -> None:
        self._client = genai.Client(api_key=api_key)
        self._working_model: str | None = None  # cache the first model that works

    @property
    def name(self) -> str:
        return "gemini"

    def explain_change(self, record: ChangeRecord) -> Explanation:
        prompt = _build_prompt(record)
        text, model_used = self._call_with_fallback(prompt)

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
            parsed_items, model_used = self._call_with_fallback(prompt, config=config)
        except Exception as e:
            logger.warning("Gemini batch API call failed: %s", e)
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

    def _call_with_fallback(
        self, prompt: str, config: dict | None = None
    ) -> tuple[Any, str]:
        """Try models in the fallback chain until one succeeds.

        Returns:
            Tuple of (response_text or parsed_object, model_name).

        Raises:
            Last exception if all models and retries fail.
        """
        # If we already found a working model, try it first
        models = (
            [self._working_model] if self._working_model
            else list(MODEL_CHAIN)
        )

        last_error: Exception | None = None

        for model in models:
            for attempt in range(1, MAX_RETRIES_PER_MODEL + 1):
                try:
                    response = self._client.models.generate_content(
                        model=model,
                        contents=prompt,
                        config=config,
                    )

                    # Extract text or parsed schema depending on configuration
                    if config and "response_schema" in config:
                        result = response.parsed
                    else:
                        result = response.text.strip() if response.text else ""

                    # Extract usage metadata
                    usage = getattr(response, "usage_metadata", None)
                    if usage:
                        prompt_tokens = getattr(usage, "prompt_token_count", 0) or 0
                        completion_tokens = getattr(usage, "candidates_token_count", 0) or 0
                        total_tokens = getattr(usage, "total_token_count", 0) or 0
                        is_estimated = False
                    else:
                        # Character-based estimation: 4 chars per token roughly
                        prompt_tokens = max(1, len(prompt) // 4)
                        completion_tokens = max(1, len(str(result)) // 4)
                        total_tokens = prompt_tokens + completion_tokens
                        is_estimated = True

                    # Estimate cost: Prompt: $0.075 / 1M, Completion: $0.30 / 1M
                    cost = (prompt_tokens * 0.075 / 1_000_000) + (completion_tokens * 0.30 / 1_000_000)

                    self.last_metrics = LLMMetrics(
                        provider="gemini",
                        model=model,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=total_tokens,
                        estimated_cost=cost,
                        is_estimated=is_estimated,
                    )

                    # Cache this model for future calls
                    self._working_model = model
                    return result, model

                except Exception as e:
                    last_error = e
                    err_str = str(e)

                    if "429" in err_str and "PerDay" in err_str:
                        # Daily quota exhausted — no point retrying this model
                        logger.info(
                            "Model %s daily quota exhausted, trying next model.",
                            model,
                        )
                        break  # move to next model

                    if "404" in err_str:
                        # Model not found — skip entirely
                        logger.info("Model %s not found, trying next.", model)
                        break

                    if "429" in err_str:
                        # Per-minute rate limit — retry with backoff
                        delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                        logger.info(
                            "Model %s rate-limited (attempt %d/%d), "
                            "retrying in %ds...",
                            model, attempt, MAX_RETRIES_PER_MODEL, delay,
                        )
                        time.sleep(delay)
                        continue

                    # Unknown error — don't retry, try next model
                    logger.warning(
                        "Model %s failed with %s: %s",
                        model, type(e).__name__, str(e)[:200],
                    )
                    break

        # If cached model failed, try the full chain
        if self._working_model:
            self._working_model = None
            return self._call_with_fallback(prompt, config=config)

        # All models exhausted
        raise last_error or RuntimeError("All Gemini models failed.")


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
