"""PostgreSQL / Supabase connection manager and transaction boundary."""

from __future__ import annotations

from contextlib import contextmanager
import logging
import re
import time
from typing import Any, Generator, Optional
from urllib.parse import urlparse, urlunparse

import psycopg
from psycopg.rows import dict_row

from ..config import Settings, get_settings
from .exceptions import (
    DatabaseConfigurationError,
    DatabaseConnectionError,
    DatabaseTransactionError,
)

logger = logging.getLogger("scd2_copilot.db")


def redact_database_url(url: Optional[str]) -> str:
    """Safely redact sensitive credentials from a database connection string."""
    if not url or not str(url).strip():
        return ""
    try:
        parsed = urlparse(str(url))
        if not parsed.netloc:
            return "<configured>"
        netloc = parsed.netloc
        if "@" in netloc:
            userinfo, hostinfo = netloc.split("@", 1)
            if ":" in userinfo:
                user, _ = userinfo.split(":", 1)
                masked_userinfo = f"{user}:***"
            else:
                masked_userinfo = "***"
            redacted_netloc = f"{masked_userinfo}@{hostinfo}"
        else:
            redacted_netloc = netloc
        return urlunparse(parsed._replace(netloc=redacted_netloc))
    except Exception:
        return "<redacted-db-url>"


def sanitize_error_message(message: str, raw_url: Optional[str] = None) -> str:
    """Remove raw database passwords or credentials from error messages."""
    sanitized = str(message)
    if raw_url:
        try:
            parsed = urlparse(raw_url)
            if parsed.password:
                sanitized = sanitized.replace(f":{parsed.password}@", ":***@")
                sanitized = sanitized.replace(f"password={parsed.password}", "password=***")
                sanitized = sanitized.replace(f"password '{parsed.password}'", "password '***'")
                sanitized = sanitized.replace(f'password "{parsed.password}"', 'password "***"')
                if len(parsed.password) >= 6:
                    sanitized = sanitized.replace(parsed.password, "***")
            if parsed.username and ":" in parsed.netloc:
                sanitized = sanitized.replace(parsed.netloc, redact_database_url(raw_url))
        except Exception:
            pass

    # Generic password patterns in connection strings and logs
    sanitized = re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", sanitized)
    sanitized = re.sub(r"password\s*=\s*['\"]?([^\s'\"]+)['\"]?", "password=***", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"password\s+['\"]([^'\"]+)['\"]", "password '***'", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"password\s*:\s*['\"]?([^\s'\"]+)['\"]?", "password: ***", sanitized, flags=re.IGNORECASE)
    return sanitized


