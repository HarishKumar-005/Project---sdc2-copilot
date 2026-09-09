"""Authoritative Benchmark Harness for M2.5: Production-Grade Vectorized Validation.

Establishes the single authoritative, statistically rigorous benchmark methodology
across 10K, 100K, 500K, 1M, 2M, and 5M rows, evaluating:
- Vectorized SCD2 Validation (validate_scd2) and full end-to-end pipeline
- Warm-up runs and multiple measured repetitions (min, median, p95, max, std_dev)
- Pure wall-clock timing separated from profiling overhead
- 3-tier memory tracking: Python heap, Polars buffers, Process Working Set (RSS)
- Engine execution modes: auto, in-memory, streaming
- Scaling linearity and time-per-million-rows calculations
- Persistent gate artifact generation comparing M2.4 baseline to M2.5 results
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import gc
import json
import math
import os
import platform
import sys
import time
import tracemalloc
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.scd2_copilot.detect_changes import detect_changes
from src.scd2_copilot.transform_scd2 import apply_scd2
from src.scd2_copilot.validate import validate_scd2
from tests.adversarial.run_profiler import generate_synthetic_data

BENCHMARK_GATE_M24_PATH = Path(__file__).resolve().parent / "benchmark_gate_m24.json"
BENCHMARK_GATE_M25_PATH = Path(__file__).resolve().parent / "benchmark_gate_m25.json"


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
    """Return (current_rss_mb, peak_working_set_mb) via Windows API."""
    handle = kernel32.GetCurrentProcess()
    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
    if GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
        return counters.WorkingSetSize / (1024 * 1024), counters.PeakWorkingSetSize / (1024 * 1024)
    return 0.0, 0.0


def compute_stats(values: list[float]) -> dict[str, float]:
    """Calculate min, median, p95, max, and std_dev from a sample."""
    if not values:
        return {"min": 0.0, "median": 0.0, "p95": 0.0, "max": 0.0, "std_dev": 0.0}
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    median_val = sorted_vals[n // 2] if n % 2 != 0 else (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2.0
    
    # Nearest rank method for p95
    p95_idx = min(n - 1, max(0, math.ceil(0.95 * n) - 1))
    p95_val = sorted_vals[p95_idx]
    
    mean_val = sum(values) / n
    variance = sum((x - mean_val) ** 2 for x in values) / n if n > 1 else 0.0
    std_dev = math.sqrt(variance)
    
    return {
        "min": round(min(values), 4),
        "median": round(median_val, 4),
        "p95": round(p95_val, 4),
        "max": round(max(values), 4),
        "std_dev": round(std_dev, 4),
    }


def get_environment_metadata() -> dict[str, Any]:
    """Capture authoritative machine and environment hardware specs."""
    return {
        "platform": platform.platform(),
        "os_version": platform.version(),
        "python_version": platform.python_version(),
        "polars_version": pl.__version__,
        "processor": platform.processor(),
        "machine": platform.machine(),
        "logical_cpu_cores": os.cpu_count() or 1,
        "benchmark_timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }


def benchmark_scale_authoritative(
    scale: int,
    composite_key: bool = False,
    engine: str = "auto",
    num_repetitions: int = 5,
) -> dict[str, Any]:
    """Execute authoritative benchmark with warm-up and statistical repetitions."""
    key_desc = "composite" if composite_key else "single"
    print(f"\n================================================================================")
    print(f"BENCHMARK: {scale:,} rows | Key: {key_desc} | Engine: {engine} | Reps: {num_repetitions}")
    print(f"================================================================================")

    b_key = ["region_id", "customer_id"] if composite_key else ["customer_id"]
    tracked = ["name", "city"]
    p_date = date(2026, 6, 8)

    # 1. Warm-up Run (1 repetition to warm CPU caches, JIT, and Polars engine)
    gc.collect()
    s_warm, t_warm = generate_synthetic_data(min(scale, 100_000), composite_key=composite_key)
    rep_warm = detect_changes(s_warm, t_warm, b_key, tracked, p_date, engine=engine)
    out_warm = apply_scd2(s_warm, t_warm, rep_warm, b_key, tracked, p_date, engine=engine)
    _ = validate_scd2(out_warm, b_key, engine=engine)
    del s_warm, t_warm, rep_warm, out_warm
    gc.collect()

    # 2. Measured Timing Repetitions (PURE wall-clock timing, NO tracemalloc)
    t_gens: list[float] = []
    t_dets: list[float] = []
    t_apps: list[float] = []
    t_vals: list[float] = []
    t_totals: list[float] = []
    t_e2es: list[float] = []

    last_output_df = None
    last_report = None
    last_source_df = None
    last_target_df = None

    for rep in range(num_repetitions):
        gc.collect()

        # Phase A: Data Generation
        t0 = time.perf_counter()
        source_df, target_df = generate_synthetic_data(scale, composite_key=composite_key)
        t_gen = time.perf_counter() - t0

        # Phase B: Change Detection
        t0 = time.perf_counter()
        report = detect_changes(source_df, target_df, b_key, tracked, p_date, engine=engine)
        t_det = time.perf_counter() - t0

        # Phase C: Transformation
        t0 = time.perf_counter()
        output_df = apply_scd2(source_df, target_df, report, b_key, tracked, p_date, engine=engine)
        t_app = time.perf_counter() - t0

        # Phase D: Invariant Validation
        t0 = time.perf_counter()
        v_report = validate_scd2(output_df, b_key, engine=engine)
        t_val = time.perf_counter() - t0
        assert v_report.passed, f"Validation failed at scale {scale:,}!"

        t_tot = t_det + t_app
        t_e2e = t_tot + t_val

        t_gens.append(t_gen)
        t_dets.append(t_det)
        t_apps.append(t_app)
        t_vals.append(t_val)
        t_totals.append(t_tot)
        t_e2es.append(t_e2e)

        print(f"  Rep {rep+1}/{num_repetitions} -> Detect: {t_det:8.4f}s | Apply: {t_app:8.4f}s | Val: {t_val:8.4f}s | Total D+A: {t_tot:8.4f}s | E2E: {t_e2e:8.4f}s")

        if rep == num_repetitions - 1:
            last_output_df = output_df
            last_report = report
            last_source_df = source_df
            last_target_df = target_df

    # 3. Dedicated Memory Measurement (Phase 3: Three-Tier Memory Profile)
    gc.collect()
    tracemalloc.start()
    rss_before, _ = get_process_memory()

    # Re-run detect & apply & validate under tracemalloc tracking
    rep_mem = detect_changes(last_source_df, last_target_df, b_key, tracked, p_date, engine=engine)
    _, peak_heap_detect = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    tracemalloc.start()
    _ = apply_scd2(last_source_df, last_target_df, rep_mem, b_key, tracked, p_date, engine=engine)
    _, peak_heap_apply = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    tracemalloc.start()
    _ = validate_scd2(last_output_df, b_key, engine=engine)
    _, peak_heap_validate = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    rss_after, rss_peak = get_process_memory()

    # Polars columnar buffer sizes via estimated_size()
    source_polars_bytes = last_source_df.estimated_size()
    target_polars_bytes = last_target_df.estimated_size()
    output_polars_bytes = last_output_df.estimated_size()
    total_polars_buffers_bytes = source_polars_bytes + target_polars_bytes + output_polars_bytes

    # Statistical Aggregation
    stats_gen = compute_stats(t_gens)
    stats_det = compute_stats(t_dets)
    stats_app = compute_stats(t_apps)
    stats_val = compute_stats(t_vals)
    stats_tot = compute_stats(t_totals)
    stats_e2e = compute_stats(t_e2es)

    median_total = stats_tot["median"]
    median_e2e = stats_e2e["median"]
    throughput_d_plus_a = scale / median_total if median_total > 0 else 0
    throughput_e2e = scale / median_e2e if median_e2e > 0 else 0
    sec_per_1m_d_plus_a = (median_total / scale) * 1_000_000
    sec_per_1m_e2e = (median_e2e / scale) * 1_000_000

    print(f"\n--- Statistical Summary for {scale:,} rows ({engine}) ---")
    print(f"  Detect Time (median): {stats_det['median']:8.4f}s  (min: {stats_det['min']:.4f}s, p95: {stats_det['p95']:.4f}s, max: {stats_det['max']:.4f}s)")
    print(f"  Apply Time  (median): {stats_app['median']:8.4f}s  (min: {stats_app['min']:.4f}s, p95: {stats_app['p95']:.4f}s, max: {stats_app['max']:.4f}s)")
    print(f"  Total D+A   (median): {stats_tot['median']:8.4f}s  (min: {stats_tot['min']:.4f}s, p95: {stats_tot['p95']:.4f}s, max: {stats_tot['max']:.4f}s)")
    print(f"  Validation  (median): {stats_val['median']:8.4f}s  (min: {stats_val['min']:.4f}s, p95: {stats_val['p95']:.4f}s, max: {stats_val['max']:.4f}s)")
    print(f"  End-to-End  (median): {stats_e2e['median']:8.4f}s")
    print(f"  Throughput (D+A)    : {throughput_d_plus_a:,.0f} rows/sec ({sec_per_1m_d_plus_a:.4f}s per 1M rows)")
    print(f"  Throughput (E2E)    : {throughput_e2e:,.0f} rows/sec ({sec_per_1m_e2e:.4f}s per 1M rows)")
    print(f"  Memory Profile:")
    print(f"    1. Python Heap (detect) : {peak_heap_detect / (1024*1024):.2f} MB")
    print(f"    2. Python Heap (apply)  : {peak_heap_apply / (1024*1024):.2f} MB")
    print(f"    3. Polars Buffers Total : {total_polars_buffers_bytes / (1024*1024):.2f} MB (Source: {source_polars_bytes/(1024*1024):.1f}MB, Target: {target_polars_bytes/(1024*1024):.1f}MB, Output: {output_polars_bytes/(1024*1024):.1f}MB)")
    print(f"    4. Windows Process RSS  : {rss_after:.2f} MB (Peak Working Set: {rss_peak:.2f} MB)")

    return {
        "num_rows": scale,
        "composite_key": composite_key,
        "engine": engine,
        "repetitions": num_repetitions,
        "counts": {
            "source_rows": last_source_df.height,
            "target_rows": last_target_df.height,
            "output_rows": last_output_df.height,
            "new": len(last_report.new),
            "changed": len(last_report.changed),
            "unchanged": len(last_report.unchanged),
            "deleted": len(last_report.deleted),
        },
        "timings_sec": {
            "generate": stats_gen,
            "detect": stats_det,
            "apply": stats_app,
            "validate": stats_val,
            "total_detect_apply": stats_tot,
            "end_to_end": stats_e2e,
        },
        "performance_metrics": {
            "throughput_detect_apply_rows_sec": round(throughput_d_plus_a, 1),
            "throughput_e2e_rows_sec": round(throughput_e2e, 1),
            "seconds_per_million_rows_detect_apply": round(sec_per_1m_d_plus_a, 4),
            "seconds_per_million_rows_e2e": round(sec_per_1m_e2e, 4),
        },
        "memory_metrics": {
            "python_heap_peak_detect_mb": round(peak_heap_detect / (1024 * 1024), 2),
            "python_heap_peak_apply_mb": round(peak_heap_apply / (1024 * 1024), 2),
            "python_heap_peak_validate_mb": round(peak_heap_validate / (1024 * 1024), 2),
            "polars_buffers_source_mb": round(source_polars_bytes / (1024 * 1024), 2),
            "polars_buffers_target_mb": round(target_polars_bytes / (1024 * 1024), 2),
            "polars_buffers_output_mb": round(output_polars_bytes / (1024 * 1024), 2),
            "polars_buffers_total_mb": round(total_polars_buffers_bytes / (1024 * 1024), 2),
            "process_rss_mb": round(rss_after, 2),
            "process_peak_working_set_mb": round(rss_peak, 2),
        },
        "validation_passed": True,
    }


def run_full_authoritative_gate():
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 80)
    print("M2.5 AUTHORITATIVE PERFORMANCE BENCHMARK & VALIDATION GATE EXECUTION")
    print("=" * 80)

    env_meta = get_environment_metadata()
    print(f"System: {env_meta['platform']} | CPU Cores: {env_meta['logical_cpu_cores']}")
    print(f"Python: {env_meta['python_version']} | Polars: {env_meta['polars_version']}")

    benchmarks_data: dict[str, Any] = {}

    # Standard Scales (10K, 100K, 500K, 1M, 2M, 5M) under "auto"
    scales_config = [
        (10_000, 5),
        (100_000, 5),
        (500_000, 5),
        (1_000_000, 5),
        (2_000_000, 3),
        (5_000_000, 3),
    ]

    for scale, reps in scales_config:
        entry = benchmark_scale_authoritative(scale, composite_key=False, engine="auto", num_repetitions=reps)
        benchmarks_data[str(scale)] = entry

    # Composite Key Workloads (100K and 1M)
    comp_100k = benchmark_scale_authoritative(100_000, composite_key=True, engine="auto", num_repetitions=5)
    benchmarks_data["100000_composite"] = comp_100k

    comp_1m = benchmark_scale_authoritative(1_000_000, composite_key=True, engine="auto", num_repetitions=3)
    benchmarks_data["1000000_composite"] = comp_1m

    # Engine Comparison at 100K (auto, in-memory, streaming)
    in_mem_100k = benchmark_scale_authoritative(100_000, composite_key=False, engine="in-memory", num_repetitions=5)
    benchmarks_data["100000_in_memory"] = in_mem_100k

    streaming_100k = benchmark_scale_authoritative(100_000, composite_key=False, engine="streaming", num_repetitions=5)
    benchmarks_data["100000_streaming"] = streaming_100k

    # Engine Comparison at 1M (auto, in-memory, streaming)
    in_mem_1m = benchmark_scale_authoritative(1_000_000, composite_key=False, engine="in-memory", num_repetitions=3)
    benchmarks_data["1000000_in_memory"] = in_mem_1m

    streaming_1m = benchmark_scale_authoritative(1_000_000, composite_key=False, engine="streaming", num_repetitions=3)
    benchmarks_data["1000000_streaming"] = streaming_1m

    # Scalability Linearity Analysis across 100K -> 500K -> 1M -> 2M -> 5M
    scales_for_analysis = [100_000, 500_000, 1_000_000, 2_000_000, 5_000_000]
    scaling_analysis = {}
    base_time = benchmarks_data["100000"]["timings_sec"]["total_detect_apply"]["median"]
    for s in scales_for_analysis:
        m_time = benchmarks_data[str(s)]["timings_sec"]["total_detect_apply"]["median"]
        expected_linear = base_time * (s / 100_000)
        scaling_ratio = m_time / expected_linear if expected_linear > 0 else 1.0
        scaling_analysis[str(s)] = {
            "num_rows": s,
            "median_time_sec": m_time,
            "expected_strictly_linear_sec": round(expected_linear, 4),
            "scaling_ratio": round(scaling_ratio, 3),  # 1.0 means perfectly linear
            "throughput_rows_sec": benchmarks_data[str(s)]["performance_metrics"]["throughput_detect_apply_rows_sec"],
            "sec_per_million_rows": benchmarks_data[str(s)]["performance_metrics"]["seconds_per_million_rows_detect_apply"],
        }

    # Load M2.4 baseline to construct comparative analysis
    m24_data = {}
    comparison_data = {}
    if BENCHMARK_GATE_M24_PATH.exists():
        try:
            with open(BENCHMARK_GATE_M24_PATH, "r", encoding="utf-8") as f:
                m24_data = json.load(f)
            m24_bench = m24_data.get("benchmarks", {})
            for key, m25_entry in benchmarks_data.items():
                if key in m24_bench:
                    m24_entry = m24_bench[key]
                    m24_val = m24_entry["timings_sec"]["validate"]["median"]
                    m25_val = m25_entry["timings_sec"]["validate"]["median"]
                    m24_e2e = m24_entry["timings_sec"]["end_to_end"]["median"]
                    m25_e2e = m25_entry["timings_sec"]["end_to_end"]["median"]
                    val_speedup = round(m24_val / m25_val, 2) if m25_val > 0 else 1.0
                    e2e_speedup = round(m24_e2e / m25_e2e, 2) if m25_e2e > 0 else 1.0
                    comparison_data[key] = {
                        "m24_validation_sec": m24_val,
                        "m25_validation_sec": m25_val,
                        "validation_speedup_x": val_speedup,
                        "m24_end_to_end_sec": m24_e2e,
                        "m25_end_to_end_sec": m25_e2e,
                        "end_to_end_speedup_x": e2e_speedup,
                    }
        except Exception as ex:
            print(f"Warning: Could not read M2.4 gate baseline: {ex}")

    full_gate_report = {
        "metadata": env_meta,
        "m24_baseline_metadata": m24_data.get("metadata", {}),
        "benchmarks": benchmarks_data,
        "scalability_analysis": scaling_analysis,
        "validation_speedup_comparison": comparison_data,
    }

    with open(BENCHMARK_GATE_M25_PATH, "w", encoding="utf-8") as f:
        json.dump(full_gate_report, f, indent=2)

    print(f"\n================================================================================")
    print(f"SUCCESS: Authoritative M2.5 Benchmark Gate written to {BENCHMARK_GATE_M25_PATH}")
    print(f"================================================================================")


if __name__ == "__main__":
    run_full_authoritative_gate()
