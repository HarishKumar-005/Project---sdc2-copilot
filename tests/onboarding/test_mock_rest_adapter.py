"""Tests for MockRESTSourceAdapter: HTTP status codes, offline transport, payloads, and error handling."""

import json
from pathlib import Path
import pytest
import httpx
import polars as pl

from scd2_copilot.onboarding.adapters.mock_rest_adapter import MockRESTSourceAdapter
from scd2_copilot.onboarding.exceptions import (
    SourceCorruptedError,
    SourceEmptyError,
    SourceTransportError,
)
from scd2_copilot.onboarding.models.source import SourceDefinition, SourceType


def test_mock_rest_adapter_with_canned_json(crm_json_path: Path) -> None:
    canned_data = json.loads(crm_json_path.read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=canned_data)

    mock_transport = httpx.MockTransport(handler)

    defn = SourceDefinition(
        source_id="crm_rest",
        source_name="CRM REST API",
        source_type=SourceType.MOCK_REST,
        connection_config={"base_url": "https://api.example.com/customers"},
    )

    adapter = MockRESTSourceAdapter(defn, transport=mock_transport)
    df = adapter.read_data()

    assert isinstance(df, pl.DataFrame)
    assert df.height == 10
    assert "Cust_ID" in df.columns
    assert "First Name" in df.columns

    schema = adapter.discover_schema(df)
    assert schema.source_id == "crm_rest"
    assert len(schema.columns) == 8
    assert schema.fingerprint.fingerprint_hash is not None


def test_mock_rest_adapter_handles_nested_dict_payload() -> None:
    payload = {
        "status": "success",
        "total": 2,
        "data": [
            {"account_id": "ACC-1", "balance": 150.0},
            {"account_id": "ACC-2", "balance": 300.5},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    mock_transport = httpx.MockTransport(handler)
    defn = SourceDefinition(
        source_id="nested_rest",
        source_name="Nested REST",
        source_type=SourceType.MOCK_REST,
        connection_config={"base_url": "https://api.example.com/data"},
    )
    adapter = MockRESTSourceAdapter(defn, transport=mock_transport)
    df = adapter.read_data()
    assert df.height == 2
    assert df.columns == ["account_id", "balance"]


def test_mock_rest_adapter_raises_transport_error_on_404_and_500() -> None:
    def handler_404(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not Found")

    mock_transport_404 = httpx.MockTransport(handler_404)
    defn = SourceDefinition(
        source_id="error_rest",
        source_name="Error REST",
        source_type=SourceType.MOCK_REST,
        connection_config={"base_url": "https://api.example.com/notfound"},
    )
    adapter = MockRESTSourceAdapter(defn, transport=mock_transport_404)
    with pytest.raises(SourceTransportError) as exc_info:
        adapter.read_data()
    assert "404" in str(exc_info.value)
    assert exc_info.value.details.get("status_code") == 404

    def handler_500(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    adapter_500 = MockRESTSourceAdapter(defn, transport=httpx.MockTransport(handler_500))
    with pytest.raises(SourceTransportError) as exc_info_500:
        adapter_500.read_data()
    assert "500" in str(exc_info_500.value)


def test_mock_rest_adapter_raises_corrupted_error_on_invalid_json() -> None:
    def handler_bad_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>Not JSON</body></html>")

    mock_transport = httpx.MockTransport(handler_bad_json)
    defn = SourceDefinition(
        source_id="corrupt_rest",
        source_name="Corrupt REST",
        source_type=SourceType.MOCK_REST,
        connection_config={"base_url": "https://api.example.com/bad"},
    )
    adapter = MockRESTSourceAdapter(defn, transport=mock_transport)
    with pytest.raises(SourceCorruptedError):
        adapter.read_data()


def test_mock_rest_adapter_raises_empty_error_on_empty_list() -> None:
    def handler_empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    mock_transport = httpx.MockTransport(handler_empty)
    defn = SourceDefinition(
        source_id="empty_rest",
        source_name="Empty REST",
        source_type=SourceType.MOCK_REST,
        connection_config={"base_url": "https://api.example.com/empty"},
    )
    adapter = MockRESTSourceAdapter(defn, transport=mock_transport)
    with pytest.raises(SourceEmptyError):
        adapter.read_data()


def test_mock_rest_adapter_handles_pagination() -> None:
    def handler_paged(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", 1))
        if page == 1:
            return httpx.Response(
                200,
                json=[
                    {"id": 1, "name": "Item 1"},
                    {"id": 2, "name": "Item 2"},
                ],
            )
        elif page == 2:
            return httpx.Response(
                200,
                json=[
                    {"id": 3, "name": "Item 3"},
                ],
            )
        else:
            return httpx.Response(200, json=[])

    mock_transport = httpx.MockTransport(handler_paged)
    defn = SourceDefinition(
        source_id="paged_rest",
        source_name="Paged REST",
        source_type=SourceType.MOCK_REST,
        connection_config={
            "base_url": "https://api.example.com/paged",
            "paginated": True,
            "page_size": 2,
            "max_records": 10,
        },
    )
    adapter = MockRESTSourceAdapter(defn, transport=mock_transport)
    df = adapter.read_data()
    assert df.height == 3
    assert df["id"].to_list() == [1, 2, 3]
