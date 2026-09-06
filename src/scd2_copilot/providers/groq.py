"""Groq LLM provider for fallback explanation generation.

Uses the Groq Python SDK to call fast-inference models.
"""

from __future__ import annotations

import logging

import groq as groq_sdk

from ..models import ChangeRecord, ChangeType, Explanation, LLMMetrics
from .base import LLMProvider
from .template import TemplateProvider

logger = logging.getLogger(__name__)


class GroqProvider(LLMProvider):
    """Generates explanations using the Groq API."""

    MODEL = "llama-3.3-70b-versatile"

    def __init__(self, api_key: str) -> None:
        self._client = groq_sdk.Groq(api_key=api_key)

    @property
    def name(self) -> str:
        return "groq"

    def explain_change(self, record: ChangeRecord) -> Explanation:
        prompt = _build_prompt(record)

        try:
            response = self._client.chat.completions.create(
                model=self.MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a data engineering assistant explaining SCD2 "
                            "(Slowly Changing Dimension Type 2) changes. "
                            "Write clear, concise explanations for business users. "
                            "Do not use markdown."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=200,
                temperature=0.3,
            )
            text = response.choices[0].message.content.strip()

            # extract token counts
            usage = getattr(response, "usage", None)
            if usage:
                prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
                completion_tokens = getattr(usage, "completion_tokens", 0) or 0
                total_tokens = getattr(usage, "total_tokens", 0) or 0
                is_estimated = False
            else:
                prompt_tokens = max(1, len(prompt) // 4)
                completion_tokens = max(1, len(text) // 4)
                total_tokens = prompt_tokens + completion_tokens
                is_estimated = True

            cost = (prompt_tokens * 0.59 / 1_000_000) + (completion_tokens * 0.79 / 1_000_000)
            self.last_metrics = LLMMetrics(
                provider="groq",
                model=self.MODEL,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                estimated_cost=cost,
                is_estimated=is_estimated,
            )
        except Exception as e:
            logger.warning("Groq API call failed: %s", e)
            raise

        return Explanation(
            business_key_values=record.business_key_values,
            change_type=record.change_type,
            text=text,
            provider=self.name,
        )

    def explain_changes_batch(self, records: list[ChangeRecord]) -> list[Explanation]:
        """Generate human-readable explanations for a batch of change records using JSON mode."""
        if not records:
            return []

        prompt = _build_batch_prompt(records)
        prompt += (
            "\n\nReturn the output ONLY as a JSON object containing a key 'explanations' which is a list of objects. "
            "Each object must have 'id' (the integer Record ID) and 'explanation' (the clear, concise 1-2 sentence explanation of the changes). "
            "Example format:\n"
            "{\n"
            "  \"explanations\": [\n"
            "    {\"id\": 0, \"explanation\": \"...\"}\n"
            "  ]\n"
            "}"
        )

        try:
            response = self._client.chat.completions.create(
                model=self.MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a data engineering assistant explaining SCD2 "
                            "(Slowly Changing Dimension Type 2) changes. "
                            "Write clear, concise explanations for business users. "
                            "You must respond ONLY with a valid JSON object matching the requested schema."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=2000,
                temperature=0.3,
                response_format={"type": "json_object"}
            )
            content = response.choices[0].message.content.strip()
            import json
            data = json.loads(content)
            items = data.get("explanations", [])
            explanation_map = {item.get("id"): item.get("explanation") for item in items if "id" in item}

            # extract token counts
            usage = getattr(response, "usage", None)
            if usage:
                prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
                completion_tokens = getattr(usage, "completion_tokens", 0) or 0
                total_tokens = getattr(usage, "total_tokens", 0) or 0
                is_estimated = False
            else:
                prompt_tokens = max(1, len(prompt) // 4)
                completion_tokens = max(1, len(content) // 4)
                total_tokens = prompt_tokens + completion_tokens
                is_estimated = True

            cost = (prompt_tokens * 0.59 / 1_000_000) + (completion_tokens * 0.79 / 1_000_000)
            self.last_metrics = LLMMetrics(
                provider="groq",
                model=self.MODEL,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                estimated_cost=cost,
                is_estimated=is_estimated,
            )
        except Exception as e:
            logger.warning("Groq batch API call failed: %s", e)
            raise

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
                        provider=self.name,
                    )
                )
            else:
                explanations.append(template.explain_change(record))
        return explanations


def _build_prompt(record: ChangeRecord) -> str:
    """Build a prompt for the Groq model."""
    key_str = ", ".join(f"{k}={v}" for k, v in record.business_key_values.items())

    lines = [
        f"Record: {key_str}",
        f"Change type: {record.change_type.value}",
    ]

    if record.change_type == ChangeType.CHANGED and record.field_changes:
        lines.append("Field changes:")
        for fc in record.field_changes:
            lines.append(f"  - {fc.column}: '{fc.old_value}' → '{fc.new_value}'")

    lines.append("")
    lines.append("Explain this SCD2 change in one or two clear sentences.")

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