class DatabaseManager:
    """Manages PostgreSQL connections, health checks, and transactional boundaries."""

    def __init__(
        self,
        database_url: Optional[str] = None,
        connect_timeout: float = 10.0,
        settings: Optional[Settings] = None,
    ) -> None:
        if database_url is not None:
            self._database_url = database_url
            self._connect_timeout = connect_timeout
        elif settings is not None:
            self._database_url = settings.database_url or ""
            self._connect_timeout = settings.db_connect_timeout if connect_timeout == 10.0 else connect_timeout
        else:
            s = get_settings()
            self._database_url = s.database_url or ""
            self._connect_timeout = s.db_connect_timeout if connect_timeout == 10.0 else connect_timeout

        self._redacted_url = redact_database_url(self._database_url)

    @property
    def is_configured(self) -> bool:
        """Return True if DATABASE_URL is non-empty."""
        return bool(self._database_url and self._database_url.strip())

    @property
    def redacted_url(self) -> str:
        """Return safe redacted connection URL."""
        return self._redacted_url

    def validate_configuration(self) -> None:
        """Raise DatabaseConfigurationError if configuration is missing or malformed."""
        if not self.is_configured:
            raise DatabaseConfigurationError(
                "DATABASE_URL is not configured. "
                "Provide a valid PostgreSQL connection string in environment or .env file."
            )
        try:
            parsed = urlparse(self._database_url)
            if parsed.scheme not in ("postgresql", "postgres"):
                raise DatabaseConfigurationError(
                    f"Unsupported database scheme: '{parsed.scheme}'. Expected 'postgresql' or 'postgres'."
                )
            if not parsed.hostname:
                raise DatabaseConfigurationError("DATABASE_URL must include a valid hostname.")
        except DatabaseConfigurationError:
            raise
        except Exception as exc:
            raise DatabaseConfigurationError(
                f"Malformed DATABASE_URL: {sanitize_error_message(str(exc), self._database_url)}"
            ) from exc

    def create_connection(self) -> psycopg.Connection[Any]:
        """Create and return a new psycopg connection configured with dict_row factory."""
        self.validate_configuration()
        try:
            conn = psycopg.connect(
                self._database_url,
                connect_timeout=int(self._connect_timeout),
                row_factory=dict_row,
                autocommit=False,
            )
            return conn
        except psycopg.OperationalError as exc:
            clean_msg = sanitize_error_message(str(exc), self._database_url)
            logger.error("Failed to connect to database (%s): %s", self._redacted_url, clean_msg)
            raise DatabaseConnectionError(
                f"Could not connect to PostgreSQL database: {clean_msg}",
                cause=exc,
            ) from exc
        except Exception as exc:
            clean_msg = sanitize_error_message(str(exc), self._database_url)
            logger.error("Unexpected error connecting to database (%s): %s", self._redacted_url, clean_msg)
            raise DatabaseConnectionError(
                f"Unexpected database connection failure: {clean_msg}",
                cause=exc,
            ) from exc

    @contextmanager
    def get_connection(self) -> Generator[psycopg.Connection[Any], None, None]:
        """Context manager yielding a standalone connection, automatically closing it on exit."""
        conn = self.create_connection()
        try:
            yield conn
        finally:
            try:
                conn.close()
            except Exception as exc:
                logger.debug("Error closing connection: %s", exc)

    # Convenience alias for get_connection
    connection = get_connection

    @contextmanager
    def transaction(
        self, existing_conn: Optional[psycopg.Connection[Any]] = None
    ) -> Generator[psycopg.Connection[Any], None, None]:
        """Context manager providing an atomic transaction boundary.

        If `existing_conn` is passed, participates in that connection's transaction without closing it.
        Otherwise, manages a fresh connection with automatic BEGIN, COMMIT, and ROLLBACK.
        """
        if existing_conn is not None:
            with existing_conn.transaction():
                yield existing_conn
            return

        with self.get_connection() as conn:
            try:
                with conn.transaction():
                    yield conn
            except Exception as exc:
                clean_msg = sanitize_error_message(str(exc), self._database_url)
                logger.warning("Transaction rolled back due to error: %s", clean_msg)
                raise DatabaseTransactionError(
                    f"Transaction failed: {clean_msg}", cause=exc
                ) from exc

    def health_check(self) -> dict[str, Any]:
        """Perform a lightweight ping to verify database connectivity.

        Returns:
            dict with 'status': 'healthy', 'latency_ms', and 'url'.
        Raises:
            DatabaseConfigurationError: If database is not configured.
            DatabaseConnectionError: If database is unreachable.
        """
        self.validate_configuration()
        start = time.perf_counter()
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1;")
                    cur.fetchone()
            latency_ms = round((time.perf_counter() - start) * 1000, 2)
            return {
                "status": "healthy",
                "latency_ms": latency_ms,
                "url": self._redacted_url,
            }
        except (DatabaseConnectionError, DatabaseConfigurationError):
            raise
        except Exception as exc:
            clean_msg = sanitize_error_message(str(exc), self._database_url)
            raise DatabaseConnectionError(
                f"Database health check failed: {clean_msg}", cause=exc
            ) from exc

    def ping(self) -> bool:
        """Lightweight boolean check whether database is reachable."""
        try:
            self.health_check()
            return True
        except Exception:
            return False
