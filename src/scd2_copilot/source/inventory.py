"""Default warehouse inventory monitor configuration factory preserving the demo domain."""

from __future__ import annotations

from typing import Optional

from ..config import Settings, get_settings
from .models import ChangeTimestampDefinition, MonitorConfig, PostgresSourceDefinition


def get_default_inventory_monitor_config(settings: Optional[Settings] = None) -> MonitorConfig:
    """Return the canonical MonitorConfig representing the warehouse inventory demo.

    This ensures that the existing demonstration domain is derived strictly from
    configuration rather than being hardcoded into the worker or pipeline.
    """
    cfg = settings or get_settings()
    table_name = cfg.ingestion_table_name if (cfg and cfg.ingestion_table_name) else "inventory_source"
    source_name = cfg.ingestion_source_name if (cfg and cfg.ingestion_source_name) else "warehouse_inventory"

    return MonitorConfig(
        name=source_name,
        source=PostgresSourceDefinition(
            schema="public",
            table=table_name,
        ),
        keys=["sku_id", "warehouse_id"],
        change_timestamp=ChangeTimestampDefinition(column="updated_at"),
        tracked_columns=["quantity_on_hand", "reorder_level", "status"],
    )
