"""Data-access repository for suspicious held batches in held_change_batch."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
from threading import RLock
from typing import Any, Optional
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from ..exceptions import EntityNotFoundError
from ..models import HeldChangeBatchRow, HoldStatus
from .base import BaseRepository

logger = logging.getLogger("scd2_copilot.db.hold")


def _safe_json_dumps(obj: Any) -> str:
    return json.dumps(obj, default=lambda x: float(x) if isinstance(x, Decimal) else str(x))


class HeldChangeBatchRepository(BaseRepository):
    """Repository for persisting, tracking, and resolving held change batches."""

    TABLE_NAME = "held_change_batch"

    def __init__(
        self,
        db: Optional[Any] = None,
        *,
        in_memory: bool = False,
    ) -> None:
        super().__init__(db=db)
        self._in_memory = in_memory or (not self.db.is_configured)
        self._lock = RLock()
        self._memory_holds: dict[UUID, HeldChangeBatchRow] = {}

    @property
    def in_memory_mode(self) -> bool:
        """Return True if running in in-memory mode."""
        return self._in_memory

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

        if self._in_memory:
            with self._lock:
                held_row = HeldChangeBatchRow(
                    hold_id=active_hold_id,
                    run_id=run_id,
                    source_name=source_name,
                    severity=severity,
                    reason=reason,
                    records_affected=records_affected,
                    evidence=dict(evidence),
                    status=status,
                    created_at=now,
                    resolved_at=None,
                )
                self._memory_holds[active_hold_id] = held_row
                return held_row

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
            Jsonb(evidence, dumps=_safe_json_dumps),
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
        if self._in_memory:
            with self._lock:
                return self._memory_holds.get(hold_id)

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
        if self._in_memory:
            with self._lock:
                holds = list(self._memory_holds.values())
                if status:
                    holds = [h for h in holds if h.status == status]
                if source_name:
                    holds = [h for h in holds if h.source_name == source_name]
                holds.sort(key=lambda h: h.created_at, reverse=True)
                return holds[:limit]

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

        if self._in_memory:
            with self._lock:
                existing = self._memory_holds.get(hold_id)
                if not existing:
                    raise EntityNotFoundError(f"Held change batch '{hold_id}' not found", table=self.TABLE_NAME)
                updated = HeldChangeBatchRow(
                    hold_id=existing.hold_id,
                    run_id=existing.run_id,
                    source_name=existing.source_name,
                    severity=existing.severity,
                    reason=existing.reason,
                    records_affected=existing.records_affected,
                    evidence=existing.evidence,
                    status=status,
                    created_at=existing.created_at,
                    resolved_at=res_time,
                )
                self._memory_holds[hold_id] = updated
                return updated

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
        if self._in_memory:
            with self._lock:
                existing = self._memory_holds.get(hold_id)
                if not existing:
                    raise EntityNotFoundError(f"Held change batch '{hold_id}' not found", table=self.TABLE_NAME)
                updated = HeldChangeBatchRow(
                    hold_id=existing.hold_id,
                    run_id=existing.run_id,
                    source_name=existing.source_name,
                    severity=existing.severity,
                    reason=existing.reason,
                    records_affected=existing.records_affected,
                    evidence=dict(evidence),
                    status=existing.status,
                    created_at=existing.created_at,
                    resolved_at=existing.resolved_at,
                )
                self._memory_holds[hold_id] = updated
                return updated

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
        if self._in_memory:
            with self._lock:
                holds = [h for h in self._memory_holds.values() if h.status == HoldStatus.HELD.value]
                if source_name:
                    holds = [h for h in holds if h.source_name == source_name]
                return len(holds)

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
        if self._in_memory:
            with self._lock:
                counts = {"held": 0, "released": 0, "discarded": 0, "reprocessed": 0, "total": 0}
                for h in self._memory_holds.values():
                    if source_name and h.source_name != source_name:
                        continue
                    st_val = str(h.status).lower()
                    counts["total"] += 1
                    if st_val in counts:
                        counts[st_val] += 1
                return counts

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


