"""Phase 1 Profiling Script for M2.5: Vectorized Validation Optimization.

Profiles the 7 stages of validate_scd2:
1. schema checks
2. null-key checks
3. duplicate-key checks (one_current_per_key)
4. current-row consistency (flag pairing checks)
5. date consistency (effective_from < effective_to)
6. temporal overlap detection (no_overlapping_dates)
7. final aggregation / report construction

Evaluates across 100K, 500K, 1M, 2M, and 5M scales.
"""

from __future__ import annotations

import gc
import json
from datetime import date
from pathlib import Path
import sys
import time
from typing import Any

import polars as pl

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.scd2_copilot.detect_changes import detect_changes
from src.scd2_copilot.transform_scd2 import apply_scd2
from src.scd2_copilot.validate import (
    _check_date_consistency,
    _check_no_null_keys,
    _check_no_overlapping_dates,
    _check_one_current_per_key,
    _check_schema_completeness,
    validate_scd2,
)
from src.scd2_copilot.models import ValidationReport, ValidationRule, ValidationStatus
from tests.adversarial.run_profiler import generate_synthetic_data


def profile_stages_for_scale(num_rows: int) -> dict[str, Any]:
    print(f"\n--- Profiling Validation Stages for {num_rows:,} rows ---")
    source_df, target_df = generate_synthetic_data(num_rows)
    b_key = ["customer_id"]
    tracked = ["name", "city"]
    p_date = date(2026, 6, 8)

    # Generate output DataFrame using vectorized detect and apply
    report = detect_changes(source_df, target_df, b_key, tracked, p_date)
    output_df = apply_scd2(source_df, target_df, report, b_key, tracked, p_date)
    
    print(f"Output DataFrame height: {output_df.height:,} rows")
    gc.collect()

    # Stage 1: Schema Completeness
    t0 = time.perf_counter()
    rule_schema = _check_schema_completeness(output_df)
    t_schema = time.perf_counter() - t0

    # Stage 2: Null-Key Checks
    t0 = time.perf_counter()
    rule_null = _check_no_null_keys(output_df, b_key)
    t_null = time.perf_counter() - t0

    # Stage 3: Duplicate-Key (one_current_per_key)
    t0 = time.perf_counter()
    rule_current = _check_one_current_per_key(output_df, b_key)
    t_current = time.perf_counter() - t0

    # Stage 4: Current-Row Consistency (flag pairing checks)
    t0 = time.perf_counter()
    bad_active = output_df.filter(
        (pl.col("is_current") == True) & pl.col("effective_to").is_not_null()
    )
    bad_closed = output_df.filter(
        (pl.col("is_current") == False) & pl.col("effective_to").is_null()
    )
    flag_count = bad_active.height + bad_closed.height
    t_flags = time.perf_counter() - t0

    # Stage 5: Date Consistency (effective_from < effective_to)
    t0 = time.perf_counter()
    bad_dates = output_df.filter(
        pl.col("effective_to").is_not_null()
        & (pl.col("effective_from") >= pl.col("effective_to"))
    )
    bad_dates_count = bad_dates.height
    t_dates = time.perf_counter() - t0

    # Stage 6: Temporal Overlap Detection
    t0 = time.perf_counter()
    rule_overlap = _check_no_overlapping_dates(output_df, b_key)
    t_overlap = time.perf_counter() - t0

    # Stage 7: Final Aggregation / Report Construction
    t0 = time.perf_counter()
    val_report = ValidationReport()
    val_report.rules.extend([rule_schema, rule_current, rule_null, rule_overlap])
    _ = val_report.passed
    _ = val_report.summary
    t_report = time.perf_counter() - t0

    # Full validate_scd2 call for end-to-end verification
    t0 = time.perf_counter()
    full_report = validate_scd2(output_df, b_key)
    t_full = time.perf_counter() - t0

    result = {
        "num_rows": num_rows,
        "output_rows": output_df.height,
        "stage_1_schema_sec": round(t_schema, 6),
        "stage_2_null_key_sec": round(t_null, 6),
        "stage_3_duplicate_key_sec": round(t_current, 6),
        "stage_4_current_row_flag_sec": round(t_flags, 6),
        "stage_5_date_consistency_sec": round(t_dates, 6),
        "stage_6_overlap_sec": round(t_overlap, 6),
        "stage_7_report_construction_sec": round(t_report, 6),
        "sum_of_stages_sec": round(t_schema + t_null + t_current + t_flags + t_dates + t_overlap + t_report, 6),
        "full_validate_scd2_sec": round(t_full, 6),
        "overlap_pct_of_total": round((t_overlap / max(t_full, 1e-6)) * 100, 2),
    }

    print(f"Results for {num_rows:,}:")
    for k, v in result.items():
        print(f"  {k}: {v}")
    return result


def main():
    scales = [100_000, 500_000, 1_000_000, 2_000_000, 5_000_000]
    all_results = {}
    for scale in scales:
        all_results[str(scale)] = profile_stages_for_scale(scale)
        gc.collect()

    out_path = Path(__file__).resolve().parent / "validation_profile_baseline.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nSaved profile results to {out_path}")


if __name__ == "__main__":
    main()
