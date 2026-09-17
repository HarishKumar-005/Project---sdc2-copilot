"""Data-access repository for generic SCD Type 2 table in monitored_entity_history."""

from __future__ import annotations

from datetime import datetime
import json
import logging
from typing import Any, Optional

import psycopg

from ..models import MonitoredEntityHistoryRow
from .base import BaseRepository

logger = logging.getLogger("scd2_copilot.db.entity_history")


class MonitoredEntityHistoryRepository(BaseRepository):
    """Repository for persisting and querying domain-agnostic SCD Type 2 history."""

    TABLE_NAME = "monitored_entity_history"

    def insert_history_rows(
        self,
        rows: list[MonitoredEntityHistoryRow],
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> int:
        """Insert generic historical SCD Type 2 rows.

        Validates temporal consistency and executes batch insert.
        """
        if not rows:
            return 0

        # Validate rows adhere to temporal SCD2 invariants before inserting
        for r in rows:
            if r.effective_to is not None and r.effective_to < r.effective_from:
                raise ValueError(
                    f"Invalid temporal interval for entity {r.entity_key}: "
                    f"effective_from ({r.effective_from}) > effective_to ({r.effective_to})"
                )
            if r.is_current and r.effective_to is not None:
                raise ValueError(f"Active row {r.entity_key} cannot have non-null effective_to")
            if not r.is_current and r.effective_to is None:
                raise ValueError(f"Closed row {r.entity_key} must have non-null effective_to")

        query = f"""
            INSERT INTO {self.TABLE_NAME} (
                history_id, source_name, entity_key, attributes,
                effective_from, effective_to, is_current, created_at
            ) VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s);
        """
        params = [
            (
                r.history_id,
                r.source_name,
                json.dumps(r.entity_key),
                json.dumps(r.attributes),
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
        source_name: str,
        entity_keys: list[dict[str, Any]],
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> list[MonitoredEntityHistoryRow]:
        """Fetch active target history records (is_current = True) for the specified entity keys."""
        if not entity_keys:
            return []

        keys_json = [json.dumps(k) for k in entity_keys]
        query = f"""
            SELECT history_id, source_name, entity_key, attributes,
                   effective_from, effective_to, is_current, created_at
            FROM {self.TABLE_NAME}
            WHERE source_name = %s
              AND is_current = TRUE
              AND entity_key = ANY(%s::jsonb[])
            ORDER BY effective_from ASC;
        """
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query, [source_name, keys_json])
                    rows = cur.fetchall()
            return [MonitoredEntityHistoryRow.from_dict(r) for r in rows]
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc

    def fetch_history_for_key(
        self,
        source_name: str,
        entity_key: dict[str, Any],
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> list[MonitoredEntityHistoryRow]:
        """Fetch full chronological history for a specific entity business key."""
        query = f"""
            SELECT history_id, source_name, entity_key, attributes,
                   effective_from, effective_to, is_current, created_at
            FROM {self.TABLE_NAME}
            WHERE source_name = %s
              AND entity_key = %s::jsonb
            ORDER BY effective_from ASC;
        """
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query, [source_name, json.dumps(entity_key)])
                    rows = cur.fetchall()
            return [MonitoredEntityHistoryRow.from_dict(r) for r in rows]
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc

    def close_current_versions(
        self,
        source_name: str,
        entity_keys: list[dict[str, Any]],
        closed_at: datetime,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> int:
        """Close active versions for the given entity keys by setting is_current = False and effective_to = closed_at."""
        if not entity_keys:
            return 0

        keys_json = [json.dumps(k) for k in entity_keys]
        query = f"""
            UPDATE {self.TABLE_NAME}
            SET is_current = FALSE,
                effective_to = %s
            WHERE source_name = %s
              AND is_current = TRUE
              AND entity_key = ANY(%s::jsonb[]);
        """
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query, [closed_at, source_name, keys_json])
                    count = cur.rowcount
                if conn is None:
                    c.commit()
            return count
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc
