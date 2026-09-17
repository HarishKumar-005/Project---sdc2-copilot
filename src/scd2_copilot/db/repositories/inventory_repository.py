"""Data-access repository for the operational inventory_source table."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any, Optional

import psycopg

from ..models import InventorySourceRow
from .base import BaseRepository

logger = logging.getLogger("scd2_copilot.db.inventory")


class InventorySourceRepository(BaseRepository):
    """Repository for querying operational inventory records."""

    TABLE_NAME = "inventory_source"

    def health_check(self, conn: Optional[psycopg.Connection[Any]] = None) -> bool:
        """Verify table exists and is readable."""
        query = f"SELECT 1 FROM {self.TABLE_NAME} LIMIT 1;"
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query)
                    cur.fetchone()
            return True
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc

    def fetch_all_inventory(
        self, conn: Optional[psycopg.Connection[Any]] = None
    ) -> list[InventorySourceRow]:
        """Fetch all operational inventory records with deterministic ordering."""
        query = f"""
            SELECT sku_id, warehouse_id, quantity_on_hand, reorder_level, status, updated_at
            FROM {self.TABLE_NAME}
            ORDER BY updated_at ASC, sku_id ASC, warehouse_id ASC;
        """
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query)
                    rows = cur.fetchall()
            return [InventorySourceRow.from_dict(r) for r in rows]
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc

    def fetch_inventory_updated_after(
        self,
        watermark: Optional[datetime],
        limit: Optional[int] = None,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> list[InventorySourceRow]:
        """Fetch inventory updated strictly after the given watermark.

        Deterministic ordering is guaranteed:
        Primary: updated_at ASC
        Tie-breakers: sku_id ASC, warehouse_id ASC

        If watermark is None, returns rows starting from the beginning of history.
        """
        if watermark is not None:
            where_clause = "WHERE updated_at > %s"
            params: list[Any] = [watermark]
        else:
            where_clause = ""
            params = []

        limit_clause = f"LIMIT {int(limit)}" if limit is not None else ""

        query = f"""
            SELECT sku_id, warehouse_id, quantity_on_hand, reorder_level, status, updated_at
            FROM {self.TABLE_NAME}
            {where_clause}
            ORDER BY updated_at ASC, sku_id ASC, warehouse_id ASC
            {limit_clause};
        """
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query, params)
                    rows = cur.fetchall()
            return [InventorySourceRow.from_dict(r) for r in rows]
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc

    def fetch_inventory_for_keys(
        self,
        keys: list[tuple[str, str]],
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> list[InventorySourceRow]:
        """Fetch inventory source records matching specific (sku_id, warehouse_id) keys."""
        if not keys:
            return []

        placeholders = ", ".join(["(%s, %s)"] * len(keys))
        params: list[Any] = [part for key_tuple in keys for part in key_tuple]

        query = f"""
            SELECT sku_id, warehouse_id, quantity_on_hand, reorder_level, status, updated_at
            FROM {self.TABLE_NAME}
            WHERE (sku_id, warehouse_id) IN ({placeholders})
            ORDER BY sku_id ASC, warehouse_id ASC;
        """
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query, params)
                    rows = cur.fetchall()
            return [InventorySourceRow.from_dict(r) for r in rows]
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc

    def upsert_inventory_rows(
        self,
        rows: list[InventorySourceRow],
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> int:
        """Upsert inventory source records (useful for test seeding and simulation)."""
        if not rows:
            return 0

        query = f"""
            INSERT INTO {self.TABLE_NAME} (
                sku_id, warehouse_id, quantity_on_hand, reorder_level, status, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (sku_id, warehouse_id) DO UPDATE SET
                quantity_on_hand = EXCLUDED.quantity_on_hand,
                reorder_level = EXCLUDED.reorder_level,
                status = EXCLUDED.status,
                updated_at = EXCLUDED.updated_at;
        """
        params = [
            (r.sku_id, r.warehouse_id, r.quantity_on_hand, r.reorder_level, r.status, r.updated_at)
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
