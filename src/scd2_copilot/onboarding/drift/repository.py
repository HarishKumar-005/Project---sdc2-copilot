"""Repository for persisting and retrieving Schema Drift Reports."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from threading import RLock
from typing import Any, Optional

import psycopg

from ...db.connection import DatabaseManager
from ...db.repositories.base import BaseRepository
from ..exceptions import DriftReportNotFoundError
from ..models.drift import (
    FieldMappingImpact,
    MappingCompatibilityState,
    SchemaDriftEvent,
    SchemaDriftReport,
)

logger = logging.getLogger("scd2_copilot.onboarding.drift.repository")


class SchemaDriftRepository(BaseRepository):
    """Persistence repository for SchemaDriftReport entities."""

    TABLE_NAME = "schema_drift_report"

    def __init__(
        self,
        db: Optional[DatabaseManager] = None,
        *,
        in_memory: bool = False,
    ) -> None:
        super().__init__(db=db)
        self._in_memory = in_memory or (not self.db.is_configured)
        self._lock = RLock()
        self._memory_reports: dict[str, SchemaDriftReport] = {}

    @property
    def in_memory_mode(self) -> bool:
        """Return True if running in in-memory mode."""
        return self._in_memory

    # ── CRUD Operations ──────────────────────────────────────────

    def save(
        self,
        report: SchemaDriftReport,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> SchemaDriftReport:
        """Save a new schema drift report."""
        if self._in_memory:
            with self._lock:
                self._memory_reports[report.report_id] = report
                return report
        return self._save_in_db(report, conn=conn)

    def get_by_id(
        self,
        report_id: str,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> Optional[SchemaDriftReport]:
        """Fetch a drift report by report_id, or None if not found."""
        if self._in_memory:
            with self._lock:
                return self._memory_reports.get(report_id)
        return self._get_by_id_in_db(report_id, conn=conn)

    def get_by_id_required(
        self,
        report_id: str,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> SchemaDriftReport:
        """Fetch a drift report by report_id, raising DriftReportNotFoundError if missing."""
        report = self.get_by_id(report_id, conn=conn)
        if report is None:
            raise DriftReportNotFoundError(
                f"Schema drift report '{report_id}' was not found.",
                details={"report_id": report_id},
            )
        return report

    def list_reports(
        self,
        source_id: Optional[str] = None,
        mapping_version_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> tuple[list[SchemaDriftReport], int]:
        """List schema drift reports with filtering and pagination."""
        if self._in_memory:
            with self._lock:
                reports = list(self._memory_reports.values())
                if source_id:
                    reports = [r for r in reports if r.source_id == source_id]
                if mapping_version_id:
                    reports = [r for r in reports if r.mapping_version_id == mapping_version_id]
                reports.sort(key=lambda r: r.created_at, reverse=True)
                total = len(reports)
                return reports[offset : offset + limit], total
        return self._list_in_db(
            source_id=source_id,
            mapping_version_id=mapping_version_id,
            limit=limit,
            offset=offset,
            conn=conn,
        )

    def clear(self) -> None:
        """Clear all in-memory reports (for testing)."""
        with self._lock:
            self._memory_reports.clear()

    # ── Database Implementation ──────────────────────────────────

    def _save_in_db(
        self,
        report: SchemaDriftReport,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> SchemaDriftReport:
        query = f"""
            INSERT INTO {self.TABLE_NAME} (
                report_id, source_id, prior_schema_version, current_schema_version,
                prior_fingerprint, current_fingerprint, mapping_version_id,
                canonical_schema_version, overall_compatibility, review_required,
                drift_events, impacted_mappings, summary_reason, created_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (report_id) DO UPDATE SET
                overall_compatibility = EXCLUDED.overall_compatibility,
                review_required = EXCLUDED.review_required,
                drift_events = EXCLUDED.drift_events,
                impacted_mappings = EXCLUDED.impacted_mappings,
                summary_reason = EXCLUDED.summary_reason
            RETURNING *;
        """
        events_json = json.dumps([e.model_dump(mode="json") for e in report.drift_events])
        impacts_json = json.dumps([i.model_dump(mode="json") for i in report.impacted_mappings])

        params = (
            report.report_id,
            report.source_id,
            report.prior_schema_version,
            report.current_schema_version,
            report.prior_fingerprint,
            report.current_fingerprint,
            report.mapping_version_id,
            report.canonical_schema_version,
            report.overall_compatibility.value,
            report.review_required,
            events_json,
            impacts_json,
            report.summary_reason,
            report.created_at,
        )
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query, params)
                    row = cur.fetchone()
                if conn is None:
                    c.commit()
            if not row:
                raise DriftReportNotFoundError(f"Failed to persist drift report '{report.report_id}'.")
            return self._row_to_report(row)
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc

    def _get_by_id_in_db(
        self,
        report_id: str,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> Optional[SchemaDriftReport]:
        query = f"SELECT * FROM {self.TABLE_NAME} WHERE report_id = %s;"
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query, (report_id,))
                    row = cur.fetchone()
            if not row:
                return None
            return self._row_to_report(row)
        except Exception as exc:
            raise self._wrap_db_error(exc, query=query, table=self.TABLE_NAME) from exc

    def _list_in_db(
        self,
        source_id: Optional[str],
        mapping_version_id: Optional[str],
        limit: int,
        offset: int,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> tuple[list[SchemaDriftReport], int]:
        filters: list[str] = []
        params: list[Any] = []

        if source_id:
            filters.append("source_id = %s")
            params.append(source_id)
        if mapping_version_id:
            filters.append("mapping_version_id = %s")
            params.append(mapping_version_id)

        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""

        count_query = f"SELECT COUNT(*) as total FROM {self.TABLE_NAME} {where_clause};"
        data_query = f"""
            SELECT * FROM {self.TABLE_NAME}
            {where_clause}
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s;
        """
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(count_query, params)
                    count_row = cur.fetchone()
                    total = count_row["total"] if count_row else 0

                    data_params = list(params) + [limit, offset]
                    cur.execute(data_query, data_params)
                    rows = cur.fetchall() or []

            reports = [self._row_to_report(r) for r in rows]
            return reports, int(total)
        except Exception as exc:
            raise self._wrap_db_error(exc, query=data_query, table=self.TABLE_NAME) from exc

    def _row_to_report(self, row: dict[str, Any]) -> SchemaDriftReport:
        events_raw = row.get("drift_events")
        if isinstance(events_raw, str):
            events_raw = json.loads(events_raw)
        drift_events = [SchemaDriftEvent(**e) for e in (events_raw or [])]

        impacts_raw = row.get("impacted_mappings")
        if isinstance(impacts_raw, str):
            impacts_raw = json.loads(impacts_raw)
        impacted_mappings = [FieldMappingImpact(**i) for i in (impacts_raw or [])]

        return SchemaDriftReport(
            report_id=str(row["report_id"]),
            source_id=str(row["source_id"]),
            prior_schema_version=int(row["prior_schema_version"]),
            current_schema_version=int(row["current_schema_version"]),
            prior_fingerprint=str(row["prior_fingerprint"]),
            current_fingerprint=str(row["current_fingerprint"]),
            mapping_version_id=str(row["mapping_version_id"]),
            canonical_schema_version=int(row["canonical_schema_version"]),
            drift_events=drift_events,
            impacted_mappings=impacted_mappings,
            overall_compatibility=MappingCompatibilityState(str(row["overall_compatibility"])),
            review_required=bool(row["review_required"]),
            summary_reason=str(row["summary_reason"]),
            created_at=row["created_at"],
        )
