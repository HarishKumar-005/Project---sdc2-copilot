from datetime import date
import json

import polars as pl
import pytest

from src.scd2_copilot.contracts import (
    ContractFailureCategory,
    DataContract,
    SchemaEvolutionOutcome,
    persist_quarantine_result,
    quarantine_result_from_validation,
    validate_data_contract,
)


def contract(**overrides):
    values = {
        "schema_version": "1",
        "required_columns": ("id", "name"),
        "allowed_types": {"id": ("Int64",), "name": ("String",)},
        "nullable": {"id": False, "name": True},
        "business_keys": ("id",),
        "tracked_columns": ("name",),
    }
    values.update(overrides)
    return DataContract(**values)


def test_valid_input_is_compatible():
    result = validate_data_contract(pl.DataFrame({"id": [1], "name": ["A"]}), contract())
    assert result.valid
    assert result.outcome == SchemaEvolutionOutcome.COMPATIBLE


@pytest.mark.parametrize("df, category", [
    (pl.DataFrame({"id": [1]}), ContractFailureCategory.MISSING_REQUIRED_COLUMNS),
    (pl.DataFrame({"id": [1], "name": ["A"], "extra": [True]}), ContractFailureCategory.UNEXPECTED_COLUMNS),
    (pl.DataFrame({"id": ["1"], "name": ["A"]}), ContractFailureCategory.INCOMPATIBLE_TYPES),
    (pl.DataFrame({"id": [None], "name": ["A"]}, schema={"id": pl.Int64, "name": pl.String}), ContractFailureCategory.NULL_BUSINESS_KEY),
    (pl.DataFrame({"id": [1, 1], "name": ["A", "B"]}), ContractFailureCategory.DUPLICATE_BUSINESS_KEY),
])
def test_contract_rejects_invalid_input(df, category):
    result = validate_data_contract(df, contract())
    assert not result.valid
    assert any(error.category == category.value for error in result.errors)
    assert result.outcome == SchemaEvolutionOutcome.INCOMPATIBLE


def test_invalid_tracked_columns_are_rejected():
    result = validate_data_contract(pl.DataFrame({"id": [1], "name": ["A"]}), contract(tracked_columns=("missing",)))
    assert any(error.category == ContractFailureCategory.INVALID_TRACKED_COLUMNS.value for error in result.errors)


def test_schema_evolution_can_be_compatible_with_warning():
    result = validate_data_contract(
        pl.DataFrame({"id": [1], "name": ["A"], "email": ["a@example.com"]}),
        contract(allow_unexpected_columns=True),
    )
    assert result.valid
    assert result.outcome == SchemaEvolutionOutcome.COMPATIBLE_WITH_WARNING
    assert result.warnings[0].category == ContractFailureCategory.UNEXPECTED_COLUMNS.value


def test_schema_evolution_breaking_type_change_is_rejected():
    result = validate_data_contract(pl.DataFrame({"id": ["1"], "name": ["A"]}), contract(schema_version="2"))
    assert result.outcome == SchemaEvolutionOutcome.INCOMPATIBLE


def test_quarantine_result_is_structured_and_does_not_contain_raw_data(tmp_path):
    result = validate_data_contract(pl.DataFrame({"id": [1]}), contract())
    quarantine = quarantine_result_from_validation(result, source_identity="C:/secret/path/source.csv", run_id="run-1")
    path = persist_quarantine_result(quarantine, tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["source_identity"] == "source.csv"
    assert payload["run_id"] == "run-1"
    assert "raw" not in json.dumps(payload).lower()
    assert payload["failure_category"] == ContractFailureCategory.MISSING_REQUIRED_COLUMNS.value


def test_contract_gate_fails_before_detect_changes(monkeypatch, tmp_path):
    from src.scd2_copilot.config import Settings
    from src.scd2_copilot.exceptions import ContractValidationError
    from src.scd2_copilot.workflow import run_pipeline

    def should_not_run(*args, **kwargs):
        raise AssertionError("detect_changes must not run after a contract rejection")

    monkeypatch.setattr("src.scd2_copilot.workflow.detect_task", should_not_run)
    source = pl.DataFrame({"id": [1]})
    target = pl.DataFrame({"id": [1], "name": ["A"], "effective_from": [date(2026, 1, 1)], "effective_to": [None], "is_current": [True]})
    with pytest.raises(ContractValidationError) as exc_info:
        run_pipeline(
            source=source,
            target=target,
            business_key_override=["id"],
            settings=Settings(runs_directory=str(tmp_path / "runs"), quarantine_directory=str(tmp_path / "quarantine")),
            data_contract=contract(),
        )
    assert exc_info.value.quarantine_result is not None
    files = list((tmp_path / "quarantine").glob("*.json"))
    assert len(files) == 1
