"""Source Onboarding view (backward compatibility alias to new_onboarding)."""

from __future__ import annotations

from .new_onboarding import render_new_onboarding_view
from ..state import OnboardingUIState


def render_source_onboarding_view(state: OnboardingUIState) -> None:
    """Render the customer source onboarding view."""
    render_new_onboarding_view(state)


__all__ = ["render_source_onboarding_view"]
