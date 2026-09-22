"""Mock REST API source adapter with dual-mode HTTP and offline ASGI/mock transport support."""

from __future__ import annotations

from typing import Any, Optional
import httpx
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


class MockRESTSourceAdapter(SourceAdapter):
    """Adapter for ingesting external records via REST APIs using httpx."""

    def __init__(
        self,
        definition: SourceDefinition,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        super().__init__(definition)
        self.base_url: str = self.definition.connection_config.get("base_url", "http://localhost:8000")
        self.endpoint: str = self.definition.connection_config.get("endpoint", "")
        self.headers: dict[str, str] = self.definition.connection_config.get("headers", {})
        self.timeout: float = float(self.definition.connection_config.get("timeout", 10.0))
        self.paginated: bool = bool(self.definition.connection_config.get("paginated", False))
        self.page_param: str = self.definition.connection_config.get("page_param", "page")
        self.limit_param: str = self.definition.connection_config.get("limit_param", "limit")
        self.page_size: int = int(self.definition.connection_config.get("page_size", 100))
        self.max_records: int = int(self.definition.connection_config.get("max_records", 10000))
        # Optional custom transport for offline mocking (e.g. httpx.MockTransport)
        self.transport: Optional[httpx.BaseTransport] = (
            transport or self.definition.connection_config.get("transport")
        )

    def _extract_record_list(self, payload: Any) -> list[dict[str, Any]]:
        """Extract list of records from either a top-level array or nested dictionary."""
        if isinstance(payload, list):
            records = payload
        elif isinstance(payload, dict):
            # Check common collection keys
            for key in ("data", "records", "items", "results", "customers"):
                if key in payload and isinstance(payload[key], list):
                    records = payload[key]
                    break
            else:
                raise SourceCorruptedError("REST response JSON dict does not contain a recognized list of records.")
        else:
            raise SourceCorruptedError(f"Unexpected REST JSON root type: {type(payload).__name__}")

        if not records:
            raise SourceEmptyError("REST source returned an empty record collection.")

        if not all(isinstance(r, dict) for r in records):
            raise SourceCorruptedError("Elements in REST record collection must be JSON objects.")

        return records

    def read_data(self) -> pl.DataFrame:
        """Fetch records over HTTP or offline transport and convert to Polars DataFrame."""
        url = f"{self.base_url.rstrip('/')}/{self.endpoint.lstrip('/')}" if self.endpoint else self.base_url

        try:
            with httpx.Client(transport=self.transport, timeout=self.timeout) as client:
                if not self.paginated:
                    response = client.get(url, headers=self.headers)
                    if response.status_code >= 400:
                        raise SourceTransportError(
                            f"HTTP error {response.status_code} from {url}: {response.text[:200]}",
                            {"status_code": response.status_code, "response_body": response.text[:500]},
                        )
                    try:
                        payload = response.json()
                    except Exception as e:
                        raise SourceCorruptedError(f"Failed to parse JSON response from {url}: {e}") from e

                    records = self._extract_record_list(payload)
                else:
                    records = []
                    page = 1
                    while len(records) < self.max_records:
                        params = {self.page_param: page, self.limit_param: self.page_size}
                        response = client.get(url, headers=self.headers, params=params)
                        if response.status_code >= 400:
                            raise SourceTransportError(
                                f"HTTP error {response.status_code} from {url} on page {page}: {response.text[:200]}",
                                {"status_code": response.status_code, "response_body": response.text[:500]},
                            )
                        try:
                            payload = response.json()
                        except Exception as e:
                            raise SourceCorruptedError(f"Failed to parse JSON response from {url}: {e}") from e

                        try:
                            page_records = self._extract_record_list(payload)
                        except SourceEmptyError:
                            break

                        if not page_records:
                            break

                        records.extend(page_records)
                        if len(page_records) < self.page_size:
                            break
                        if isinstance(payload, dict) and "next" in payload and payload.get("next") is None:
                            break
                        page += 1

                    if not records:
                        raise SourceEmptyError("REST source returned an empty record collection across all pages.")

        except (SourceTransportError, SourceCorruptedError, SourceEmptyError):
            raise
        except httpx.TimeoutException as te:
            raise SourceTransportError(f"Request to {url} timed out: {te}") from te
        except httpx.RequestError as re:
            raise SourceTransportError(f"Network error connecting to {url}: {re}") from re
        except Exception as e:
            raise SourceTransportError(f"Failed to execute HTTP request: {e}") from e

        try:
            df = pl.DataFrame(records)
        except Exception as e:
            raise SourceCorruptedError(f"Failed to convert REST records into DataFrame: {e}") from e

        if df.height == 0:
            raise SourceEmptyError("DataFrame constructed from REST records contains zero rows.")

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
