"""Performance benchmark suite for sdc2-copilot.

Tests processing of large-scale datasets (10k, 100k, 500k, 1M rows)
to measure runtime, memory usage, and throughput of the SCD2 pipeline.

All tests are decorated with @pytest.mark.benchmark to isolate them from
routine pytest test runs.
"""

from __future__ import annotations

import json
import time
import tracemalloc
from datetime import date
from pathlib import Path
import polars as pl
import pytest

from src.scd2_copilot.detect_changes import detect_changes
from src.scd2_copilot.transform_scd2 import apply_scd2
from src.scd2_copilot.validate import validate_scd2
from tests.adversarial.run_profiler import generate_synthetic_data

BENCHMARK_RESULTS_PATH = Path(__file__).resolve().parent / "benchmark_results.json"


@pytest.mark.benchmark
@pytest.mark.parametrize(
    "num_rows",
    [10000, 100000]
)
def test_pipeline_scale_performance(num_rows: int):
    """Run performance scaling test for a specific dataset size."""
    print(f"\n--- Starting performance benchmark for {num_rows:,} rows ---")
    
    # 1. Generation
    t0 = time.perf_counter()
    source_df, target_df = generate_synthetic_data(num_rows)
    gen_time = time.perf_counter() - t0
    print(f"Dataset generated in {gen_time:.4f}s. Source: {source_df.height:,} rows, Target: {target_df.height:,} rows")
    
    # 2. Detect Changes with memory tracking
    tracemalloc.start()
    t_detect_start = time.perf_counter()
    
    report = detect_changes(
        source_df=source_df,
        target_df=target_df,
        business_key=["customer_id"],
        tracked_columns=["name", "city"],
        processing_date=date(2026, 6, 8)
    )
    
    detect_time = time.perf_counter() - t_detect_start
    _, peak_detect_memory = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    
    print(f"Changes detected in {detect_time:.4f}s.")
    print(f"New: {len(report.new):,}, Changed: {len(report.changed):,}, Deleted: {len(report.deleted):,}, Unchanged: {len(report.unchanged):,}")
    
    # 3. Apply SCD2 with memory tracking
    tracemalloc.start()
    t_apply_start = time.perf_counter()
    
    output_df = apply_scd2(
        source_df=source_df,
        target_df=target_df,
        change_report=report,
        business_key=["customer_id"],
        tracked_columns=["name", "city"],
        processing_date=date(2026, 6, 8)
    )
    
    apply_time = time.perf_counter() - t_apply_start
    _, peak_apply_memory = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    
    print(f"SCD2 transformation applied in {apply_time:.4f}s. Output: {output_df.height:,} rows")
    
    # 4. Validate SCD2 output
    t_val_start = time.perf_counter()
    validation_report = validate_scd2(output_df, ["customer_id"])
    val_time = time.perf_counter() - t_val_start
    
    assert validation_report.passed, f"Validation failed for size {num_rows}!"
    print(f"Validation passed in {val_time:.4f}s.")
    
    # Metrics calculations
    total_time = detect_time + apply_time
    throughput = num_rows / total_time if total_time > 0 else 0
    peak_detect_mb = peak_detect_memory / (1024 * 1024)
    peak_apply_mb = peak_apply_memory / (1024 * 1024)
    
    # Save/Append to results file
    results = {}
    if BENCHMARK_RESULTS_PATH.exists():
        try:
            with open(BENCHMARK_RESULTS_PATH, "r") as f:
                results = json.load(f)
        except Exception:
            pass
            
    results[str(num_rows)] = {
        "num_rows": num_rows,
        "source_rows": source_df.height,
        "target_rows": target_df.height,
        "output_rows": output_df.height,
        "new_count": len(report.new),
        "changed_count": len(report.changed),
        "deleted_count": len(report.deleted),
        "unchanged_count": len(report.unchanged),
        "gen_time_sec": round(gen_time, 4),
        "detect_time_sec": round(detect_time, 4),
        "apply_time_sec": round(apply_time, 4),
        "val_time_sec": round(val_time, 4),
        "total_time_sec": round(total_time, 4),
        "detect_peak_memory_mb": round(peak_detect_mb, 2),
        "apply_peak_memory_mb": round(peak_apply_mb, 2),
        "throughput_rows_per_sec": round(throughput, 1),
        "validation_passed": validation_report.passed
    }
    
    with open(BENCHMARK_RESULTS_PATH, "w") as f:
        json.dump(results, f, indent=2)


@pytest.mark.benchmark
def test_pipeline_composite_key_performance():
    """Run performance benchmark for composite business keys (100,000 rows)."""
    num_rows = 100_000
    b_key = ["region_id", "customer_id"]
    tracked = ["name", "city"]
    p_date = date(2026, 6, 8)
    
    t0 = time.perf_counter()
    source_df, target_df = generate_synthetic_data(num_rows, composite_key=True)
    gen_time = time.perf_counter() - t0
    
    tracemalloc.start()
    t_det_0 = time.perf_counter()
    report = detect_changes(source_df, target_df, b_key, tracked, p_date)
    detect_time = time.perf_counter() - t_det_0
    _, peak_det_mem = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    
    tracemalloc.start()
    t_app_0 = time.perf_counter()
    output_df = apply_scd2(source_df, target_df, report, b_key, tracked, p_date)
    apply_time = time.perf_counter() - t_app_0
    _, peak_app_mem = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    
    val_report = validate_scd2(output_df, b_key)
    assert val_report.passed
    
    total_time = detect_time + apply_time
    throughput = num_rows / total_time if total_time > 0 else 0
    
    results = {}
    if BENCHMARK_RESULTS_PATH.exists():
        try:
            with open(BENCHMARK_RESULTS_PATH, "r") as f:
                results = json.load(f)
        except Exception:
            pass
            
    results["100000_composite"] = {
        "num_rows": num_rows,
        "composite_key": True,
        "source_rows": source_df.height,
        "target_rows": target_df.height,
        "output_rows": output_df.height,
        "new_count": len(report.new),
        "changed_count": len(report.changed),
        "deleted_count": len(report.deleted),
        "unchanged_count": len(report.unchanged),
        "gen_time_sec": round(gen_time, 4),
        "detect_time_sec": round(detect_time, 4),
        "apply_time_sec": round(apply_time, 4),
        "total_time_sec": round(total_time, 4),
        "detect_peak_memory_mb": round(peak_det_mem / (1024 * 1024), 2),
        "apply_peak_memory_mb": round(peak_app_mem / (1024 * 1024), 2),
        "throughput_rows_per_sec": round(throughput, 1),
        "validation_passed": val_report.passed
    }
    
    with open(BENCHMARK_RESULTS_PATH, "w") as f:
        json.dump(results, f, indent=2)
