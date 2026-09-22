"""Tests for CSVSourceAdapter: delimiter detection, error taxonomy, schema discovery, and immutability."""

import hashlib
from pathlib import Path
import pytest
import polars as pl

from scd2_copilot.onboarding.adapters.csv_adapter import CSVSourceAdapter
from scd2_copilot.onboarding.exceptions import (
    SourceCorruptedError,
    SourceEmptyError,
    SourceSchemaError,
    SourceTransportError,
)
from scd2_copilot.onboarding.models.source import SourceDefinition, SourceType


def test_csv_adapter_reads_valid_crm_file(crm_csv_path: Path) -> None:
    defn = SourceDefinition(
        source_id="crm_test",
        source_name="CRM Test",
        source_type=SourceType.CSV,
        connection_config={"file_path": str(crm_csv_path)},
    )
    adapter = CSVSourceAdapter(defn)
    df = adapter.read_data()

    assert isinstance(df, pl.DataFrame)
    assert df.height == 10
    assert "Cust_ID" in df.columns
    assert "First Name" in df.columns
    assert "Contact Email" in df.columns

    schema = adapter.discover_schema(df)
    assert schema.source_id == "crm_test"
    assert len(schema.columns) == 8
    assert schema.fingerprint.fingerprint_hash is not None
    assert len(schema.fingerprint.fingerprint_hash) == 64


def test_csv_adapter_auto_detects_delimiters(tmp_path: Path) -> None:
    delimiters = [",", ";", "\t", "|"]
    for delim in delimiters:
        content = f"id{delim}name{delim}score\n1{delim}Alice{delim}95\n2{delim}Bob{delim}88\n"
        csv_file = tmp_path / f"test_{ord(delim)}.csv"
        csv_file.write_text(content, encoding="utf-8")

        defn = SourceDefinition(
            source_id="delim_test",
            source_name="Delimiter Test",
            source_type=SourceType.CSV,
            connection_config={"file_path": str(csv_file)},
        )
        adapter = CSVSourceAdapter(defn)
        df = adapter.read_data()
        assert df.height == 2
        assert df.columns == ["id", "name", "score"]
        assert df["name"].to_list() == ["Alice", "Bob"]


def test_csv_adapter_raises_source_schema_error_on_duplicate_headers(tmp_path: Path) -> None:
    content = "id,name,email,name\n1,Alice,alice@example.com,A.\n"
    csv_file = tmp_path / "duplicate_headers.csv"
    csv_file.write_text(content, encoding="utf-8")

    defn = SourceDefinition(
        source_id="dup_test",
        source_name="Dup Test",
        source_type=SourceType.CSV,
        connection_config={"file_path": str(csv_file)},
    )
    adapter = CSVSourceAdapter(defn)
    with pytest.raises(SourceSchemaError) as exc_info:
        adapter.read_data()
    assert "Duplicate column headers found" in str(exc_info.value)
    assert "name" in exc_info.value.details.get("duplicates", [])


def test_csv_adapter_raises_source_empty_error_on_zero_bytes(tmp_path: Path) -> None:
    empty_file = tmp_path / "empty.csv"
    empty_file.write_text("", encoding="utf-8")

    defn = SourceDefinition(
        source_id="empty_test",
        source_name="Empty Test",
        source_type=SourceType.CSV,
        connection_config={"file_path": str(empty_file)},
    )
    adapter = CSVSourceAdapter(defn)
    with pytest.raises(SourceEmptyError) as exc_info:
        adapter.read_data()
    assert "0 bytes" in str(exc_info.value) or "no non-empty lines" in str(exc_info.value)


def test_csv_adapter_raises_source_empty_error_on_header_only_file(tmp_path: Path) -> None:
    header_file = tmp_path / "header_only.csv"
    header_file.write_text("id,name,email\n", encoding="utf-8")

    defn = SourceDefinition(
        source_id="header_test",
        source_name="Header Only Test",
        source_type=SourceType.CSV,
        connection_config={"file_path": str(header_file)},
    )
    adapter = CSVSourceAdapter(defn)
    with pytest.raises(SourceEmptyError) as exc_info:
        adapter.read_data()
    assert "zero data records" in str(exc_info.value)


def test_csv_adapter_raises_source_transport_error_on_missing_file() -> None:
    defn = SourceDefinition(
        source_id="missing_test",
        source_name="Missing Test",
        source_type=SourceType.CSV,
        connection_config={"file_path": "non_existent_file_path_12345.csv"},
    )
    adapter = CSVSourceAdapter(defn)
    with pytest.raises(SourceTransportError) as exc_info:
        adapter.read_data()
    assert "not found" in str(exc_info.value).lower()


def test_csv_adapter_leaves_source_file_immutable(crm_csv_path: Path) -> None:
    # Compute SHA-256 before reading
    hash_before = hashlib.sha256(crm_csv_path.read_bytes()).hexdigest()

    defn = SourceDefinition(
        source_id="immutability_test",
        source_name="Immutability Test",
        source_type=SourceType.CSV,
        connection_config={"file_path": str(crm_csv_path)},
    )
    adapter = CSVSourceAdapter(defn)
    adapter.read_data()
    adapter.discover_schema()

    # Compute SHA-256 after reading
    hash_after = hashlib.sha256(crm_csv_path.read_bytes()).hexdigest()
    assert hash_before == hash_after, "Source file was unexpectedly mutated during ingestion"
