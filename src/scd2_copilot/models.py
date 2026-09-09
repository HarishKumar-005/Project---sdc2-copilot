"""Typed data models for the SCD2 Copilot pipeline.

All inter-module data contracts are defined here as dataclasses.
"""

from __future__ import annotations

from collections.abc import Iterable, MutableSequence, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Optional, Union

import polars as pl


# ── Scalar Normalization ───────────────────────────────


def _normalize_scalar(value: Any) -> str | None:
    """Normalize a Python scalar value for comparison (handle None, strip strings)."""
    if value is None:
        return None
    s = str(value).strip()
    return None if s == "" else s


# ── Change types ───────────────────────────────────────


class SnapshotMode(str, Enum):
    """Snapshot mode defining how source population is interpreted."""

    FULL = "full"
    INCREMENTAL = "incremental"


class DeletePolicy(str, Enum):
    """Supported delete policies when an active target record is absent from source."""

    SOFT_DELETE = "soft_delete"
    IGNORE = "ignore"


class ChangeType(str, Enum):
    """Category of change detected for a business key."""

    NEW = "new"
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    DELETED = "deleted"


class DeduplicationStatus(str, Enum):
    """Authoritative execution deduplication outcome."""

    NEW_EXECUTION = "new_execution"
    REUSED_EXECUTION = "reused_execution"
    FORCED_REEXECUTION = "forced_reexecution"


# ── Change records ─────────────────────────────────────


@dataclass(frozen=True)
class FieldChange:
    """A single field-level change within a record."""

    column: str
    old_value: Any
    new_value: Any


@dataclass(frozen=True)
class ChangeRecord:
    """A detected change for one business key."""

    business_key_values: dict[str, Any]
    change_type: ChangeType
    field_changes: list[FieldChange] = field(default_factory=list)


# ── Lazy Record Sequence ───────────────────────────────


