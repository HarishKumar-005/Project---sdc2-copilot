"""Data-access repository for the SCD Type 2 table in inventory_history."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any, Optional

import psycopg

from ..models import InventoryHistoryRow
from .base import BaseRepository

logger = logging.getLogger("scd2_copilot.db.history")


class InventoryHistoryRepository(BaseRepository):
    """Repository for persisting and querying SCD Type 2 historical inventory state."""

    TABLE_NAME = "inventory_history"

    def insert_history_rows(
        self,
        rows: list[InventoryHistoryRow],
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> int:
        """Insert historical SCD Type 2 rows.

        Validates temporal consistency and executes batch insert.
        """
        if not rows:
            return 0

        # Validate rows adhere to temporal SCD2 invariants before inserting
        for r in rows:
            if r.effective_to is not None and r.effective_to < r.effective_from:
                raise ValueError(
                    f"Invalid temporal interval for {r.business_key}: "
                    f"effective_from ({r.effective_from}) > effective_to ({r.effective_to})"
                )
            if r.is_current and r.effective_to is not None:
                raise ValueError(f"Active row {r.business_key} cannot have non-null effective_to")
            if not r.is_current and r.effective_to is None:
                raise ValueError(f"Closed row {r.business_key} must have non-null effective_to")

        query = f"""
            INSERT INTO {self.TABLE_NAME} (
                history_id, sku_id, warehouse_id, quantity_on_hand, reorder_level,
                status, effective_from, effective_to, is_current, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
        """
        params = [
            (
                r.history_id,
                r.sku_id,
                r.warehouse_id,
                r.quantity_on_hand,
                r.reorder_level,
                r.status,
                r.effective_from,
                r.effective_to,
                r.is_current,
                r.created_at,
            )
            for r in rows
        ]
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.executemany(query, params)
                if conn is None:
                    c.commit()
            return len(rows)
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc

    def fetch_current_history_for_keys(
        self,
        keys: list[tuple[str, str]],
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> list[InventoryHistoryRow]:
        """Fetch active target history records (is_current = True) for the specified keys."""
        if not keys:
            return []

        placeholders = ", ".join(["(%s, %s)"] * len(keys))
        params: list[Any] = [part for key_tuple in keys for part in key_tuple]

        query = f"""
            SELECT history_id, sku_id, warehouse_id, quantity_on_hand, reorder_level,
                   status, effective_from, effective_to, is_current, created_at
            FROM {self.TABLE_NAME}
            WHERE is_current = TRUE
              AND (sku_id, warehouse_id) IN ({placeholders})
            ORDER BY sku_id ASC, warehouse_id ASC;
        """
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query, params)
                    rows = cur.fetchall()
            return [InventoryHistoryRow.from_dict(r) for r in rows]
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc

    def fetch_history_for_keys(
        self,
        keys: list[tuple[str, str]],
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> list[InventoryHistoryRow]:
        """Fetch full chronological history for the specified business keys."""
        if not keys:
            return []

        placeholders = ", ".join(["(%s, %s)"] * len(keys))
        params: list[Any] = [part for key_tuple in keys for part in key_tuple]

        query = f"""
            SELECT history_id, sku_id, warehouse_id, quantity_on_hand, reorder_level,
                   status, effective_from, effective_to, is_current, created_at
            FROM {self.TABLE_NAME}
            WHERE (sku_id, warehouse_id) IN ({placeholders})
            ORDER BY sku_id ASC, warehouse_id ASC, effective_from ASC;
        """
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query, params)
                    rows = cur.fetchall()
            return [InventoryHistoryRow.from_dict(r) for r in rows]
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc

    def close_current_versions(
        self,
        keys: list[tuple[str, str]],
        closed_at: datetime,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> int:
        """Close active versions for the given keys by setting is_current = False and effective_to = closed_at."""
        if not keys:
            return 0

        placeholders = ", ".join(["(%s, %s)"] * len(keys))
        params: list[Any] = [closed_at] + [part for key_tuple in keys for part in key_tuple]

        query = f"""
            UPDATE {self.TABLE_NAME}
            SET is_current = FALSE,
                effective_to = %s
            WHERE is_current = TRUE
              AND (sku_id, warehouse_id) IN ({placeholders});
        """
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query, params)
                    count = cur.rowcount
                if conn is None:
                    c.commit()
            return count
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc
