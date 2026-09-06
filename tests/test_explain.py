"""Tests for the explain module using the template provider."""

from datetime import date

import pytest

from src.scd2_copilot.models import ChangeRecord, ChangeReport, ChangeType, FieldChange
from src.scd2_copilot.providers.template import TemplateProvider
from src.scd2_copilot.explain import explain_changes


class TestTemplateProvider:
    """Test the deterministic template explanation provider."""

    @pytest.fixture
    def provider(self):
        return TemplateProvider()

    def test_new_record_explanation(self, provider):
        record = ChangeRecord(
            business_key_values={"customer_id": 104},
            change_type=ChangeType.NEW,
        )
        explanation = provider.explain_change(record)
        assert "New record" in explanation.text
        assert "customer_id=104" in explanation.text
        assert explanation.provider == "template"

    def test_changed_record_explanation(self, provider):
        record = ChangeRecord(
            business_key_values={"customer_id": 101},
            change_type=ChangeType.CHANGED,
            field_changes=[
                FieldChange(column="city", old_value="Chennai", new_value="Bengaluru")
            ],
        )
        explanation = provider.explain_change(record)
        assert "city" in explanation.text
        assert "Chennai" in explanation.text
        assert "Bengaluru" in explanation.text
        assert explanation.provider == "template"

    def test_deleted_record_explanation(self, provider):
        record = ChangeRecord(
            business_key_values={"customer_id": 105},
            change_type=ChangeType.DELETED,
        )
        explanation = provider.explain_change(record)
        assert "no longer present" in explanation.text or "soft delete" in explanation.text.lower()
        assert explanation.provider == "template"

    def test_unchanged_record_explanation(self, provider):
        record = ChangeRecord(
            business_key_values={"customer_id": 102},
            change_type=ChangeType.UNCHANGED,
        )
        explanation = provider.explain_change(record)
        assert "no changes" in explanation.text.lower()
        assert explanation.provider == "template"


class TestExplainChanges:
    """Test the explain_changes orchestration function."""

    def test_explain_skips_unchanged(self):
        """Only NEW, CHANGED, DELETED should get explanations."""
        report = ChangeReport(
            new=[ChangeRecord({"id": 1}, ChangeType.NEW)],
            changed=[ChangeRecord({"id": 2}, ChangeType.CHANGED, [FieldChange("x", "a", "b")])],
            unchanged=[ChangeRecord({"id": 3}, ChangeType.UNCHANGED)],
            deleted=[ChangeRecord({"id": 4}, ChangeType.DELETED)],
            processing_date=date(2026, 6, 8),
        )
        provider = TemplateProvider()
        result = explain_changes(report, provider=provider)

        # 3 explanations (new + changed + deleted), not 4
        assert len(result.explanations) == 3
        explained_ids = {list(e.business_key_values.values())[0] for e in result.explanations}
        assert explained_ids == {1, 2, 4}
        # No warnings when template is used directly
        assert len(result.warnings) == 0

    def test_explain_empty_report(self):
        """No changes → no explanations."""
        report = ChangeReport(processing_date=date(2026, 6, 8))
        provider = TemplateProvider()
        result = explain_changes(report, provider=provider)
        assert len(result.explanations) == 0
        assert len(result.warnings) == 0

    def test_explain_batch_fallback_on_failure(self):
        """If primary provider batch call fails, it should gracefully fall back to template."""
        from src.scd2_copilot.providers.base import LLMProvider

        class FailingProvider(LLMProvider):
            @property
            def name(self) -> str:
                return "failing_mock"
            def explain_change(self, record):
                raise RuntimeError("Should not be called individually")
            def explain_changes_batch(self, records):
                raise RuntimeError("Batch call failed")

        report = ChangeReport(
            new=[ChangeRecord({"id": 1}, ChangeType.NEW)],
            processing_date=date(2026, 6, 8),
        )
        provider = FailingProvider()
        result = explain_changes(report, provider=provider)

        # Should fall back to template
        assert len(result.explanations) == 1
        assert result.explanations[0].provider == "template"
        assert len(result.warnings) == 1
        assert "Fell back to template" in result.warnings[0]
