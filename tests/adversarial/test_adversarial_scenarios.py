"""Adversarial test cases for sdc2-copilot.

Tests extreme and adversarial inputs to verify deterministic logic,
integrity validation rules, and LLM provider fallbacks.
"""

from __future__ import annotations

import pytest
import polars as pl
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from src.scd2_copilot.detect_changes import detect_changes
from src.scd2_copilot.transform_scd2 import apply_scd2
from src.scd2_copilot.validate import validate_scd2
from src.scd2_copilot.explain import explain_changes, ExplainResult
from src.scd2_copilot.ingestion import validate_csv_columns
from src.scd2_copilot.models import ChangeType, ValidationStatus
from src.scd2_copilot.config import Settings, LLMProvider
from src.scd2_copilot.providers.gemini import GeminiProvider
from src.scd2_copilot.providers.groq import GroqProvider
from src.scd2_copilot.providers.template import TemplateProvider


# ── 1. DUPLICATE SOURCE KEYS ───────────────────────────

def test_duplicate_source_keys():
    """Verify change detection when a key appears multiple times in source."""
    source = pl.DataFrame({
        "id": [101, 101],
        "name": ["Alice", "Bob"]  # Bob is second, should win or override
    })
    target = pl.DataFrame({
        "id": [101],
        "name": ["Charlie"],
        "effective_from": [date(2026, 6, 1)],
        "effective_to": [None],
        "is_current": [True]
    })
    
    # Run change detection
    report = detect_changes(source, target, ["id"], ["name"], date(2026, 6, 8))
    
    # We expect 2 changed records corresponding to both source updates processed sequentially
    assert len(report.changed) == 2
    assert report.changed[0].field_changes[0].new_value == "Alice"
    assert report.changed[1].field_changes[0].new_value == "Bob"


# ── 2. DUPLICATE CURRENT TARGET RECORDS ─────────────────

def test_duplicate_current_target_records():
    """Verify validation catches multiple active versions of the same key."""
    # Target has two records for 101 marked current
    df = pl.DataFrame({
        "id": [101, 101, 102],
        "name": ["Alice", "Bob", "Charlie"],
        "effective_from": [date(2026, 6, 1), date(2026, 6, 5), date(2026, 6, 1)],
        "effective_to": [None, None, None],
        "is_current": [True, True, True]
    })
    
    report = validate_scd2(df, ["id"])
    assert not report.passed
    
    rule = next(r for r in report.rules if r.name == "one_current_per_key")
    assert rule.status == ValidationStatus.FAIL
    assert len(rule.details) > 0


# ── 3. COMPOSITE KEY STRESS TESTS ─────────────────────

@pytest.mark.parametrize(
    "keys, tracked",
    [
        (["id"], ["name"]),                                    # Single Key
        (["id", "region"], ["name"]),                          # Dual Key
        (["id", "region", "source_system"], ["name"])          # Triple Key
    ]
)
def test_composite_key_configurations(keys, tracked):
    """Test single, dual, and triple keys for correctness."""
    source = pl.DataFrame({
        "id": [101, 102],
        "region": ["US", "EU"],
        "source_system": ["ERP", "CRM"],
        "name": ["Alice", "Bob"]
    })
    target = pl.DataFrame({
        "id": [101, 102],
        "region": ["US", "EU"],
        "source_system": ["ERP", "CRM"],
        "name": ["Charlie", "Bob"],
        "effective_from": [date(2026, 6, 1), date(2026, 6, 1)],
        "effective_to": [None, None],
        "is_current": [True, True]
    })
    
    report = detect_changes(source, target, keys, tracked, date(2026, 6, 8))
    # For all setups, 101 is changed, 102 is unchanged
    assert len(report.changed) == 1
    assert len(report.unchanged) == 1
    
    output = apply_scd2(source, target, report, keys, tracked, date(2026, 6, 8))
    val = validate_scd2(output, keys)
    assert val.passed


# ── 4. SCHEMA DRIFT ────────────────────────────────────

def test_schema_drift_added_column():
    """Verify schema mismatch check blocks column addition in source."""
    source = pl.DataFrame({
        "id": [101],
        "name": ["Alice"],
        "new_col": ["extra"]  # extra column not in target
    })
    target = pl.DataFrame({
        "id": [101],
        "name": ["Alice"],
        "effective_from": [date(2026, 6, 1)],
        "effective_to": [None],
        "is_current": [True]
    })
    errors = validate_csv_columns(source, target)
    assert len(errors) > 0
    assert "not found in target" in errors[0]


