"""Tests for M3.6: Idempotency, Run Identity & Execution Deduplication.

Covers:
1. Configuration settings for idempotency and fingerprint algorithm.
2. DeduplicationStatus enum and OrchestrationSummary model extension.
3. RunMetadata extension and backward compatibility with legacy M3.5 metadata.
4. Dataset digesting (files, DataFrames, in-memory buffers).
5. Execution fingerprint determinism (independent of run_id, timestamps, flow IDs).
6. Execution fingerprint sensitivity (date, mode, policy, business keys, tracked columns, dataset changes).
7. find_run_by_fingerprint and link_run_fingerprint index operations.
8. First execution creates a new run (NEW_EXECUTION).
9. Second identical execution reuses existing completed run (REUSED_EXECUTION) and bypasses engine.
10. Forced re-execution creates a new run_id while preserving fingerprint (FORCED_REEXECUTION).
11. Persistence failure does not poison future execution fingerprint lookup.
12. Incomplete or corrupted run is rejected for reuse.
13. Scheduled and manual runs produce identical fingerprint for identical logical inputs.
14. Local filesystem race handling and safe publication.
"""

from __future__ import annotations

from datetime import date, datetime
import io
import json
import os
from pathlib import Path
from typing import Any
import uuid

import polars as pl
import pytest

from src.scd2_copilot.config import Settings, get_settings
from src.scd2_copilot.models import (
    ChangeRecord,
    ChangeReport,
    ChangeType,
    DeduplicationStatus,
    DeletePolicy,
    Explanation,
    FieldChange,
    LLMMetrics,
    OrchestrationSummary,
    PipelineResult,
    SnapshotMode,
    ValidationReport,
    ValidationRule,
    ValidationStatus,
)


def test_config_idempotency_settings():
    """Settings should support idempotency_enabled and fingerprint_algorithm."""
    settings = Settings(idempotency_enabled=True, fingerprint_algorithm="sha256")
    assert settings.idempotency_enabled is True
    assert settings.fingerprint_algorithm == "sha256"


def test_deduplication_status_enum():
    """DeduplicationStatus enum defines the three authoritative execution outcomes."""
    assert DeduplicationStatus.NEW_EXECUTION.value == "new_execution"
    assert DeduplicationStatus.REUSED_EXECUTION.value == "reused_execution"
    assert DeduplicationStatus.FORCED_REEXECUTION.value == "forced_reexecution"


def test_orchestration_summary_idempotency_fields():
    """OrchestrationSummary tracks execution_fingerprint, is_reused, reused_from_run_id, and deduplication_status."""
    summary = OrchestrationSummary()
    assert summary.execution_fingerprint is None
    assert summary.is_reused is False
    assert summary.reused_from_run_id is None
    assert summary.deduplication_status == "new_execution"


def test_run_metadata_fingerprint_fields_and_backward_compatibility():
    """RunMetadata tracks fingerprint and preserves backward compatibility with legacy M3.5 metadata."""
    from src.scd2_copilot.artifacts import RunMetadata

    meta = RunMetadata(
        run_id="run_test_idemp",
        execution_fingerprint="abc123def456",
        is_reused=True,
        reused_from_run_id="run_original_001",
        deduplication_status="reused_execution",
    )
    assert meta.execution_fingerprint == "abc123def456"
    assert meta.is_reused is True
    assert meta.reused_from_run_id == "run_original_001"
    assert meta.deduplication_status == "reused_execution"
    assert meta.metadata_version == "1.1"

    # Legacy M3.5 dictionary missing execution_fingerprint and metadata_version
    legacy_dict = {
        "run_id": "run_legacy_001",
        "created_at": "2026-09-07T12:00:00",
        "row_counts": {"output": 10},
    }
    loaded = RunMetadata.from_dict(legacy_dict)
    assert loaded.run_id == "run_legacy_001"
    assert loaded.execution_fingerprint is None
    assert loaded.is_reused is False
    assert loaded.metadata_version == "1.1"


