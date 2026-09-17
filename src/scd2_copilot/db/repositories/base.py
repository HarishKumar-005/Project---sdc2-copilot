"""Base repository abstraction providing safe connection and error handling."""

from __future__ import annotations

from contextlib import contextmanager
import logging
from typing import Any, Generator, Optional

import psycopg
from psycopg.errors import IntegrityError

from ..connection import DatabaseManager, sanitize_error_message
from ..exceptions import (
    DatabaseError,
    DatabaseIntegrityError,
    DatabaseQueryError,
)

logger = logging.getLogger("scd2_copilot.db.repository")


class BaseRepository:
    """Base class for all database repositories."""

    def __init__(self, db: Optional[DatabaseManager] = None) -> None:
        self.db = db if db is not None else DatabaseManager()

    @contextmanager
    def _connection(
        self, conn: Optional[psycopg.Connection[Any]] = None
    ) -> Generator[psycopg.Connection[Any], None, None]:
        """Resolve either caller-provided connection or checkout a connection from manager."""
        if conn is not None:
            yield conn
        else:
            with self.db.get_connection() as managed_conn:
                yield managed_conn

    def _wrap_db_error(
        self,
        exc: Exception,
        *,
        query: Optional[str] = None,
        table: Optional[str] = None,
    ) -> DatabaseError:
        """Translate lower-level driver exceptions into sanitized domain database errors."""
        raw_url = getattr(self.db, "_database_url", None)
        clean_msg = sanitize_error_message(str(exc), raw_url)

        if isinstance(exc, IntegrityError):
            return DatabaseIntegrityError(
                f"Integrity constraint violation on '{table or 'database'}': {clean_msg}",
                query=query,
                table=table,
                cause=exc,
            )
        if isinstance(exc, DatabaseError):
            return exc
        return DatabaseQueryError(
            f"Query failed on '{table or 'database'}': {clean_msg}",
            query=query,
            table=table,
            cause=exc,
        )
