"""Evidence-grounded AI explanation subsystem for SCD2 Copilot V2."""

from __future__ import annotations

from .grounding import validate_explanation_grounding
from .models import (
    BatchExplanationResult,
    ExplanationContext,
    GroundingViolation,
)
from .prompt import PROMPT_VERSION, build_explanation_prompt
from .providers import (
    BaseExplanationProvider,
    DeterministicExplanationProvider,
    GeminiExplanationProvider,
    GroqExplanationProvider,
)
from .service import ExplanationService

__all__ = [
    "ExplanationContext",
    "BatchExplanationResult",
    "GroundingViolation",
    "build_explanation_prompt",
    "PROMPT_VERSION",
    "validate_explanation_grounding",
    "BaseExplanationProvider",
    "DeterministicExplanationProvider",
    "GeminiExplanationProvider",
    "GroqExplanationProvider",
    "ExplanationService",
]