def test_dataset_digest_file_and_dataframe(tmp_path: Path):
    """compute_dataset_digest produces deterministic SHA-256 hashes for files, buffers, and DataFrames."""
    from src.scd2_copilot.artifacts import compute_dataset_digest

    # 1. File hashing
    file_a = tmp_path / "source_a.csv"
    file_a.write_text("id,name\n1,Ravi\n2,Priya\n", encoding="utf-8")

    file_b = tmp_path / "source_b.csv"
    file_b.write_text("id,name\n1,Ravi\n2,Priya\n", encoding="utf-8")

    file_diff = tmp_path / "source_diff.csv"
    file_diff.write_text("id,name\n1,Ravi\n2,Arun\n", encoding="utf-8")

    digest_a = compute_dataset_digest(file_a)
    digest_b = compute_dataset_digest(file_b)
    digest_diff = compute_dataset_digest(file_diff)

    assert digest_a == digest_b
    assert digest_a != digest_diff

    # 2. DataFrame hashing
    df1 = pl.DataFrame({"id": [1, 2], "name": ["Ravi", "Priya"]})
    df2 = pl.DataFrame({"id": [1, 2], "name": ["Ravi", "Priya"]})
    df3 = pl.DataFrame({"id": [1, 2], "name": ["Ravi", "Arun"]})

    df_digest_1 = compute_dataset_digest(df1)
    df_digest_2 = compute_dataset_digest(df2)
    df_digest_3 = compute_dataset_digest(df3)

    assert df_digest_1 == df_digest_2
    assert df_digest_1 != df_digest_3

    # 3. In-memory buffer hashing
    buf = io.BytesIO(b"id,name\n1,Ravi\n2,Priya\n")
    buf_digest = compute_dataset_digest(buf)
    assert buf_digest == digest_a
    # Ensure buffer seek position was restored
    assert buf.tell() == 0


def test_execution_fingerprint_determinism_and_sensitivity(tmp_path: Path):
    """Execution fingerprint is deterministic and sensitive to all logical inputs and configuration."""
    from src.scd2_copilot.artifacts import compute_execution_fingerprint

    src_file = tmp_path / "source.csv"
    src_file.write_text("id,name,tier\n1,Ravi,Gold\n", encoding="utf-8")

    tgt_file = tmp_path / "target.csv"
    tgt_file.write_text("id,name,tier\n1,Ravi,Silver\n", encoding="utf-8")

    base_fp = compute_execution_fingerprint(
        source=src_file,
        target=tgt_file,
        processing_date=date(2026, 6, 8),
        snapshot_mode=SnapshotMode.FULL,
        delete_policy=DeletePolicy.SOFT_DELETE,
        business_key=["id"],
        tracked_columns=["name", "tier"],
    )

    # 1. Identical parameters produce identical fingerprint
    fp_identical = compute_execution_fingerprint(
        source=src_file,
        target=tgt_file,
        processing_date="2026-06-08",
        snapshot_mode="full",
        delete_policy="soft_delete",
        business_key=["id"],
        tracked_columns=["name", "tier"],
    )
    assert base_fp == fp_identical

    # 2. Date sensitivity
    fp_diff_date = compute_execution_fingerprint(
        source=src_file,
        target=tgt_file,
        processing_date=date(2026, 6, 9),
        snapshot_mode=SnapshotMode.FULL,
        delete_policy=DeletePolicy.SOFT_DELETE,
        business_key=["id"],
        tracked_columns=["name", "tier"],
    )
    assert base_fp != fp_diff_date

    # 3. Snapshot mode sensitivity
    fp_diff_mode = compute_execution_fingerprint(
        source=src_file,
        target=tgt_file,
        processing_date=date(2026, 6, 8),
        snapshot_mode=SnapshotMode.INCREMENTAL,
        delete_policy=DeletePolicy.SOFT_DELETE,
        business_key=["id"],
        tracked_columns=["name", "tier"],
    )
    assert base_fp != fp_diff_mode

    # 4. Delete policy sensitivity
    fp_diff_policy = compute_execution_fingerprint(
        source=src_file,
        target=tgt_file,
        processing_date=date(2026, 6, 8),
        snapshot_mode=SnapshotMode.FULL,
        delete_policy=DeletePolicy.IGNORE,
        business_key=["id"],
        tracked_columns=["name", "tier"],
    )
    assert base_fp != fp_diff_policy

    # 5. Business key sensitivity
    fp_diff_key = compute_execution_fingerprint(
        source=src_file,
        target=tgt_file,
        processing_date=date(2026, 6, 8),
        snapshot_mode=SnapshotMode.FULL,
        delete_policy=DeletePolicy.SOFT_DELETE,
        business_key=["id", "name"],
        tracked_columns=["tier"],
    )
    assert base_fp != fp_diff_key

    # 6. Tracked columns sensitivity
    fp_diff_tracked = compute_execution_fingerprint(
        source=src_file,
        target=tgt_file,
        processing_date=date(2026, 6, 8),
        snapshot_mode=SnapshotMode.FULL,
        delete_policy=DeletePolicy.SOFT_DELETE,
        business_key=["id"],
        tracked_columns=["name"],
    )
    assert base_fp != fp_diff_tracked


def test_find_and_link_run_fingerprint(tmp_path: Path):
    """link_run_fingerprint writes index file and find_run_by_fingerprint retrieves it."""
    from src.scd2_copilot.artifacts import (
        find_run_by_fingerprint,
        link_run_fingerprint,
        write_run_artifacts,
    )

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    # Create dummy pipeline result and persist
    scd2_df = pl.DataFrame({
        "id": [1],
        "name": ["Ravi"],
        "effective_from": [date(2026, 6, 8)],
        "effective_to": [None],
        "is_current": [True],
    })
    change_report = ChangeReport(
        new=[ChangeRecord(business_key_values={"id": 1}, change_type=ChangeType.NEW)],
        processing_date=date(2026, 6, 8),
    )
    val_report = ValidationReport(rules=[])
    pipe_res = PipelineResult(
        change_report=change_report,
        scd2_output=scd2_df,
        validation_report=val_report,
        business_key=["id"],
        tracked_columns=["name"],
    )

    meta = write_run_artifacts(
        pipeline_result=pipe_res,
        run_id="run_link_test_001",
        base_dir=runs_dir,
        execution_fingerprint="fp_test_12345678",
    )
    assert meta.run_id == "run_link_test_001"
    assert meta.execution_fingerprint == "fp_test_12345678"

    # Fast index file should exist
    index_file = runs_dir / ".fingerprints" / "fp_test_12345678.json"
    assert index_file.is_file()

    # Fast lookup should return run metadata
    found = find_run_by_fingerprint("fp_test_12345678", base_dir=runs_dir)
    assert found is not None
    assert found.run_id == "run_link_test_001"
    assert found.execution_fingerprint == "fp_test_12345678"

    # Non-existent fingerprint returns None
    assert find_run_by_fingerprint("fp_non_existent", base_dir=runs_dir) is None


def test_pipeline_first_execution_and_second_reused_execution(tmp_path: Path):
    """First execution computes and persists; second identical execution reuses result and bypasses engine."""
    from src.scd2_copilot.workflow import run_pipeline

    runs_dir = tmp_path / "runs"
    settings = Settings(
        runs_directory=str(runs_dir),
        idempotency_enabled=True,
        llm_provider="template",
    )

    src_file = tmp_path / "source.csv"
    src_file.write_text("id,name,tier\n101,Aarav,Gold\n102,Diya,Silver\n", encoding="utf-8")

    tgt_file = tmp_path / "target.csv"
    tgt_file.write_text("id,name,tier,effective_from,effective_to,is_current\n101,Aarav,Bronze,2026-06-01,,true\n", encoding="utf-8")

    # 1. First execution -> NEW_EXECUTION
    res1 = run_pipeline(
        source=src_file,
        target=tgt_file,
        processing_date="2026-06-08",
        business_key_override=["id"],
        tracked_columns_override=["name", "tier"],
        settings=settings,
    )
    summary1 = res1.orchestration_summary
    assert summary1 is not None
    assert summary1.deduplication_status == "new_execution"
    assert summary1.is_reused is False
    assert summary1.reused_from_run_id is None
    assert summary1.execution_fingerprint is not None
    assert summary1.task_statuses.get("detect_changes") == "COMPLETED"
    assert summary1.task_statuses.get("transform_scd2") == "COMPLETED"

    fp1 = summary1.execution_fingerprint
    run1_id = summary1.run_id

    # 2. Second execution -> REUSED_EXECUTION (bypasses detect/transform/validation/explain)
    res2 = run_pipeline(
        source=src_file,
        target=tgt_file,
        processing_date="2026-06-08",
        business_key_override=["id"],
        tracked_columns_override=["name", "tier"],
        settings=settings,
    )
    summary2 = res2.orchestration_summary
    assert summary2 is not None
    assert summary2.deduplication_status == "reused_execution"
    assert summary2.is_reused is True
    assert summary2.reused_from_run_id == run1_id
    assert summary2.execution_fingerprint == fp1
    assert summary2.task_statuses.get("detect_changes") == "REUSED"
    assert summary2.task_statuses.get("transform_scd2") == "REUSED"
    assert summary2.task_statuses.get("validate_output") == "REUSED"
    assert summary2.task_statuses.get("explain_changes") == "REUSED"
    assert summary2.task_statuses.get("persist_artifacts") == "SKIPPED"

    # Verify identical output data
    assert res2.scd2_output.shape == res1.scd2_output.shape
    assert res2.scd2_output.equals(res1.scd2_output)
    assert res2.change_report.summary == res1.change_report.summary
    assert res2.validation_report.passed == res1.validation_report.passed

    # 3. Third execution with force_recompute=True -> FORCED_REEXECUTION
    res3 = run_pipeline(
        source=src_file,
        target=tgt_file,
        processing_date="2026-06-08",
        business_key_override=["id"],
        tracked_columns_override=["name", "tier"],
        settings=settings,
        force_recompute=True,
    )
    summary3 = res3.orchestration_summary
    assert summary3 is not None
    assert summary3.deduplication_status == "forced_reexecution"
    assert summary3.is_reused is False
    assert summary3.reused_from_run_id is None
    assert summary3.run_id != run1_id
    assert summary3.execution_fingerprint == fp1
    assert summary3.task_statuses.get("detect_changes") == "COMPLETED"


