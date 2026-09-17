"""Unit tests for generic history model, repository, and DataFrame adaptation."""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import polars as pl
import pytest

from src.scd2_copilot.db.models import MonitoredEntityHistoryRow
from src.scd2_copilot.db.repositories.monitored_entity_history_repository import (
    MonitoredEntityHistoryRepository,
)
from src.scd2_copilot.worker.batch import generic_history_rows_to_target_df


class TestMonitoredEntityHistoryModel:
    """Tests for MonitoredEntityHistoryRow validation and serialization."""

    def test_valid_active_row(self) -> None:
        row = MonitoredEntityHistoryRow(
            source_name="product_monitor",
            entity_key={"product_id": "P-1"},
            attributes={"price": 100, "tier": "gold"},
            effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
            is_current=True,
        )
        assert row.source_name == "product_monitor"
        assert row.entity_key == {"product_id": "P-1"}
        assert row.attributes == {"price": 100, "tier": "gold"}
        assert row.effective_to is None
        assert row.is_current is True
        assert isinstance(row.history_id, UUID)
        assert row.business_key_tuple == ("P-1",)

    def test_valid_closed_row(self) -> None:
        row = MonitoredEntityHistoryRow(
            source_name="product_monitor",
            entity_key={"product_id": "P-1"},
            attributes={"price": 100, "tier": "gold"},
            effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
            effective_to=datetime(2026, 1, 10, tzinfo=timezone.utc),
            is_current=False,
        )
        assert row.is_current is False
        assert row.effective_to == datetime(2026, 1, 10, tzinfo=timezone.utc)

    def test_empty_source_name_raises(self) -> None:
        with pytest.raises(ValueError, match="source_name cannot be empty"):
            MonitoredEntityHistoryRow(
                source_name="",
                entity_key={"product_id": "P-1"},
                attributes={"price": 100},
                effective_from=datetime.now(timezone.utc),
                is_current=True,
            )

    def test_empty_entity_key_raises(self) -> None:
        with pytest.raises(ValueError, match="entity_key must be a non-empty dictionary"):
            MonitoredEntityHistoryRow(
                source_name="product_monitor",
                entity_key={},
                attributes={"price": 100},
                effective_from=datetime.now(timezone.utc),
                is_current=True,
            )

    def test_reversed_interval_raises(self) -> None:
        with pytest.raises(ValueError, match="effective_from must be before or equal to effective_to"):
            MonitoredEntityHistoryRow(
                source_name="product_monitor",
                entity_key={"product_id": "P-1"},
                attributes={"price": 100},
                effective_from=datetime(2026, 1, 10, tzinfo=timezone.utc),
                effective_to=datetime(2026, 1, 1, tzinfo=timezone.utc),
                is_current=False,
            )

    def test_active_row_with_effective_to_raises(self) -> None:
        with pytest.raises(ValueError, match="Active row .* must have effective_to = None"):
            MonitoredEntityHistoryRow(
                source_name="product_monitor",
                entity_key={"product_id": "P-1"},
                attributes={"price": 100},
                effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
                effective_to=datetime(2026, 1, 10, tzinfo=timezone.utc),
                is_current=True,
            )

    def test_closed_row_with_none_effective_to_raises(self) -> None:
        with pytest.raises(ValueError, match="Closed row .* must have a non-null effective_to"):
            MonitoredEntityHistoryRow(
                source_name="product_monitor",
                entity_key={"product_id": "P-1"},
                attributes={"price": 100},
                effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
                effective_to=None,
                is_current=False,
            )

    def test_composite_key_tuple(self) -> None:
        row = MonitoredEntityHistoryRow(
            source_name="order_monitor",
            entity_key={"store_id": "S10", "item_id": "I20"},
            attributes={"qty": 5},
            effective_from=datetime.now(timezone.utc),
            is_current=True,
        )
        assert row.business_key_tuple == ("I20", "S10")

    def test_to_dict_and_from_dict_roundtrip(self) -> None:
        row = MonitoredEntityHistoryRow(
            source_name="product_monitor",
            entity_key={"product_id": "P-99"},
            attributes={"price": 49.99, "tier": "silver"},
            effective_from=datetime(2026, 2, 1, tzinfo=timezone.utc),
            is_current=True,
        )
        d = row.to_dict()
        assert d["source_name"] == "product_monitor"
        assert d["entity_key"] == {"product_id": "P-99"}
        assert d["attributes"] == {"price": 49.99, "tier": "silver"}
        assert d["is_current"] is True

        # from_dict with stringified JSON (like DB JSONB drivers can return)
        d["entity_key"] = json.dumps(d["entity_key"])
        d["attributes"] = json.dumps(d["attributes"])
        row2 = MonitoredEntityHistoryRow.from_dict(d)
        assert row2.history_id == row.history_id
        assert row2.entity_key == {"product_id": "P-99"}
        assert row2.attributes == {"price": 49.99, "tier": "silver"}


class TestGenericHistoryDataFrameAdaptation:
    """Tests for generic_history_rows_to_target_df()."""

    def test_empty_rows_returns_typed_empty_df(self) -> None:
        df = generic_history_rows_to_target_df(
            [],
            key_columns=["org_id", "user_id"],
            tracked_columns=["role", "score"],
        )
        assert df.is_empty()
        expected_cols = ["org_id", "user_id", "role", "score", "effective_from", "effective_to", "is_current"]
        assert df.columns == expected_cols
        assert df.schema["effective_from"] == pl.Date
        assert df.schema["effective_to"] == pl.Date
        assert df.schema["is_current"] == pl.Boolean

    def test_populated_rows_with_dynamic_casts(self) -> None:
        rows = [
            MonitoredEntityHistoryRow(
                source_name="users",
                entity_key={"user_id": "U-1"},
                attributes={"role": "admin", "score": 95},
                effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
                effective_to=datetime(2026, 1, 15, tzinfo=timezone.utc),
                is_current=False,
            ),
            MonitoredEntityHistoryRow(
                source_name="users",
                entity_key={"user_id": "U-1"},
                attributes={"role": "superadmin", "score": 100},
                effective_from=datetime(2026, 1, 15, tzinfo=timezone.utc),
                effective_to=None,
                is_current=True,
            ),
        ]
        df = generic_history_rows_to_target_df(
            rows,
            key_columns=["user_id"],
            tracked_columns=["role", "score"],
        )
        assert len(df) == 2
        assert df.schema["user_id"] == pl.String
        assert df.schema["role"] == pl.String
        assert df.schema["score"] == pl.Int64
        assert df.schema["effective_from"] == pl.Date
        assert df.schema["effective_to"] == pl.Date
        assert df.schema["is_current"] == pl.Boolean

        dicts = df.to_dicts()
        assert dicts[0]["effective_from"] == date(2026, 1, 1)
        assert dicts[0]["effective_to"] == date(2026, 1, 15)
        assert dicts[0]["is_current"] is False
        assert dicts[1]["effective_to"] is None
        assert dicts[1]["is_current"] is True


class TestMonitoredEntityHistoryRepositoryUnit:
    """Unit tests for repository validation and error wrapping with mocks."""

    def test_insert_empty_returns_zero(self) -> None:
        mock_db = MagicMock()
        repo = MonitoredEntityHistoryRepository(db=mock_db)
        assert repo.insert_history_rows([]) == 0
        mock_db.get_connection.assert_not_called()

    def test_fetch_current_empty_keys_returns_empty(self) -> None:
        mock_db = MagicMock()
        repo = MonitoredEntityHistoryRepository(db=mock_db)
        assert repo.fetch_current_history_for_keys("src", []) == []
        mock_db.get_connection.assert_not_called()

    def test_close_empty_keys_returns_zero(self) -> None:
        mock_db = MagicMock()
        repo = MonitoredEntityHistoryRepository(db=mock_db)
        assert repo.close_current_versions("src", [], datetime.now(timezone.utc)) == 0
        mock_db.get_connection.assert_not_called()
