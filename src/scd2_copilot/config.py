"""Application configuration via pydantic-settings.

Loads settings from .env file and environment variables.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
import json
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse, urlunparse

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


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SAMPLE_DATA_DIR = PROJECT_ROOT / "sample-data"


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


class StreamlitSecretsSettingsSource(PydanticBaseSettingsSource):
    """Read configuration from streamlit.secrets when running inside Streamlit."""

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        try:
            import streamlit as st
            if hasattr(st, "secrets") and st.secrets:
                for k, v in st.secrets.items():
                    if k.lower() == field_name.lower():
                        return v, field_name, False
        except Exception:
            pass
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        try:
            import streamlit as st
            if hasattr(st, "secrets") and st.secrets:
                for k, v in st.secrets.items():
                    data[k.lower()] = v
        except Exception:
            pass
        return data


class Settings(BaseSettings):
    """Application-wide settings loaded from .env / environment."""

    model_config = SettingsConfigDict(
        env_file=(str(PROJECT_ROOT / ".env"), ".env"),
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
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
            StreamlitSecretsSettingsSource(settings_cls),
        )

    @field_validator("gemini_fallback_models", "cors_allowed_origins", "recovery_operator_emails", mode="before")
    @classmethod
    def _validate_string_list_input(cls, v: object) -> list[str]:
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

    # ── Database & Supabase settings (V2) ───────────────
    database_url: Optional[str] = ""
    db_pool_min: int = 1
    db_pool_max: int = 10
    db_connect_timeout: float = 10.0
    supabase_url: Optional[str] = ""
    supabase_publishable_key: Optional[str] = ""
    supabase_secret_key: Optional[str] = ""

    # ── Incremental Ingestion Worker settings (V2.2) ───
    ingestion_enabled: bool = True
    ingestion_batch_size: int = 50
    ingestion_poll_interval_seconds: float = 15.0
    ingestion_source_name: str = "inventory"
    ingestion_table_name: str = "inventory_source"
    ingestion_retry_count: int = 3
    ingestion_retry_backoff_seconds: float = 2.0

    # ── Guardrail & Change Significance settings (V2.3) ───
    guardrail_enabled: bool = True
    guardrail_max_changed_records: int = 25
    guardrail_max_affected_population_ratio: float = 0.80
    guardrail_min_evaluated_records_for_ratio: int = 10
    guardrail_max_quantity_relative_change: float = 3.0
    guardrail_min_absolute_quantity_change: int = 50
    guardrail_max_deactivation_count: int = 5
    guardrail_max_changes_per_second: float = 50.0
    guardrail_min_velocity_records: int = 10
    guardrail_max_warehouses_affected: int = 5

    # ── AI Explanation settings (V2.5) ─────────────────
    ai_explanation_enabled: bool = True
    ai_explanation_for_normal_batches: bool = False
    ai_explanation_timeout_seconds: float = 10.0
    ai_explanation_retry_count: int = 1

    # ── Operational API & Supabase Auth settings (V2.6) ──
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_base_url: str = "http://localhost:8000"
    api_auth_required: bool = False
    cors_allowed_origins: list[str] = [
        "http://localhost:8501",
        "http://127.0.0.1:8501",
        "https://sdc2-copilot.streamlit.app",
        "https://project-sdc2-copilot.onrender.com",
    ]
    streamlit_app_url: Optional[str] = ""
    api_request_timeout_seconds: float = 10.0
    api_max_query_limit: int = 100
    supabase_auth_issuer: Optional[str] = ""
    supabase_auth_audience: str = "authenticated"
    supabase_jwks_url: Optional[str] = ""
    recovery_operator_emails: list[str] = []

    @field_validator("cors_allowed_origins")
    @classmethod
    def _validate_cors_origins(cls, v: list[str]) -> list[str]:
        if "*" in v:
            raise ValueError(
                "Wildcard origin '*' is forbidden in cors_allowed_origins when credentials are enabled. "
                "Specify explicit origin URLs (e.g. 'http://localhost:8501')."
            )
        import os
        render_ext = os.environ.get("RENDER_EXTERNAL_URL")
        if render_ext and render_ext.strip():
            clean_render = render_ext.strip().rstrip("/")
            if clean_render not in v:
                v = list(v) + [clean_render]
        return v

    @field_validator("guardrail_max_affected_population_ratio")
    @classmethod
    def _validate_ratio(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError("guardrail_max_affected_population_ratio must be between 0.0 and 1.0")
        return v

    @field_validator(
        "guardrail_max_changed_records",
        "guardrail_min_evaluated_records_for_ratio",
        "guardrail_min_absolute_quantity_change",
        "guardrail_max_deactivation_count",
        "guardrail_min_velocity_records",
        "guardrail_max_warehouses_affected",
    )
    @classmethod
    def _validate_non_negative_int(cls, v: int) -> int:
        if v < 0:
            raise ValueError("Guardrail integer threshold cannot be negative")
        return v

    @field_validator(
        "guardrail_max_quantity_relative_change",
        "guardrail_max_changes_per_second",
    )
    @classmethod
    def _validate_non_negative_float(cls, v: float) -> float:
        if v < 0.0:
            raise ValueError("Guardrail float threshold cannot be negative")
        return v

    @property
    def has_database(self) -> bool:
        """True if DATABASE_URL is configured."""
        return bool(self.database_url and self.database_url.strip())

    @property
    def resolved_supabase_jwks_url(self) -> str:
        """Return the JWKS URL, deriving it from supabase_url if not explicitly set."""
        if self.supabase_jwks_url and self.supabase_jwks_url.strip():
            return self.supabase_jwks_url.strip()
        if self.supabase_url and self.supabase_url.strip():
            return f"{self.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
        return ""

    @property
    def resolved_supabase_issuer(self) -> str:
        """Return expected JWT issuer, deriving it from supabase_url if not explicitly set."""
        if self.supabase_auth_issuer and self.supabase_auth_issuer.strip():
            return self.supabase_auth_issuer.strip()
        if self.supabase_url and self.supabase_url.strip():
            return f"{self.supabase_url.rstrip('/')}/auth/v1"
        return ""


    def get_redacted_database_url(self) -> str:
        """Return database URL with credentials safely masked."""
        if not self.has_database:
            return ""
        try:
            parsed = urlparse(self.database_url)
            if not parsed.netloc:
                return "<configured>"
            netloc = parsed.netloc
            if "@" in netloc:
                userinfo, hostinfo = netloc.split("@", 1)
                if ":" in userinfo:
                    user, _ = userinfo.split(":", 1)
                    masked_userinfo = f"{user}:***"
                else:
                    masked_userinfo = "***"
                redacted_netloc = f"{masked_userinfo}@{hostinfo}"
            else:
                redacted_netloc = netloc
            return urlunparse(parsed._replace(netloc=redacted_netloc))
        except Exception:
            return "<redacted-db-url>"

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


def get_settings() -> Settings:
    """Factory that creates a Settings instance (cacheable by caller)."""
    return Settings()
