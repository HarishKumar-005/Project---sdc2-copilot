"""Performance benchmark suite for sdc2-copilot.

Tests processing of large-scale datasets (1k, 10k, 25k, 50k, 100k rows)
to measure runtime, memory usage, and throughput of the SCD2 pipeline.
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

BENCHMARK_RESULTS_PATH = Path(__file__).resolve().parent / "benchmark_results.json"


def generate_synthetic_data(num_rows: int) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Generate synthetic source and target dataframes for benchmarking.
    
    Generates a mix of:
    - Unchanged records (80%)
    - Changed records (10%)
    - New records (5%)
    - Deleted records (5%)
    """
    # Keys
    all_keys = list(range(1, num_rows + 1))
    
    # Split key space
    # Target keys: 1 to 95% of num_rows
    # Source keys: 5% of num_rows to 100% of num_rows
    split_index_source_start = int(num_rows * 0.05)
    split_index_target_end = int(num_rows * 0.95)
    
    target_keys = all_keys[:split_index_target_end]
    source_keys = all_keys[split_index_source_start:]
    
    # Target dataframe representation
    target_names = [f"Name_{k}" for k in target_keys]
    target_cities = [f"City_{k}" for k in target_keys]
    # We will make some records inactive to represent historical records
    # Let's say 20% of target records are inactive (old history)
    effective_from_dates = []
    effective_to_dates = []
    is_current_flags = []
    
    # To maintain consistency, we will create target records
    target_rows = []
    for idx, k in enumerate(target_keys):
        # 10% of keys have a history (an inactive record and an active record)
        if idx % 10 == 0:
            # Inactive record
            target_rows.append({
                "customer_id": k,
                "name": f"OldName_{k}",
                "city": f"OldCity_{k}",
                "effective_from": date(2026, 1, 1),
                "effective_to": date(2026, 6, 1),
                "is_current": False
            })
            # Active record
            target_rows.append({
                "customer_id": k,
                "name": f"Name_{k}",
                "city": f"City_{k}",
                "effective_from": date(2026, 6, 1),
                "effective_to": None,
                "is_current": True
            })
        else:
            target_rows.append({
                "customer_id": k,
                "name": f"Name_{k}",
                "city": f"City_{k}",
                "effective_from": date(2026, 6, 1),
                "effective_to": None,
                "is_current": True
            })
            
    target_df = pl.DataFrame(target_rows)
    
    # Source dataframe representation
    source_rows = []
    for k in source_keys:
        # Check if it should be changed (10% of source keys that exist in target)
        if k in target_keys and k % 10 == 1:
            source_rows.append({
                "customer_id": k,
                "name": f"NewName_{k}",  # changed
                "city": f"City_{k}"
            })
        else:
            source_rows.append({
                "customer_id": k,
                "name": f"Name_{k}",      # unchanged
                "city": f"City_{k}"
            })
            
    source_df = pl.DataFrame(source_rows)
    
    return source_df, target_df


@pytest.mark.parametrize(
    "num_rows",
    [1000, 10000, 25000, 50000, 100000]
)
def test_pipeline_scale_performance(num_rows):
    """Run performance scaling test for a specific dataset size."""
    print(f"\n--- Starting performance benchmark for {num_rows} rows ---")
    
    # 1. Generation
    t0 = time.perf_counter()
    source_df, target_df = generate_synthetic_data(num_rows)
    gen_time = time.perf_counter() - t0
    print(f"Dataset generated in {gen_time:.4f}s. Source: {source_df.height} rows, Target: {target_df.height} rows")
    
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
    print(f"New: {len(report.new)}, Changed: {len(report.changed)}, Deleted: {len(report.deleted)}, Unchanged: {len(report.unchanged)}")
    
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
    
    print(f"SCD2 transformation applied in {apply_time:.4f}s. Output: {output_df.height} rows")
    
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
        "new_count": len(report.new),
        "changed_count": len(report.changed),
        "deleted_count": len(report.deleted),
        "detect_time_sec": detect_time,
        "apply_time_sec": apply_time,
        "total_time_sec": total_time,
        "detect_peak_memory_mb": peak_detect_mb,
        "apply_peak_memory_mb": peak_apply_mb,
        "throughput_rows_per_sec": throughput,
        "validation_passed": validation_report.passed
    }
    
    with open(BENCHMARK_RESULTS_PATH, "w") as f:
        json.dump(results, f, indent=2)
        
    # Reasonable sanity assertions to ensure no massive performance degradation
    # A 100k transformation should easily complete in less than 15 seconds with Polars + Python dict lookups
    assert total_time < 15.0, f"Performance too slow: {total_time:.2f}s for {num_rows} rows"
