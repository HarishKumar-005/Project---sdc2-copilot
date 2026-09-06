"""Abstract base for LLM explanation providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..models import ChangeRecord, Explanation, LLMMetrics


class LLMProvider(ABC):
    """Protocol for LLM explanation providers.

    Every provider must implement ``explain_change`` which takes a
    ChangeRecord and returns a human-readable Explanation.
    """

    def __init__(self) -> None:
        self.last_metrics: Optional[LLMMetrics] = None

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for this provider (e.g., 'gemini', 'groq', 'template')."""
        ...

    @abstractmethod
    def explain_change(self, record: ChangeRecord) -> Explanation:
        """Generate a human-readable explanation for a single change.

        Args:
            record: The detected change record.

        Returns:
            An Explanation with the human-readable text.
        """
        ...

    def explain_changes_batch(self, records: list[ChangeRecord]) -> list[Explanation]:
        """Generate human-readable explanations for a batch of change records.

        Default implementation falls back to calling explain_change sequentially.
        Providers should override this method to perform efficient batch API calls.

        Args:
            records: The list of detected change records.

        Returns:
            A list of Explanations.
        """
        return [self.explain_change(r) for r in records]