def test_reuse_disabled_via_flag(tmp_path: Path):
    """Disabling reuse via reuse_existing=False forces full pipeline run."""
    from src.scd2_copilot.workflow import run_pipeline

    runs_dir = tmp_path / "runs"
    settings = Settings(
        runs_directory=str(runs_dir),
        idempotency_enabled=True,
        llm_provider="template",
    )

    src_file = tmp_path / "source.csv"
    src_file.write_text("id,val\n1,Alpha\n", encoding="utf-8")
    tgt_file = tmp_path / "target.csv"
    tgt_file.write_text("id,val,effective_from,effective_to,is_current\n1,Beta,2026-01-01,,true\n", encoding="utf-8")

    res1 = run_pipeline(source=src_file, target=tgt_file, settings=settings, processing_date="2026-06-08")
    assert res1.orchestration_summary.deduplication_status == "new_execution"

    # Run with reuse_existing=False
    res2 = run_pipeline(source=src_file, target=tgt_file, settings=settings, processing_date="2026-06-08", reuse_existing=False)
    assert res2.orchestration_summary.deduplication_status == "new_execution"
    assert res2.orchestration_summary.is_reused is False


def test_failed_run_never_poisons_fingerprint_index(tmp_path: Path):
    """A run with failure status or corrupted files is never reused."""
    from src.scd2_copilot.artifacts import (
        DEFAULT_ARTIFACT_FILES,
        RunMetadata,
        find_run_by_fingerprint,
        link_run_fingerprint,
    )

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    bad_run_dir = runs_dir / "run_failed_001"
    bad_run_dir.mkdir(parents=True, exist_ok=True)

    # Save failed metadata
    meta = RunMetadata(
        run_id="run_failed_001",
        execution_fingerprint="fp_failed_999",
        failure_category="DATA_QUALITY_ERROR",
        error_message="Duplicate keys detected",
        artifact_status="failed",
    )
    with open(bad_run_dir / DEFAULT_ARTIFACT_FILES["metadata"], "w", encoding="utf-8") as f:
        json.dump(meta.to_dict(), f)

    # Manually link fingerprint
    link_run_fingerprint("fp_failed_999", "run_failed_001", base_dir=runs_dir)

    # find_run_by_fingerprint must reject failed run
    assert find_run_by_fingerprint("fp_failed_999", base_dir=runs_dir) is None


