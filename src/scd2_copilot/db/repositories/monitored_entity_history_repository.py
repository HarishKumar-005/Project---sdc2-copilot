"""Data-access repository for generic SCD Type 2 table in monitored_entity_history."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from threading import RLock
from typing import Any, Optional

import psycopg

from ..connection import DatabaseManager
from ..models import MonitoredEntityHistoryRow, _ensure_utc
from .base import BaseRepository

logger = logging.getLogger("scd2_copilot.db.entity_history")


class MonitoredEntityHistoryRepository(BaseRepository):
    """Repository for persisting and querying domain-agnostic SCD Type 2 history."""

    TABLE_NAME = "monitored_entity_history"

    def __init__(
        self,
        db: Optional[DatabaseManager] = None,
        *,
        in_memory: bool = False,
    ) -> None:
        super().__init__(db=db)
        self._in_memory = in_memory or (not self.db.is_configured)
        self._lock = RLock()
        self._memory_rows: list[MonitoredEntityHistoryRow] = []

    @property
    def in_memory_mode(self) -> bool:
        """Return True if running in in-memory mode."""
        return self._in_memory

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

        if self._in_memory:
            with self._lock:
                self._memory_rows.extend(rows)
                return len(rows)

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

        if self._in_memory:
            with self._lock:
                matching = [
                    r
                    for r in self._memory_rows
                    if r.source_name == source_name
                    and r.is_current
                    and r.entity_key in entity_keys
                ]
                return sorted(
                    matching,
                    key=lambda r: _ensure_utc(r.effective_from)
                    or datetime.min.replace(tzinfo=timezone.utc),
                )

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
        if self._in_memory:
            with self._lock:
                matching = [
                    r
                    for r in self._memory_rows
                    if r.source_name == source_name and r.entity_key == entity_key
                ]
                return sorted(
                    matching,
                    key=lambda r: _ensure_utc(r.effective_from)
                    or datetime.min.replace(tzinfo=timezone.utc),
                )

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

    def fetch_history_at_timestamp(
        self,
        source_name: str,
        entity_key: dict[str, Any],
        as_of: datetime,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> Optional[MonitoredEntityHistoryRow]:
        """Fetch active historical record for key at specific timestamp under [effective_from, effective_to) half-open semantics."""
        if self._in_memory:
            with self._lock:
                as_of_utc = _ensure_utc(as_of)
                matching = []
                for r in self._memory_rows:
                    if r.source_name == source_name and r.entity_key == entity_key:
                        eff_from = _ensure_utc(r.effective_from)
                        eff_to = _ensure_utc(r.effective_to)
                        if eff_from is not None and as_of_utc is not None and eff_from <= as_of_utc:
                            if eff_to is None or eff_to > as_of_utc:
                                matching.append(r)
                if not matching:
                    return None
                matching.sort(
                    key=lambda r: _ensure_utc(r.effective_from)
                    or datetime.min.replace(tzinfo=timezone.utc),
                    reverse=True,
                )
                return matching[0]

        query = f"""
            SELECT history_id, source_name, entity_key, attributes,
                   effective_from, effective_to, is_current, created_at
            FROM {self.TABLE_NAME}
            WHERE source_name = %s
              AND entity_key = %s::jsonb
              AND effective_from <= %s
              AND (effective_to > %s OR effective_to IS NULL)
            ORDER BY effective_from DESC
            LIMIT 1;
        """
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query, [source_name, json.dumps(entity_key), as_of, as_of])
                    row = cur.fetchone()
            return MonitoredEntityHistoryRow.from_dict(row) if row else None
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

        if self._in_memory:
            with self._lock:
                count = 0
                new_list = []
                for r in self._memory_rows:
                    if (
                        r.source_name == source_name
                        and r.is_current
                        and r.entity_key in entity_keys
                    ):
                        closed_r = MonitoredEntityHistoryRow(
                            history_id=r.history_id,
                            source_name=r.source_name,
                            entity_key=r.entity_key,
                            attributes=r.attributes,
                            effective_from=r.effective_from,
                            effective_to=closed_at,
                            is_current=False,
                            created_at=r.created_at,
                        )
                        new_list.append(closed_r)
                        count += 1
                    else:
                        new_list.append(r)
                self._memory_rows = new_list
                return count

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

    def get_all_rows(
        self,
        source_name: Optional[str] = None,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> list[MonitoredEntityHistoryRow]:
        """Fetch all rows from monitored_entity_history table, optionally filtered by source_name."""
        if self._in_memory:
            with self._lock:
                if source_name:
                    return [r for r in self._memory_rows if r.source_name == source_name]
                return list(self._memory_rows)

        where_clause = "WHERE source_name = %s" if source_name else ""
        params = [source_name] if source_name else []
        query = f"""
            SELECT history_id, source_name, entity_key, attributes,
                   effective_from, effective_to, is_current, created_at
            FROM {self.TABLE_NAME}
            {where_clause}
            ORDER BY effective_from ASC;
        """
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query, params)
                    rows = cur.fetchall()
            return [MonitoredEntityHistoryRow.from_dict(r) for r in rows]
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc


