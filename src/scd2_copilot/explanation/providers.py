"""Explanation providers for generating evidence-grounded batch operational explanations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
import json
import logging
import re
import time
from typing import Any, Optional

from ..config import DEFAULT_GEMINI_FALLBACK_MODELS, DEFAULT_GEMINI_MODEL
from .models import BatchExplanationResult, ExplanationContext
from .prompt import PROMPT_VERSION, SYSTEM_INSTRUCTIONS, build_explanation_prompt

logger = logging.getLogger("scd2_copilot.explanation.providers")


def _clean_json_response(raw_text: str) -> dict[str, Any]:
    """Strip markdown fencing and parse JSON from model output."""
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\n?", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\n?```$", "", cleaned)
    return json.loads(cleaned)


class BaseExplanationProvider(ABC):
    """Abstract base provider for batch operational explanations."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier."""
        ...

    @abstractmethod
    def generate_explanation(self, context: ExplanationContext) -> BatchExplanationResult:
        """Generate structured explanation from trusted evidence context."""
        ...


class DeterministicExplanationProvider(BaseExplanationProvider):
    """Zero-dependency, reproducible deterministic template provider.

    Always available, requires no external credentials, runs completely offline.
    """

    @property
    def name(self) -> str:
        return "deterministic"

    def generate_explanation(self, context: ExplanationContext) -> BatchExplanationResult:
        ev = context.evidence
        stream_name = context.source_name

        # 1. Summary
        if context.decision == "SUSPICIOUS":
            rule_names = ", ".join(r.rule_name for r in context.triggered_rules) or "operational boundaries"
            summary = (
                f"Batch for stream '{stream_name}' was held at the downstream boundary. "
                f"Deterministic guardrail evaluated {ev.evaluated_records} records with {ev.changed_records} "
                f"mutations and flagged severity {context.severity} due to: {rule_names}."
            )
        else:
            summary = (
                f"Batch for stream '{stream_name}' was successfully committed downstream. "
                f"All {ev.evaluated_records} records complied with deterministic operational boundaries."
            )

        # 2. What Changed — use generic dispersion labels when available
        dispersion_label = ""
        if ev.dispersion_key_column:
            dispersion_label = (
                f" Key dispersion: {ev.key_value_dispersion} distinct {ev.dispersion_key_column} value(s)."
            )
        elif ev.warehouses_affected > 0 or ev.skus_affected > 0:
            dispersion_label = (
                f" Across {ev.warehouses_affected} warehouse(s) and {ev.skus_affected} SKU(s)."
            )

        what_changed = (
            f"Observed {ev.evaluated_records} total evaluated records in batch window: "
            f"{ev.changed_records} changed, {ev.new_records} new, {ev.unchanged_records} unchanged. "
            f"Affected population ratio was {round(ev.affected_population_ratio * 100, 1)}%."
            f"{dispersion_label}"
        )

        # 3. Why Flagged
        if context.decision == "SUSPICIOUS":
            why_parts = []
            for r in context.triggered_rules:
                why_parts.append(f"{r.rule_name} ({r.rule_id}): {r.message}")
            why_flagged = "Triggered deterministic rules: " + "; ".join(why_parts)
        else:
            why_flagged = "No deterministic guardrail rules triggered. Change magnitude is within expected bounds."

        # 4. Evidence Points — include domain-specific metrics only when populated
        evidence_points = [
            f"Total records evaluated: {ev.evaluated_records} (changed: {ev.changed_records}, new: {ev.new_records})",
            f"Population mutation ratio: {round(ev.affected_population_ratio * 100, 1)}%",
        ]
        if ev.status_deactivations_count > 0:
            col_label = f" ({ev.categorical_column_analyzed})" if ev.categorical_column_analyzed else ""
            evidence_points.append(f"Categorical deactivations{col_label}: {ev.status_deactivations_count}")
        if ev.max_quantity_absolute_change > 0:
            col_label = f" ({ev.numeric_column_analyzed})" if ev.numeric_column_analyzed else ""
            evidence_points.append(
                f"Max numeric change{col_label}: {ev.max_quantity_absolute_change} units "
                f"(rel: {round(ev.max_quantity_relative_change * 100, 1)}%)"
            )
        if ev.per_column_change_counts:
            col_summary = ", ".join(f"{col}: {cnt}" for col, cnt in ev.per_column_change_counts.items())
            evidence_points.append(f"Per-column changes: {col_summary}")
        if ev.event_window_seconds > 0:
            evidence_points.append(
                f"Event velocity: {round(ev.velocity_changes_per_second, 2)} changes/sec over {round(ev.event_window_seconds, 1)}s window"
            )

        # 5. Validation Summary
        if context.validation_passed:
            validation_summary = (
                "SCD2 invariant validation passed completely. Temporal half-open intervals [effective_from, effective_to) "
                "and business key uniqueness constraints are valid."
            )
        else:
            failures_str = "; ".join(context.validation_failures) if context.validation_failures else "Invariant failure"
            validation_summary = f"SCD2 invariant validation failed: {failures_str}."

        # 6. Containment Summary
        if context.processing_status == "HELD":
            containment_summary = (
                "Batch held in quarantine (held_change_batch). Watermark was preserved at previous checkpoint (T0) "
                "to prevent downstream propagation. Target history remains unmodified pending operator resolution."
            )
        else:
            containment_summary = (
                "Batch processed and committed to target history. Watermark advanced to the batch timestamp."
            )

        return BatchExplanationResult(
            summary=summary,
            what_changed=what_changed,
            why_flagged=why_flagged,
            evidence_points=evidence_points,
            validation_summary=validation_summary,
            containment_summary=containment_summary,
            decision=context.decision,
            severity=context.severity,
            provider=self.name,
            model=None,
            explanation_version=PROMPT_VERSION,
            generated_at=datetime.now(timezone.utc),
            grounding_passed=True,
            is_fallback=True,
        )