def test_corrupted_run_directory_rejected(tmp_path: Path):
    """If run directory is missing required parquet artifact, it is rejected for reuse."""
    from src.scd2_copilot.artifacts import (
        DEFAULT_ARTIFACT_FILES,
        RunMetadata,
        find_run_by_fingerprint,
        link_run_fingerprint,
    )

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    corrupted_dir = runs_dir / "run_corrupt_001"
    corrupted_dir.mkdir(parents=True, exist_ok=True)

    meta = RunMetadata(
        run_id="run_corrupt_001",
        execution_fingerprint="fp_corrupt_888",
        artifact_status="persisted",
    )
    with open(corrupted_dir / DEFAULT_ARTIFACT_FILES["metadata"], "w", encoding="utf-8") as f:
        json.dump(meta.to_dict(), f)
    # Note: no scd2_output.parquet created!

    link_run_fingerprint("fp_corrupt_888", "run_corrupt_001", base_dir=runs_dir)
    assert find_run_by_fingerprint("fp_corrupt_888", base_dir=runs_dir) is None


def test_delete_run_cleans_fingerprint_link(tmp_path: Path):
    """delete_run removes both the run folder and its fingerprint index mapping."""
    from src.scd2_copilot.artifacts import (
        delete_run,
        find_run_by_fingerprint,
        write_run_artifacts,
    )

    runs_dir = tmp_path / "runs"
    scd2_df = pl.DataFrame({"id": [1], "name": ["Alpha"], "effective_from": [date(2026, 6, 8)], "effective_to": [None], "is_current": [True]})
    res = PipelineResult(
        change_report=ChangeReport(new=[ChangeRecord(business_key_values={"id": 1}, change_type=ChangeType.NEW)], processing_date=date(2026, 6, 8)),
        scd2_output=scd2_df,
        validation_report=ValidationReport(rules=[]),
        business_key=["id"],
        tracked_columns=["name"],
    )
    meta = write_run_artifacts(pipeline_result=res, run_id="run_to_delete", base_dir=runs_dir, execution_fingerprint="fp_delete_me")
    assert find_run_by_fingerprint("fp_delete_me", base_dir=runs_dir) is not None

    # Delete run
    assert delete_run("run_to_delete", base_dir=runs_dir) is True
    # Fast index file should be removed
    assert not (runs_dir / ".fingerprints" / "fp_delete_me.json").exists()
    # Lookup should return None
    assert find_run_by_fingerprint("fp_delete_me", base_dir=runs_dir) is None


def test_fallback_scan_heals_missing_index(tmp_path: Path):
    """If index file is missing, fallback directory scan locates run and heals index."""
    from src.scd2_copilot.artifacts import (
        find_run_by_fingerprint,
        write_run_artifacts,
    )

    runs_dir = tmp_path / "runs"
    scd2_df = pl.DataFrame({"id": [1], "name": ["Alpha"], "effective_from": [date(2026, 6, 8)], "effective_to": [None], "is_current": [True]})
    res = PipelineResult(
        change_report=ChangeReport(new=[ChangeRecord(business_key_values={"id": 1}, change_type=ChangeType.NEW)], processing_date=date(2026, 6, 8)),
        scd2_output=scd2_df,
        validation_report=ValidationReport(rules=[]),
        business_key=["id"],
        tracked_columns=["name"],
    )
    write_run_artifacts(pipeline_result=res, run_id="run_heal_001", base_dir=runs_dir, execution_fingerprint="fp_heal_me")

    index_file = runs_dir / ".fingerprints" / "fp_heal_me.json"
    assert index_file.is_file()

    # Manually delete index file to simulate index loss
    index_file.unlink()
    assert not index_file.is_file()

    # find_run_by_fingerprint should fall back to scanning runs_dir, locate run, and heal index
    found = find_run_by_fingerprint("fp_heal_me", base_dir=runs_dir)
    assert found is not None
    assert found.run_id == "run_heal_001"
    # Verify index was healed
    assert index_file.is_file()


