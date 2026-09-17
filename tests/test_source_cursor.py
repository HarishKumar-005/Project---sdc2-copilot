"""Unit tests for SourceCursor deterministic composite cursor semantics."""

from __future__ import annotations

from datetime import datetime, timezone
import pytest

from src.scd2_copilot.source.models import SourceCursor


class TestSourceCursor:
    """Tests for SourceCursor instantiation, normalization, and ordering."""

    def test_cursor_utc_normalization(self) -> None:
        naive_dt = datetime(2026, 9, 16, 12, 0, 0)
        cursor = SourceCursor(
            timestamp=naive_dt,
            keys={"sku_id": "SKU-100"},
            key_columns=["sku_id"],
        )
        assert cursor.timestamp.tzinfo == timezone.utc

    def test_single_key_key_tuple(self) -> None:
        dt = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
        cursor = SourceCursor(
            timestamp=dt,
            keys={"sku_id": "SKU-001"},
            key_columns=["sku_id"],
        )
        assert cursor.key_tuple() == ("SKU-001",)

    def test_composite_key_key_tuple_preserves_order(self) -> None:
        dt = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
        cursor = SourceCursor(
            timestamp=dt,
            keys={"warehouse_id": "WH-EAST", "sku_id": "SKU-001"},
            key_columns=["sku_id", "warehouse_id"],
        )
        assert cursor.key_tuple() == ("SKU-001", "WH-EAST")

    def test_timestamp_ordering_precedence(self) -> None:
        t1 = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 9, 16, 12, 0, 1, tzinfo=timezone.utc)

        c1 = SourceCursor(timestamp=t1, keys={"id": "Z-999"}, key_columns=["id"])
        c2 = SourceCursor(timestamp=t2, keys={"id": "A-001"}, key_columns=["id"])

        assert c1 < c2
        assert c2 > c1
        assert c1 <= c2
        assert c2 >= c1
        assert c1 != c2

    def test_same_timestamp_lexicographical_single_key(self) -> None:
        t = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
        c1 = SourceCursor(timestamp=t, keys={"id": "A-001"}, key_columns=["id"])
        c2 = SourceCursor(timestamp=t, keys={"id": "B-002"}, key_columns=["id"])
        c3 = SourceCursor(timestamp=t, keys={"id": "B-002"}, key_columns=["id"])

        assert c1 < c2
        assert c2 > c1
        assert c2 == c3
        assert c2 <= c3
        assert c2 >= c3

    def test_same_timestamp_lexicographical_composite_key(self) -> None:
        t = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
        keys = ["sku_id", "warehouse_id"]

        c1 = SourceCursor(timestamp=t, keys={"sku_id": "SKU-1", "warehouse_id": "WH-1"}, key_columns=keys)
        c2 = SourceCursor(timestamp=t, keys={"sku_id": "SKU-1", "warehouse_id": "WH-2"}, key_columns=keys)
        c3 = SourceCursor(timestamp=t, keys={"sku_id": "SKU-2", "warehouse_id": "WH-1"}, key_columns=keys)

        assert c1 < c2 < c3
        sorted_list = sorted([c3, c1, c2])
        assert sorted_list == [c1, c2, c3]

    def test_roundtrip_dict_serialization(self) -> None:
        t = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
        original = SourceCursor(
            timestamp=t,
            keys={"sku_id": "SKU-001", "warehouse_id": "WH-1"},
            key_columns=["sku_id", "warehouse_id"],
        )
        d = original.to_dict()
        assert d == {
            "timestamp": t.isoformat(),
            "keys": {"sku_id": "SKU-001", "warehouse_id": "WH-1"},
            "key_columns": ["sku_id", "warehouse_id"],
        }
        reconstructed = SourceCursor.from_dict(d)
        assert reconstructed == original
        assert reconstructed.timestamp == original.timestamp
        assert reconstructed.keys == original.keys
        assert reconstructed.key_columns == original.key_columns
