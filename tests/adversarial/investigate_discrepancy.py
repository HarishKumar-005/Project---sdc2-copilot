"""Investigation of M2.2 (34.22s) vs M2.3 pre-profile (23.93s) timing discrepancy.

Tests the unoptimized M2.2 eager materialization on 1,000,000 rows across
different process states, tracemalloc tracking overhead, and garbage collection.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import gc
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

from src.scd2_copilot.models import ChangeRecord, ChangeType, DeletePolicy, FieldChange, SnapshotMode
from src.scd2_copilot.validate import validate_business_keys
from tests.adversarial.run_profiler import generate_synthetic_data


# ── Windows Working Set Measurement ──────────────────────────────────────


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


def _normalize_expr(col_expr: pl.Expr) -> pl.Expr:
    s = col_expr.cast(pl.String).str.strip_chars()
    return pl.when(s == "").then(None).otherwise(s)


def run_unoptimized_detect(source_df: pl.DataFrame, target_df: pl.DataFrame) -> tuple[float, dict[str, float]]:
    """Execute the exact unoptimized M2.2 change detection path."""
    business_key = ["customer_id"]
    tracked_columns = ["name", "city"]
    
    t0 = time.perf_counter()
    validate_business_keys(source_df, business_key, dataset_name="source")
    validate_business_keys(target_df, business_key, dataset_name="target")
    t_val = time.perf_counter() - t0

    target_current = target_df.filter(pl.col("is_current") == True).with_columns(
        pl.lit(True).alias("_target_match")
    )

    t1 = time.perf_counter()
    joined = source_df.join(
        target_current,
        on=business_key,
        how="left",
        suffix="_target",
        validate="m:1",
    )
    diff_exprs = [
        _normalize_expr(pl.col(c)).ne_missing(_normalize_expr(pl.col(f"{c}_target")))
        for c in tracked_columns
    ]
    is_changed_expr = pl.any_horizontal(diff_exprs)
    new_slice = joined.filter(pl.col("_target_match").is_null())
    changed_slice = joined.filter(pl.col("_target_match").is_not_null() & is_changed_expr)
    unchanged_slice = joined.filter(pl.col("_target_match").is_not_null() & (~is_changed_expr))
    deleted_slice = target_current.join(source_df, on=business_key, how="anti")
    t_relational = time.perf_counter() - t1

    # Stage 10: Eager ChangeRecord materialization
    t2 = time.perf_counter()
    new_kds = new_slice.select(business_key).to_dicts()
    new_records = [ChangeRecord(business_key_values=kd, change_type=ChangeType.NEW) for kd in new_kds]
    del_kds = deleted_slice.select(business_key).to_dicts()
    del_records = [ChangeRecord(business_key_values=kd, change_type=ChangeType.DELETED) for kd in del_kds]
    unc_kds = unchanged_slice.select(business_key).to_dicts()
    unc_records = [ChangeRecord(business_key_values=kd, change_type=ChangeType.UNCHANGED) for kd in unc_kds]
    t_stage10 = time.perf_counter() - t2

    # Stage 11: Eager FieldChange materialization
    t3 = time.perf_counter()
    cols_to_select = business_key + tracked_columns + [f"{c}_target" for c in tracked_columns]
    changed_rows = changed_slice.select(cols_to_select).to_dicts()
    changed_records = []
    for row in changed_rows:
        kd = {k: row[k] for k in business_key}
        fcs = []
        for col in tracked_columns:
            s_val = row[col]
            t_val = row[f"{col}_target"]
            if _normalize_scalar(s_val) != _normalize_scalar(t_val):
                fcs.append(FieldChange(column=col, old_value=t_val, new_value=s_val))
        changed_records.append(
            ChangeRecord(
                business_key_values=kd,
                change_type=ChangeType.CHANGED,
                field_changes=fcs,
            )
        )
    t_stage11 = time.perf_counter() - t3

    total_time = time.perf_counter() - t0
    breakdown = {
        "validation": t_val,
        "relational": t_relational,
        "stage10_changerecord_dicts": t_stage10,
        "stage11_fieldchange_dicts": t_stage11,
    }
    return total_time, breakdown


def investigate():
    print("=" * 80)
    print("M2.4 DISCREPANCY RECONCILIATION INVESTIGATION (1M ROWS)")
    print("=" * 80)
    print("Generating 1M rows synthetic data...")
    source_df, target_df = generate_synthetic_data(1_000_000)
    rss_init, _ = get_process_memory()
    print(f"Initial Process RSS: {rss_init:.2f} MB")

    results = {}

    # Test 1: With tracemalloc (as in M2.2 run_profiler and M2.3 profile_detect_stages)
    print("\n[Condition 1] Unoptimized M2.2 Eager Path WITH tracemalloc:")
    timings_with_tm = []
    for rep in range(3):
        gc.collect()
        tracemalloc.start()
        t, breakdown = run_unoptimized_detect(source_df, target_df)
        _, peak_tm = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        rss_cur, rss_peak = get_process_memory()
        timings_with_tm.append(t)
        print(f"  Rep {rep+1}: {t:.4f}s | Stage 10: {breakdown['stage10_changerecord_dicts']:.4f}s | Stage 11: {breakdown['stage11_fieldchange_dicts']:.4f}s | RSS: {rss_cur:.2f} MB (Peak: {rss_peak:.2f} MB)")

    # Test 2: WITHOUT tracemalloc
    print("\n[Condition 2] Unoptimized M2.2 Eager Path WITHOUT tracemalloc:")
    timings_no_tm = []
    for rep in range(3):
        gc.collect()
        t, breakdown = run_unoptimized_detect(source_df, target_df)
        rss_cur, rss_peak = get_process_memory()
        timings_no_tm.append(t)
        print(f"  Rep {rep+1}: {t:.4f}s | Stage 10: {breakdown['stage10_changerecord_dicts']:.4f}s | Stage 11: {breakdown['stage11_fieldchange_dicts']:.4f}s | RSS: {rss_cur:.2f} MB (Peak: {rss_peak:.2f} MB)")

    # Test 3: Accumulated heap without gc.collect() (Simulating multi-scale consecutive runs)
    print("\n[Condition 3] Consecutive Unoptimized Runs WITHOUT gc.collect() (Heap fragmentation simulation):")
    timings_dirty_heap = []
    for rep in range(3):
        t, breakdown = run_unoptimized_detect(source_df, target_df)
        rss_cur, rss_peak = get_process_memory()
        timings_dirty_heap.append(t)
        print(f"  Run {rep+1}: {t:.4f}s | Stage 10: {breakdown['stage10_changerecord_dicts']:.4f}s | Stage 11: {breakdown['stage11_fieldchange_dicts']:.4f}s | RSS: {rss_cur:.2f} MB (Peak: {rss_peak:.2f} MB)")

    # Test 4: M2.3 Candidate with LazyRecordSequence on same data
    from src.scd2_copilot.detect_changes import detect_changes
    print("\n[Condition 4] M2.3 Candidate with LazyRecordSequence (Authoritative):")
    timings_m23 = []
    for rep in range(3):
        gc.collect()
        t0 = time.perf_counter()
        rep_obj = detect_changes(source_df, target_df, ["customer_id"], ["name", "city"], date(2026, 6, 8))
        t_m23 = time.perf_counter() - t0
        rss_cur, rss_peak = get_process_memory()
        timings_m23.append(t_m23)
        print(f"  Rep {rep+1}: {t_m23:.4f}s | len(new)={len(rep_obj.new)} | RSS: {rss_cur:.2f} MB (Peak: {rss_peak:.2f} MB)")

    discrepancy_report = {
        "unoptimized_with_tracemalloc_sec": {
            "min": round(min(timings_with_tm), 4),
            "median": round(sorted(timings_with_tm)[len(timings_with_tm)//2], 4),
            "max": round(max(timings_with_tm), 4),
        },
        "unoptimized_without_tracemalloc_sec": {
            "min": round(min(timings_no_tm), 4),
            "median": round(sorted(timings_no_tm)[len(timings_no_tm)//2], 4),
            "max": round(max(timings_no_tm), 4),
        },
        "unoptimized_consecutive_no_gc_sec": {
            "min": round(min(timings_dirty_heap), 4),
            "median": round(sorted(timings_dirty_heap)[len(timings_dirty_heap)//2], 4),
            "max": round(max(timings_dirty_heap), 4),
        },
        "m23_candidate_lazy_sec": {
            "min": round(min(timings_m23), 4),
            "median": round(sorted(timings_m23)[len(timings_m23)//2], 4),
            "max": round(max(timings_m23), 4),
        },
    }

    out_file = Path(__file__).resolve().parent / "discrepancy_findings.json"
    with open(out_file, "w") as f:
        json.dump(discrepancy_report, f, indent=2)
    print(f"\nSaved discrepancy findings to {out_file}")


if __name__ == "__main__":
    investigate()