def test_dataset_content_change_produces_distinct_run(tmp_path: Path):
    """Modifying source data changes fingerprint and triggers new execution."""
    from src.scd2_copilot.workflow import run_pipeline

    runs_dir = tmp_path / "runs"
    settings = Settings(runs_directory=str(runs_dir), idempotency_enabled=True, llm_provider="template")

    src_file = tmp_path / "source.csv"
    src_file.write_text("id,status\n1,Active\n", encoding="utf-8")
    tgt_file = tmp_path / "target.csv"
    tgt_file.write_text("id,status,effective_from,effective_to,is_current\n1,Pending,2026-01-01,,true\n", encoding="utf-8")

    res1 = run_pipeline(source=src_file, target=tgt_file, settings=settings, processing_date="2026-06-08")
    fp1 = res1.orchestration_summary.execution_fingerprint

    # Change source data
    src_file.write_text("id,status\n1,Suspended\n", encoding="utf-8")
    res2 = run_pipeline(source=src_file, target=tgt_file, settings=settings, processing_date="2026-06-08")
    fp2 = res2.orchestration_summary.execution_fingerprint

    assert fp1 != fp2
    assert res2.orchestration_summary.deduplication_status == "new_execution"
    assert res2.orchestration_summary.is_reused is False


def test_deployment_parameters_idempotency():
    """DeploymentParameters supports reuse_existing and force_recompute with safe defaults."""
    from src.scd2_copilot.deployment import DeploymentParameters

    params = DeploymentParameters(source="data/source.csv", target="data/target.csv")
    assert params.reuse_existing is True
    assert params.force_recompute is False

    params_forced = DeploymentParameters(
        source="data/source.csv",
        target="data/target.csv",
        force_recompute=True,
        reuse_existing=False,
    )
    assert params_forced.force_recompute is True
    assert params_forced.reuse_existing is False


def test_deployment_cli_idempotency_flags():
    """CLI parser correctly parses --no-reuse and --force-recompute flags."""
    from src.scd2_copilot.deployment import parse_args

    args_default = parse_args(["--trigger"])
    assert args_default.no_reuse is False
    assert args_default.force_recompute is False

    args_custom = parse_args(["--trigger", "--no-reuse", "--force-recompute"])
    assert args_custom.no_reuse is True
    assert args_custom.force_recompute is True


def test_composite_business_key_idempotency(tmp_path: Path):
    """Composite business keys produce stable fingerprints and correctly reuse runs."""
    from src.scd2_copilot.workflow import run_pipeline

    runs_dir = tmp_path / "runs"
    settings = Settings(runs_directory=str(runs_dir), idempotency_enabled=True, llm_provider="template")

    src_file = tmp_path / "source.csv"
    src_file.write_text("region,store_id,revenue\nNorth,101,5000\nSouth,102,7000\n", encoding="utf-8")
    tgt_file = tmp_path / "target.csv"
    tgt_file.write_text("region,store_id,revenue,effective_from,effective_to,is_current\nNorth,101,4500,2026-01-01,,true\n", encoding="utf-8")

    res1 = run_pipeline(
        source=src_file,
        target=tgt_file,
        business_key_override=["region", "store_id"],
        tracked_columns_override=["revenue"],
        settings=settings,
        processing_date="2026-06-08",
    )
    assert res1.orchestration_summary.deduplication_status == "new_execution"

    res2 = run_pipeline(
        source=src_file,
        target=tgt_file,
        business_key_override=["region", "store_id"],
        tracked_columns_override=["revenue"],
        settings=settings,
        processing_date="2026-06-08",
    )
    assert res2.orchestration_summary.deduplication_status == "reused_execution"
    assert res2.orchestration_summary.is_reused is True
    assert res2.scd2_output.equals(res1.scd2_output)


def test_scheduled_and_manual_runs_identical_fingerprint(tmp_path: Path):
    """Scheduled and manual runs with identical inputs yield identical execution fingerprints."""
    from src.scd2_copilot.artifacts import compute_execution_fingerprint

    src_file = tmp_path / "source.csv"
    src_file.write_text("id,val\n1,X\n", encoding="utf-8")
    tgt_file = tmp_path / "target.csv"
    tgt_file.write_text("id,val,effective_from,effective_to,is_current\n1,Y,2026-01-01,,true\n", encoding="utf-8")

    fp_manual = compute_execution_fingerprint(
        source=src_file,
        target=tgt_file,
        processing_date=date(2026, 6, 8),
        snapshot_mode=SnapshotMode.FULL,
        delete_policy=DeletePolicy.SOFT_DELETE,
        business_key=["id"],
        tracked_columns=["val"],
    )

    fp_scheduled = compute_execution_fingerprint(
        source=src_file,
        target=tgt_file,
        processing_date="2026-06-08",
        snapshot_mode="full",
        delete_policy="soft_delete",
        business_key=["id"],
        tracked_columns=["val"],
    )

    assert fp_manual == fp_scheduled


