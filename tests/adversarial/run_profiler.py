"""Performance profiler and baseline benchmark execution for M2.2 Vectorized Engine.

Profiles the vectorized Polars SCD2 pipeline (detect_changes, apply_scd2, validate_scd2)
across 10K, 100K, 500K, and 1M scales, and compares eager vs in-memory vs streaming modes.
"""

from __future__ import annotations

import cProfile
import io
import json
import pstats
from datetime import date
from pathlib import Path
import sys
import time
import tracemalloc
import polars as pl

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.scd2_copilot.detect_changes import detect_changes
from src.scd2_copilot.transform_scd2 import apply_scd2
from src.scd2_copilot.validate import validate_scd2

BENCHMARK_RESULTS_PATH = Path(__file__).resolve().parent / "benchmark_results.json"


def generate_synthetic_data(num_rows: int, composite_key: bool = False) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Generate deterministic synthetic source and target dataframes at high speed."""
    split_src_start = int(num_rows * 0.05) + 1
    split_tgt_end = int(num_rows * 0.95)
    
    tgt_active = pl.DataFrame({
        "customer_id": pl.int_range(1, split_tgt_end + 1, dtype=pl.Int64, eager=True),
    }).with_columns([
        pl.format("Name_{}", pl.col("customer_id")).alias("name"),
        pl.format("City_{}", pl.col("customer_id")).alias("city"),
        pl.lit(date(2026, 6, 1)).cast(pl.Date).alias("effective_from"),
        pl.lit(None).cast(pl.Date).alias("effective_to"),
        pl.lit(True).alias("is_current"),
    ])
    
    tgt_hist = tgt_active.filter(pl.col("customer_id") % 10 == 0).with_columns([
        pl.format("OldName_{}", pl.col("customer_id")).alias("name"),
        pl.format("OldCity_{}", pl.col("customer_id")).alias("city"),
        pl.lit(date(2026, 1, 1)).cast(pl.Date).alias("effective_from"),
        pl.lit(date(2026, 6, 1)).cast(pl.Date).alias("effective_to"),
        pl.lit(False).alias("is_current"),
    ])
    
    target_df = pl.concat([tgt_active, tgt_hist])
    
    source_df = pl.DataFrame({
        "customer_id": pl.int_range(split_src_start, num_rows + 1, dtype=pl.Int64, eager=True),
    }).with_columns([
        pl.when((pl.col("customer_id") <= split_tgt_end) & (pl.col("customer_id") % 10 == 1))
        .then(pl.format("NewName_{}", pl.col("customer_id")))
        .otherwise(pl.format("Name_{}", pl.col("customer_id")))
        .alias("name"),
        pl.format("City_{}", pl.col("customer_id")).alias("city"),
    ])
    
    if composite_key:
        target_df = target_df.with_columns(
            pl.format("REG_{}", (pl.col("customer_id") % 5) + 1).alias("region_id")
        ).select(["region_id", "customer_id", "name", "city", "effective_from", "effective_to", "is_current"])
        source_df = source_df.with_columns(
            pl.format("REG_{}", (pl.col("customer_id") % 5) + 1).alias("region_id")
        ).select(["region_id", "customer_id", "name", "city"])
        
    return source_df, target_df


def profile_hot_path(num_rows: int = 50_000) -> str:
    """Run cProfile on vectorized detect_changes and apply_scd2."""
    print(f"\n==================== PROFILING VECTORIZED ENGINE ({num_rows:,} rows) ====================")
    source_df, target_df = generate_synthetic_data(num_rows)
    b_key = ["customer_id"]
    tracked = ["name", "city"]
    p_date = date(2026, 6, 8)
    
    profiler = cProfile.Profile()
    profiler.enable()
    
    report = detect_changes(source_df, target_df, b_key, tracked, p_date)
    output_df = apply_scd2(source_df, target_df, report, b_key, tracked, p_date)
    
    profiler.disable()
    
    s = io.StringIO()
    ps = pstats.Stats(profiler, stream=s).sort_stats("cumulative")
    ps.print_stats(30)
    profile_text = s.getvalue()
    print(profile_text[:2000])
    return profile_text


def benchmark_scale(num_rows: int, composite_key: bool = False, engine: str = "auto") -> dict:
    """Benchmark a specific dataset scale with isolated stage timing and tracemalloc."""
    key_desc = "composite (region_id, customer_id)" if composite_key else "single (customer_id)"
    print(f"\n--- Running Vectorized Benchmark: {num_rows:,} rows | Key: {key_desc} | Engine: {engine} ---")
    
    # 1. Generation
    t0 = time.perf_counter()
    source_df, target_df = generate_synthetic_data(num_rows, composite_key=composite_key)
    gen_time = time.perf_counter() - t0
    
    b_key = ["region_id", "customer_id"] if composite_key else ["customer_id"]
    tracked = ["name", "city"]
    p_date = date(2026, 6, 8)
    
    # 2. Detect Changes
    tracemalloc.start()
    t_det_0 = time.perf_counter()
    report = detect_changes(
        source_df=source_df,
        target_df=target_df,
        business_key=b_key,
        tracked_columns=tracked,
        processing_date=p_date,
        engine=engine,
    )
    detect_time = time.perf_counter() - t_det_0
    _, peak_detect_mem = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    
    # 3. Apply SCD2
    tracemalloc.start()
    t_app_0 = time.perf_counter()
    output_df = apply_scd2(
        source_df=source_df,
        target_df=target_df,
        change_report=report,
        business_key=b_key,
        tracked_columns=tracked,
        processing_date=p_date,
        engine=engine,
    )
    apply_time = time.perf_counter() - t_app_0
    _, peak_apply_mem = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    
    # 4. Validation
    t_val_0 = time.perf_counter()
    v_report = validate_scd2(output_df, b_key)
    val_time = time.perf_counter() - t_val_0
    assert v_report.passed, f"Validation failed for {num_rows} rows!"
    
    total_time = detect_time + apply_time
    e2e_time = total_time + val_time
    throughput = num_rows / total_time if total_time > 0 else 0
    e2e_throughput = num_rows / e2e_time if e2e_time > 0 else 0
    
    res = {
        "num_rows": num_rows,
        "composite_key": composite_key,
        "engine": engine,
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
        "e2e_time_sec": round(e2e_time, 4),
        "detect_peak_memory_mb": round(peak_detect_mem / (1024 * 1024), 2),
        "apply_peak_memory_mb": round(peak_apply_mem / (1024 * 1024), 2),
        "throughput_rows_per_sec": round(throughput, 1),
        "e2e_throughput_rows_per_sec": round(e2e_throughput, 1),
        "validation_passed": v_report.passed,
    }
    
    print(f"Gen: {gen_time:.4f}s | Detect: {detect_time:.4f}s ({res['detect_peak_memory_mb']} MB) | "
          f"Apply: {apply_time:.4f}s ({res['apply_peak_memory_mb']} MB) | Val: {val_time:.4f}s | "
          f"Total: {total_time:.4f}s | Throughput: {throughput:,.0f} rows/s")
    return res


def main():
    profile_hot_path(50_000)
    
    scales = [10_000, 100_000, 500_000, 1_000_000]
    results = {}
    
    # Load M2.1 baseline if exists to preserve comparison
    baseline_path = Path(__file__).resolve().parent / "benchmark_results_m21_baseline.json"
    if BENCHMARK_RESULTS_PATH.exists() and not baseline_path.exists():
        with open(BENCHMARK_RESULTS_PATH, "r") as f:
            baseline_data = json.load(f)
        with open(baseline_path, "w") as f:
            json.dump(baseline_data, f, indent=2)
            
    for scale in scales:
        entry = benchmark_scale(scale, composite_key=False, engine="auto")
        results[str(scale)] = entry
        
    # Composite key comparison at 100K
    comp_entry = benchmark_scale(100_000, composite_key=True, engine="auto")
    results["100000_composite"] = comp_entry
    
    # Engine comparisons at 100K (in-memory vs streaming)
    in_mem_entry = benchmark_scale(100_000, composite_key=False, engine="in-memory")
    results["100000_in_memory"] = in_mem_entry
    
    streaming_entry = benchmark_scale(100_000, composite_key=False, engine="streaming")
    results["100000_streaming"] = streaming_entry
    
    with open(BENCHMARK_RESULTS_PATH, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved all M2.2 benchmark results to {BENCHMARK_RESULTS_PATH}")


if __name__ == "__main__":
    main()