class GeminiExplanationProvider(BaseExplanationProvider):
    """Gemini LLM explanation provider via Google GenAI SDK with model fallbacks."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        fallback_models: Optional[list[str]] = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.api_key = api_key
        self.primary_model = model or DEFAULT_GEMINI_MODEL
        self.fallback_models = fallback_models or list(DEFAULT_GEMINI_FALLBACK_MODELS)
        self.timeout_seconds = timeout_seconds

    @property
    def name(self) -> str:
        return "gemini"

    def _get_model_chain(self) -> list[str]:
        chain = [self.primary_model]
        for m in self.fallback_models:
            if m and m not in chain:
                chain.append(m)
        return chain

    def generate_explanation(self, context: ExplanationContext) -> BatchExplanationResult:
        if not self.api_key and not getattr(self, "_client", None):
            raise ValueError("Gemini API key is missing or unconfigured.")

        client = getattr(self, "_client", None)
        if client is None:
            from google import genai

            client = genai.Client(api_key=self.api_key)
        prompt = build_explanation_prompt(context)
        models_to_try = self._get_model_chain()

        last_error: Optional[Exception] = None
        for model_id in models_to_try:
            try:
                logger.info("Attempting Gemini explanation using model '%s'", model_id)
                response = client.models.generate_content(
                    model=model_id,
                    contents=prompt,
                    config={
                        "system_instruction": SYSTEM_INSTRUCTIONS,
                        "response_mime_type": "application/json",
                        "temperature": 0.2,
                    },
                )
                raw_text = response.text or ""
                parsed = _clean_json_response(raw_text)

                return BatchExplanationResult(
                    summary=str(parsed.get("summary", "")),
                    what_changed=str(parsed.get("what_changed", "")),
                    why_flagged=str(parsed.get("why_flagged", "")),
                    evidence_points=[str(p) for p in parsed.get("evidence_points", [])],
                    validation_summary=str(parsed.get("validation_summary", "")),
                    containment_summary=str(parsed.get("containment_summary", "")),
                    decision=context.decision,
                    severity=context.severity,
                    provider=self.name,
                    model=model_id,
                    explanation_version=PROMPT_VERSION,
                    generated_at=datetime.now(timezone.utc),
                    grounding_passed=True,
                    is_fallback=False,
                )

            except Exception as exc:
                last_error = exc
                logger.warning("Gemini model '%s' failed for explanation: %s", model_id, exc)
                # If auth error or daily quota exhausted, don't keep trying models
                msg = str(exc).lower()
                if "unauthenticated" in msg or "api key not valid" in msg or "perday" in msg:
                    break

        raise ValueError(f"All Gemini models failed. Last error: {last_error}") from last_error


class GroqExplanationProvider(BaseExplanationProvider):
    """Groq LLM explanation provider for fast inference fallback."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "openai/gpt-oss-120b",
        timeout_seconds: float = 10.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds

    @property
    def name(self) -> str:
        return "groq"

    def generate_explanation(self, context: ExplanationContext) -> BatchExplanationResult:
        if not self.api_key and not getattr(self, "_client", None):
            raise ValueError("Groq API key is missing or unconfigured.")

        client = getattr(self, "_client", None)
        if client is None:
            import groq as groq_sdk

            client = groq_sdk.Groq(api_key=self.api_key, timeout=self.timeout_seconds)
        prompt = build_explanation_prompt(context)

        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_INSTRUCTIONS},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
        )

        raw_text = response.choices[0].message.content or ""
        parsed = _clean_json_response(raw_text)

        return BatchExplanationResult(
            summary=str(parsed.get("summary", "")),
            what_changed=str(parsed.get("what_changed", "")),
            why_flagged=str(parsed.get("why_flagged", "")),
            evidence_points=[str(p) for p in parsed.get("evidence_points", [])],
            validation_summary=str(parsed.get("validation_summary", "")),
            containment_summary=str(parsed.get("containment_summary", "")),
            decision=context.decision,
            severity=context.severity,
            provider=self.name,
            model=self.model,
            explanation_version=PROMPT_VERSION,
            generated_at=datetime.now(timezone.utc),
            grounding_passed=True,
            is_fallback=False,
        )