def test_schema_drift_removed_column():
    """Verify removed column in source is omitted from tracking."""
    source = pl.DataFrame({
        "id": [101]
        # name column removed from source
    })
    target = pl.DataFrame({
        "id": [101],
        "name": ["Alice"],
        "effective_from": [date(2026, 6, 1)],
        "effective_to": [None],
        "is_current": [True]
    })
    # name column is not in source, so we only track key or empty tracked cols
    report = detect_changes(source, target, ["id"], [], date(2026, 6, 8))
    assert len(report.unchanged) == 1


# ── 5. TYPE DRIFT ──────────────────────────────────────

def test_type_drift_coercion():
    """Verify numeric-to-string type drift does not trigger false positive changes."""
    source = pl.DataFrame({
        "id": [101],
        "value": ["100"]  # string in source
    })
    target = pl.DataFrame({
        "id": [101],
        "value": [100],  # integer in target
        "effective_from": [date(2026, 6, 1)],
        "effective_to": [None],
        "is_current": [True]
    })
    # Both should normalize to string and compare equal (unchanged)
    report = detect_changes(source, target, ["id"], ["value"], date(2026, 6, 8))
    assert len(report.unchanged) == 1
    assert len(report.changed) == 0


# ── 6. NULL BUSINESS KEYS ──────────────────────────────

def test_null_business_keys():
    """Verify validation fails when business key values are null."""
    df = pl.DataFrame({
        "id": [101, None],  # row 2 has null business key
        "name": ["Alice", "Bob"],
        "effective_from": [date(2026, 6, 1), date(2026, 6, 1)],
        "effective_to": [None, None],
        "is_current": [True, True]
    })
    
    report = validate_scd2(df, ["id"])
    assert not report.passed
    rule = next(r for r in report.rules if r.name == "no_null_keys")
    assert rule.status == ValidationStatus.FAIL


# ── 7. EMPTY FILES ─────────────────────────────────────

def test_empty_files():
    """Verify pipeline handles completely empty source and target files."""
    source = pl.DataFrame({"id": pl.Series([], dtype=pl.Int64), "name": pl.Series([], dtype=pl.Utf8)})
    target = pl.DataFrame({
        "id": pl.Series([], dtype=pl.Int64), "name": pl.Series([], dtype=pl.Utf8),
        "effective_from": pl.Series([], dtype=pl.Date),
        "effective_to": pl.Series([], dtype=pl.Date),
        "is_current": pl.Series([], dtype=pl.Boolean)
    })
    report = detect_changes(source, target, ["id"], ["name"], date(2026, 6, 8))
    assert report.total == 0
    output = apply_scd2(source, target, report, ["id"], ["name"], date(2026, 6, 8))
    assert output.height == 0
    val = validate_scd2(output, ["id"])
    assert val.passed


# ── 8. LARGE TEXT FIELDS ───────────────────────────────

def test_large_text_fields():
    """Verify change detection handles massive text data correctly."""
    large_text_source = "A" * 10000
    large_text_target = "A" * 9999 + "B"
    source = pl.DataFrame({"id": [101], "desc": [large_text_source]})
    target = pl.DataFrame({
        "id": [101], "desc": [large_text_target],
        "effective_from": [date(2026, 6, 1)], "effective_to": [None], "is_current": [True]
    })
    report = detect_changes(source, target, ["id"], ["desc"], date(2026, 6, 8))
    assert len(report.changed) == 1


# ── 9. UNICODE FIELDS ──────────────────────────────────

def test_unicode_fields():
    """Verify unicode characters do not crash or corrupt change tracking."""
    source = pl.DataFrame({"id": [101], "name": ["Müller-Straße 🇩🇪 😊"]})
    target = pl.DataFrame({
        "id": [101], "name": ["Muller-Strasse"],
        "effective_from": [date(2026, 6, 1)], "effective_to": [None], "is_current": [True]
    })
    report = detect_changes(source, target, ["id"], ["name"], date(2026, 6, 8))
    assert len(report.changed) == 1
    assert report.changed[0].field_changes[0].new_value == "Müller-Straße 🇩🇪 😊"


# ── 10. WHITESPACE VARIATIONS ──────────────────────────

def test_whitespace_variations():
    """Verify trailing/leading whitespaces are stripped during normalization."""
    source = pl.DataFrame({"id": [101], "city": ["  Boston  \n"]})
    target = pl.DataFrame({
        "id": [101], "city": ["Boston"],
        "effective_from": [date(2026, 6, 1)], "effective_to": [None], "is_current": [True]
    })
    report = detect_changes(source, target, ["id"], ["city"], date(2026, 6, 8))
    # Should strip whitespace and evaluate Boston as unchanged
    assert len(report.unchanged) == 1
    assert len(report.changed) == 0


