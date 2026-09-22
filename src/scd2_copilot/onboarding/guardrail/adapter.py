"""Adapter connecting customer canonical batches to the parent GuardrailEngine and MicroBatch abstractions."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
import logging
from typing import Any, Optional, Union
from uuid import UUID

import polars as pl

from ...explanation.models import ExplanationContext
from ...guardrail.models import GuardrailDecision
from ...source.models import (
    ChangeTimestampDefinition,
    MonitorConfig,
    PostgresSourceDefinition,
)
from ...worker.batch import MicroBatch
from ..models.transformation import CanonicalCustomerRecord
from .models import CustomerGuardrailConfig

logger = logging.getLogger("scd2_copilot.onboarding.guardrail.adapter")


class CustomerGuardrailAdapter:
    """Adapts canonical customer records to the parent GuardrailEngine MicroBatch and ExplanationContext."""

    def __init__(self, config: Optional[CustomerGuardrailConfig] = None) -> None:
        self.config = config or CustomerGuardrailConfig()

    def canonical_records_to_micro_batch(
        self,
        records: Union[list[CanonicalCustomerRecord], list[dict[str, Any]], pl.DataFrame],
        config: Optional[CustomerGuardrailConfig] = None,
        watermark_start: Optional[datetime] = None,
        processing_date: Optional[date] = None,
    ) -> MicroBatch:
        """Transform canonical customer entities into a standardized MicroBatch.

        Extracts timestamps, keys, and tracked attributes for guardrail evaluation.
        """
        cfg = config or self.config

        # 1. Normalize input to list of dicts
        raw_list: list[dict[str, Any]] = []
        if isinstance(records, pl.DataFrame):
            raw_list = records.to_dicts()
        elif isinstance(records, list):
            for r in records:
                if isinstance(r, CanonicalCustomerRecord):
                    d = r.model_dump()
                    raw_list.append(d)
                elif isinstance(r, dict):
                    raw_list.append(dict(r))
                elif hasattr(r, "to_dict"):
                    raw_list.append(r.to_dict())
                else:
                    raw_list.append(dict(r.__dict__))

        # 2. Extract and sanitize records with proper datetime timestamps
        sanitized_records: list[dict[str, Any]] = []
        for row in raw_list:
            rec = {
                "customer_id": str(row.get("customer_id") or ""),
                "first_name": row.get("first_name"),
                "last_name": row.get("last_name"),
                "email": row.get("email"),
                "date_of_birth": str(row.get("date_of_birth")) if row.get("date_of_birth") else None,
                "status": str(row.get("status") or "").upper(),
            }
            # Process timestamp for velocity analysis and containment replay
            ts = row.get("updated_at")
            if ts is None and processing_date:
                ts = datetime.combine(processing_date, time(12, 0), tzinfo=timezone.utc)
            elif ts is None:
                ts = row.get("created_at")

            if isinstance(ts, str):
                try:
                    rec["created_at"] = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except Exception:
                    rec["created_at"] = datetime.now(timezone.utc)
            elif isinstance(ts, datetime):
                rec["created_at"] = ts
            elif isinstance(ts, date):
                rec["created_at"] = datetime.combine(ts, time(12, 0), tzinfo=timezone.utc)
            else:
                rec["created_at"] = datetime.now(timezone.utc)

            sanitized_records.append(rec)

        # 3. Calculate watermark window if available
        watermarks = [r["created_at"] for r in sanitized_records if r.get("created_at")]
        w_start = watermark_start
        w_end = max(watermarks) if watermarks else None

        return MicroBatch(
            source_records=sanitized_records,
            watermark_start=w_start,
            watermark_end=w_end,
            key_columns=["customer_id"],
            tracked_columns=["first_name", "last_name", "email", "date_of_birth", "status"],
            timestamp_column="created_at",
            source_name=cfg.source_name,
        )

    def build_monitor_config(
        self,
        config: Optional[CustomerGuardrailConfig] = None,
    ) -> MonitorConfig:
        """Construct a neutral MonitorConfig representation for customer historical entity monitoring."""
        cfg = config or self.config
        return MonitorConfig(
            name=cfg.source_name,
            source=PostgresSourceDefinition(
                type="postgresql",
                schema_name="public",
                table_name="customer_onboarding",
            ),
            keys=["customer_id"],
            change_timestamp=ChangeTimestampDefinition(column="created_at"),
            tracked_columns=["first_name", "last_name", "email", "date_of_birth", "status"],
        )

    def build_explanation_context(
        self,
        guardrail_decision: GuardrailDecision,
        source_name: str,
        run_id: Optional[UUID] = None,
        hold_id: Optional[UUID] = None,
        records_sample: Optional[list[dict[str, Any]]] = None,
    ) -> ExplanationContext:
        """Construct immutable ExplanationContext from a GuardrailDecision for the explanation service."""
        return ExplanationContext.from_guardrail_decision(
            decision=guardrail_decision,
            source_name=source_name,
            hold_id=hold_id,
            records_sample=records_sample or [],
        )
