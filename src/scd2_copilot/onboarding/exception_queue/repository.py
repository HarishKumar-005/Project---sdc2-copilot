"""In-memory and JSON artifact repository for Customer Data Onboarding exceptions."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..exceptions import ExceptionNotFoundError
from ..models.exception import CustomerRecordException, ExceptionBatch, ExceptionStatus


class ExceptionQueueRepository:
    """Thread-safe in-memory and artifact repository for record exceptions."""

    def __init__(self) -> None:
        self._exceptions: dict[str, CustomerRecordException] = {}

    def add(self, exception: CustomerRecordException) -> None:
        """Add an exception to the repository, updating if already present."""
        self._exceptions[exception.exception_id] = exception

    def add_batch(self, exceptions: list[CustomerRecordException]) -> None:
        """Add multiple exceptions to the repository."""
        for exc in exceptions:
            self.add(exc)

    def get(self, exception_id: str) -> Optional[CustomerRecordException]:
        """Retrieve an exception by ID, or None if not found."""
        return self._exceptions.get(exception_id)

    def get_required(self, exception_id: str) -> CustomerRecordException:
        """Retrieve an exception by ID, raising ExceptionNotFoundError if not found."""
        exc = self.get(exception_id)
        if exc is None:
            raise ExceptionNotFoundError(
                f"Exception with ID '{exception_id}' not found in exception queue repository.",
                details={"exception_id": exception_id},
            )
        return exc

    def update(self, exception: CustomerRecordException) -> None:
        """Update an existing exception record."""
        if exception.exception_id not in self._exceptions:
            raise ExceptionNotFoundError(
                f"Cannot update non-existent exception '{exception.exception_id}'.",
                details={"exception_id": exception.exception_id},
            )
        self._exceptions[exception.exception_id] = exception

    def list_exceptions(
        self,
        status: Optional[ExceptionStatus] = None,
        rule_id: Optional[str] = None,
        source_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> list[CustomerRecordException]:
        """Filter and list exceptions matching criteria."""
        results = list(self._exceptions.values())
        if status is not None:
            results = [e for e in results if e.status == status]
        if rule_id is not None:
            results = [e for e in results if e.rule_id == rule_id]
        if source_id is not None:
            results = [e for e in results if e.source_id == source_id]
        if run_id is not None:
            results = [e for e in results if e.run_id == run_id]
        return results

    def count(self, status: Optional[ExceptionStatus] = None) -> int:
        """Count exceptions matching optional status filter."""
        if status is None:
            return len(self._exceptions)
        return sum(1 for e in self._exceptions.values() if e.status == status)

    def clear(self) -> None:
        """Remove all exceptions from repository."""
        self._exceptions.clear()

    def save_to_artifact(
        self,
        path: Path | str,
        batch_id: str,
        source_id: str,
        mapping_version_id: str,
        run_id: Optional[str] = None,
    ) -> Path:
        """Persist all current exceptions as an ExceptionBatch JSON artifact."""
        batch = ExceptionBatch(
            batch_id=batch_id,
            source_id=source_id,
            run_id=run_id,
            mapping_version_id=mapping_version_id,
            exceptions=list(self._exceptions.values()),
        )
        return batch.save_artifact(path)

    def load_from_artifact(self, path: Path | str) -> list[CustomerRecordException]:
        """Load an ExceptionBatch artifact and populate the repository."""
        batch = ExceptionBatch.load_artifact(path)
        self.add_batch(batch.exceptions)
        return batch.exceptions
