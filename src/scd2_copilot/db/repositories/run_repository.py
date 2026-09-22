"""Data-access repository for execution audit runs in processing_run."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from threading import RLock
from typing import Any, Optional
from uuid import UUID, uuid4

import psycopg

from ..exceptions import EntityNotFoundError
from ..models import ProcessingRunRow, RunStatus
from .base import BaseRepository

logger = logging.getLogger("scd2_copilot.db.run")


class ProcessingRunRepository(BaseRepository):
    """Repository for persisting and managing processing run lifecycle records."""

    TABLE_NAME = "processing_run"

    def __init__(
        self,
        db: Optional[Any] = None,
        *,
        in_memory: bool = False,
    ) -> None:
        super().__init__(db=db)
        self._in_memory = in_memory or (not self.db.is_configured)
        self._lock = RLock()
        self._memory_runs: dict[UUID, ProcessingRunRow] = {}

    @property
    def in_memory_mode(self) -> bool:
        """Return True if running in in-memory mode."""
        return self._in_memory

    def create_run(
        self,
        source_name: str,
        run_id: Optional[UUID] = None,
        started_at: Optional[datetime] = None,
        status: str = RunStatus.PROCESSING.value,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> ProcessingRunRow:
        """Create an initial run entry in PROCESSING (or specified) status."""
        active_run_id = run_id or uuid4()
        now = started_at or datetime.now(timezone.utc)

        if self._in_memory:
            with self._lock:
                run_row = ProcessingRunRow(
                    run_id=active_run_id,
                    source_name=source_name,
                    status=status,
                    started_at=now,
                    completed_at=None,
                    records_seen=0,
                    records_changed=0,
                    records_held=0,
                    error_message=None,
                    created_at=now,
                )
                self._memory_runs[active_run_id] = run_row
                return run_row

        query = f"""
            INSERT INTO {self.TABLE_NAME} (
                run_id, source_name, status, started_at, records_seen,
                records_changed, records_held, error_message, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING run_id, source_name, status, started_at, completed_at,
                      records_seen, records_changed, records_held, error_message, created_at;
        """
        params = (
            active_run_id,
            source_name,
            status,
            now,
            0,
            0,
            0,
            None,
            now,
        )
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query, params)
                    row = cur.fetchone()
                if conn is None:
                    c.commit()
            if not row:
                raise EntityNotFoundError(f"Failed to create run '{active_run_id}'")
            return ProcessingRunRow.from_dict(row)
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc

    def mark_run_processing(
        self,
        run_id: UUID,
        started_at: Optional[datetime] = None,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> ProcessingRunRow:
        """Explicitly ensure run status is PROCESSING."""
        now = started_at or datetime.now(timezone.utc)
        if self._in_memory:
            with self._lock:
                existing = self._memory_runs.get(run_id)
                if not existing:
                    raise EntityNotFoundError(f"Processing run '{run_id}' not found", table=self.TABLE_NAME)
                updated = ProcessingRunRow(
                    run_id=existing.run_id,
                    source_name=existing.source_name,
                    status=RunStatus.PROCESSING.value,
                    started_at=now,
                    completed_at=existing.completed_at,
                    records_seen=existing.records_seen,
                    records_changed=existing.records_changed,
                    records_held=existing.records_held,
                    error_message=existing.error_message,
                    created_at=existing.created_at,
                )
                self._memory_runs[run_id] = updated
                return updated

        query = f"""
            UPDATE {self.TABLE_NAME}
            SET status = %s, started_at = %s
            WHERE run_id = %s
            RETURNING run_id, source_name, status, started_at, completed_at,
                      records_seen, records_changed, records_held, error_message, created_at;
        """
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query, (RunStatus.PROCESSING.value, now, run_id))
                    row = cur.fetchone()
                if conn is None:
                    c.commit()
            if not row:
                raise EntityNotFoundError(f"Processing run '{run_id}' not found", table=self.TABLE_NAME)
            return ProcessingRunRow.from_dict(row)
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc

    mark_run_started = mark_run_processing

    def mark_run_committed(
        self,
        run_id: UUID,
        records_seen: int,
        records_changed: int,
        records_held: int,
        completed_at: Optional[datetime] = None,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> ProcessingRunRow:
        """Mark a run as COMMITTED with execution record metrics."""
        now = completed_at or datetime.now(timezone.utc)
        if self._in_memory:
            with self._lock:
                existing = self._memory_runs.get(run_id)
                if not existing:
                    raise EntityNotFoundError(f"Processing run '{run_id}' not found", table=self.TABLE_NAME)
                updated = ProcessingRunRow(
                    run_id=existing.run_id,
                    source_name=existing.source_name,
                    status=RunStatus.COMMITTED.value,
                    started_at=existing.started_at,
                    completed_at=now,
                    records_seen=records_seen,
                    records_changed=records_changed,
                    records_held=records_held,
                    error_message=None,
                    created_at=existing.created_at,
                )
                self._memory_runs[run_id] = updated
                return updated

        query = f"""
            UPDATE {self.TABLE_NAME}
            SET status = %s,
                completed_at = %s,
                records_seen = %s,
                records_changed = %s,
                records_held = %s,
                error_message = NULL
            WHERE run_id = %s
            RETURNING run_id, source_name, status, started_at, completed_at,
                      records_seen, records_changed, records_held, error_message, created_at;
        """
        params = (
            RunStatus.COMMITTED.value,
            now,
            records_seen,
            records_changed,
            records_held,
            run_id,
        )
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query, params)
                    row = cur.fetchone()
                if conn is None:
                    c.commit()
            if not row:
                raise EntityNotFoundError(f"Processing run '{run_id}' not found", table=self.TABLE_NAME)
            return ProcessingRunRow.from_dict(row)
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc

    mark_run_completed = mark_run_committed

    def mark_run_held(
        self,
        run_id: UUID,
        records_seen: int,
        records_held: int,
        completed_at: Optional[datetime] = None,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> ProcessingRunRow:
        """Mark a run as HELD with execution record metrics and zero changes committed."""
        now = completed_at or datetime.now(timezone.utc)
        if self._in_memory:
            with self._lock:
                existing = self._memory_runs.get(run_id)
                if not existing:
                    raise EntityNotFoundError(f"Processing run '{run_id}' not found", table=self.TABLE_NAME)
                updated = ProcessingRunRow(
                    run_id=existing.run_id,
                    source_name=existing.source_name,
                    status=RunStatus.HELD.value,
                    started_at=existing.started_at,
                    completed_at=now,
                    records_seen=records_seen,
                    records_changed=0,
                    records_held=records_held,
                    error_message=None,
                    created_at=existing.created_at,
                )
                self._memory_runs[run_id] = updated
                return updated

        query = f"""
            UPDATE {self.TABLE_NAME}
            SET status = %s,
                completed_at = %s,
                records_seen = %s,
                records_changed = %s,
                records_held = %s,
                error_message = NULL
            WHERE run_id = %s
            RETURNING run_id, source_name, status, started_at, completed_at,
                      records_seen, records_changed, records_held, error_message, created_at;
        """
        params = (
            RunStatus.HELD.value,
            now,
            records_seen,
            0,
            records_held,
            run_id,
        )
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query, params)
                    row = cur.fetchone()
                if conn is None:
                    c.commit()
            if not row:
                raise EntityNotFoundError(f"Processing run '{run_id}' not found", table=self.TABLE_NAME)
            return ProcessingRunRow.from_dict(row)
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc

    def mark_run_failed(
        self,
        run_id: UUID,
        error_message: str,
        completed_at: Optional[datetime] = None,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> ProcessingRunRow:
        """Mark a run as FAILED with an error message."""
        now = completed_at or datetime.now(timezone.utc)
        clean_error = error_message.strip() if error_message else "Unknown execution error"
        if self._in_memory:
            with self._lock:
                existing = self._memory_runs.get(run_id)
                if not existing:
                    raise EntityNotFoundError(f"Processing run '{run_id}' not found", table=self.TABLE_NAME)
                updated = ProcessingRunRow(
                    run_id=existing.run_id,
                    source_name=existing.source_name,
                    status=RunStatus.FAILED.value,
                    started_at=existing.started_at,
                    completed_at=now,
                    records_seen=existing.records_seen,
                    records_changed=existing.records_changed,
                    records_held=existing.records_held,
                    error_message=clean_error,
                    created_at=existing.created_at,
                )
                self._memory_runs[run_id] = updated
                return updated

        query = f"""
            UPDATE {self.TABLE_NAME}
            SET status = %s,
                completed_at = %s,
                error_message = %s
            WHERE run_id = %s
            RETURNING run_id, source_name, status, started_at, completed_at,
                      records_seen, records_changed, records_held, error_message, created_at;
        """
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query, (RunStatus.FAILED.value, now, clean_error, run_id))
                    row = cur.fetchone()
                if conn is None:
                    c.commit()
            if not row:
                raise EntityNotFoundError(f"Processing run '{run_id}' not found", table=self.TABLE_NAME)
            return ProcessingRunRow.from_dict(row)
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc

    def get_run(
        self,
        run_id: UUID,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> Optional[ProcessingRunRow]:
        """Fetch a specific processing run by its ID."""
        if self._in_memory:
            with self._lock:
                return self._memory_runs.get(run_id)

        query = f"""
            SELECT run_id, source_name, status, started_at, completed_at,
                   records_seen, records_changed, records_held, error_message, created_at
            FROM {self.TABLE_NAME}
            WHERE run_id = %s;
        """
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query, (run_id,))
                    row = cur.fetchone()
            return ProcessingRunRow.from_dict(row) if row else None
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc

    def get_recent_runs(
        self,
        limit: int = 20,
        source_name: Optional[str] = None,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> list[ProcessingRunRow]:
        """Fetch the most recent processing runs ordered by started_at DESC."""
        if self._in_memory:
            with self._lock:
                runs = list(self._memory_runs.values())
                if source_name:
                    runs = [r for r in runs if r.source_name == source_name]
                runs.sort(key=lambda r: (r.started_at or r.created_at, r.created_at), reverse=True)
                return runs[:limit]

        where_clause = "WHERE source_name = %s" if source_name else ""
        params: list[Any] = [source_name] if source_name else []

        query = f"""
            SELECT run_id, source_name, status, started_at, completed_at,
                   records_seen, records_changed, records_held, error_message, created_at
            FROM {self.TABLE_NAME}
            {where_clause}
            ORDER BY started_at DESC, created_at DESC
            LIMIT {int(limit)};
        """
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query, params)
                    row = cur.fetchall()
            return [ProcessingRunRow.from_dict(r) for r in row]
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc

    def get_run_counts(
        self,
        source_name: Optional[str] = None,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> dict[str, int]:
        """Return counts of runs grouped by status, plus total and record aggregates."""
        if self._in_memory:
            with self._lock:
                counts = {
                    "total": 0,
                    "processing": 0,
                    "committed": 0,
                    "held": 0,
                    "failed": 0,
                    "total_records_processed": 0,
                    "total_records_changed": 0,
                    "total_records_held": 0,
                }
                for r in self._memory_runs.values():
                    if source_name and r.source_name != source_name:
                        continue
                    st = str(r.status).lower()
                    counts["total"] += 1
                    if st in counts:
                        counts[st] += 1
                    counts["total_records_processed"] += r.records_seen
                    counts["total_records_changed"] += r.records_changed
                    counts["total_records_held"] += r.records_held
                return counts

        where_clause = "WHERE source_name = %s" if source_name else ""
        params: list[Any] = [source_name] if source_name else []

        query = f"""
            SELECT status, COUNT(*) AS count,
                   COALESCE(SUM(records_seen), 0) AS total_seen,
                   COALESCE(SUM(records_changed), 0) AS total_changed,
                   COALESCE(SUM(records_held), 0) AS total_held
            FROM {self.TABLE_NAME}
            {where_clause}
            GROUP BY status;
        """
        counts = {
            "total": 0,
            "processing": 0,
            "committed": 0,
            "held": 0,
            "failed": 0,
            "total_records_processed": 0,
            "total_records_changed": 0,
            "total_records_held": 0,
        }
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query, params)
                    rows = cur.fetchall()
            for r in rows:
                st_val = str(r["status"]).lower()
                cnt = int(r["count"])
                counts["total"] += cnt
                if st_val in counts:
                    counts[st_val] = cnt
                counts["total_records_processed"] += int(r["total_seen"])
                counts["total_records_changed"] += int(r["total_changed"])
                counts["total_records_held"] += int(r["total_held"])
            return counts
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc

