"""Basic smoke test — verifies imports and package structure."""

import pytest


def test_package_imports():
    """All core modules should be importable."""
    from src.scd2_copilot import __version__
    from src.scd2_copilot.config import Settings
    from src.scd2_copilot.models import ChangeReport, ValidationReport, PipelineResult
    from src.scd2_copilot.ingestion import load_csv
    from src.scd2_copilot.schema import detect_business_key, detect_tracked_columns
    from src.scd2_copilot.detect_changes import detect_changes
    from src.scd2_copilot.transform_scd2 import apply_scd2
    from src.scd2_copilot.validate import validate_scd2
    from src.scd2_copilot.explain import explain_changes
    from src.scd2_copilot.providers.base import LLMProvider
    from src.scd2_copilot.providers.template import TemplateProvider

    assert __version__ == "0.1.0"


def test_settings_defaults():
    """Settings should load with sane defaults when no env vars are set."""
    from src.scd2_copilot.config import Settings, LLMProvider
    # Bypass .env file by setting _env_file to None
    s = Settings(_env_file=None, gemini_api_key="", groq_api_key="", llm_provider="template")
    assert s.llm_provider == LLMProvider.TEMPLATE
    assert s.gemini_api_key == ""
    assert s.groq_api_key == ""


def test_settings_loads_env():
    """Settings should pick up values from .env when present."""
    from src.scd2_copilot.config import Settings, LLMProvider
    s = Settings()  # loads real .env
    # The type should always be a valid LLMProvider enum member
    assert isinstance(s.llm_provider, LLMProvider)