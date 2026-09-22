"""Adapter translating canonical customer records to and from the deterministic Polars SCD2 engine."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
import logging
from typing import Any, Optional, Union

import polars as pl

from ...models import ChangeReport, ValidationReport
from ...db.models import MonitoredEntityHistoryRow
from ...detect_changes import detect_changes
from ...transform_scd2 import apply_scd2
from ...validate import validate_scd2
from ...worker.batch import generic_history_rows_to_target_df
from ..models.transformation import CanonicalCustomerRecord
from .models import CustomerSCD2Config

logger = logging.getLogger("scd2_copilot.onboarding.scd2.adapter")


class CustomerSCD2Adapter:
    """Translates canonical customer data into the authoritative SCD2 engine boundary.

    Guarantees:
    - Never reimplements change detection or interval assignment.
    - Strips all onboarding operational metadata (run_id, mapping_version_id, etc.)
      so that operational metadata changes NEVER cause customer SCD2 versions.
    - Operates purely on customer_id and configured canonical business attributes.
    """

    def __init__(self, config: Optional[CustomerSCD2Config] = None) -> None:
        self.config = config or CustomerSCD2Config()

    def canonical_records_to_source_df(
        self,
        records: Union[list[CanonicalCustomerRecord], pl.DataFrame],
        config: Optional[CustomerSCD2Config] = None,
    ) -> pl.DataFrame:
        """Convert validated canonical customer records to a source Polars DataFrame.

        Filters strictly to the business key and tracked columns.
        """
        cfg = config or self.config
        schema: dict[str, Any] = {k: pl.String for k in cfg.business_key}
        for col in cfg.tracked_columns:
            if col == "date_of_birth":
                schema[col] = pl.Date
            else:
                schema[col] = pl.String

        if isinstance(records, pl.DataFrame):
            # Select and cast desired columns
            cols_to_keep = [c for c in cfg.business_key + cfg.tracked_columns if c in records.columns]
            df = records.select(cols_to_keep)
            cast_map = {}
            for c in cols_to_keep:
                if c == "date_of_birth" and df.schema[c] != pl.Date:
                    cast_map[c] = pl.Date
                elif c != "date_of_birth" and df.schema[c] != pl.String:
                    cast_map[c] = pl.String
            return df.with_columns([pl.col(c).cast(t) for c, t in cast_map.items()])

        if not records:
            return pl.DataFrame(schema=schema)

        data: list[dict[str, Any]] = []
        for r in records:
            row: dict[str, Any] = {"customer_id": str(r.customer_id)}
            for col in cfg.tracked_columns:
                val = getattr(r, col, None)
                if isinstance(val, date) and not isinstance(val, datetime):
                    row[col] = val
                elif val is not None:
                    row[col] = str(val)
                else:
                    row[col] = None
            data.append(row)

        return pl.DataFrame(data, schema=schema)

    def current_history_to_target_df(
        self,
        history_rows: Union[list[MonitoredEntityHistoryRow], pl.DataFrame],
        config: Optional[CustomerSCD2Config] = None,
    ) -> pl.DataFrame:
        """Convert active history rows from MonitoredEntityHistoryRepository into target_df."""
        cfg = config or self.config
        if isinstance(history_rows, pl.DataFrame):
            target_df = history_rows
        elif not history_rows:
            # Strongly typed empty DataFrame matching canonical schema and SCD2 temporal metadata
            schema: dict[str, Any] = {k: pl.String for k in cfg.business_key}
            for col in cfg.tracked_columns:
                if col == "date_of_birth":
                    schema[col] = pl.Date
                else:
                    schema[col] = pl.String
            schema["effective_from"] = pl.Date
            schema["effective_to"] = pl.Date
            schema["is_current"] = pl.Boolean
            return pl.DataFrame(schema=schema)
        else:
            target_df = generic_history_rows_to_target_df(
                rows=history_rows,
                key_columns=cfg.business_key,
                tracked_columns=cfg.tracked_columns,
            )

        # Align schema with canonical types (e.g. date_of_birth as pl.Date)
        if "date_of_birth" in target_df.columns and target_df.schema["date_of_birth"] != pl.Date:
            target_df = target_df.with_columns(
                pl.col("date_of_birth").cast(pl.String).str.to_date(strict=False)
            )
        return target_df

    def execute_scd2(
        self,
        source_df: pl.DataFrame,
        target_df: pl.DataFrame,
        processing_date: date,
        config: Optional[CustomerSCD2Config] = None,
    ) -> tuple[pl.DataFrame, ChangeReport, ValidationReport]:
        """Execute the deterministic parent SCD2 engine pipeline: detect_changes, apply_scd2, validate_scd2.

        Returns:
            (scd2_df, change_report, validation_report)
        """
        cfg = config or self.config

        # 1. Deterministic change detection
        change_report = detect_changes(
            source_df=source_df,
            target_df=target_df,
            business_key=cfg.business_key,
            tracked_columns=cfg.tracked_columns,
            processing_date=processing_date,
            snapshot_mode=cfg.snapshot_mode,
            delete_policy=cfg.delete_policy,
            engine=cfg.engine,
        )

        # 2. SCD2 historical transformation
        scd2_df = apply_scd2(
            source_df=source_df,
            target_df=target_df,
            change_report=change_report,
            business_key=cfg.business_key,
            tracked_columns=cfg.tracked_columns,
            processing_date=processing_date,
            delete_policy=cfg.delete_policy,
            engine=cfg.engine,
        )

        # 3. Deterministic 5-rule SCD2 invariant validation
        validation_report = validate_scd2(
            df=scd2_df,
            business_key=cfg.business_key,
            engine=cfg.engine,
        )

        return scd2_df, change_report, validation_report

    def build_rows_to_insert(
        self,
        scd2_df: pl.DataFrame,
        change_report: ChangeReport,
        processing_date: date,
        config: Optional[CustomerSCD2Config] = None,
    ) -> list[MonitoredEntityHistoryRow]:
        """Extract new active SCD2 rows from scd2_df for NEW and CHANGED records and convert to MonitoredEntityHistoryRow instances."""
        cfg = config or self.config
        new_or_changed_keys = [
            str(r.business_key_values["customer_id"])
            for r in list(change_report.new) + list(change_report.changed)
        ]
        if not new_or_changed_keys:
            return []

        new_active_df = scd2_df.filter(
            (pl.col("is_current") == True)
            & (pl.col("customer_id").cast(pl.String).is_in(new_or_changed_keys))
        )
        if new_active_df.is_empty():
            return []

        now_utc = datetime.now(timezone.utc)
        eff_from_dt = datetime.combine(processing_date, time.min, tzinfo=timezone.utc)

        rows: list[MonitoredEntityHistoryRow] = []
        for row in new_active_df.iter_rows(named=True):
            entity_key = {k: str(row[k]) for k in cfg.business_key}
            attributes: dict[str, Any] = {}
            for col in cfg.tracked_columns:
                val = row.get(col)
                if isinstance(val, (date, datetime)):
                    attributes[col] = val.isoformat()
                else:
                    attributes[col] = val

            hist_row = MonitoredEntityHistoryRow(
                source_name=cfg.entity_source_name,
                entity_key=entity_key,
                attributes=attributes,
                effective_from=eff_from_dt,
                effective_to=None,
                is_current=True,
                created_at=now_utc,
            )
            rows.append(hist_row)

        return rows

    def extract_keys_to_close(
        self,
        change_report: ChangeReport,
        config: Optional[CustomerSCD2Config] = None,
    ) -> list[dict[str, Any]]:
        """Extract entity keys for rows that must be closed (CHANGED + DELETED when SOFT_DELETE)."""
        cfg = config or self.config
        records_to_close = list(change_report.changed)
        if getattr(change_report, "delete_policy", None) == "soft_delete":
            records_to_close.extend(change_report.deleted)

        entity_keys: list[dict[str, Any]] = []
        for r in records_to_close:
            key_dict = {k: str(r.business_key_values[k]) for k in cfg.business_key}
            entity_keys.append(key_dict)

        return entity_keys
