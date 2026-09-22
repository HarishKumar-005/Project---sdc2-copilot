"""API routes for PostgreSQL monitor configuration and schema validation (V3 Phase 1)."""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ...config import Settings, get_settings
from ...db.connection import DatabaseManager
from ...source import (
    MonitorConfig,
    PostgresSourceAdapter,
    get_default_inventory_monitor_config,
    get_default_product_master_monitor_config,
    get_monitor_registry,
)
from ...source.exceptions import (
    SourceColumnNotFoundError,
    SourceConfigurationError,
    SourceConnectionError,
    SourceSchemaNotFoundError,
    SourceTableNotFoundError,
    SourceTypeMismatchError,
)
from ..auth.dependencies import get_optional_operator
from ..dependencies import get_db
from ..schemas import (
    ChangeTimestampSchema,
    ColumnMetadataSchema,
    MonitorConfigRequest,
    MonitorConfigResponse,
    MonitorListResponse,
    MonitorRecordsResponse,
    MonitorValidationResponse,
    PostgresSourceSchema,
)

logger = logging.getLogger("scd2_copilot.api.monitors")

router = APIRouter(prefix="/monitors", tags=["Monitors"])


def _to_response_schema(config: MonitorConfig) -> MonitorConfigResponse:
    return MonitorConfigResponse(
        name=config.name,
        source=PostgresSourceSchema(
            type=config.source.type,
            schema=config.source.schema_name,
            table=config.source.table_name,
        ),
        keys=config.business_keys,
        change_timestamp=ChangeTimestampSchema(column=config.change_timestamp.column),
        tracked_columns=config.tracked_columns,
    )


@router.get("", response_model=MonitorListResponse, summary="List configured monitors")
def list_monitors(settings: Settings = Depends(get_settings)) -> MonitorListResponse:
    """Return all active monitor configurations from the neutral runtime registry."""
    registry = get_monitor_registry()
    monitors = registry.list_all()
    return MonitorListResponse(
        monitors=[_to_response_schema(m) for m in monitors],
        total=len(monitors),
    )


@router.get("/{name}", response_model=MonitorConfigResponse, summary="Get monitor configuration by name")
def get_monitor(name: str, settings: Settings = Depends(get_settings)) -> MonitorConfigResponse:
    """Return monitor configuration by name from the neutral runtime registry."""
    registry = get_monitor_registry()
    config = registry.get(name)
    if config is not None:
        return _to_response_schema(config)
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Monitor configuration '{name}' not found.",
    )


@router.post(
    "/validate",
    response_model=MonitorValidationResponse,
    summary="Validate a monitor configuration against PostgreSQL",
)
def validate_monitor(
    payload: Optional[MonitorConfigRequest] = None,
    settings: Settings = Depends(get_settings),
    db: DatabaseManager = Depends(get_db),
) -> MonitorValidationResponse:
    """Validate a submitted or active monitor configuration against live PostgreSQL metadata.

    Inspects:
    - Connection reachability
    - Schema existence
    - Table existence
    - Business key columns existence
    - Change timestamp column existence and temporal data type
    - Tracked columns existence
    - SCD2 column conflict prevention
    """
    if payload is not None:
        try:
            config = MonitorConfig(
                name=payload.name,
                source={
                    "type": payload.source.type,
                    "schema": payload.source.schema_name,
                    "table": payload.source.table_name,
                },
                keys=payload.business_keys,
                change_timestamp={"column": payload.change_timestamp.column},
                tracked_columns=payload.tracked_columns,
            )
        except Exception as exc:
            return MonitorValidationResponse(
                is_valid=False,
                errors=[f"Invalid configuration payload: {exc}"],
                warnings=[],
                discovered_columns={},
                primary_keys=[],
            )
    else:
        config = get_monitor_registry().get("active") or get_default_product_master_monitor_config(settings=settings)

    adapter = PostgresSourceAdapter(config=config, db_manager=db)
    validation_result = adapter.validate_configuration(raise_on_error=False)

    col_schemas = {}
    primary_keys = []
    if validation_result.metadata:
        primary_keys = validation_result.metadata.primary_keys
        for col_name, cm in validation_result.metadata.columns.items():
            col_schemas[col_name] = ColumnMetadataSchema(
                column_name=cm.column_name,
                data_type=cm.data_type,
                is_nullable=cm.is_nullable,
                ordinal_position=cm.ordinal_position,
            )

    return MonitorValidationResponse(
        is_valid=validation_result.is_valid,
        errors=validation_result.errors,
        warnings=validation_result.warnings,
        discovered_columns=col_schemas,
        primary_keys=primary_keys,
    )


@router.get(
    "/{name}/records",
    response_model=MonitorRecordsResponse,
    summary="List operational source records for a monitor",
)
def list_monitor_records(
    name: str,
    limit: int = Query(50, ge=1, le=100, description="Maximum records to return (1-100)."),
    db: DatabaseManager = Depends(get_db),
    _operator: Any = Depends(get_optional_operator),
) -> MonitorRecordsResponse:
    """Fetch bounded records directly from the configured monitored PostgreSQL table."""
    registry = get_monitor_registry()
    config = registry.get(name)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Monitor configuration '{name}' not found.",
        )
    adapter = PostgresSourceAdapter(config=config, db_manager=db)
    try:
        rows = adapter.read_initial_snapshot(limit=limit)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to read source records for monitor '{name}': {exc}",
        )
    return MonitorRecordsResponse(
        monitor=config.name,
        records=rows,
        total=len(rows),
        limit=limit,
    )

