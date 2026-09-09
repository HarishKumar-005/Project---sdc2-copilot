"""Sanity and isolation tests for the SCD2 benchmark suite.

Verifies:
- Vectorized synthetic data generation speed and exact distribution proportions.
- Single and composite business key support.
- Full compliance with SCD2 invariants.
- Strict test isolation ensuring benchmark tests are marked and deselected during routine runs.
"""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path
import polars as pl
import pytest

from tests.adversarial.run_profiler import generate_synthetic_data
from src.scd2_copilot.detect_changes import detect_changes
from src.scd2_copilot.transform_scd2 import apply_scd2
from src.scd2_copilot.validate import validate_scd2


def test_synthetic_data_proportions():
    """Verify synthetic data generator produces the intended distribution."""
    num_rows = 1000
    source_df, target_df = generate_synthetic_data(num_rows, composite_key=False)
    
    # Target should have keys 1 to 950 active (95%), plus 10% historical (95 rows) -> 1045 rows
    assert target_df.height == 1045
    assert target_df.filter(pl.col("is_current") == True).height == 950
    assert target_df.filter(pl.col("is_current") == False).height == 95
    
    # Source should have keys 51 to 1000 (950 rows)
    assert source_df.height == 950
    
    report = detect_changes(
        source_df=source_df,
        target_df=target_df,
        business_key=["customer_id"],
        tracked_columns=["name", "city"],
        processing_date=date(2026, 6, 8),
    )
    
    # Exact counts:
    # New: keys 951..1000 (50 keys, exactly 5%)
    assert len(report.new) == 50
    # Deleted: keys 1..50 (50 keys, exactly 5%)
    assert len(report.deleted) == 50
    # Overlapping keys: 51..950 (900 keys)
    # Changed: keys where k % 10 == 1 -> 90 keys (exactly 10% of 900)
    assert len(report.changed) == 90
    # Unchanged: remaining 810 keys
    assert len(report.unchanged) == 810


def test_synthetic_data_scd2_validation():
    """Verify generated data passes all deterministic SCD2 invariant checks."""
    source_df, target_df = generate_synthetic_data(1000, composite_key=False)
    b_key = ["customer_id"]
    tracked = ["name", "city"]
    p_date = date(2026, 6, 8)
    
    report = detect_changes(source_df, target_df, b_key, tracked, p_date)
    output_df = apply_scd2(source_df, target_df, report, b_key, tracked, p_date)
    
    val_report = validate_scd2(output_df, b_key)
    assert val_report.passed, f"Validation failed: {val_report.rule_results}"
    
    # Explicit invariant verification
    # 1. Exactly one current row per active business key
    current_rows = output_df.filter(pl.col("is_current") == True)
    assert current_rows.height == current_rows.select(b_key).n_unique()
    
    # 2. All current rows have effective_to is None
    assert current_rows.filter(pl.col("effective_to").is_not_null()).height == 0
    
    # 3. All non-current rows have effective_to is not None
    closed_rows = output_df.filter(pl.col("is_current") == False)
    assert closed_rows.filter(pl.col("effective_to").is_null()).height == 0
    
    # 4. Invariant: effective_from < effective_to for all closed rows
    invalid_dates = closed_rows.filter(pl.col("effective_from") >= pl.col("effective_to"))
    assert invalid_dates.height == 0


def test_synthetic_data_composite_key():
    """Verify composite business keys are supported in generation and SCD2 pipeline."""
    source_df, target_df = generate_synthetic_data(1000, composite_key=True)
    b_key = ["region_id", "customer_id"]
    tracked = ["name", "city"]
    p_date = date(2026, 6, 8)
    
    assert "region_id" in source_df.columns
    assert "region_id" in target_df.columns
    
    report = detect_changes(source_df, target_df, b_key, tracked, p_date)
    output_df = apply_scd2(source_df, target_df, report, b_key, tracked, p_date)
    
    val_report = validate_scd2(output_df, b_key)
    assert val_report.passed, f"Composite validation failed: {val_report.rule_results}"
    assert output_df.select(b_key).n_unique() > 0


def test_synthetic_generation_performance():
    """Verify synthetic data generation is vectorized and takes less than 0.1s for 10K rows."""
    import time
    t0 = time.perf_counter()
    source_df, target_df = generate_synthetic_data(10_000)
    gen_time = time.perf_counter() - t0
    assert gen_time < 0.1, f"Generation too slow: {gen_time:.4f}s"
    assert source_df.height == 9500
    assert target_df.height == 10450


def test_benchmark_marker_isolation():
    """Verify all benchmark test functions are marked with @pytest.mark.benchmark."""
    bench_file = Path(__file__).resolve().parent / "adversarial" / "test_performance_benchmarks.py"
    assert bench_file.exists()
    
    tree = ast.parse(bench_file.read_text(encoding="utf-8"))
    test_functions = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    ]
    
    assert len(test_functions) > 0
    for fn in test_functions:
        marker_names = []
        for dec in fn.decorator_list:
            if isinstance(dec, ast.Attribute) and dec.attr == "benchmark":
                marker_names.append("benchmark")
            elif isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr == "benchmark":
                marker_names.append("benchmark")
        assert "benchmark" in marker_names, f"Function {fn.name} missing @pytest.mark.benchmark"
