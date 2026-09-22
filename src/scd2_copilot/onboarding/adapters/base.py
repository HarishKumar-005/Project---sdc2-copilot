"""Abstract base class for onboarding source adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional
import polars as pl

from ..models.schema_snapshot import SourceSchemaSnapshot
from ..models.source import SourceDefinition


class SourceAdapter(ABC):
    """Abstract boundary for external customer source adapters."""

    def __init__(self, definition: SourceDefinition) -> None:
        self.definition = definition

    @property
    def source_id(self) -> str:
        return self.definition.source_id

    @property
    def source_name(self) -> str:
        return self.definition.source_name

    @abstractmethod
    def read_data(self) -> pl.DataFrame:
        """Read external records into a normalized Polars DataFrame without mutating the source.

        Raises:
            SourceEmptyError: If source has 0 bytes, 0 rows, or empty payload.
            SourceCorruptedError: If source payload or format cannot be parsed.
            SourceTransportError: If network/transport/file-system reading fails.
            SourceSchemaError: If headers or columns cannot be resolved.
        """
        pass

    @abstractmethod
    def discover_schema(self, data: Optional[pl.DataFrame] = None) -> SourceSchemaSnapshot:
        """Inspect and return the schema snapshot and deterministic fingerprint of the source."""
        pass

    def validate_source(self) -> bool:
        """Check source reachability and structural validity."""
        df = self.read_data()
        return df.height > 0 and df.width > 0
