"""CSV source adapter with delimiter auto-detection, schema discovery, and untrusted input protection."""

from __future__ import annotations

import csv
import io
import os
from pathlib import Path
from typing import Any, Optional
import polars as pl

from ..exceptions import (
    SourceCorruptedError,
    SourceEmptyError,
    SourceSchemaError,
    SourceTransportError,
)
from ..models.schema_snapshot import (
    ColumnSnapshot,
    SourceSchemaSnapshot,
    map_polars_type_to_inferred,
)
from ..models.source import SourceDefinition
from ..profiler.fingerprint import compute_schema_fingerprint, normalize_column_name
from .base import SourceAdapter


class CSVSourceAdapter(SourceAdapter):
    """Adapter for ingesting and discovering schemas from untrusted CSV files."""

    def __init__(self, definition: SourceDefinition) -> None:
        super().__init__(definition)
        self.file_path: Optional[str] = self.definition.connection_config.get("file_path")
        self.file_bytes: Optional[bytes] = self.definition.connection_config.get("file_bytes")
        self.explicit_delimiter: Optional[str] = self.definition.connection_config.get("delimiter")
        self.encoding: str = self.definition.connection_config.get("encoding", "utf-8")

    def _get_raw_bytes(self) -> bytes:
        """Fetch raw bytes from file path or memory buffer."""
        if self.file_bytes is not None:
            return self.file_bytes
        if self.file_path is None:
            raise SourceTransportError("Neither 'file_path' nor 'file_bytes' provided in connection config.")
        
        path = Path(self.file_path)
        if not path.exists():
            raise SourceTransportError(f"Source CSV file not found: {self.file_path}")
        if not path.is_file():
            raise SourceTransportError(f"Source CSV path is not a regular file: {self.file_path}")
        
        try:
            return path.read_bytes()
        except Exception as e:
            raise SourceTransportError(f"Failed to read CSV file: {e}", {"path": self.file_path}) from e

    def _detect_delimiter_and_headers(self, raw_bytes: bytes) -> tuple[str, list[str], str]:
        """Detect text encoding, delimiter, and validate header row without mutating source."""
        if not raw_bytes or len(raw_bytes.strip()) == 0:
            raise SourceEmptyError("Source CSV is empty (0 bytes).")

        # Try specified encoding, fallback to latin-1
        text: str
        used_encoding = self.encoding
        try:
            text = raw_bytes.decode(self.encoding)
        except UnicodeDecodeError:
            try:
                text = raw_bytes.decode("latin-1")
                used_encoding = "latin-1"
            except Exception as e:
                raise SourceCorruptedError(f"Unable to decode CSV content with encodings: {e}") from e

        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            raise SourceEmptyError("Source CSV has no non-empty lines.")

        first_line = lines[0]

        # Determine delimiter
        delimiter = self.explicit_delimiter
        if not delimiter:
            candidates = [",", ";", "\t", "|"]
            counts = {cand: first_line.count(cand) for cand in candidates}
            best = max(counts, key=counts.get) # type: ignore
            delimiter = best if counts[best] > 0 else ","

        # Parse header row using detected delimiter
        try:
            reader = csv.reader(io.StringIO(first_line), delimiter=delimiter)
            headers = next(reader)
        except Exception as e:
            raise SourceCorruptedError(f"Failed to parse CSV header line: {e}") from e

        if not headers or all(h.strip() == "" for h in headers):
            raise SourceSchemaError("CSV header row is empty or contains only whitespace.")

        # Check for duplicate headers
        seen: set[str] = set()
        duplicates: list[str] = []
        clean_headers: list[str] = []
        for h in headers:
            trimmed = h.strip()
            if trimmed in seen:
                duplicates.append(trimmed)
            seen.add(trimmed)
            clean_headers.append(trimmed)

        if duplicates:
            raise SourceSchemaError(
                f"Duplicate column headers found in CSV: {duplicates}",
                {"duplicates": duplicates, "headers": clean_headers},
            )

        return delimiter, clean_headers, used_encoding

    def read_data(self) -> pl.DataFrame:
        """Read CSV into a Polars DataFrame with validation."""
        raw_bytes = self._get_raw_bytes()
        delimiter, headers, encoding = self._detect_delimiter_and_headers(raw_bytes)

        try:
            df = pl.read_csv(
                io.BytesIO(raw_bytes),
                separator=delimiter,
                encoding=encoding,
                infer_schema_length=10000,
                ignore_errors=False,
                truncate_ragged_lines=False,
            )
        except pl.exceptions.ComputeError as ce:
            raise SourceCorruptedError(f"Malformed or ragged CSV structure: {ce}") from ce
        except Exception as e:
            raise SourceCorruptedError(f"Error parsing CSV content: {e}") from e

        # Ensure we strip leading/trailing whitespace from column names in the resulting DataFrame
        rename_map = {col: col.strip() for col in df.columns}
        df = df.rename(rename_map)

        if df.height == 0:
            raise SourceEmptyError("Source CSV contains header row but zero data records.")

        return df

    def discover_schema(self, data: Optional[pl.DataFrame] = None) -> SourceSchemaSnapshot:
        """Discover schema, infer types, and generate schema fingerprint."""
        df = data if data is not None else self.read_data()
        columns: list[ColumnSnapshot] = []

        for idx, (col_name, dtype) in enumerate(df.schema.items()):
            series = df[col_name]
            inferred = map_polars_type_to_inferred(dtype)
            nullable = series.null_count() > 0
            normalized = normalize_column_name(col_name)

            columns.append(
                ColumnSnapshot(
                    original_name=col_name,
                    normalized_name=normalized,
                    inferred_type=inferred,
                    polars_type=str(dtype),
                    nullable=nullable,
                    ordinal_position=idx,
                )
            )

        fingerprint = compute_schema_fingerprint(columns)
        return SourceSchemaSnapshot(
            source_id=self.source_id,
            schema_version=1,
            fingerprint=fingerprint,
            columns=columns,
        )
