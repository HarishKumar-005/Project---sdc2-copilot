"""Operational routes for SCD Type 2 history inspection."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ...db.repositories import InventoryHistoryRepository, MonitoredEntityHistoryRepository
from ..auth.dependencies import get_optional_operator
from ..dependencies import get_generic_history_repo, get_history_repo
from ..schemas import (
    EntityHistoryQueryResponse,
    EntityHistoryRecordResponse,
    HistoryQueryResponse,
    HistoryRecordResponse,
)

logger = logging.getLogger("scd2_copilot.api.routes.history")

router = APIRouter(prefix="/history", tags=["SCD2 History"])


@router.get(
    "/{source_name}/entity",
    response_model=EntityHistoryQueryResponse,
    summary="Query chronological SCD2 history for a generic entity key",
)
def get_entity_history(
    source_name: str,
    key: str = Query(..., description="JSON-encoded business key dictionary, e.g. '{\"product_id\": \"P-1\"}'"),
    generic_history_repo: MonitoredEntityHistoryRepository = Depends(get_generic_history_repo),
    _operator: Any = Depends(get_optional_operator),
) -> EntityHistoryQueryResponse:
    """Fetch complete chronological SCD2 historical versions for a generic monitored entity key."""
    clean_source = source_name.strip()
    try:
        entity_key = json.loads(key)
        if not isinstance(entity_key, dict) or not entity_key:
            raise ValueError("Key must be a non-empty JSON object")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid entity key JSON payload: {exc}",
        )

    rows = generic_history_repo.fetch_history_for_key(clean_source, entity_key)

    versions = [
        EntityHistoryRecordResponse(
            history_id=r.history_id,
            source_name=r.source_name,
            entity_key=r.entity_key,
            attributes=r.attributes,
            effective_from=r.effective_from,
            effective_to=r.effective_to,
            is_current=r.is_current,
            created_at=r.created_at,
        )
        for r in rows
    ]

    return EntityHistoryQueryResponse(
        source_name=clean_source,
        entity_key=entity_key,
        versions=versions,
        total_versions=len(versions),
    )


@router.get(
    "/{sku_id}/{warehouse_id}",
    response_model=HistoryQueryResponse,
    summary="Query chronological SCD2 history for a business key",
)
def get_key_history(
    sku_id: str,
    warehouse_id: str,
    history_repo: InventoryHistoryRepository = Depends(get_history_repo),
    _operator: Any = Depends(get_optional_operator),
) -> HistoryQueryResponse:
    """Fetch complete chronological SCD2 historical versions for the specified (sku_id, warehouse_id) key."""
    clean_sku = sku_id.strip()
    clean_wh = warehouse_id.strip()

    rows = history_repo.fetch_history_for_keys([(clean_sku, clean_wh)])

    versions = [
        HistoryRecordResponse(
            history_id=r.history_id,
            sku_id=r.sku_id,
            warehouse_id=r.warehouse_id,
            quantity_on_hand=r.quantity_on_hand,
            reorder_level=r.reorder_level,
            status=r.status,
            effective_from=r.effective_from,
            effective_to=r.effective_to,
            is_current=r.is_current,
            created_at=r.created_at,
        )
        for r in rows
    ]

    return HistoryQueryResponse(
        sku_id=clean_sku,
        warehouse_id=clean_wh,
        versions=versions,
        total_versions=len(versions),
    )
