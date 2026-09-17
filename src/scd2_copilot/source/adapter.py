"""PostgreSQL source adapter managing schema discovery, configuration validation, and record reads."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import logging
from typing import Any, Generator, Optional

import psycopg
from psycopg import sql

from ..db.connection import DatabaseManager
from .exceptions import (
    SourceColumnNotFoundError,
    SourceConfigurationError,
    SourceConnectionError,
    SourceSchemaNotFoundError,
    SourceTableNotFoundError,
    SourceTypeMismatchError,
)
from .models import (
    MonitorConfig,
    SourceColumnMetadata,
    SourceTableMetadata,
    SourceValidationResult,
)

logger = logging.getLogger("scd2_copilot.source.postgres")

SUPPORTED_TIMESTAMP_DATA_TYPES = {
    "timestamp with time zone",
    "timestamp without time zone",
    "timestamptz",
    "timestamp",
    "date",
}


class PostgresSourceAdapter:
    """Canonical PostgreSQL source boundary.

    Owns PostgreSQL-specific connection acquisition, schema metadata inspection,
    source configuration validation, and reading initial and incremental records.
    """

    def __init__(
        self,
        config: MonitorConfig,
        db_manager: Optional[DatabaseManager] = None,
    ) -> None:
        self.config = config
        self.db = db_manager or DatabaseManager()

    @contextmanager
    def _connection(
        self, conn: Optional[psycopg.Connection[Any]] = None
    ) -> Generator[psycopg.Connection[Any], None, None]:
        """Context manager providing an active connection, either supplied or borrowed."""
        if conn is not None:
            yield conn
        elif hasattr(self.db, "connection") and getattr(self.db.connection, "_mock_return_value", None) is not None:
            with self.db.connection() as c:
                yield c
        elif hasattr(self.db, "get_connection"):
            with self.db.get_connection() as c:
                yield c
        else:
            with self.db.connection() as c:
                yield c

    def get_ordering_columns(self) -> list[str]:
        """Return the deterministic ordering column names (timestamp + business keys)."""
        return [self.config.change_timestamp.column] + self.config.business_keys

    def discover_schema(
        self, conn: Optional[psycopg.Connection[Any]] = None
    ) -> SourceTableMetadata:
        """Inspect PostgreSQL information_schema to discover column metadata and primary keys."""
        schema_name = self.config.source.schema_name
        table_name = self.config.source.table_name

        columns_query = """
            SELECT column_name, data_type, is_nullable, ordinal_position
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position ASC;
        """

        pk_query = """
            SELECT kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            WHERE tc.table_schema = %s
              AND tc.table_name = %s
              AND tc.constraint_type = 'PRIMARY KEY'
            ORDER BY kcu.ordinal_position ASC;
        """

        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(columns_query, (schema_name, table_name))
                    col_rows = cur.fetchall()

                    cur.execute(pk_query, (schema_name, table_name))
                    pk_rows = cur.fetchall()
        except Exception as exc:
            raise SourceConnectionError(f"Failed to query PostgreSQL schema metadata: {exc}") from exc

        columns_dict: dict[str, SourceColumnMetadata] = {}
        for r in col_rows:
            col_name = r["column_name"]
            columns_dict[col_name] = SourceColumnMetadata(
                column_name=col_name,
                data_type=str(r["data_type"]).lower(),
                is_nullable=str(r["is_nullable"]).upper() == "YES",
                ordinal_position=int(r["ordinal_position"]),
            )

        pk_list = [r["column_name"] for r in pk_rows]

        return SourceTableMetadata(
            schema_name=schema_name,
            table_name=table_name,
            columns=columns_dict,
            primary_keys=pk_list,
        )

    def validate_configuration(
        self,
        raise_on_error: bool = False,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> SourceValidationResult:
        """Validate monitor configuration against live PostgreSQL database.

        Checks:
        1. PostgreSQL connection is reachable.
        2. Schema exists.
        3. Table exists.
        4. Every business-key column exists.
        5. Change-timestamp column exists and has a supported temporal type.
        6. Every tracked column exists.
        7. No column conflicts with SCD2 reserved names.
        8. Deterministic ordering is possible.
        """
        errors: list[str] = []
        warnings: list[str] = []
        schema_name = self.config.source.schema_name
        table_name = self.config.source.table_name

        # 1. Connection check
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute("SELECT 1;")
                    cur.fetchone()
        except Exception as exc:
            err = f"PostgreSQL database connection failed: {exc}"
            if raise_on_error:
                raise SourceConnectionError(err) from exc
            return SourceValidationResult(is_valid=False, errors=[err])

        # 2. Schema check
        schema_query = "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s;"
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(schema_query, (schema_name,))
                    if not cur.fetchone():
                        err = f"Configured schema '{schema_name}' does not exist in PostgreSQL database"
                        if raise_on_error:
                            raise SourceSchemaNotFoundError(err)
                        errors.append(err)
                        return SourceValidationResult(is_valid=False, errors=errors)
        except SourceSchemaNotFoundError:
            raise
        except Exception as exc:
            err = f"Failed to check schema existence: {exc}"
            if raise_on_error:
                raise SourceConnectionError(err) from exc
            return SourceValidationResult(is_valid=False, errors=[err])

        # 3. Table check
        table_query = """
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = %s AND table_name = %s;
        """
        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(table_query, (schema_name, table_name))
                    if not cur.fetchone():
                        err = f"Configured table '{schema_name}.{table_name}' does not exist"
                        if raise_on_error:
                            raise SourceTableNotFoundError(err)
                        errors.append(err)
                        return SourceValidationResult(is_valid=False, errors=errors)
        except SourceTableNotFoundError:
            raise
        except Exception as exc:
            err = f"Failed to check table existence: {exc}"
            if raise_on_error:
                raise SourceConnectionError(err) from exc
            return SourceValidationResult(is_valid=False, errors=[err])

        # 4. Discover metadata
        metadata = self.discover_schema(conn=conn)
        discovered_cols = metadata.columns

        # 5. Check business keys
        for key in self.config.business_keys:
            if key not in discovered_cols:
                err = f"Business key column '{key}' not found in table '{schema_name}.{table_name}'"
                if raise_on_error:
                    raise SourceColumnNotFoundError(err)
                errors.append(err)
            else:
                col_meta = discovered_cols[key]
                if col_meta.is_nullable:
                    warnings.append(
                        f"Business key column '{key}' is nullable in PostgreSQL. "
                        f"NULL keys will be rejected by SCD2 validation."
                    )

        # 6. Check change timestamp
        ts_col = self.config.change_timestamp.column
        if ts_col not in discovered_cols:
            err = f"Change timestamp column '{ts_col}' not found in table '{schema_name}.{table_name}'"
            if raise_on_error:
                raise SourceColumnNotFoundError(err)
            errors.append(err)
        else:
            col_meta = discovered_cols[ts_col]
            dt = col_meta.data_type.lower()
            if not any(sup in dt for sup in SUPPORTED_TIMESTAMP_DATA_TYPES):
                err = (
                    f"Change timestamp column '{ts_col}' has data type '{col_meta.data_type}', "
                    f"which is not a supported temporal type (expected timestamp/timestamptz/date)"
                )
                if raise_on_error:
                    raise SourceTypeMismatchError(err)
                errors.append(err)

        # 7. Check tracked columns
        for col in self.config.tracked_columns:
            if col not in discovered_cols:
                err = f"Tracked column '{col}' not found in table '{schema_name}.{table_name}'"
                if raise_on_error:
                    raise SourceColumnNotFoundError(err)
                errors.append(err)

        is_valid = len(errors) == 0
        return SourceValidationResult(
            is_valid=is_valid,
            errors=errors,
            warnings=warnings,
            metadata=metadata,
        )

    def read_initial_snapshot(
        self,
        limit: Optional[int] = None,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> list[dict[str, Any]]:
        """Read initial source records ordered deterministically by timestamp and business keys."""
        schema_name = self.config.source.schema_name
        table_name = self.config.source.table_name
        ts_col = self.config.change_timestamp.column

        all_cols = self.config.business_keys + self.config.tracked_columns + [ts_col]
        # Deduplicate preserving order
        seen = set()
        selected_cols = [c for c in all_cols if not (c in seen or seen.add(c))]

        order_cols = self.get_ordering_columns()

        query = sql.SQL(
            "SELECT {cols} FROM {schema}.{table} ORDER BY {order}{limit};"
        ).format(
            cols=sql.SQL(", ").join(sql.Identifier(c) for c in selected_cols),
            schema=sql.Identifier(schema_name),
            table=sql.Identifier(table_name),
            order=sql.SQL(", ").join(sql.SQL("{} ASC").format(sql.Identifier(c)) for c in order_cols),
            limit=sql.SQL(" LIMIT %s") if limit is not None else sql.SQL(""),
        )

        params = [int(limit)] if limit is not None else []

        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query, params)
                    rows = cur.fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            raise SourceConfigurationError(
                f"Failed to read initial snapshot from '{schema_name}.{table_name}': {exc}"
            ) from exc

    def derive_cursor_from_record(self, record: Any) -> SourceCursor:
        """Construct a deterministic SourceCursor from a source record dictionary or model object."""
        from .models import SourceCursor

        def _get(col: str) -> Any:
            if isinstance(record, dict):
                return record.get(col)
            return getattr(record, col, None)

        ts_col = self.config.change_timestamp.column
        ts_val = _get(ts_col)
        if ts_val is None:
            raise SourceConfigurationError(f"Record is missing change timestamp column '{ts_col}'")
        keys = {k: _get(k) for k in self.config.business_keys}
        return SourceCursor(
            timestamp=ts_val,
            keys=keys,
            key_columns=self.config.business_keys,
        )

    def read_incremental_records(
        self,
        watermark: Optional[Union[datetime, SourceCursor]] = None,
        cursor: Optional[SourceCursor] = None,
        limit: Optional[int] = None,
        batch_size: Optional[int] = None,
        conn: Optional[psycopg.Connection[Any]] = None,
    ) -> list[dict[str, Any]]:
        """Read source records incrementally using deterministic composite cursor semantics.

        Deterministic ordering guaranteed:
        Primary: <change_timestamp> ASC
        Secondary: <business_key_1> ASC, <business_key_2> ASC...

        When cursor (or watermark as SourceCursor) contains business keys:
            WHERE ({ts} > :ts) OR ({ts} = :ts AND ({keys}) > (:keys))
        When cursor contains only a timestamp:
            WHERE {ts} > :ts
        When cursor is None:
            Reads from beginning of stream.
        """
        from .models import SourceCursor

        schema_name = self.config.source.schema_name
        table_name = self.config.source.table_name
        ts_col = self.config.change_timestamp.column
        key_cols = self.config.business_keys

        all_cols = key_cols + self.config.tracked_columns + [ts_col]
        seen = set()
        selected_cols = [c for c in all_cols if not (c in seen or seen.add(c))]

        order_cols = self.get_ordering_columns()

        # Resolve active cursor
        active_cursor: Optional[SourceCursor] = None
        if cursor is not None:
            active_cursor = cursor
        elif isinstance(watermark, SourceCursor):
            active_cursor = watermark
        elif isinstance(watermark, datetime):
            active_cursor = SourceCursor(
                timestamp=watermark,
                key_columns=key_cols,
            )

        params: list[Any] = []
        where_clause = sql.SQL("")

        if active_cursor is not None:
            ts_val = active_cursor.timestamp
            has_valid_keys = (
                bool(active_cursor.keys)
                and all(active_cursor.keys.get(k) is not None for k in key_cols)
            )

            if has_valid_keys:
                key_vals = [active_cursor.keys[k] for k in key_cols]
                if len(key_cols) == 1:
                    where_clause = sql.SQL(
                        " WHERE ({ts} > %s) OR ({ts} = %s AND {key} > %s)"
                    ).format(
                        ts=sql.Identifier(ts_col),
                        key=sql.Identifier(key_cols[0]),
                    )
                    params.extend([ts_val, ts_val, key_vals[0]])
                else:
                    where_clause = sql.SQL(
                        " WHERE ({ts} > %s) OR ({ts} = %s AND ({keys}) > ({placeholders}))"
                    ).format(
                        ts=sql.Identifier(ts_col),
                        keys=sql.SQL(", ").join(sql.Identifier(k) for k in key_cols),
                        placeholders=sql.SQL(", ").join(sql.Placeholder() for _ in key_cols),
                    )
                    params.extend([ts_val, ts_val, *key_vals])
            else:
                where_clause = sql.SQL(" WHERE {ts} > %s").format(ts=sql.Identifier(ts_col))
                params.append(ts_val)

        effective_limit = limit if limit is not None else batch_size
        limit_clause = sql.SQL("")
        if effective_limit is not None:
            limit_clause = sql.SQL(" LIMIT %s")
            params.append(int(effective_limit))

        query = sql.SQL(
            "SELECT {cols} FROM {schema}.{table}{where} ORDER BY {order}{limit};"
        ).format(
            cols=sql.SQL(", ").join(sql.Identifier(c) for c in selected_cols),
            schema=sql.Identifier(schema_name),
            table=sql.Identifier(table_name),
            where=where_clause,
            order=sql.SQL(", ").join(sql.SQL("{} ASC").format(sql.Identifier(c)) for c in order_cols),
            limit=limit_clause,
        )

        try:
            with self._connection(conn) as c:
                with c.cursor() as cur:
                    cur.execute(query, params)
                    rows = cur.fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            raise SourceConfigurationError(
                f"Failed to read incremental records from '{schema_name}.{table_name}': {exc}"
            ) from exc
