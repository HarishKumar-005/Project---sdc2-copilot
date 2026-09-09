"""Explanation orchestration: generate explanations for all detected changes.

Routes change records through the provider chain: primary → fallback → template.
Tracks fallback events so the UI can warn the user.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .config import LLMProvider as LLMProviderEnum, Settings
from .models import ChangeRecord, ChangeReport, Explanation, LLMMetrics
from .providers.base import LLMProvider
from .providers.gemini import GeminiProvider
from .providers.groq import GroqProvider
from .providers.template import TemplateProvider

logger = logging.getLogger(__name__)


@dataclass
class ExplainResult:
    """Result of explanation generation, including any warnings."""

    explanations: list[Explanation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    provider_used: str = "template"
    metrics: Optional[LLMMetrics] = None
    fallback_count: int = 0


def get_provider(settings: Settings) -> LLMProvider:
    """Create the appropriate LLM provider based on settings.

    Falls back through: configured provider → next available → template.
    """
    effective = settings.get_effective_provider()

    if effective == LLMProviderEnum.GEMINI:
        return GeminiProvider(
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
            fallback_models=settings.gemini_fallback_models,
        )
    elif effective == LLMProviderEnum.GROQ:
        return GroqProvider(api_key=settings.groq_api_key)
    else:
        return TemplateProvider()


def explain_changes(
    change_report: ChangeReport,
    settings: Optional[Settings] = None,
    provider: Optional[LLMProvider] = None,
) -> ExplainResult:
    """Generate explanations for all non-unchanged changes using batch processing.

    Uses a fallback chain: primary provider → alternative → template.
    Returns an ExplainResult with explanations and any fallback warnings.

    Args:
        change_report: The detected changes.
        settings: App settings (used if provider is not given).
        provider: Override provider instance (for testing).

    Returns:
        ExplainResult with explanations list and warnings list.
    """
    if provider is None:
        if settings is None:
            from .config import get_settings
            settings = get_settings()
        provider = get_provider(settings)

    template = TemplateProvider()
    result = ExplainResult(provider_used=provider.name)

    # Collect all records that need explanation
    records_to_explain: list[ChangeRecord] = (
        change_report.new + change_report.changed + change_report.deleted
    )

    if not records_to_explain:
        return result

    import time

    t_start = time.perf_counter()
    try:
        # Run in batch
        explanations = provider.explain_changes_batch(records_to_explain)
        duration = time.perf_counter() - t_start

        # Calculate fallback count
        fallback_count = 0
        if provider.name != "template":
            for exp in explanations:
                if exp.provider == "template":
                    fallback_count += 1

        result.explanations = explanations
        result.fallback_count = fallback_count

        if fallback_count > 0:
            if fallback_count == len(records_to_explain):
                result.provider_used = "template"
            else:
                result.provider_used = f"{provider.name} (partial template fallback)"

            result.warnings.append(
                f"⚠️ {provider.name.capitalize()} API fell back to template "
                f"for {fallback_count} explanation(s) due to missing items in the response."
            )

        # Populate metrics
        metrics = getattr(provider, "last_metrics", None)
        if metrics:
            metrics.request_duration = duration
            metrics.num_changes_explained = len(records_to_explain)
            metrics.avg_tokens_per_change = (metrics.total_tokens / len(records_to_explain)) if records_to_explain else 0.0
            result.metrics = metrics

        # If Gemini used a fallback model, note it in warnings
        if provider.name == "gemini":
            attempted = getattr(provider, "last_attempted_models", [])
            if len(attempted) > 1 and metrics:
                result.warnings.append(
                    f"⚠️ Gemini primary model failed. Successfully used fallback model '{metrics.model}'."
                )

        return result

    except Exception as e:
        duration = time.perf_counter() - t_start
        error_msg = f"{type(e).__name__}: {e}"
        logger.warning(
            "Batch provider '%s' failed: %s.",
            provider.name,
            e,
        )

        # Runtime fallback: Gemini -> Groq -> Template
        if provider.name == "gemini" and settings and settings.has_groq_key:
            try:
                logger.info("Attempting runtime fallback to Groq provider.")
                groq_provider = GroqProvider(api_key=settings.groq_api_key)
                t_groq_start = time.perf_counter()
                groq_explanations = groq_provider.explain_changes_batch(records_to_explain)
                groq_duration = time.perf_counter() - t_groq_start

                result.explanations = groq_explanations
                result.provider_used = "groq"
                groq_metrics = getattr(groq_provider, "last_metrics", None)
                if groq_metrics:
                    groq_metrics.request_duration = groq_duration
                    groq_metrics.num_changes_explained = len(records_to_explain)
                    groq_metrics.avg_tokens_per_change = (
                        groq_metrics.total_tokens / len(records_to_explain)
                    ) if records_to_explain else 0.0
                    result.metrics = groq_metrics

                result.warnings.append(
                    f"⚠️ Gemini API failed: {error_msg}. Fell back to Groq."
                )
                return result

            except Exception as groq_err:
                logger.warning("Groq fallback also failed: %s. Falling back to template.", groq_err)
                result.warnings.append(
                    f"⚠️ Gemini API failed: {error_msg}. Groq fallback failed: {type(groq_err).__name__}: {groq_err}. Fell back to template."
                )
        else:
            if provider.name != "template":
                result.warnings.append(
                    f"⚠️ {provider.name.capitalize()} API failed: {error_msg}. Fell back to template."
                )

        # Batch call failed: generate explanations with template
        result.explanations = [template.explain_change(r) for r in records_to_explain]
        result.provider_used = "template"

        result.metrics = LLMMetrics(
            provider="template",
            model="local-templates",
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            estimated_cost=0.0,
            request_duration=duration,
            num_changes_explained=len(records_to_explain),
            avg_tokens_per_change=0.0,
            is_estimated=False,
        )

        return result
