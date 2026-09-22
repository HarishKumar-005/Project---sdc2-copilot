"""Source definition and transport types for onboarding."""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field


class SourceType(str, Enum):
    """Supported source transport types."""

    CSV = "csv"
    MOCK_REST = "mock_rest"


class SourceDefinition(BaseModel):
    """Stable logical identity and configuration of an external data source."""

    source_id: str = Field(..., description="Unique logical identifier for the source system (e.g. crm_customers)")
    source_name: str = Field(..., description="Human-readable name of the source system")
    source_type: SourceType = Field(..., description="Transport mechanism")
    connection_config: dict[str, Any] = Field(
        default_factory=dict,
        description="Transport-specific parameters (e.g. file_path, base_url, headers)",
    )
    description: Optional[str] = Field(default=None, description="Optional description of the source domain")