class LazyRecordSequence(MutableSequence):
    """High-performance, memory-efficient sequence proxy over a Polars DataFrame.

    Provides O(1) len(), zero-copy lazy evaluation of ChangeRecord and FieldChange
    objects on demand, slicing, fast iteration, and full Python list compatibility.
    """

    def __init__(
        self,
        df: pl.DataFrame | None = None,
        business_key: list[str] | None = None,
        change_type: ChangeType = ChangeType.NEW,
        tracked_columns: list[str] | None = None,
    ) -> None:
        self._df = df if df is not None else pl.DataFrame()
        self._business_key = list(business_key) if business_key is not None else []
        self._change_type = change_type
        self._tracked_columns = list(tracked_columns) if tracked_columns is not None else []
        self._appended: list[ChangeRecord] = []
        self._cache: dict[int, ChangeRecord] = {}
        self._materialized_list: list[ChangeRecord] | None = None

    def _ensure_materialized(self) -> list[ChangeRecord]:
        if self._materialized_list is None:
            self._materialized_list = list(self._iter_all())
        return self._materialized_list

    def __len__(self) -> int:
        if self._materialized_list is not None:
            return len(self._materialized_list)
        return self._df.height + len(self._appended)

    def __bool__(self) -> bool:
        return len(self) > 0

    def __getitem__(self, index: int | slice) -> Any:
        if self._materialized_list is not None:
            return self._materialized_list[index]

        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]

        if index < 0:
            index += len(self)

        if index < 0 or index >= len(self):
            raise IndexError(f"LazyRecordSequence index out of range: {index} (length {len(self)})")

        df_height = self._df.height
        if index >= df_height:
            return self._appended[index - df_height]

        if index in self._cache:
            return self._cache[index]

        rec = self._materialize_row(index)
        self._cache[index] = rec
        return rec

    def _materialize_row(self, index: int) -> ChangeRecord:
        if self._change_type in (ChangeType.NEW, ChangeType.UNCHANGED, ChangeType.DELETED):
            if len(self._business_key) == 1:
                k = self._business_key[0]
                val = self._df[k][index]
                kd = {k: val}
            else:
                row_tuple = self._df.select(self._business_key).row(index)
                kd = dict(zip(self._business_key, row_tuple))
            return ChangeRecord(business_key_values=kd, change_type=self._change_type)
        else:
            # CHANGED
            cols = self._business_key + self._tracked_columns + [f"{c}_target" for c in self._tracked_columns]
            row = self._df.select(cols).row(index, named=True)
            kd = {k: row[k] for k in self._business_key}
            fcs: list[FieldChange] = []
            for col in self._tracked_columns:
                s_val = row[col]
                t_val = row[f"{col}_target"]
                if _normalize_scalar(s_val) != _normalize_scalar(t_val):
                    fcs.append(FieldChange(column=col, old_value=t_val, new_value=s_val))
            return ChangeRecord(
                business_key_values=kd,
                change_type=ChangeType.CHANGED,
                field_changes=fcs,
            )

    def _iter_all(self):
        if self._df.height > 0:
            if self._change_type in (ChangeType.NEW, ChangeType.UNCHANGED, ChangeType.DELETED):
                if len(self._business_key) == 1:
                    k = self._business_key[0]
                    col_vals = self._df[k].to_list()
                    for val in col_vals:
                        yield ChangeRecord(business_key_values={k: val}, change_type=self._change_type)
                else:
                    for row in self._df.select(self._business_key).iter_rows(named=False):
                        yield ChangeRecord(business_key_values=dict(zip(self._business_key, row)), change_type=self._change_type)
            else:
                # CHANGED
                cols = self._business_key + self._tracked_columns + [f"{c}_target" for c in self._tracked_columns]
                for row in self._df.select(cols).iter_rows(named=True):
                    kd = {k: row[k] for k in self._business_key}
                    fcs = []
                    for col in self._tracked_columns:
                        s_val = row[col]
                        t_val = row[f"{col}_target"]
                        if _normalize_scalar(s_val) != _normalize_scalar(t_val):
                            fcs.append(FieldChange(column=col, old_value=t_val, new_value=s_val))
                    yield ChangeRecord(business_key_values=kd, change_type=ChangeType.CHANGED, field_changes=fcs)

        for item in self._appended:
            yield item

    def __iter__(self):
        if self._materialized_list is not None:
            return iter(self._materialized_list)
        return self._iter_all()

    def append(self, item: Any) -> None:
        if self._materialized_list is not None:
            self._materialized_list.append(item)
        else:
            self._appended.append(item)

    def extend(self, items: Iterable[Any]) -> None:
        if self._materialized_list is not None:
            self._materialized_list.extend(items)
        else:
            self._appended.extend(items)

    def insert(self, index: int, item: Any) -> None:
        mat = self._ensure_materialized()
        mat.insert(index, item)

    def __setitem__(self, index: int | slice, value: Any) -> None:
        mat = self._ensure_materialized()
        mat[index] = value

    def __delitem__(self, index: int | slice) -> None:
        mat = self._ensure_materialized()
        del mat[index]

    def pop(self, index: int = -1) -> ChangeRecord:
        if self._materialized_list is not None:
            return self._materialized_list.pop(index)
        if index == -1 and self._appended:
            return self._appended.pop()
        mat = self._ensure_materialized()
        return mat.pop(index)

    def clear(self) -> None:
        self._df = pl.DataFrame()
        self._appended.clear()
        self._cache.clear()
        self._materialized_list = []

    def copy(self) -> list[ChangeRecord]:
        return list(self)

    def to_list(self) -> list[ChangeRecord]:
        return list(self)

    def sort(self, *, key=None, reverse: bool = False) -> None:
        mat = self._ensure_materialized()
        mat.sort(key=key, reverse=reverse)

    def __add__(self, other: Any) -> list[ChangeRecord]:
        if isinstance(other, (list, tuple, Sequence)):
            return list(self) + list(other)
        return NotImplemented

    def __radd__(self, other: Any) -> list[ChangeRecord]:
        if isinstance(other, (list, tuple, Sequence)):
            return list(other) + list(self)
        return NotImplemented

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, (list, tuple, Sequence)):
            if len(self) != len(other):
                return False
            if len(self) == 0:
                return True
            return all(a == b for a, b in zip(self, other))
        return False

    def __repr__(self) -> str:
        length = len(self)
        if length == 0:
            return "[]"
        if length <= 4:
            items_str = ", ".join(repr(self[i]) for i in range(length))
            return f"[{items_str}]"
        return f"[{repr(self[0])}, {repr(self[1])}, ... ({length - 2} more)]"


# ── Change report ──────────────────────────────────────


