"""Product Master monitor configuration factory for the canonical PostgreSQL demonstration."""

from __future__ import annotations

from typing import Optional

from ..config import Settings, get_settings
from .models import ChangeTimestampDefinition, MonitorConfig, PostgresSourceDefinition


def get_default_product_master_monitor_config(settings: Optional[Settings] = None) -> MonitorConfig:
    """Return the canonical MonitorConfig representing the Product Master demo.

    Configured fields:
    - name: product_master
    - source: public.product_master
    - business_keys: [product_id]
    - change_timestamp: updated_at
    - tracked_columns: [product_name, category, supplier_id, price, status]
    """
    cfg = settings or get_settings()
    source_name = cfg.ingestion_source_name if (cfg and cfg.ingestion_source_name and cfg.ingestion_source_name != "inventory") else "product_master"
    table_name = cfg.ingestion_table_name if (cfg and cfg.ingestion_table_name and cfg.ingestion_table_name != "inventory_source") else "product_master"

    return MonitorConfig(
        name=source_name,
        source=PostgresSourceDefinition(
            schema="public",
            table=table_name,
        ),
        keys=["product_id"],
        change_timestamp=ChangeTimestampDefinition(column="updated_at"),
        tracked_columns=[
            "product_name",
            "category",
            "supplier_id",
            "price",
            "status",
        ],
    )
