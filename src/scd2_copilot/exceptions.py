"""Custom exception classes for the SCD2 Copilot pipeline."""

from __future__ import annotations

from typing import Any


class SCD2Error(Exception):
    """Base exception for all SCD2 Copilot pipeline errors."""


class DuplicateBusinessKeyError(SCD2Error, ValueError):
    """Raised when duplicate business keys are detected in active records.

    Attributes:
        dataset_name: Name of the dataset with duplicates (e.g. 'source' or 'target').
        business_key: List of column names forming the business key.
        duplicate_keys: Sample of detected duplicate key value dictionaries.
        duplicate_count: Total number of distinct duplicate business keys.
    """

    def __init__(
        self,
        message: str,
        *,
        dataset_name: str,
        business_key: list[str],
        duplicate_keys: list[dict[str, Any]],
        duplicate_count: int,
    ) -> None:
        super().__init__(message)
        self.dataset_name = dataset_name
        self.business_key = business_key
        self.duplicate_keys = duplicate_keys
        self.duplicate_count = duplicate_count


class InvalidTemporalValueError(SCD2Error, ValueError):
    """Raised when an invalid date or timestamp value cannot be parsed.

    Attributes:
        column: Column name containing the invalid temporal value(s).
        invalid_samples: Sample of detected invalid values.
        dataset_name: Optional dataset name (e.g. 'source' or 'target').
    """

    def __init__(
        self,
        message: str,
        *,
        column: str,
        invalid_samples: list[str] | None = None,
        dataset_name: str | None = None,
    ) -> None:
        super().__init__(message)
        self.column = column
        self.invalid_samples = invalid_samples or []
        self.dataset_name = dataset_name


class ContractValidationError(SCD2Error, ValueError):
    """Raised when an incoming source is rejected by its data contract."""

    def __init__(self, message: str, *, validation_result: Any, quarantine_result: Any = None) -> None:
        super().__init__(message)
        self.validation_result = validation_result
        self.quarantine_result = quarantine_result

