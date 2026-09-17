"""Incremental ingestion worker subsystem for SCD2 Copilot V2."""

from .batch import MicroBatch, history_rows_to_target_df
from .exceptions import (
    BatchProcessingError,
    CheckpointAdvancementError,
    WorkerError,
    WorkerShutdownException,
    WorkerValidationFailureError,
)
from .worker import IngestionWorker, WorkerCycleResult

__all__ = [
    "IngestionWorker",
    "WorkerCycleResult",
    "MicroBatch",
    "history_rows_to_target_df",
    "WorkerError",
    "BatchProcessingError",
    "WorkerValidationFailureError",
    "CheckpointAdvancementError",
    "WorkerShutdownException",
]
