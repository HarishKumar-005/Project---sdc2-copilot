"""Decoupled artifact persistence service for SCD2 Copilot pipeline runs.

Provides durable, atomic persistence of pipeline execution outputs and structured
retrieval of historical runs without recomputation.

Storage layout:
    data/runs/<run_id>/
        metadata.json       - Run metadata, execution parameters, row counts, validation summary, AI metrics
        scd2_output.parquet - Full SCD2 DataFrame stored natively in Parquet format with zstd compression
        changes.json        - Structured change report summaries and change records
        validation.json     - Full validation report with rule statuses and messages
        explanations.json   - AI and template explanations with usage metrics
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import time
from typing import Any, BinaryIO, Optional, Union
import uuid

import polars as pl

from .config import Settings, get_settings
from .explain import ExplainResult
from .models import (
    ChangeRecord,
    ChangeReport,
    ChangeType,
    DeletePolicy,
    Explanation,
    FieldChange,
    LLMMetrics,
    PipelineResult,
    SnapshotMode,
    ValidationReport,
    ValidationRule,
    ValidationStatus,
)

logger = logging.getLogger(__name__)

DEFAULT_ARTIFACT_FILES = {
    "metadata": "metadata.json",
    "scd2_output": "scd2_output.parquet",
    "changes": "changes.json",
    "validation": "validation.json",
    "explanations": "explanations.json",
}


@dataclass
class RunMetadata:
    """Metadata describing a persisted pipeline run."""

    run_id: str
    flow_run_id: Optional[str] = None
    flow_run_name: Optional[str] = None
    deployment_id: Optional[str] = None
    deployment_name: Optional[str] = None
    trigger_type: str = "manual"  # "manual" or "scheduled"
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    total_duration_seconds: float = 0.0
    processing_date: Optional[str] = None
    snapshot_mode: Optional[str] = None
    delete_policy: Optional[str] = None
    business_key: list[str] = field(default_factory=list)
    tracked_columns: list[str] = field(default_factory=list)
    source_path: Optional[str] = None
    target_path: Optional[str] = None
    row_counts: dict[str, int] = field(default_factory=dict)
    change_counts: dict[str, int] = field(default_factory=dict)
    validation_summary: dict[str, Any] = field(default_factory=dict)
    ai_status: str = "none"
    ai_provider: Optional[str] = None
    ai_model: Optional[str] = None
    ai_fallback_count: int = 0
    failure_category: Optional[str] = None
    error_message: Optional[str] = None
    warnings: list[str] = field(default_factory=list)
    artifact_status: str = "persisted"
    execution_fingerprint: Optional[str] = None
    metadata_version: str = "1.1"
    is_reused: bool = False
    reused_from_run_id: Optional[str] = None
    deduplication_status: str = "new_execution"
    artifact_directory: str = ""
    artifact_files: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ARTIFACT_FILES))
    created_by: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert metadata to JSON-serializable dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunMetadata:
        """Construct RunMetadata from dictionary, ignoring unexpected keys."""
        valid_keys = cls.__dataclass_fields__.keys()
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        if "metadata_version" not in filtered:
            filtered["metadata_version"] = "1.1"
        return cls(**filtered)


@dataclass
class PersistedRun:
    """Complete retrieved contents of a persisted pipeline run."""

    metadata: RunMetadata
    scd2_output: pl.DataFrame
    change_report: ChangeReport
    validation_report: ValidationReport
    explain_result: ExplainResult


def generate_run_id() -> str:
    """Generate a unique, chronological run identifier."""
    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    short_uuid = uuid.uuid4().hex[:8]
    return f"run_{now_str}_{short_uuid}"


def _serialize_change_record(rec: ChangeRecord) -> dict[str, Any]:
    return {
        "business_key_values": rec.business_key_values,
        "change_type": rec.change_type.value if hasattr(rec.change_type, "value") else str(rec.change_type),
        "field_changes": [
            {
                "column": fc.column,
                "old_value": str(fc.old_value) if isinstance(fc.old_value, (date, datetime)) else fc.old_value,
                "new_value": str(fc.new_value) if isinstance(fc.new_value, (date, datetime)) else fc.new_value,
            }
            for fc in (rec.field_changes or [])
        ],
    }


def _deserialize_change_record(data: dict[str, Any]) -> ChangeRecord:
    raw_type = data.get("change_type", "new")
    try:
        ctype = ChangeType(raw_type)
    except ValueError:
        ctype = ChangeType.NEW

    fcs = [
        FieldChange(
            column=fc["column"],
            old_value=fc.get("old_value"),
            new_value=fc.get("new_value"),
        )
        for fc in data.get("field_changes", [])
    ]
    return ChangeRecord(
        business_key_values=data.get("business_key_values", {}),
        change_type=ctype,
        field_changes=fcs,
    )


# ── Idempotency & Execution Fingerprinting (M3.6) ─────────────


def compute_dataset_digest(data: Union[str, Path, BinaryIO, pl.DataFrame]) -> str:
    """Compute deterministic SHA-256 digest of input dataset (file, buffer, or DataFrame).

    For DataFrames, uses Polars vectorized hash_rows() combined with schema and shape
    for sub-millisecond hashing without Python conversion overhead.
    For streams/buffers, maintains initial stream position.
    """
    if isinstance(data, pl.DataFrame):
        hasher = hashlib.sha256()
        schema_str = json.dumps([(name, str(dtype)) for name, dtype in data.schema.items()], sort_keys=True)
        hasher.update(schema_str.encode("utf-8"))
        hasher.update(f"{data.shape[0]}x{data.shape[1]}".encode("utf-8"))
        if data.height > 0:
            hasher.update(data.hash_rows().to_numpy().tobytes())
        return hasher.hexdigest()

    if hasattr(data, "read"):
        pos = data.tell() if hasattr(data, "tell") else None
        hasher = hashlib.sha256()
        carry = b""
        while True:
            chunk = data.read(65536)
            if not chunk:
                if carry:
                    hasher.update(carry.replace(b"\r\n", b"\n"))
                break
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            buf = carry + chunk
            if buf.endswith(b"\r"):
                carry = b"\r"
                to_process = buf[:-1]
            else:
                carry = b""
                to_process = buf
            hasher.update(to_process.replace(b"\r\n", b"\n"))
        if pos is not None and hasattr(data, "seek"):
            data.seek(pos)
        return hasher.hexdigest()

    if isinstance(data, (str, Path)):
        p = Path(data)
        if p.is_file():
            hasher = hashlib.sha256()
            is_parquet = p.suffix.lower() == ".parquet"
            with open(p, "rb") as f:
                carry = b""
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        if carry:
                            if not is_parquet:
                                carry = carry.replace(b"\r\n", b"\n")
                            hasher.update(carry)
                        break
                    buf = carry + chunk
                    if not is_parquet and buf.endswith(b"\r"):
                        carry = b"\r"
                        to_process = buf[:-1]
                    else:
                        carry = b""
                        to_process = buf
                    if not is_parquet:
                        to_process = to_process.replace(b"\r\n", b"\n")
                    hasher.update(to_process)
            return hasher.hexdigest()
        elif p.exists():
            raise ValueError(f"Path {p} is not a regular file")
        elif isinstance(data, str) and ("\n" in data or "," in data):
            norm_str = data.replace("\r\n", "\n")
            return hashlib.sha256(norm_str.encode("utf-8")).hexdigest()
        else:
            raise FileNotFoundError(f"File not found: {p}")

    if data is None:
        return hashlib.sha256(b"__none__").hexdigest()

    raise TypeError(f"Unsupported data type for digest: {type(data)}")


def compute_execution_fingerprint(
    source: Any,
    target: Any,
    processing_date: Union[str, date],
    snapshot_mode: Union[str, SnapshotMode],
    delete_policy: Union[str, DeletePolicy],
    business_key: list[str],
    tracked_columns: list[str],
    algorithm: str = "sha256",
) -> str:
    """Compute canonical execution fingerprint uniquely identifying the logical inputs.

    Strictly excludes:
    - run_id
    - flow_run_id / Prefect IDs
    - execution timestamps (started_at, completed_at)
    - durations and memory metrics
    - LLM explanation text and metrics
    - host / environment specifics

    Includes:
    - source dataset digest
    - target dataset digest
    - normalized processing date (YYYY-MM-DD)
    - normalized snapshot mode (full / incremental)
    - normalized delete policy (soft_delete / ignore)
    - normalized business key columns
    - normalized tracked columns
    """
    source_digest = compute_dataset_digest(source)
    target_digest = compute_dataset_digest(target)

    if isinstance(processing_date, (date, datetime)):
        proc_date_str = processing_date.strftime("%Y-%m-%d")
    else:
        proc_date_str = str(processing_date).strip()

    snap_mode_str = snapshot_mode.value if hasattr(snapshot_mode, "value") else str(snapshot_mode).lower().strip()
    del_pol_str = delete_policy.value if hasattr(delete_policy, "value") else str(delete_policy).lower().strip()

    norm_business_key = [str(k).strip() for k in business_key]
    norm_tracked_columns = sorted([str(c).strip() for c in tracked_columns])

    canonical_dict = {
        "source_digest": source_digest,
        "target_digest": target_digest,
        "processing_date": proc_date_str,
        "snapshot_mode": snap_mode_str,
        "delete_policy": del_pol_str,
        "business_key": norm_business_key,
        "tracked_columns": norm_tracked_columns,
    }

    serialized = json.dumps(canonical_dict, sort_keys=True, separators=(",", ":"))
    if algorithm.lower() == "sha256":
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    else:
        h = hashlib.new(algorithm.lower())
        h.update(serialized.encode("utf-8"))
        return h.hexdigest()


def get_fingerprints_dir(base_dir: Optional[Union[str, Path]] = None) -> Path:
    """Return the directory used for fast fingerprint indexing."""
    if base_dir is None:
        runs_dir = get_settings().get_runs_dir()
    else:
        runs_dir = Path(base_dir)
    fp_dir = runs_dir / ".fingerprints"
    fp_dir.mkdir(parents=True, exist_ok=True)
    return fp_dir


def link_run_fingerprint(
    fingerprint: str,
    run_id: str,
    base_dir: Optional[Union[str, Path]] = None,
) -> None:
    """Record an execution fingerprint index mapping pointing to a completed run_id.

    Uses atomic temporary file write with retry on contention to ensure safe concurrent operations.
    """
    if not fingerprint or not run_id:
        return
    fp_dir = get_fingerprints_dir(base_dir=base_dir)
    target_file = fp_dir / f"{fingerprint}.json"
    tmp_file = fp_dir / f".tmp_{fingerprint}_{uuid.uuid4().hex[:6]}.json"

    payload = {
        "fingerprint": fingerprint,
        "run_id": run_id,
        "linked_at": datetime.now().isoformat(),
    }
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

        for attempt in range(5):
            try:
                os.replace(str(tmp_file), str(target_file))
                logger.debug("Linked fingerprint %s to run %s", fingerprint, run_id)
                return
            except PermissionError:
                if attempt < 4:
                    time.sleep(0.02 * (attempt + 1))
                else:
                    logger.warning("Could not atomically replace fingerprint link %s after 5 attempts", target_file)
    finally:
        if tmp_file.exists():
            tmp_file.unlink(missing_ok=True)


def find_run_by_fingerprint(
    fingerprint: str,
    base_dir: Optional[Union[str, Path]] = None,
) -> Optional[RunMetadata]:
    """Find the most recent valid, completed run matching an execution fingerprint.

    Checks fast index in data/runs/.fingerprints/ first. If not found, falls back
    to scanning run directories and updates the index for subsequent O(1) lookups.
    Only returns runs that completed successfully without failure.
    """
    if not fingerprint:
        return None

    if base_dir is None:
        runs_dir = get_settings().get_runs_dir()
    else:
        runs_dir = Path(base_dir)

    fp_dir = runs_dir / ".fingerprints"
    index_file = fp_dir / f"{fingerprint}.json"

    # 1. Fast index lookup
    if index_file.is_file():
        try:
            with open(index_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            indexed_run_id = data.get("run_id")
            if indexed_run_id and run_exists(indexed_run_id, base_dir=runs_dir):
                meta = get_run_metadata(indexed_run_id, base_dir=runs_dir)
                if meta.artifact_status == "persisted" and not meta.failure_category:
                    return meta
                else:
                    logger.warning(
                        "Indexed run %s for fingerprint %s has failed/corrupted status; ignoring",
                        indexed_run_id,
                        fingerprint,
                    )
        except Exception as exc:
            logger.warning("Failed reading fingerprint index %s: %s", index_file, exc)

    # 2. Fallback scan of all runs
    try:
        all_runs = list_runs(base_dir=runs_dir)
        for run in all_runs:
            if (
                run.execution_fingerprint == fingerprint
                and run.artifact_status == "persisted"
                and not run.failure_category
                and run_exists(run.run_id, base_dir=runs_dir)
            ):
                try:
                    link_run_fingerprint(fingerprint, run.run_id, base_dir=runs_dir)
                except Exception:
                    pass
                return run
    except Exception as exc:
        logger.warning("Error during fingerprint fallback scan: %s", exc)

    return None


def write_run_artifacts(
    pipeline_result: PipelineResult,
    run_id: Optional[str] = None,
    base_dir: Optional[Union[str, Path]] = None,
    source_path: Optional[str] = None,
    target_path: Optional[str] = None,
    processing_date: Optional[Union[str, date]] = None,
    snapshot_mode: Optional[Union[str, SnapshotMode]] = None,
    delete_policy: Optional[Union[str, DeletePolicy]] = None,
    overwrite: bool = False,
    settings: Optional[Settings] = None,
    execution_fingerprint: Optional[str] = None,
    is_reused: bool = False,
    reused_from_run_id: Optional[str] = None,
    deduplication_status: str = "new_execution",
    created_by: Optional[dict[str, Any]] = None,
) -> RunMetadata:
    """Persist pipeline outputs and metadata to isolated filesystem directory with write atomicity.

    Args:
        pipeline_result: Output from run_pipeline.
        run_id: Unique run ID (auto-generated if None).
        base_dir: Base directory for runs (defaults to settings.runs_directory).
        source_path: Input source path for audit trail.
        target_path: Input target path for audit trail.
        processing_date: Pipeline processing date.
        snapshot_mode: Snapshot mode used.
        delete_policy: Delete policy used.
        overwrite: If True, allows replacing an existing run folder.
        settings: Application settings.

    Returns:
        RunMetadata instance describing persisted artifacts.

    Raises:
        FileExistsError: If run_id already exists and overwrite is False.
        ValueError: If scd2_output is not a valid Polars DataFrame.
        IOError: If writing any artifact file fails.
    """
    if settings is None:
        settings = get_settings()

    if base_dir is None:
        runs_dir = settings.get_runs_dir()
    else:
        runs_dir = Path(base_dir)

    runs_dir.mkdir(parents=True, exist_ok=True)

    if not run_id:
        run_id = generate_run_id()

    final_dir = runs_dir / run_id
    if final_dir.exists() and not overwrite:
        raise FileExistsError(f"Run '{run_id}' already exists at {final_dir}")

    # Stage write in a hidden temporary directory to ensure atomicity
    staging_dir = runs_dir / f".tmp_{run_id}_{uuid.uuid4().hex[:6]}"
    staging_dir.mkdir(parents=True, exist_ok=True)

    try:
        # 1. Parquet SCD2 output
        parquet_filename = DEFAULT_ARTIFACT_FILES["scd2_output"]
        parquet_path = staging_dir / parquet_filename
        scd2_df = pipeline_result.scd2_output
        if not isinstance(scd2_df, pl.DataFrame):
            raise ValueError(f"Expected scd2_output to be polars.DataFrame, got {type(scd2_df)}")
        scd2_df.write_parquet(parquet_path, compression="zstd")

        # 2. Changes JSON
        changes_filename = DEFAULT_ARTIFACT_FILES["changes"]
        changes_path = staging_dir / changes_filename
        change_report = pipeline_result.change_report

        # Serialize change records (up to safe limits, full summary always preserved)
        changes_payload: dict[str, Any] = {
            "summary": dict(change_report.summary),
            "processing_date": str(change_report.processing_date),
            "snapshot_mode": change_report.snapshot_mode.value if hasattr(change_report.snapshot_mode, "value") else str(change_report.snapshot_mode),
            "delete_policy": change_report.delete_policy.value if hasattr(change_report.delete_policy, "value") else str(change_report.delete_policy),
            "changed": [_serialize_change_record(r) for r in change_report.changed[:10000]],
            "new": [_serialize_change_record(r) for r in change_report.new[:10000]],
            "deleted": [_serialize_change_record(r) for r in change_report.deleted[:10000]],
            "unchanged_count": len(change_report.unchanged),
        }
        with open(changes_path, "w", encoding="utf-8") as f:
            json.dump(changes_payload, f, indent=2)

        # 3. Validation JSON
        val_filename = DEFAULT_ARTIFACT_FILES["validation"]
        val_path = staging_dir / val_filename
        val_report = pipeline_result.validation_report
        val_payload = {
            "passed": val_report.passed,
            "summary": dict(val_report.summary),
            "rules": [
                {
                    "name": r.name,
                    "status": r.status.value if hasattr(r.status, "value") else str(r.status),
                    "message": r.message,
                    "details": r.details,
                }
                for r in val_report.rules
            ],
        }
        with open(val_path, "w", encoding="utf-8") as f:
            json.dump(val_payload, f, indent=2)

        # 4. Explanations JSON
        exp_filename = DEFAULT_ARTIFACT_FILES["explanations"]
        exp_path = staging_dir / exp_filename
        exp_result = pipeline_result.explain_result or ExplainResult(
            explanations=pipeline_result.explanations,
            provider_used=pipeline_result.provider_used or "template",
            metrics=pipeline_result.metrics,
        )

        metrics_payload = None
        if exp_result.metrics is not None:
            m = exp_result.metrics
            metrics_payload = {
                "provider": m.provider,
                "model": m.model,
                "prompt_tokens": m.prompt_tokens,
                "completion_tokens": m.completion_tokens,
                "total_tokens": m.total_tokens,
                "estimated_cost": m.estimated_cost,
                "request_duration": m.request_duration,
                "num_changes_explained": m.num_changes_explained,
                "avg_tokens_per_change": m.avg_tokens_per_change,
                "is_estimated": m.is_estimated,
            }

        exp_payload = {
            "provider_used": exp_result.provider_used,
            "fallback_count": exp_result.fallback_count,
            "warnings": exp_result.warnings,
            "metrics": metrics_payload,
            "explanations": [
                {
                    "business_key_values": e.business_key_values,
                    "change_type": e.change_type.value if hasattr(e.change_type, "value") else str(e.change_type),
                    "text": e.text,
                    "provider": e.provider,
                }
                for e in exp_result.explanations
            ],
        }
        with open(exp_path, "w", encoding="utf-8") as f:
            json.dump(exp_payload, f, indent=2)

        # 5. Metadata JSON
        orch = pipeline_result.orchestration_summary
        meta_filename = DEFAULT_ARTIFACT_FILES["metadata"]
        meta_path = staging_dir / meta_filename

        resolved_proc_date = str(processing_date or (orch and orch.completed_at and orch.completed_at.date()) or date.today())
        resolved_snapshot = str(snapshot_mode or change_report.snapshot_mode.value)
        resolved_delete = str(delete_policy or change_report.delete_policy.value)

        # Resolve idempotency fields from orchestration_summary if not explicitly passed
        if orch:
            if execution_fingerprint is None and orch.execution_fingerprint:
                execution_fingerprint = orch.execution_fingerprint
            if not is_reused and orch.is_reused:
                is_reused = orch.is_reused
            if reused_from_run_id is None and orch.reused_from_run_id:
                reused_from_run_id = orch.reused_from_run_id
            if deduplication_status == "new_execution" and orch.deduplication_status != "new_execution":
                deduplication_status = orch.deduplication_status

        metadata = RunMetadata(
            run_id=run_id,
            flow_run_id=orch.flow_run_id if orch else None,
            flow_run_name=orch.flow_run_name if orch else None,
            deployment_id=orch.deployment_id if orch else None,
            deployment_name=orch.deployment_name if orch else None,
            trigger_type=orch.trigger_type if orch else "manual",
            started_at=orch.started_at.isoformat() if orch and orch.started_at else None,
            completed_at=orch.completed_at.isoformat() if orch and orch.completed_at else None,
            created_at=datetime.now().isoformat(),
            total_duration_seconds=orch.total_duration_seconds if orch else (pipeline_result.execution_time or 0.0),
            processing_date=resolved_proc_date,
            snapshot_mode=resolved_snapshot,
            delete_policy=resolved_delete,
            business_key=list(pipeline_result.business_key),
            tracked_columns=list(pipeline_result.tracked_columns),
            source_path=str(source_path) if source_path else None,
            target_path=str(target_path) if target_path else None,
            row_counts=dict(orch.row_counts) if orch else {"output": scd2_df.height},
            change_counts=dict(change_report.summary),
            validation_summary=dict(val_report.summary) if val_report else {},
            ai_status=orch.ai_status if orch else "none",
            ai_provider=exp_result.provider_used,
            ai_model=orch.ai_model if orch else (exp_result.metrics.model if exp_result.metrics else None),
            ai_fallback_count=exp_result.fallback_count,
            failure_category=orch.failure_category if orch else None,
            error_message=orch.error_message if orch else None,
            warnings=list(orch.warnings) if orch else list(exp_result.warnings),
            execution_fingerprint=execution_fingerprint,
            metadata_version="1.1",
            is_reused=is_reused,
            reused_from_run_id=reused_from_run_id,
            deduplication_status=deduplication_status,
            artifact_directory=str(final_dir),
            artifact_files=dict(DEFAULT_ARTIFACT_FILES),
            created_by=created_by or (orch.created_by if orch and hasattr(orch, "created_by") else None),
        )

        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(metadata.to_dict(), f, indent=2)

        # Validate that all required files exist and are non-empty
        for key, fname in DEFAULT_ARTIFACT_FILES.items():
            fpath = staging_dir / fname
            if not fpath.is_file() or fpath.stat().st_size == 0:
                raise IOError(f"Staged artifact file {fname} is missing or empty")

        # Atomic promotion of staging directory
        if final_dir.exists() and overwrite:
            backup_dir = runs_dir / f".backup_{run_id}_{uuid.uuid4().hex[:6]}"
            os.replace(str(final_dir), str(backup_dir))
            try:
                os.replace(str(staging_dir), str(final_dir))
                shutil.rmtree(str(backup_dir), ignore_errors=True)
            except Exception:
                os.replace(str(backup_dir), str(final_dir))
                raise
        else:
            os.replace(str(staging_dir), str(final_dir))

        # Link execution fingerprint in fast index if run succeeded without failure
        if execution_fingerprint and not metadata.failure_category:
            try:
                link_run_fingerprint(execution_fingerprint, run_id, base_dir=runs_dir)
            except Exception as e:
                logger.warning("Failed to link execution fingerprint %s: %s", execution_fingerprint, e)

        logger.info("Persisted run '%s' to %s", run_id, final_dir)
        return metadata

    except Exception as exc:
        shutil.rmtree(str(staging_dir), ignore_errors=True)
        logger.error("Failed to persist run '%s': %s", run_id, exc)
        raise


def run_exists(run_id: str, base_dir: Optional[Union[str, Path]] = None) -> bool:
    """Check if a complete, valid run exists on disk."""
    if not run_id:
        return False
    if base_dir is None:
        runs_dir = get_settings().get_runs_dir()
    else:
        runs_dir = Path(base_dir)

    target_dir = runs_dir / run_id
    if not target_dir.is_dir():
        return False

    meta_file = target_dir / DEFAULT_ARTIFACT_FILES["metadata"]
    parquet_file = target_dir / DEFAULT_ARTIFACT_FILES["scd2_output"]
    return meta_file.is_file() and parquet_file.is_file()


def get_run_metadata(run_id: str, base_dir: Optional[Union[str, Path]] = None) -> RunMetadata:
    """Retrieve RunMetadata quickly without reading the Parquet DataFrame into memory."""
    if base_dir is None:
        runs_dir = get_settings().get_runs_dir()
    else:
        runs_dir = Path(base_dir)

    target_dir = runs_dir / run_id
    meta_path = target_dir / DEFAULT_ARTIFACT_FILES["metadata"]
    if not meta_path.is_file():
        raise FileNotFoundError(f"Run metadata not found for '{run_id}' at {meta_path}")

    with open(meta_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    return RunMetadata.from_dict(data)


def read_run_artifacts(run_id: str, base_dir: Optional[Union[str, Path]] = None) -> PersistedRun:
    """Retrieve all artifacts for a run and reconstruct complete pipeline data objects.

    Args:
        run_id: Identifier of the persisted run.
        base_dir: Base directory containing runs.

    Returns:
        PersistedRun containing restored metadata, scd2_output, ChangeReport,
        ValidationReport, and ExplainResult.

    Raises:
        FileNotFoundError: If the run or any required artifact is missing.
    """
    if base_dir is None:
        runs_dir = get_settings().get_runs_dir()
    else:
        runs_dir = Path(base_dir)

    target_dir = runs_dir / run_id
    if not target_dir.is_dir():
        raise FileNotFoundError(f"Run directory not found: {target_dir}")

    # 1. Metadata
    metadata = get_run_metadata(run_id, base_dir=runs_dir)

    # 2. SCD2 Output Parquet
    parquet_path = target_dir / DEFAULT_ARTIFACT_FILES["scd2_output"]
    if not parquet_path.is_file():
        raise FileNotFoundError(f"SCD2 parquet artifact missing: {parquet_path}")
    scd2_output = pl.read_parquet(parquet_path)

    # 3. Changes JSON
    changes_path = target_dir / DEFAULT_ARTIFACT_FILES["changes"]
    if not changes_path.is_file():
        raise FileNotFoundError(f"Changes artifact missing: {changes_path}")
    with open(changes_path, "r", encoding="utf-8") as f:
        changes_data = json.load(f)

    changed_records = [_deserialize_change_record(r) for r in changes_data.get("changed", [])]
    new_records = [_deserialize_change_record(r) for r in changes_data.get("new", [])]
    deleted_records = [_deserialize_change_record(r) for r in changes_data.get("deleted", [])]

    # Reconstruct unchanged records to match summary count if needed
    unchanged_count = changes_data.get("unchanged_count", 0)
    unchanged_records = [
        ChangeRecord(business_key_values={}, change_type=ChangeType.UNCHANGED)
        for _ in range(unchanged_count)
    ]

    proc_date_str = changes_data.get("processing_date")
    proc_date = date.fromisoformat(proc_date_str) if proc_date_str else date.today()

    try:
        snap_mode = SnapshotMode(changes_data.get("snapshot_mode", "full"))
    except ValueError:
        snap_mode = SnapshotMode.FULL

    try:
        del_policy = DeletePolicy(changes_data.get("delete_policy", "soft_delete"))
    except ValueError:
        del_policy = DeletePolicy.SOFT_DELETE

    change_report = ChangeReport(
        new=new_records,
        changed=changed_records,
        unchanged=unchanged_records,
        deleted=deleted_records,
        processing_date=proc_date,
        snapshot_mode=snap_mode,
        delete_policy=del_policy,
    )

    # 4. Validation JSON
    val_path = target_dir / DEFAULT_ARTIFACT_FILES["validation"]
    if not val_path.is_file():
        raise FileNotFoundError(f"Validation artifact missing: {val_path}")
    with open(val_path, "r", encoding="utf-8") as f:
        val_data = json.load(f)

    rules = [
        ValidationRule(
            name=r["name"],
            status=ValidationStatus(r["status"]) if isinstance(r["status"], str) else r["status"],
            message=r["message"],
            details=r.get("details", []),
        )
        for r in val_data.get("rules", [])
    ]
    validation_report = ValidationReport(rules=rules)

    # 5. Explanations JSON
    exp_path = target_dir / DEFAULT_ARTIFACT_FILES["explanations"]
    if not exp_path.is_file():
        raise FileNotFoundError(f"Explanations artifact missing: {exp_path}")
    with open(exp_path, "r", encoding="utf-8") as f:
        exp_data = json.load(f)

    explanations = [
        Explanation(
            business_key_values=e.get("business_key_values", {}),
            change_type=ChangeType(e.get("change_type", "new")),
            text=e.get("text", ""),
            provider=e.get("provider", "template"),
        )
        for e in exp_data.get("explanations", [])
    ]

    metrics = None
    m_data = exp_data.get("metrics")
    if m_data:
        metrics = LLMMetrics(
            provider=m_data.get("provider", "template"),
            model=m_data.get("model", ""),
            prompt_tokens=m_data.get("prompt_tokens", 0),
            completion_tokens=m_data.get("completion_tokens", 0),
            total_tokens=m_data.get("total_tokens", 0),
            estimated_cost=m_data.get("estimated_cost", 0.0),
            request_duration=m_data.get("request_duration", 0.0),
            num_changes_explained=m_data.get("num_changes_explained", 0),
            avg_tokens_per_change=m_data.get("avg_tokens_per_change", 0.0),
            is_estimated=m_data.get("is_estimated", False),
        )

    explain_result = ExplainResult(
        explanations=explanations,
        warnings=exp_data.get("warnings", []),
        provider_used=exp_data.get("provider_used", "template"),
        metrics=metrics,
        fallback_count=exp_data.get("fallback_count", 0),
    )

    return PersistedRun(
        metadata=metadata,
        scd2_output=scd2_output,
        change_report=change_report,
        validation_report=validation_report,
        explain_result=explain_result,
    )


def list_runs(
    base_dir: Optional[Union[str, Path]] = None,
    limit: Optional[int] = None,
) -> list[RunMetadata]:
    """List all persisted runs ordered by creation timestamp descending.

    Ignores hidden folders (like .tmp_* or .backup_*).
    """
    if base_dir is None:
        runs_dir = get_settings().get_runs_dir()
    else:
        runs_dir = Path(base_dir)

    if not runs_dir.is_dir():
        return []

    runs: list[RunMetadata] = []
    for entry in runs_dir.iterdir():
        if entry.is_dir() and not entry.name.startswith("."):
            if run_exists(entry.name, base_dir=runs_dir):
                try:
                    meta = get_run_metadata(entry.name, base_dir=runs_dir)
                    runs.append(meta)
                except Exception as exc:
                    logger.warning("Skipping invalid run directory %s: %s", entry.name, exc)

    # Sort descending by created_at or started_at
    runs.sort(key=lambda r: r.created_at or r.started_at or "", reverse=True)

    if limit is not None and limit > 0:
        return runs[:limit]
    return runs


def delete_run(run_id: str, base_dir: Optional[Union[str, Path]] = None) -> bool:
    """Delete a persisted run directory and clean up any associated fingerprint link.

    Returns:
        True if deleted, False if run directory did not exist.
    """
    if base_dir is None:
        runs_dir = get_settings().get_runs_dir()
    else:
        runs_dir = Path(base_dir)

    target_dir = runs_dir / run_id
    if target_dir.is_dir():
        try:
            meta_path = target_dir / DEFAULT_ARTIFACT_FILES["metadata"]
            if meta_path.is_file():
                with open(meta_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                fp = data.get("execution_fingerprint")
                if fp:
                    fp_file = runs_dir / ".fingerprints" / f"{fp}.json"
                    if fp_file.is_file():
                        fp_file.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Error cleaning up fingerprint index for run %s: %s", run_id, exc)

        shutil.rmtree(str(target_dir))
        logger.info("Deleted run '%s'", run_id)
        return True
    return False
