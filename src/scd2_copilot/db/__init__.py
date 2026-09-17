"""PostgreSQL/Supabase data-access layer for SCD2 Copilot V2."""

from .connection import DatabaseManager, redact_database_url, sanitize_error_message
from .exceptions import (
    DatabaseConfigurationError,
    DatabaseConnectionError,
    DatabaseError,
    DatabaseIntegrityError,
    DatabaseQueryError,
    DatabaseTransactionError,
    EntityNotFoundError,
)
from .models import (
    HeldChangeBatchRow,
    HoldSeverity,
    HoldStatus,
    InventoryHistoryRow,
    InventorySourceRow,
    MonitoredEntityHistoryRow,
    ProcessingCheckpointRow,
    ProcessingRunRow,
    RunStatus,
)
from .repositories import (
    BaseRepository,
    CheckpointRepository,
    HeldChangeBatchRepository,
    InventoryHistoryRepository,
    InventorySourceRepository,
    ProcessingRunRepository,
)

__all__ = [
    # Connection
    "DatabaseManager",
    "redact_database_url",
    "sanitize_error_message",
    # Exceptions
    "DatabaseError",
    "DatabaseConfigurationError",
    "DatabaseConnectionError",
    "DatabaseQueryError",
    "DatabaseTransactionError",
    "DatabaseIntegrityError",
    "EntityNotFoundError",
    # Models & Enums
    "InventorySourceRow",
    "InventoryHistoryRow",
    "MonitoredEntityHistoryRow",
    "ProcessingRunRow",
    "ProcessingCheckpointRow",
    "HeldChangeBatchRow",
    "RunStatus",
    "HoldStatus",
    "HoldSeverity",
    # Repositories
    "BaseRepository",
    "CheckpointRepository",
    "HeldChangeBatchRepository",
    "InventoryHistoryRepository",
    "InventorySourceRepository",
    "ProcessingRunRepository",
]
