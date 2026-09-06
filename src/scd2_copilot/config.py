"""Application configuration via pydantic-settings.

Loads settings from .env file and environment variables.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from pathlib import Path

# pyrefly: ignore [missing-import]
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMProvider(str, Enum):
    """Supported LLM provider identifiers."""

    GEMINI = "gemini"
    GROQ = "groq"
    TEMPLATE = "template"


class Settings(BaseSettings):
    """Application-wide settings loaded from .env / environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── LLM provider keys ──────────────────────────────
    gemini_api_key: str = ""
    groq_api_key: str = ""
    openrouter_api_key: str = ""

    # ── LLM selection ──────────────────────────────────
    llm_provider: LLMProvider = LLMProvider.TEMPLATE

    # ── App settings ───────────────────────────────────
    app_env: str = "development"
    default_timezone: str = "UTC"

    # ── SCD2 defaults ──────────────────────────────────
    processing_date: date = date.today()

    @property
    def has_gemini_key(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def has_groq_key(self) -> bool:
        return bool(self.groq_api_key)

    def get_effective_provider(self) -> LLMProvider:
        """Return the best available provider based on config and keys."""
        if self.llm_provider == LLMProvider.GEMINI and self.has_gemini_key:
            return LLMProvider.GEMINI
        if self.llm_provider == LLMProvider.GROQ and self.has_groq_key:
            return LLMProvider.GROQ
        # Fallback chain: try gemini, then groq, then template
        if self.has_gemini_key:
            return LLMProvider.GEMINI
        if self.has_groq_key:
            return LLMProvider.GROQ
        return LLMProvider.TEMPLATE


# ── Paths ──────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SAMPLE_DATA_DIR = PROJECT_ROOT / "sample-data"


def get_settings() -> Settings:
    """Factory that creates a Settings instance (cacheable by caller)."""
    return Settings()
