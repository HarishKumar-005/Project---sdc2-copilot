"""Explanation service orchestrating provider fallback, grounding checks, and persistence."""

from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import UUID

import psycopg

from ..config import Settings, get_settings
from ..db.connection import DatabaseManager
from ..db.repositories import HeldChangeBatchRepository
from .grounding import validate_explanation_grounding
from .models import BatchExplanationResult, ExplanationContext
from .providers import (
    BaseExplanationProvider,
    DeterministicExplanationProvider,
    GeminiExplanationProvider,
    GroqExplanationProvider,
)

logger = logging.getLogger("scd2_copilot.explanation")


_UNSET: Any = object()


class ExplanationService:
    """Orchestrates evidence-grounded operational explanations.

    Guarantees:
    1. AI never modifies decision or severity.
    2. Grounding validator detects hallucinated rules or contradicted status.
    3. Seamless fallback: Gemini -> Groq -> Deterministic Template.
    4. Deterministic template operates 100% offline without credentials.
    5. Persistence is purely additive in held_change_batch JSONB evidence.
    6. Explanation failure never breaks containment, rollback, or history.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        db_manager: Optional[DatabaseManager] = None,
        hold_repo: Optional[HeldChangeBatchRepository] = None,
        gemini_provider: Optional[BaseExplanationProvider] | Any = _UNSET,
        groq_provider: Optional[BaseExplanationProvider] | Any = _UNSET,
        template_provider: Optional[BaseExplanationProvider] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.db = db_manager or DatabaseManager(settings=self.settings)
        self.hold_repo = hold_repo or HeldChangeBatchRepository(db=self.db)

        # Provider initialization
        self.template_provider = template_provider or DeterministicExplanationProvider()

        if gemini_provider is not _UNSET:
            self.gemini_provider = gemini_provider
        elif self.settings.has_gemini_key:
            self.gemini_provider = GeminiExplanationProvider(
                api_key=self.settings.gemini_api_key,
                model=self.settings.gemini_model,
                fallback_models=self.settings.gemini_fallback_models,
                timeout_seconds=self.settings.ai_explanation_timeout_seconds,
            )
        else:
            self.gemini_provider = None

        if groq_provider is not _UNSET:
            self.groq_provider = groq_provider
        elif self.settings.has_groq_key:
            self.groq_provider = GroqExplanationProvider(
                api_key=self.settings.groq_api_key,
                timeout_seconds=self.settings.ai_explanation_timeout_seconds,
            )
        else:
            self.groq_provider = None

    def explain_batch(self, context: ExplanationContext) -> BatchExplanationResult:
        """Generate structured operational explanation through the provider fallback chain.

        Evaluates grounding checks before accepting remote LLM output.
        Falls back to deterministic template on provider error or ungrounded claims.
        """
        # If AI explanation disabled entirely, use deterministic generator
        if not self.settings.ai_explanation_enabled:
            return self.template_provider.generate_explanation(context)

        # To conserve free-tier API quota, normal batches use deterministic explanation by default
        if context.decision == "NORMAL" and not self.settings.ai_explanation_for_normal_batches:
            return self.template_provider.generate_explanation(context)

        accumulated_warnings: list[str] = []

        # 1. Primary: Gemini
        if self.gemini_provider:
            try:
                res = self.gemini_provider.generate_explanation(context)
                is_grounded, violations = validate_explanation_grounding(res, context)
                if is_grounded:
                    logger.info("Gemini generated grounded explanation for stream '%s'", context.source_name)
                    return res
                else:
                    v_msgs = [f"{v.rule_name}: {v.description}" for v in violations]
                    warn_msg = f"Gemini output rejected due to grounding failure: {'; '.join(v_msgs)}"
                    logger.warning(warn_msg)
                    accumulated_warnings.append(warn_msg)
            except Exception as exc:
                warn_msg = f"Gemini explanation provider failed: {type(exc).__name__}: {exc}"
                logger.warning(warn_msg)
                accumulated_warnings.append(warn_msg)

        # 2. Secondary: Groq
        if self.groq_provider:
            try:
                logger.info("Attempting Groq fallback for explanation on stream '%s'", context.source_name)
                res = self.groq_provider.generate_explanation(context)
                is_grounded, violations = validate_explanation_grounding(res, context)
                if is_grounded:
                    res.warnings.extend(accumulated_warnings)
                    return res
                else:
                    v_msgs = [f"{v.rule_name}: {v.description}" for v in violations]
                    warn_msg = f"Groq output rejected due to grounding failure: {'; '.join(v_msgs)}"
                    logger.warning(warn_msg)
                    accumulated_warnings.append(warn_msg)
            except Exception as exc:
                warn_msg = f"Groq explanation provider failed: {type(exc).__name__}: {exc}"
                logger.warning(warn_msg)
                accumulated_warnings.append(warn_msg)

        # 3. Deterministic Template Fallback
        fallback_res = self.template_provider.generate_explanation(context)
        fallback_res.warnings.extend(accumulated_warnings)
        return fallback_res

    def explain_held_batch(
        self,
        hold_id: UUID,
        persist: bool = True,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> BatchExplanationResult:
        """Fetch held batch from database, generate explanation, and additively persist it."""
        held_row = self.hold_repo.get_hold(hold_id, conn=conn)
        if held_row is None:
            raise ValueError(f"Held change batch '{hold_id}' not found.")

        context = ExplanationContext.from_held_batch(held_row)
        explanation = self.explain_batch(context)

        if persist:
            try:
                self.hold_repo.attach_explanation(
                    hold_id=hold_id,
                    explanation=explanation.to_dict(),
                    conn=conn,
                )
                logger.info("Attached explanation to held batch %s in database.", hold_id)
            except Exception as exc:
                logger.error("Failed to persist explanation to held batch %s: %s", hold_id, exc)

        return explanation
