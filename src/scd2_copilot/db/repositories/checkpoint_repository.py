"""Data-access repository for ingestion checkpoints in processing_checkpoint."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any, Optional
from uuid import UUID

import psycopg

from ..models import ProcessingCheckpointRow
from .base import BaseRepository

logger = logging.getLogger("scd2_copilot.db.checkpoint")


class CheckpointRepository(BaseRepository):
    """Repository for stream watermarks and recovery checkpoints."""

    TABLE_NAME = "processing_checkpoint"

    def get_checkpoint(
        self,
        source_name: str,
        table_name: str = "inventory_source",
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> Optional[ProcessingCheckpointRow]:
        """Fetch the active checkpoint for a given stream and table."""
        query = f"""
            SELECT source_name, table_name, watermark_value, last_successful_run_id, updated_at, cursor_keys
            FROM {self.TABLE_NAME}
            WHERE source_name = %s AND table_name = %s;
        """
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query, (source_name, table_name))
                    row = cur.fetchone()
            return ProcessingCheckpointRow.from_dict(row) if row else None
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc

    def get_latest_checkpoint(
        self,
        table_name: Optional[str] = None,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> Optional[ProcessingCheckpointRow]:
        """Fetch the most recently updated checkpoint across all streams (or matching table_name)."""
        where_clause = "WHERE table_name = %s" if table_name else ""
        params: list[Any] = [table_name] if table_name else []
        query = f"""
            SELECT source_name, table_name, watermark_value, last_successful_run_id, updated_at, cursor_keys
            FROM {self.TABLE_NAME}
            {where_clause}
            ORDER BY updated_at DESC
            LIMIT 1;
        """
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query, params)
                    row = cur.fetchone()
            return ProcessingCheckpointRow.from_dict(row) if row else None
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc

    def initialize_checkpoint_if_missing(
        self,
        source_name: str,
        table_name: str = "inventory_source",
        watermark: Optional[datetime] = None,
        run_id: Optional[UUID] = None,
        cursor_keys: Optional[dict[str, Any]] = None,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> ProcessingCheckpointRow:
        """Ensure a checkpoint exists for the stream, inserting an initial row if absent."""
        import json

        existing = self.get_checkpoint(source_name, table_name=table_name, conn=conn)
        if existing is not None:
            return existing

        now = datetime.now(timezone.utc)
        cursor_json = json.dumps(cursor_keys) if cursor_keys is not None else None
        query = f"""
            INSERT INTO {self.TABLE_NAME} (
                source_name, table_name, watermark_value, last_successful_run_id, updated_at, cursor_keys
            ) VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_name, table_name) DO NOTHING
            RETURNING source_name, table_name, watermark_value, last_successful_run_id, updated_at, cursor_keys;
        """
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query, (source_name, table_name, watermark, run_id, now, cursor_json))
                    row = cur.fetchone()
                if conn is None:
                    c.commit()

            if row:
                return ProcessingCheckpointRow.from_dict(row)
            # If inserted concurrently by another process, fetch and return
            rechecked = self.get_checkpoint(source_name, table_name=table_name, conn=conn)
            if rechecked:
                return rechecked
            return ProcessingCheckpointRow(
                source_name=source_name,
                table_name=table_name,
                watermark_value=watermark,
                last_successful_run_id=run_id,
                updated_at=now,
                cursor_keys=cursor_keys,
            )
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc

    def update_checkpoint(
        self,
        source_name: str,
        watermark: datetime,
        run_id: Optional[UUID] = None,
        cursor_keys: Optional[dict[str, Any]] = None,
        table_name: str = "inventory_source",
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> ProcessingCheckpointRow:
        """Atomically update the stream watermark, composite cursor keys, and last successful run ID."""
        import json

        now = datetime.now(timezone.utc)
        cursor_json = json.dumps(cursor_keys) if cursor_keys is not None else None
        query = f"""
            INSERT INTO {self.TABLE_NAME} (
                source_name, table_name, watermark_value, last_successful_run_id, updated_at, cursor_keys
            ) VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_name, table_name) DO UPDATE SET
                watermark_value = EXCLUDED.watermark_value,
                last_successful_run_id = COALESCE(EXCLUDED.last_successful_run_id, processing_checkpoint.last_successful_run_id),
                updated_at = EXCLUDED.updated_at,
                cursor_keys = EXCLUDED.cursor_keys
            RETURNING source_name, table_name, watermark_value, last_successful_run_id, updated_at, cursor_keys;
        """
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query, (source_name, table_name, watermark, run_id, now, cursor_json))
                    row = cur.fetchone()
                if conn is None:
                    c.commit()
            return ProcessingCheckpointRow.from_dict(row)  # type: ignore[arg-type]
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc
