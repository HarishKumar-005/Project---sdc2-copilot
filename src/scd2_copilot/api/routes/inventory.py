"""Operational routes for inspection of source inventory records."""

from __future__ import annotations

import logging
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Query, status

from ...db.repositories import InventorySourceRepository
from ..auth.dependencies import get_optional_operator
from ..dependencies import get_inventory_repo
from ..schemas import InventoryListResponse, InventoryRecordResponse

logger = logging.getLogger("scd2_copilot.api.routes.inventory")

router = APIRouter(prefix="/inventory", tags=["Operational Inventory Source"])


@router.get("", response_model=InventoryListResponse, summary="List operational inventory records")
def list_inventory(
    limit: int = Query(50, ge=1, le=100, description="Maximum records to return (1-100)."),
    inventory_repo: InventorySourceRepository = Depends(get_inventory_repo),
    _operator: Any = Depends(get_optional_operator),
) -> InventoryListResponse:
    """Fetch bounded operational inventory records from inventory_source."""
    rows = inventory_repo.fetch_inventory_updated_after(watermark=None, limit=limit)
    records = [
        InventoryRecordResponse(
            sku_id=r.sku_id,
            warehouse_id=r.warehouse_id,
            quantity_on_hand=r.quantity_on_hand,
            reorder_level=r.reorder_level,
            status=r.status,
            updated_at=r.updated_at,
        )
        for r in rows
    ]
    return InventoryListResponse(
        records=records,
        total=len(records),
        limit=limit,
    )


@router.get(
    "/{sku_id}/{warehouse_id}",
    response_model=InventoryRecordResponse,
    summary="Get operational inventory record by key",
)
def get_inventory_record(
    sku_id: str,
    warehouse_id: str,
    inventory_repo: InventorySourceRepository = Depends(get_inventory_repo),
    _operator: Any = Depends(get_optional_operator),
) -> InventoryRecordResponse:
    """Fetch specific operational inventory record for (sku_id, warehouse_id)."""
    clean_sku = sku_id.strip()
    clean_wh = warehouse_id.strip()
    rows = inventory_repo.fetch_inventory_for_keys([(clean_sku, clean_wh)])
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "RECORD_NOT_FOUND",
                "message": f"Inventory record '{clean_sku}/{clean_wh}' was not found in source.",
            },
        )
    row = rows[0]
    return InventoryRecordResponse(
        sku_id=row.sku_id,
        warehouse_id=row.warehouse_id,
        quantity_on_hand=row.quantity_on_hand,
        reorder_level=row.reorder_level,
        status=row.status,
        updated_at=row.updated_at,
    )
