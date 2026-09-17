"""Data-access repository for suspicious held batches in held_change_batch."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any, Optional
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from ..exceptions import EntityNotFoundError
from ..models import HeldChangeBatchRow, HoldStatus
from .base import BaseRepository

logger = logging.getLogger("scd2_copilot.db.hold")


class HeldChangeBatchRepository(BaseRepository):
    """Repository for persisting, tracking, and resolving held change batches."""

    TABLE_NAME = "held_change_batch"

    def create_hold(
        self,
        run_id: UUID,
        source_name: str,
        severity: str,
        reason: str,
        records_affected: int,
        evidence: dict[str, Any],
        status: str = HoldStatus.HELD.value,
        hold_id: Optional[UUID] = None,
        created_at: Optional[datetime] = None,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> HeldChangeBatchRow:
        """Create a new quarantine/hold record for a suspicious change batch."""
        active_hold_id = hold_id or uuid4()
        now = created_at or datetime.now(timezone.utc)

        query = f"""
            INSERT INTO {self.TABLE_NAME} (
                hold_id, run_id, source_name, severity, reason,
                records_affected, evidence, status, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING hold_id, run_id, source_name, severity, reason,
                      records_affected, evidence, status, created_at, resolved_at;
        """
        params = (
            active_hold_id,
            run_id,
            source_name,
            severity,
            reason,
            records_affected,
            Jsonb(evidence),
            status,
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
                raise EntityNotFoundError(f"Failed to create hold '{active_hold_id}'")
            return HeldChangeBatchRow.from_dict(row)
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc

    def get_hold(
        self,
        hold_id: UUID,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> Optional[HeldChangeBatchRow]:
        """Fetch a specific held change batch by its UUID."""
        query = f"""
            SELECT hold_id, run_id, source_name, severity, reason,
                   records_affected, evidence, status, created_at, resolved_at
            FROM {self.TABLE_NAME}
            WHERE hold_id = %s;
        """
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query, (hold_id,))
                    row = cur.fetchone()
            return HeldChangeBatchRow.from_dict(row) if row else None
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc

    def get_recent_holds(
        self,
        limit: int = 20,
        status: Optional[str] = None,
        source_name: Optional[str] = None,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> list[HeldChangeBatchRow]:
        """Fetch recent held batches with optional status and source filters."""
        where_clauses: list[str] = []
        params: list[Any] = []

        if status:
            where_clauses.append("status = %s")
            params.append(status)
        if source_name:
            where_clauses.append("source_name = %s")
            params.append(source_name)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        query = f"""
            SELECT hold_id, run_id, source_name, severity, reason,
                   records_affected, evidence, status, created_at, resolved_at
            FROM {self.TABLE_NAME}
            {where_sql}
            ORDER BY created_at DESC
            LIMIT {int(limit)};
        """
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query, params)
                    rows = cur.fetchall()
            return [HeldChangeBatchRow.from_dict(r) for r in rows]
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc

    def update_hold_status(
        self,
        hold_id: UUID,
        status: str,
        resolved_at: Optional[datetime] = None,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> HeldChangeBatchRow:
        """Update the resolution status and timestamp of a held change batch."""
        res_time = resolved_at
        if res_time is None and status in (
            HoldStatus.RELEASED.value,
            HoldStatus.DISCARDED.value,
            HoldStatus.REPROCESSED.value,
            HoldStatus.APPROVED.value,
            HoldStatus.REJECTED.value,
        ):
            res_time = datetime.now(timezone.utc)

        query = f"""
            UPDATE {self.TABLE_NAME}
            SET status = %s,
                resolved_at = %s
            WHERE hold_id = %s
            RETURNING hold_id, run_id, source_name, severity, reason,
                      records_affected, evidence, status, created_at, resolved_at;
        """
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query, (status, res_time, hold_id))
                    row = cur.fetchone()
                if conn is None:
                    c.commit()
            if not row:
                raise EntityNotFoundError(f"Held change batch '{hold_id}' not found", table=self.TABLE_NAME)
            return HeldChangeBatchRow.from_dict(row)
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc

    def release_hold(
        self,
        hold_id: UUID,
        resolved_at: Optional[datetime] = None,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> HeldChangeBatchRow:
        """Mark a held batch as RELEASED (released for downstream processing)."""
        return self.update_hold_status(
            hold_id=hold_id,
            status=HoldStatus.RELEASED.value,
            resolved_at=resolved_at,
            conn=conn,
        )

    approve_hold = release_hold

    def discard_hold(
        self,
        hold_id: UUID,
        resolved_at: Optional[datetime] = None,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> HeldChangeBatchRow:
        """Mark a held batch as DISCARDED (permanently quarantined)."""
        return self.update_hold_status(
            hold_id=hold_id,
            status=HoldStatus.DISCARDED.value,
            resolved_at=resolved_at,
            conn=conn,
        )

    reject_hold = discard_hold

    def reprocess_hold(
        self,
        hold_id: UUID,
        resolved_at: Optional[datetime] = None,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> HeldChangeBatchRow:
        """Mark a held batch as REPROCESSED."""
        return self.update_hold_status(
            hold_id=hold_id,
            status=HoldStatus.REPROCESSED.value,
            resolved_at=resolved_at,
            conn=conn,
        )

    def update_hold_evidence(
        self,
        hold_id: UUID,
        evidence: dict[str, Any],
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> HeldChangeBatchRow:
        """Update the JSONB evidence payload for a held change batch."""
        query = f"""
            UPDATE {self.TABLE_NAME}
            SET evidence = %s
            WHERE hold_id = %s
            RETURNING hold_id, run_id, source_name, severity, reason,
                      records_affected, evidence, status, created_at, resolved_at;
        """
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query, (Jsonb(evidence), hold_id))
                    row = cur.fetchone()
                if conn is None:
                    c.commit()
            if not row:
                raise EntityNotFoundError(f"Held change batch '{hold_id}' not found", table=self.TABLE_NAME)
            return HeldChangeBatchRow.from_dict(row)
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc

    def attach_explanation(
        self,
        hold_id: UUID,
        explanation: dict[str, Any],
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> HeldChangeBatchRow:
        """Additively attach explanation metadata into the existing evidence JSONB without overwriting."""
        existing = self.get_hold(hold_id, conn=conn)
        if not existing:
            raise EntityNotFoundError(f"Held change batch '{hold_id}' not found", table=self.TABLE_NAME)
        updated_evidence = dict(existing.evidence or {})
        updated_evidence["explanation"] = explanation
        return self.update_hold_evidence(hold_id=hold_id, evidence=updated_evidence, conn=conn)

    def count_active_holds(
        self,
        source_name: Optional[str] = None,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> int:
        """Count the number of unresolved (status = 'HELD') batches."""
        where_clause = "WHERE status = %s"
        params: list[Any] = [HoldStatus.HELD.value]
        if source_name:
            where_clause += " AND source_name = %s"
            params.append(source_name)

        query = f"SELECT COUNT(*) AS count FROM {self.TABLE_NAME} {where_clause};"
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query, params)
                    row = cur.fetchone()
            return int(row["count"]) if row else 0
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc

    def get_hold_counts(
        self,
        source_name: Optional[str] = None,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> dict[str, int]:
        """Return counts of held batches grouped by status."""
        where_clause = "WHERE source_name = %s" if source_name else ""
        params: list[Any] = [source_name] if source_name else []

        query = f"""
            SELECT status, COUNT(*) AS count
            FROM {self.TABLE_NAME}
            {where_clause}
            GROUP BY status;
        """
        counts = {"held": 0, "released": 0, "discarded": 0, "reprocessed": 0, "total": 0}
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
            return counts
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc


