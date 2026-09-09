"""Application configuration via pydantic-settings.

Loads settings from .env file and environment variables.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
import json
from pathlib import Path
from typing import Any, Optional

from pydantic import field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from .models import DeletePolicy, SnapshotMode


class LLMProvider(str, Enum):
    """Supported LLM provider identifiers."""

    GEMINI = "gemini"
    GROQ = "groq"
    TEMPLATE = "template"


# ── Gemini model defaults (Single Source of Truth) ─────
DEFAULT_GEMINI_MODEL: str = "gemini-3.8-flash"
DEFAULT_GEMINI_FALLBACK_MODELS: list[str] = [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",
]


def _decode_complex_env_value(field_name: str, field: Any, value: Any) -> Any:
    """Helper to decode complex values from env, accepting JSON or comma-separated lists."""
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return []
        try:
            return json.loads(v)
        except Exception:
            return [x.strip() for x in v.split(",") if x.strip()]
    return value


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
    gemini_model: str = DEFAULT_GEMINI_MODEL
    gemini_fallback_models: list[str] = list(DEFAULT_GEMINI_FALLBACK_MODELS)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        if hasattr(env_settings, "decode_complex_value"):
            env_settings.decode_complex_value = _decode_complex_env_value
        if hasattr(dotenv_settings, "decode_complex_value"):
            dotenv_settings.decode_complex_value = _decode_complex_env_value
        return (init_settings, env_settings, dotenv_settings, file_secret_settings)

    @field_validator("gemini_fallback_models", mode="before")
    @classmethod
    def _validate_gemini_fallback_models_input(cls, v: object) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            if v.startswith("[") and v.endswith("]"):
                try:
                    parsed = json.loads(v)
                    if isinstance(parsed, list):
                        return [str(m).strip() for m in parsed if str(m).strip()]
                except Exception:
                    pass
            return [m.strip() for m in v.split(",") if m.strip()]
        if isinstance(v, (list, tuple)):
            return [str(m).strip() for m in v if str(m).strip()]
        return []

    @model_validator(mode="after")
    def _normalize_and_deduplicate_models(self) -> Settings:
        """Enforce non-empty normalized model IDs and remove duplicates and primary from fallbacks."""
        # 1. Normalize primary model
        if not self.gemini_model or not self.gemini_model.strip():
            self.gemini_model = DEFAULT_GEMINI_MODEL
        else:
            self.gemini_model = self.gemini_model.strip()

        # 2. Normalize and deduplicate fallback models, removing empty strings and primary model
        deduped: list[str] = []
        seen = {self.gemini_model}
        for m in self.gemini_fallback_models:
            clean = m.strip()
            if clean and clean not in seen:
                deduped.append(clean)
                seen.add(clean)
        self.gemini_fallback_models = deduped
        return self

    def get_gemini_model_chain(self) -> list[str]:
        """Return the ordered list of Gemini models to try (primary followed by unique fallbacks)."""
        return [self.gemini_model] + self.gemini_fallback_models

    # ── App settings ───────────────────────────────────
    app_env: str = "development"
    default_timezone: str = "UTC"

    # ── SCD2 defaults ──────────────────────────────────
    processing_date: date = date.today()
    snapshot_mode: SnapshotMode = SnapshotMode.FULL
    delete_policy: DeletePolicy = DeletePolicy.SOFT_DELETE

    # ── Schedule settings (M3.4) ───────────────────────
    pipeline_schedule_cron: Optional[str] = "0 2 * * *"
    pipeline_schedule_timezone: str = "UTC"
    pipeline_schedule_active: bool = True

    # ── Default dataset paths for scheduled execution ───
    default_source_path: str = "data/source.csv"
    default_target_path: str = "data/target_scd2.csv"

    # ── Artifact persistence settings (M3.5) ───────────
    runs_directory: str = "data/runs"
    persist_artifacts: bool = True

    # ── Input quarantine (M4.3) ───────────────────────
    quarantine_directory: str = "data/quarantine"

    # ── Idempotency settings (M3.6) ────────────────────
    idempotency_enabled: bool = True
    fingerprint_algorithm: str = "sha256"

    # ── Prefect local server settings ──────────────────
    prefect_api_url: str = "http://127.0.0.1:4200/api"

    def get_runs_dir(self) -> Path:
        """Resolve the runs directory as an absolute Path relative to project root if not absolute."""
        p = Path(self.runs_directory)
        if not p.is_absolute():
            return PROJECT_ROOT / p
        return p

    def get_quarantine_dir(self) -> Path:
        """Resolve the structured input-quarantine directory."""
        p = Path(self.quarantine_directory)
        return PROJECT_ROOT / p if not p.is_absolute() else p

    @property
    def has_gemini_key(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def has_groq_key(self) -> bool:
        return bool(self.groq_api_key)

    def get_effective_provider(self) -> LLMProvider:
        """Return the best available provider based on config and keys."""
        if self.llm_provider == LLMProvider.TEMPLATE:
            return LLMProvider.TEMPLATE
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
