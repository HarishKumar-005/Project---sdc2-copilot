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
from ..onboarding.drift.repository import SchemaDriftRepository
from ..onboarding.drift.service import SchemaDriftService
from ..onboarding.runs.repository import OnboardingRunRepository
from ..onboarding.runs.service import OnboardingRunService

_db_manager_instance: Optional[DatabaseManager] = None
_onboarding_repo_instance: Optional[OnboardingRunRepository] = None
_drift_repo_instance: Optional[SchemaDriftRepository] = None


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


def get_onboarding_run_repo(db: DatabaseManager = Depends(get_db)) -> OnboardingRunRepository:
    """Dependency provider for OnboardingRunRepository."""
    global _onboarding_repo_instance
    if _onboarding_repo_instance is None:
        _onboarding_repo_instance = OnboardingRunRepository(db=db)
    return _onboarding_repo_instance


def set_onboarding_run_repo(repo: Optional[OnboardingRunRepository]) -> None:
    """Allows tests to override the OnboardingRunRepository singleton."""
    global _onboarding_repo_instance
    _onboarding_repo_instance = repo


def get_onboarding_run_service(
    repo: OnboardingRunRepository = Depends(get_onboarding_run_repo),
) -> OnboardingRunService:
    """Dependency provider for OnboardingRunService."""
    return OnboardingRunService(repository=repo)


def get_drift_repository(db: DatabaseManager = Depends(get_db)) -> SchemaDriftRepository:
    """Dependency provider for SchemaDriftRepository."""
    global _drift_repo_instance
    if _drift_repo_instance is None:
        _drift_repo_instance = SchemaDriftRepository(db=db)
    return _drift_repo_instance


def set_drift_repository(repo: Optional[SchemaDriftRepository]) -> None:
    """Allows tests to override the SchemaDriftRepository singleton."""
    global _drift_repo_instance
    _drift_repo_instance = repo


def get_drift_service(
    repo: SchemaDriftRepository = Depends(get_drift_repository),
) -> SchemaDriftService:
    """Dependency provider for SchemaDriftService."""
    return SchemaDriftService(repository=repo)

