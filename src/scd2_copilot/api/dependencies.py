"""Dependency injection providers for FastAPI route handlers."""

from __future__ import annotations

from typing import Optional

from fastapi import Depends

from ..config import Settings, get_settings
from ..containment.service import ContainmentService
from ..db.connection import DatabaseManager
from ..db.repositories import (
    CheckpointRepository,
    HeldChangeBatchRepository,
    InventoryHistoryRepository,
    InventorySourceRepository,
    MonitoredEntityHistoryRepository,
    ProcessingRunRepository,
)

_db_manager_instance: Optional[DatabaseManager] = None


def get_db(settings: Settings = Depends(get_settings)) -> DatabaseManager:
    """Dependency provider returning the singleton DatabaseManager."""
    global _db_manager_instance
    if _db_manager_instance is None:
        _db_manager_instance = DatabaseManager(settings=settings)
    return _db_manager_instance


def set_db(db: Optional[DatabaseManager]) -> None:
    """Allows tests to override the DatabaseManager singleton."""
    global _db_manager_instance
    _db_manager_instance = db


def get_run_repo(db: DatabaseManager = Depends(get_db)) -> ProcessingRunRepository:
    """Dependency provider for ProcessingRunRepository."""
    return ProcessingRunRepository(db=db)


def get_hold_repo(db: DatabaseManager = Depends(get_db)) -> HeldChangeBatchRepository:
    """Dependency provider for HeldChangeBatchRepository."""
    return HeldChangeBatchRepository(db=db)


def get_checkpoint_repo(db: DatabaseManager = Depends(get_db)) -> CheckpointRepository:
    """Dependency provider for CheckpointRepository."""
    return CheckpointRepository(db=db)


def get_history_repo(db: DatabaseManager = Depends(get_db)) -> InventoryHistoryRepository:
    """Dependency provider for InventoryHistoryRepository."""
    return InventoryHistoryRepository(db=db)


def get_generic_history_repo(
    db: DatabaseManager = Depends(get_db),
) -> MonitoredEntityHistoryRepository:
    """Dependency provider for MonitoredEntityHistoryRepository."""
    return MonitoredEntityHistoryRepository(db=db)


def get_inventory_repo(db: DatabaseManager = Depends(get_db)) -> InventorySourceRepository:
    """Dependency provider for InventorySourceRepository."""
    return InventorySourceRepository(db=db)


def get_containment_service(
    db: DatabaseManager = Depends(get_db),
    settings: Settings = Depends(get_settings),
    hold_repo: HeldChangeBatchRepository = Depends(get_hold_repo),
    run_repo: ProcessingRunRepository = Depends(get_run_repo),
    checkpoint_repo: CheckpointRepository = Depends(get_checkpoint_repo),
    history_repo: InventoryHistoryRepository = Depends(get_history_repo),
    inventory_repo: InventorySourceRepository = Depends(get_inventory_repo),
) -> ContainmentService:
    """Dependency provider for ContainmentService."""
    return ContainmentService(
        db_manager=db,
        settings=settings,
        hold_repo=hold_repo,
        run_repo=run_repo,
        checkpoint_repo=checkpoint_repo,
        history_repo=history_repo,
        inventory_repo=inventory_repo,
    )
