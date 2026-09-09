"""Deep stage profiler for detect_changes (Phase 1 & Phase 3 of M2.3).

Instruments and measures all 12 distinct stages of detect_changes across
10K, 100K, 500K, and 1M row scales, capturing:
- Stage-by-stage wall-clock timings
- Python heap allocation (tracemalloc)
- Polars columnar buffer sizes (estimated_size())
- Process Resident Set Size (RSS / Peak Working Set via Windows API)
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import io
import json
from datetime import date
from pathlib import Path
import sys
import time
import tracemalloc
import polars as pl

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.scd2_copilot.models import (
    ChangeRecord,
    ChangeReport,
    ChangeType,
    DeletePolicy,
    FieldChange,
    LazyRecordSequence,
    SnapshotMode,
)
from src.scd2_copilot.validate import validate_business_keys
from tests.adversarial.run_profiler import generate_synthetic_data


# ── Process Memory Helpers (Windows API) ─────────────────────────────────


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


psapi = ctypes.windll.psapi
kernel32 = ctypes.windll.kernel32
GetProcessMemoryInfo = psapi.GetProcessMemoryInfo
GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wintypes.DWORD]
GetProcessMemoryInfo.restype = wintypes.BOOL


def get_process_memory() -> tuple[float, float]:
    """Return (current_rss_mb, peak_working_set_mb) for the current process."""
    handle = kernel32.GetCurrentProcess()
    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
    if GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
        return counters.WorkingSetSize / (1024 * 1024), counters.PeakWorkingSetSize / (1024 * 1024)
    return 0.0, 0.0


def _normalize_scalar(value):
    if value is None:
        return None
    s = str(value).strip()
    return None if s == "" else s


def _normalize_expr(col_expr: pl.Expr, dtype: pl.DataType | None = None) -> pl.Expr:
    if dtype in (pl.String, pl.Utf8, None):
        s = col_expr.cast(pl.String).str.strip_chars()
        return pl.when(s == "").then(None).otherwise(s)
    return col_expr


def instrumented_detect_changes(
    source_df: pl.DataFrame,
    target_df: pl.DataFrame,
    business_key: list[str],
    tracked_columns: list[str],
    processing_date: date,
    snapshot_mode: SnapshotMode = SnapshotMode.FULL,
    delete_policy: DeletePolicy = DeletePolicy.SOFT_DELETE,
) -> tuple[ChangeReport, dict[str, float], dict[str, float]]:
    """Execute detect_changes while instrumenting all 12 stages separately."""
    timings: dict[str, float] = {}

    # Stage 1: Input Validation
    t0 = time.perf_counter()
    if not isinstance(snapshot_mode, SnapshotMode):
        snapshot_mode = SnapshotMode(str(snapshot_mode).lower().strip())
    if not isinstance(delete_policy, DeletePolicy):
        delete_policy = DeletePolicy(str(delete_policy).lower().strip())
    timings["1_input_validation"] = time.perf_counter() - t0

    # Stage 2: Business-Key Validation
    t0 = time.perf_counter()
    validate_business_keys(source_df, business_key, dataset_name="source")
    validate_business_keys(target_df, business_key, dataset_name="target")
    timings["2_business_key_validation"] = time.perf_counter() - t0

    report = ChangeReport(
        processing_date=processing_date,
        snapshot_mode=snapshot_mode,
        delete_policy=delete_policy,
    )

    # Stage 3: Active Target Extraction
    t0 = time.perf_counter()
    if "is_current" in target_df.columns:
        target_current = target_df.filter(pl.col("is_current") == True)  # noqa: E712
    else:
        target_current = target_df
    target_matched = target_current.with_columns(pl.lit(True).alias("_target_match"))
    timings["3_active_target_extraction"] = time.perf_counter() - t0

    # Stage 4: Normalization Expressions Construction
    t0 = time.perf_counter()
    diff_exprs = []
    for col in tracked_columns:
        s_dtype = source_df.schema.get(col, pl.String)
        t_dtype = target_df.schema.get(col, pl.String)
        if s_dtype != t_dtype or s_dtype in (pl.String, pl.Utf8) or t_dtype in (pl.String, pl.Utf8):
            s_norm = _normalize_expr(pl.col(col).cast(pl.String), pl.String)
            t_norm = _normalize_expr(pl.col(f"{col}_target").cast(pl.String), pl.String)
        else:
            s_norm = pl.col(col)
            t_norm = pl.col(f"{col}_target")
        diff_exprs.append(s_norm.ne_missing(t_norm))
    is_changed_expr = pl.any_horizontal(diff_exprs) if diff_exprs else pl.lit(False)
    timings["4_normalization_expressions"] = time.perf_counter() - t0

    # Stage 5: LEFT JOIN (source LEFT JOIN target_current)
    t0 = time.perf_counter()
    joined = source_df.join(
        target_matched,
        on=business_key,
        how="left",
        suffix="_target",
        validate="m:1",
    )
    timings["5_left_join"] = time.perf_counter() - t0

    # Stage 6: Field Comparison Expressions Evaluation
    t0 = time.perf_counter()
    new_mask = pl.col("_target_match").is_null()
    changed_mask = pl.col("_target_match").is_not_null() & is_changed_expr
    unchanged_mask = pl.col("_target_match").is_not_null() & (~is_changed_expr)
    timings["6_field_comparison_evaluation"] = time.perf_counter() - t0

    # Stage 7: NEW / CHANGED / UNCHANGED Slicing / Filtering
    t0 = time.perf_counter()
    new_slice = joined.filter(new_mask)
    changed_slice = joined.filter(changed_mask)
    unchanged_slice = joined.filter(unchanged_mask)
    timings["7_classification_filtering"] = time.perf_counter() - t0

    # Stage 8: DELETED Anti-Join
    t0 = time.perf_counter()
    if snapshot_mode == SnapshotMode.FULL and delete_policy == DeletePolicy.SOFT_DELETE:
        deleted_slice = target_current.join(source_df, on=business_key, how="anti")
    else:
        deleted_slice = target_current.clear()
    timings["8_deleted_anti_join"] = time.perf_counter() - t0

    # Stage 9: Vectorized Key DataFrames Attachment
    t0 = time.perf_counter()
    report.new_keys_df = new_slice.select(business_key)
    report.changed_keys_df = changed_slice.select(business_key)
    report.unchanged_keys_df = unchanged_slice.select(business_key)
    report.deleted_keys_df = deleted_slice.select(business_key)
    timings["9_key_dataframes_attachment"] = time.perf_counter() - t0

    # Stage 10: ChangeRecord Attachment (New, Deleted, Unchanged LazyRecordSequences)
    t0 = time.perf_counter()
    report.new = LazyRecordSequence(new_slice.select(business_key), business_key, ChangeType.NEW)
    report.deleted = LazyRecordSequence(deleted_slice.select(business_key), business_key, ChangeType.DELETED)
    report.unchanged = LazyRecordSequence(unchanged_slice.select(business_key), business_key, ChangeType.UNCHANGED)
    timings["10_changerecord_attachment"] = time.perf_counter() - t0

    # Stage 11: Field-Change Attachment (Changed LazyRecordSequence)
    t0 = time.perf_counter()
    report.changed = LazyRecordSequence(changed_slice, business_key, ChangeType.CHANGED, tracked_columns)
    timings["11_field_change_attachment"] = time.perf_counter() - t0

    # Stage 12: Sorting / Canonicalization (N/A for detect_changes, 0.0s)
    timings["12_sorting_canonicalization"] = 0.0

    # Calculate memory metrics
    polars_bytes = (
        source_df.estimated_size()
        + target_df.estimated_size()
        + joined.estimated_size()
        + new_slice.estimated_size()
        + changed_slice.estimated_size()
        + unchanged_slice.estimated_size()
        + deleted_slice.estimated_size()
    )
    polars_mb = polars_bytes / (1024 * 1024)

    memory_metrics = {
        "polars_buffers_mb": round(polars_mb, 2),
    }

    return report, timings, memory_metrics


def run_stage_profiling():
    print("=" * 80)
    print("M2.3 DETECT_CHANGES STAGE-BY-STAGE PROFILING (PHASE 1 & PHASE 3)")
    print("=" * 80)

    scales = [10_000, 100_000, 500_000, 1_000_000]
    all_profiles: dict[str, dict] = {}

    for scale in scales:
        print(f"\nProfiling {scale:,} rows...")
        source_df, target_df = generate_synthetic_data(scale)
        b_key = ["customer_id"]
        tracked = ["name", "city"]
        p_date = date(2026, 6, 8)

        # Start tracemalloc & record initial process RSS
        tracemalloc.start()
        rss_start, _ = get_process_memory()

        t_start = time.perf_counter()
        report, stage_timings, mem_metrics = instrumented_detect_changes(
            source_df, target_df, b_key, tracked, p_date
        )
        total_wall = time.perf_counter() - t_start

        _, peak_tracemalloc = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        rss_current, rss_peak = get_process_memory()
        python_heap_mb = peak_tracemalloc / (1024 * 1024)

        mem_metrics["python_heap_tracemalloc_mb"] = round(python_heap_mb, 2)
        mem_metrics["process_rss_mb"] = round(rss_current, 2)
        mem_metrics["process_peak_working_set_mb"] = round(rss_peak, 2)

        stage_percentages = {
            stage: round((t / total_wall) * 100, 2) if total_wall > 0 else 0
            for stage, t in stage_timings.items()
        }

        print(f"Total Detect Time: {total_wall:.4f}s")
        print(f"Memory -> Python Heap: {python_heap_mb:.2f} MB | Polars Buffers: {mem_metrics['polars_buffers_mb']:.2f} MB | Process RSS: {rss_current:.2f} MB (Peak: {rss_peak:.2f} MB)")
        print("\nStage Breakdown:")
        for stage, duration in stage_timings.items():
            pct = stage_percentages[stage]
            bar = "#" * int(pct / 2)
            print(f"  {stage:<35}: {duration:8.4f}s ({pct:5.1f}%) {bar}")

        all_profiles[str(scale)] = {
            "num_rows": scale,
            "total_wall_time_sec": round(total_wall, 4),
            "stage_timings_sec": {k: round(v, 4) for k, v in stage_timings.items()},
            "stage_percentages": stage_percentages,
            "memory_metrics": mem_metrics,
        }

    out_path = Path(__file__).resolve().parent / "detect_stage_profile.json"
    with open(out_path, "w") as f:
        json.dump(all_profiles, f, indent=2)
    print(f"\nSaved stage profiling evidence to {out_path}")


if __name__ == "__main__":
    run_stage_profiling()