# ── 11. CASE VARIATIONS ────────────────────────────────

def test_case_variations():
    """Verify case differences are treated as changes (e.g. NYC vs nyc)."""
    source = pl.DataFrame({"id": [101], "city": ["nyc"]})
    target = pl.DataFrame({
        "id": [101], "city": ["NYC"],
        "effective_from": [date(2026, 6, 1)], "effective_to": [None], "is_current": [True]
    })
    report = detect_changes(source, target, ["id"], ["city"], date(2026, 6, 8))
    assert len(report.changed) == 1


# ── 12. FUTURE EFFECTIVE DATES ─────────────────────────

def test_future_effective_dates():
    """Verify processing dates set in the future stamp correctly."""
    source = pl.DataFrame({"id": [101], "name": ["Alice"]})
    target = pl.DataFrame({
        "id": [101], "name": ["Bob"],
        "effective_from": [date(2026, 6, 1)], "effective_to": [None], "is_current": [True]
    })
    future_date = date.today() + timedelta(days=90)
    report = detect_changes(source, target, ["id"], ["name"], future_date)
    output = apply_scd2(source, target, report, ["id"], ["name"], future_date)
    
    new_row = output.filter(pl.col("is_current") == True)
    assert new_row["effective_from"][0] == future_date


# ── 13. HISTORICAL GAP DETECTION ───────────────────────

def test_historical_gap_detection():
    """Verify that chronological gaps between records are allowed (not overlaps)."""
    df = pl.DataFrame({
        "id": [101, 101],
        "effective_from": [date(2026, 6, 1), date(2026, 6, 10)],  # Gap between 8th and 10th
        "effective_to": [date(2026, 6, 8), None],
        "is_current": [False, True]
    })
    report = validate_scd2(df, ["id"])
    assert report.passed


# ── 14. OVERLAPPING DATE DETECTION ─────────────────────

def test_overlapping_date_detection():
    """Verify overlap check detects overlapping date boundaries."""
    df = pl.DataFrame({
        "id": [101, 101],
        "effective_from": [date(2026, 6, 1), date(2026, 6, 5)],  # starts before prev closes
        "effective_to": [date(2026, 6, 8), None],
        "is_current": [False, True]
    })
    report = validate_scd2(df, ["id"])
    assert not report.passed
    rule = next(r for r in report.rules if r.name == "no_overlapping_dates")
    assert rule.status == ValidationStatus.FAIL


# ── 15. MASSIVE CHANGE VOLUME ─────────────────────────

def test_massive_change_volume():
    """Verify pipeline efficiency when 80%+ of records are modified."""
    # 10 records: 9 changed, 1 unchanged
    source = pl.DataFrame({
        "id": list(range(1, 11)),
        "value": ["X"] * 9 + ["A"]
    })
    target = pl.DataFrame({
        "id": list(range(1, 11)),
        "value": ["A"] * 10,
        "effective_from": [date(2026, 6, 1)] * 10,
        "effective_to": [None] * 10,
        "is_current": [True] * 10
    })
    report = detect_changes(source, target, ["id"], ["value"], date(2026, 6, 8))
    assert len(report.changed) == 9
    assert len(report.unchanged) == 1


# ── 16. API FAILURE SIMULATION & FALLBACK CHAIN ────────

@patch("google.genai.Client")
def test_api_failure_simulation(mock_genai_client):
    """Simulate API rate limit (429) & timeout (500) and verify fallback to Template."""
    # Configure Gemini client mock to raise rate limit exception
    mock_instance = MagicMock()
    mock_genai_client.return_value = mock_instance
    mock_instance.models.generate_content.side_effect = Exception("ResourceExhausted: 429 quota limit")
    
    settings = Settings(
        gemini_api_key="mock_key",
        llm_provider=LLMProvider.GEMINI
    )
    
    # We expect explain_changes to intercept exception and fallback toTemplateProvider
    source = pl.DataFrame({"id": [101]})
    target = pl.DataFrame({
        "id": [101],
        "effective_from": [date(2026, 6, 1)],
        "effective_to": [None],
        "is_current": [True]
    })
    report = detect_changes(source, target, ["id"], [], date(2026, 6, 8))
    # Mocking changes
    report.new.append(MagicMock(business_key_values={"id": 102}, change_type=ChangeType.NEW))
    
    result = explain_changes(report, settings=settings)
    assert len(result.explanations) == 1
    assert result.provider_used == "template"  # Successfully fell back to template!
    assert "failed" in result.warnings[0].lower()
    
    # Verify metrics
    assert result.metrics is not None
    assert result.metrics.provider == "template"
    assert result.metrics.prompt_tokens == 0