@dataclass
class ChangeReport:
    """Aggregated result of change detection across all records."""

    new: Sequence[ChangeRecord] | list[ChangeRecord] = field(default_factory=list)
    changed: Sequence[ChangeRecord] | list[ChangeRecord] = field(default_factory=list)
    unchanged: Sequence[ChangeRecord] | list[ChangeRecord] = field(default_factory=list)
    deleted: Sequence[ChangeRecord] | list[ChangeRecord] = field(default_factory=list)
    processing_date: date = field(default_factory=date.today)
    snapshot_mode: SnapshotMode = SnapshotMode.FULL
    delete_policy: DeletePolicy = DeletePolicy.SOFT_DELETE
    classified_df: Optional[Any] = None
    new_keys_df: Optional[Any] = None
    changed_keys_df: Optional[Any] = None
    unchanged_keys_df: Optional[Any] = None
    deleted_keys_df: Optional[Any] = None

    @property
    def total(self) -> int:
        return len(self.new) + len(self.changed) + len(self.unchanged) + len(self.deleted)

    @property
    def summary(self) -> dict[str, int]:
        return {
            "new": len(self.new),
            "changed": len(self.changed),
            "unchanged": len(self.unchanged),
            "deleted": len(self.deleted),
            "total": self.total,
        }


# ── Validation ─────────────────────────────────────────


class ValidationStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"


@dataclass(frozen=True)
class ValidationRule:
    """Result of a single validation rule check."""

    name: str
    status: ValidationStatus
    message: str
    details: list[str] = field(default_factory=list)


@dataclass
class ValidationReport:
    """Aggregated validation results."""

    rules: list[ValidationRule] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.status != ValidationStatus.FAIL for r in self.rules)

    @property
    def summary(self) -> dict[str, int]:
        return {
            "pass": sum(1 for r in self.rules if r.status == ValidationStatus.PASS),
            "fail": sum(1 for r in self.rules if r.status == ValidationStatus.FAIL),
            "warn": sum(1 for r in self.rules if r.status == ValidationStatus.WARN),
        }


# ── Explanations ───────────────────────────────────────


@dataclass(frozen=True)
class Explanation:
    """A human-readable explanation of a single change."""

    business_key_values: dict[str, Any]
    change_type: ChangeType
    text: str
    provider: str  # which LLM provider generated this


# ── Pipeline result ────────────────────────────────────


@dataclass
class LLMMetrics:
    """Detailed LLM token usage, cost, and latency metrics."""

    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost: float
    request_duration: float = 0.0
    num_changes_explained: int = 0
    avg_tokens_per_change: float = 0.0
    is_estimated: bool = False


@dataclass
class OrchestrationSummary:
    """Run-level Prefect execution summary and observability metadata."""

    flow_run_id: Optional[str] = None
    flow_run_name: Optional[str] = None
    deployment_id: Optional[str] = None
    deployment_name: Optional[str] = None
    trigger_type: str = "manual"  # "manual" or "scheduled"
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    total_duration_seconds: float = 0.0
    task_durations: dict[str, float] = field(default_factory=dict)
    task_statuses: dict[str, str] = field(default_factory=dict)
    row_counts: dict[str, int] = field(default_factory=dict)
    change_counts: dict[str, int] = field(default_factory=dict)
    validation_summary: dict[str, Any] = field(default_factory=dict)
    ai_status: str = "success"  # "success", "fallback", "template", "unavailable", "failed"
    ai_provider: str = "template"
    ai_model: Optional[str] = None
    ai_fallback_count: int = 0
    failure_category: Optional[str] = None
    error_message: Optional[str] = None
    warnings: list[str] = field(default_factory=list)
    run_id: Optional[str] = None
    execution_fingerprint: Optional[str] = None
    is_reused: bool = False
    reused_from_run_id: Optional[str] = None
    deduplication_status: str = "new_execution"
    artifact_status: str = "none"  # "persisted", "failed", "skipped", "none"
    artifact_directory: Optional[str] = None
    artifact_files: list[str] = field(default_factory=list)
    persistence_duration_seconds: float = 0.0


@dataclass
class PipelineResult:
    """Full output of the SCD2 pipeline."""

    change_report: ChangeReport
    # scd2_output is a polars.DataFrame but we use Any to avoid
    # importing polars at the type level (keeps models lightweight)
    scd2_output: Any
    validation_report: ValidationReport
    explanations: list[Explanation] = field(default_factory=list)
    metrics: Optional[LLMMetrics] = None
    explain_result: Optional[Any] = None
    execution_time: Optional[float] = None
    source_df: Optional[Any] = None
    target_df: Optional[Any] = None
    business_key: list[str] = field(default_factory=list)
    tracked_columns: list[str] = field(default_factory=list)
    provider_used: Optional[str] = None
    orchestration_summary: Optional[OrchestrationSummary] = None


