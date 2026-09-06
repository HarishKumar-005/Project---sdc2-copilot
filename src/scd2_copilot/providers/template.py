"""Deterministic template-based explanation provider.

Always available, requires no API key. Generates structured English
sentences from the change record data.
"""

from __future__ import annotations

from ..models import ChangeRecord, ChangeType, Explanation, LLMMetrics
from .base import LLMProvider


class TemplateProvider(LLMProvider):
    """Generates explanations using deterministic string templates."""

    @property
    def name(self) -> str:
        return "template"

    def explain_change(self, record: ChangeRecord) -> Explanation:
        self.last_metrics = LLMMetrics(
            provider="template",
            model="local-templates",
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            estimated_cost=0.0,
            is_estimated=False,
        )
        key_str = ", ".join(f"{k}={v}" for k, v in record.business_key_values.items())

        if record.change_type == ChangeType.NEW:
            text = f"New record added for {key_str}."

        elif record.change_type == ChangeType.CHANGED:
            changes = []
            for fc in record.field_changes:
                changes.append(f"{fc.column} changed from '{fc.old_value}' to '{fc.new_value}'")
            changes_str = "; ".join(changes)
            text = (
                f"Record {key_str} was updated: {changes_str}. "
                f"The previous version was closed and a new current record was created."
            )

        elif record.change_type == ChangeType.DELETED:
            text = (
                f"Record {key_str} is no longer present in the source. "
                f"The current record was closed (soft delete)."
            )

        elif record.change_type == ChangeType.UNCHANGED:
            text = f"Record {key_str} has no changes. Carried forward as-is."

        else:
            text = f"Record {key_str}: unknown change type '{record.change_type}'."

        return Explanation(
            business_key_values=record.business_key_values,
            change_type=record.change_type,
            text=text,
            provider=self.name,
        )

    def explain_changes_batch(self, records: list[ChangeRecord]) -> list[Explanation]:
        self.last_metrics = LLMMetrics(
            provider="template",
            model="local-templates",
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            estimated_cost=0.0,
            is_estimated=False,
        )
        return [self.explain_change(r) for r in records]