def test_concurrent_fingerprint_linking(tmp_path: Path):
    """Multiple rapid / concurrent calls to link_run_fingerprint safely update index."""
    from src.scd2_copilot.artifacts import find_run_by_fingerprint, link_run_fingerprint
    import threading

    runs_dir = tmp_path / "runs"

    def link_fp(run_suffix: int):
        link_run_fingerprint("fp_concurrent_shared", f"run_worker_{run_suffix}", base_dir=runs_dir)

    threads = [threading.Thread(target=link_fp, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # The index file must be valid JSON and point to one of the workers
    index_file = runs_dir / ".fingerprints" / "fp_concurrent_shared.json"
    assert index_file.is_file()
    with open(index_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["fingerprint"] == "fp_concurrent_shared"
    assert data["run_id"].startswith("run_worker_")


def test_empty_dataframe_digest():
    """Empty DataFrames compute deterministic digest without errors."""
    from src.scd2_copilot.artifacts import compute_dataset_digest

    df_empty1 = pl.DataFrame(schema={"id": pl.Int64, "name": pl.Utf8})
    df_empty2 = pl.DataFrame(schema={"id": pl.Int64, "name": pl.Utf8})
    df_empty_diff_schema = pl.DataFrame(schema={"id": pl.Int64, "tier": pl.Utf8})

    d1 = compute_dataset_digest(df_empty1)
    d2 = compute_dataset_digest(df_empty2)
    d3 = compute_dataset_digest(df_empty_diff_schema)

    assert d1 == d2
    assert d1 != d3


def test_fingerprint_excludes_ephemeral_fields(tmp_path: Path):
    """Fingerprint computation does not depend on ephemeral runtime attributes."""
    from src.scd2_copilot.artifacts import compute_execution_fingerprint

    src_file = tmp_path / "source.csv"
    src_file.write_text("id,val\n1,Alpha\n", encoding="utf-8")
    tgt_file = tmp_path / "target.csv"
    tgt_file.write_text("id,val,effective_from,effective_to,is_current\n1,Alpha,2026-01-01,,true\n", encoding="utf-8")

    fp1 = compute_execution_fingerprint(
        source=src_file,
        target=tgt_file,
        processing_date=date(2026, 6, 8),
        snapshot_mode=SnapshotMode.FULL,
        delete_policy=DeletePolicy.SOFT_DELETE,
        business_key=["id"],
        tracked_columns=["val"],
    )

    # Calling again with identical parameters yields identical fingerprint regardless of time or process
    fp2 = compute_execution_fingerprint(
        source=src_file,
        target=tgt_file,
        processing_date="2026-06-08",
        snapshot_mode="full",
        delete_policy="soft_delete",
        business_key=["id"],
        tracked_columns=["val"],
    )

    assert fp1 == fp2


def test_reused_execution_preserves_full_fidelity(tmp_path: Path):
    """Reused pipeline execution completely restores all result attributes."""
    from src.scd2_copilot.workflow import run_pipeline

    runs_dir = tmp_path / "runs"
    settings = Settings(runs_directory=str(runs_dir), idempotency_enabled=True, llm_provider="template")

    src_file = tmp_path / "source.csv"
    src_file.write_text("id,name,score\n1,Alice,95\n2,Bob,80\n", encoding="utf-8")
    tgt_file = tmp_path / "target.csv"
    tgt_file.write_text("id,name,score,effective_from,effective_to,is_current\n1,Alice,90,2026-01-01,,true\n", encoding="utf-8")

    res1 = run_pipeline(source=src_file, target=tgt_file, settings=settings, processing_date="2026-06-08")
    res2 = run_pipeline(source=src_file, target=tgt_file, settings=settings, processing_date="2026-06-08")

    assert res2.orchestration_summary.is_reused is True
    assert res2.orchestration_summary.reused_from_run_id == res1.orchestration_summary.run_id
    assert res2.orchestration_summary.deduplication_status == "reused_execution"
    assert res2.scd2_output.equals(res1.scd2_output)
    assert len(res2.explanations) == len(res1.explanations)
    assert res2.provider_used == res1.provider_used
    assert res2.validation_report.passed == res1.validation_report.passed
    assert res2.validation_report.summary == res1.validation_report.summary
