"""Explicit input data contracts, schema evolution, and quarantine records."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
from pathlib import Path
from typing import Any, Mapping, Optional
import uuid

import polars as pl


class SchemaEvolutionOutcome(str, Enum):
    COMPATIBLE = "compatible"
    COMPATIBLE_WITH_WARNING = "compatible_with_warning"
    INCOMPATIBLE = "incompatible/rejected"


class ContractFailureCategory(str, Enum):
    MISSING_REQUIRED_COLUMNS = "missing_required_columns"
    UNEXPECTED_COLUMNS = "unexpected_columns"
    INCOMPATIBLE_TYPES = "incompatible_types"
    NULL_BUSINESS_KEY = "null_business_key"
    DUPLICATE_BUSINESS_KEY = "duplicate_business_key"
    INVALID_BUSINESS_KEY = "invalid_business_key"
    INVALID_TRACKED_COLUMNS = "invalid_tracked_columns"
    SCHEMA_VERSION = "schema_version"


def _type_name(dtype: Any) -> str:
    return str(dtype)


def _normalise_allowed_types(value: Any) -> tuple[str, ...]:
    values = value if isinstance(value, (list, tuple, set)) else (value,)
    return tuple(_type_name(v) for v in values)


@dataclass(frozen=True)
class DataContract:
    """Versioned contract for an incoming source snapshot."""

    schema_version: str = "1"
    required_columns: tuple[str, ...] = ()
    allowed_types: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    nullable: Mapping[str, bool] = field(default_factory=dict)
    business_keys: tuple[str, ...] = ()
    tracked_columns: tuple[str, ...] = ()
    allow_unexpected_columns: bool = False
    contract_name: str = "default"

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_columns", tuple(self.required_columns))
        object.__setattr__(self, "business_keys", tuple(self.business_keys))
        object.__setattr__(self, "tracked_columns", tuple(self.tracked_columns))
        object.__setattr__(self, "allowed_types", {str(k): _normalise_allowed_types(v) for k, v in dict(self.allowed_types).items()})
        object.__setattr__(self, "nullable", {str(k): bool(v) for k, v in dict(self.nullable).items()})

    @classmethod
    def from_dataframes(cls, source_df: pl.DataFrame, target_df: pl.DataFrame, business_keys: list[str], tracked_columns: list[str], *, schema_version: str = "1") -> "DataContract":
        target_types = target_df.schema
        return cls(
            schema_version=schema_version,
            required_columns=tuple(source_df.columns),
            allowed_types={col: (_type_name(target_types.get(col, source_df.schema[col])),) for col in source_df.columns},
            nullable={col: source_df[col].null_count() > 0 for col in source_df.columns},
            business_keys=tuple(business_keys),
            tracked_columns=tuple(tracked_columns),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DataContract":
        return cls(**dict(value))


@dataclass(frozen=True)
class ContractValidationErrorDetail:
    category: str
    reason: str
    columns: tuple[str, ...] = ()
    keys: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ContractValidationResult:
    valid: bool
    outcome: SchemaEvolutionOutcome
    schema_version: str
    errors: tuple[ContractValidationErrorDetail, ...] = ()
    warnings: tuple[ContractValidationErrorDetail, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "outcome": self.outcome.value,
            "schema_version": self.schema_version,
            "errors": [e.to_dict() for e in self.errors],
            "warnings": [e.to_dict() for e in self.warnings],
        }


@dataclass(frozen=True)
class QuarantineResult:
    quarantine_id: str
    failure_category: str
    reason: str
    affected_columns: tuple[str, ...] = ()
    affected_keys: tuple[str, ...] = ()
    source_identity: str = "unknown"
    run_id: Optional[str] = None
    flow_run_id: Optional[str] = None
    schema_version: Optional[str] = None
    evolution_outcome: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ContractValidationException(ValueError):
    """Raised when the contract gate rejects an incoming source."""

    def __init__(self, result: ContractValidationResult, quarantine: QuarantineResult | None = None):
        self.result = result
        self.quarantine = quarantine
        super().__init__("; ".join(error.reason for error in result.errors) or "Input rejected by data contract")


def _detail(category: ContractFailureCategory, reason: str, columns: list[str] | None = None, keys: list[str] | None = None) -> ContractValidationErrorDetail:
    return ContractValidationErrorDetail(category.value, reason, tuple(columns or ()), tuple(keys or ()))


def validate_data_contract(source_df: pl.DataFrame, contract: DataContract, *, observed_schema_version: str | None = None) -> ContractValidationResult:
    """Validate the incoming source before it reaches deterministic SCD2 logic."""
    errors: list[ContractValidationErrorDetail] = []
    warnings: list[ContractValidationErrorDetail] = []
    source_columns = set(source_df.columns)
    missing = sorted(set(contract.required_columns) - source_columns)
    if missing:
        errors.append(_detail(ContractFailureCategory.MISSING_REQUIRED_COLUMNS, f"Missing required column(s): {', '.join(missing)}", missing))
    unexpected = sorted(source_columns - set(contract.required_columns))
    if unexpected:
        detail = _detail(ContractFailureCategory.UNEXPECTED_COLUMNS, f"Unexpected column(s): {', '.join(unexpected)}", unexpected)
        (warnings if contract.allow_unexpected_columns else errors).append(detail)

    missing_keys = [c for c in contract.business_keys if c not in source_columns]
    if not contract.business_keys or missing_keys or len(set(contract.business_keys)) != len(contract.business_keys):
        errors.append(_detail(ContractFailureCategory.INVALID_BUSINESS_KEY, "Business keys must be non-empty, unique, and present in the source", missing_keys))
    invalid_tracked = [c for c in contract.tracked_columns if c not in source_columns or c in contract.business_keys]
    if not contract.tracked_columns or invalid_tracked or len(set(contract.tracked_columns)) != len(contract.tracked_columns):
        errors.append(_detail(ContractFailureCategory.INVALID_TRACKED_COLUMNS, "Tracked columns must be non-empty, unique, present, and different from business keys", invalid_tracked))

    for column, expected in contract.allowed_types.items():
        if column in source_df.columns and _type_name(source_df.schema[column]) not in expected:
            errors.append(_detail(ContractFailureCategory.INCOMPATIBLE_TYPES, f"Column '{column}' has type {_type_name(source_df.schema[column])}; expected one of {', '.join(expected)}", [column]))
    for column, nullable in contract.nullable.items():
        if column in source_df.columns and not nullable and source_df[column].null_count() > 0:
            errors.append(_detail(ContractFailureCategory.INCOMPATIBLE_TYPES, f"Column '{column}' is non-nullable but contains null values", [column]))

    if not missing_keys and contract.business_keys:
        key_frame = source_df.select(list(contract.business_keys))
        null_mask = None
        for column in contract.business_keys:
            expr = pl.col(column).is_null()
            if source_df.schema[column] == pl.String:
                expr = expr | (pl.col(column).str.strip_chars() == "")
            null_mask = expr if null_mask is None else (null_mask | expr)
        null_keys = key_frame.filter(null_mask).head(10) if null_mask is not None else pl.DataFrame()
        if null_keys.height:
            errors.append(_detail(ContractFailureCategory.NULL_BUSINESS_KEY, "Business key contains null or blank values", list(contract.business_keys), [str(row) for row in null_keys.iter_rows(named=False)]))
        duplicate = key_frame.group_by(list(contract.business_keys)).len().filter(pl.col("len") > 1)
        if duplicate.height:
            keys = [str(row) for row in duplicate.drop("len").head(10).iter_rows(named=False)]
            errors.append(_detail(ContractFailureCategory.DUPLICATE_BUSINESS_KEY, "Source contains duplicate business keys", list(contract.business_keys), keys))

    if observed_schema_version is not None and str(observed_schema_version) != str(contract.schema_version):
        warnings.append(_detail(ContractFailureCategory.SCHEMA_VERSION, f"Observed schema version {observed_schema_version} differs from contract version {contract.schema_version}"))
    outcome = SchemaEvolutionOutcome.INCOMPATIBLE if errors else (SchemaEvolutionOutcome.COMPATIBLE_WITH_WARNING if warnings else SchemaEvolutionOutcome.COMPATIBLE)
    return ContractValidationResult(not errors, outcome, str(contract.schema_version), tuple(errors), tuple(warnings))


def validate_contract(source_df: pl.DataFrame, contract: DataContract, **kwargs: Any) -> ContractValidationResult:
    return validate_data_contract(source_df, contract, **kwargs)


def evaluate_schema_evolution(source_df: pl.DataFrame, contract: DataContract, **kwargs: Any) -> SchemaEvolutionOutcome:
    """Return the explicit compatible/warning/rejected schema outcome."""
    return validate_data_contract(source_df, contract, **kwargs).outcome


def quarantine_result_from_validation(result: ContractValidationResult, *, source_identity: str, run_id: str | None = None, flow_run_id: str | None = None) -> QuarantineResult:
    first = result.errors[0] if result.errors else ContractValidationErrorDetail("contract", "Input rejected")
    columns = tuple(sorted({column for error in result.errors for column in error.columns}))
    keys = tuple(key for error in result.errors for key in error.keys)[:10]
    return QuarantineResult(
        quarantine_id=f"quarantine_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}",
        failure_category=first.category,
        reason="; ".join(error.reason for error in result.errors),
        affected_columns=columns,
        affected_keys=keys,
        source_identity=Path(str(source_identity)).name if source_identity else "unknown",
        run_id=run_id,
        flow_run_id=flow_run_id,
        schema_version=result.schema_version,
        evolution_outcome=result.outcome.value,
    )


def persist_quarantine_result(result: QuarantineResult, directory: str | Path) -> Path:
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{result.quarantine_id}.json"
    path.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    return path


# Descriptive aliases keep the public contract vocabulary discoverable without
# changing the canonical implementation names.
SchemaContract = DataContract
__all__ = ["ContractFailureCategory", "ContractValidationErrorDetail", "ContractValidationException", "ContractValidationResult", "DataContract", "QuarantineResult", "SchemaContract", "SchemaEvolutionOutcome", "evaluate_schema_evolution", "persist_quarantine_result", "quarantine_result_from_validation", "validate_contract", "validate_data_contract"]
