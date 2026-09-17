"""Configurable PostgreSQL source package for SCD2 Copilot."""

from __future__ import annotations

from .adapter import PostgresSourceAdapter
from .exceptions import (
    SourceColumnNotFoundError,
    SourceConfigurationError,
    SourceConnectionError,
    SourceSchemaNotFoundError,
    SourceTableNotFoundError,
    SourceTypeMismatchError,
)
from .inventory import get_default_inventory_monitor_config
from .models import (
    ChangeTimestampDefinition,
    MonitorConfig,
    NormalizedSourceRecord,
    PostgresSourceDefinition,
    SourceColumnMetadata,
    SourceCursor,
    SourceTableMetadata,
    SourceValidationResult,
)
from .registry import MonitorRegistry, get_monitor_registry

__all__ = [
    "ChangeTimestampDefinition",
    "MonitorConfig",
    "MonitorRegistry",
    "NormalizedSourceRecord",
    "PostgresSourceAdapter",
    "PostgresSourceDefinition",
    "SourceColumnMetadata",
    "SourceColumnNotFoundError",
    "SourceConfigurationError",
    "SourceConnectionError",
    "SourceCursor",
    "SourceSchemaNotFoundError",
    "SourceTableMetadata",
    "SourceTableNotFoundError",
    "SourceTypeMismatchError",
    "SourceValidationResult",
    "get_default_inventory_monitor_config",
    "get_monitor_registry",
]
