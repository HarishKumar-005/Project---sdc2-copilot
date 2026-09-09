"""Automated Benchmark Gate and Regression Threshold Tests for M2.4.

Validates:
- Benchmark gate artifact existence, complete metadata, and schema
- Deterministic reproducibility of the synthetic dataset generator
- Strict byte-for-byte result equivalence across auto, in-memory, and streaming engines
- Authoritative throughput regression gates (100K >= 1M rows/s, 1M >= 1.5M rows/s)
- Zero-leak Python heap memory gate (heap <= 1.0 MB during 1M row detection)
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
import polars as pl
from polars.testing import assert_frame_equal
import pytest

from src.scd2_copilot.detect_changes import detect_changes
from src.scd2_copilot.models import LazyRecordSequence
from src.scd2_copilot.transform_scd2 import apply_scd2
from src.scd2_copilot.validate import validate_scd2
from tests.adversarial.run_profiler import generate_synthetic_data

BENCHMARK_GATE_PATH = Path(__file__).resolve().parent / "benchmark_gate_m24.json"
BENCHMARK_GATE_M25_PATH = Path(__file__).resolve().parent / "benchmark_gate_m25.json"

pytestmark = pytest.mark.benchmark


def test_benchmark_artifact_exists_and_valid():
    """Verify that the M2.4 benchmark gate artifact exists with full metadata and all scales."""
    assert BENCHMARK_GATE_PATH.exists(), f"Gate artifact missing at {BENCHMARK_GATE_PATH}"
    with open(BENCHMARK_GATE_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 1. Metadata verification
    assert "metadata" in data
    meta = data["metadata"]
    assert meta["polars_version"] == "1.44.1"
    assert "logical_cpu_cores" in meta
    assert "platform" in meta
    assert "benchmark_timestamp_utc" in meta

    # 2. Benchmarks verification across all required scales
    assert "benchmarks" in data
    b = data["benchmarks"]
    required_scales = [
        "10000",
        "100000",
        "500000",
        "1000000",
        "2000000",
        "5000000",
        "100000_composite",
        "1000000_composite",
        "100000_in_memory",
        "100000_streaming",
        "1000000_in_memory",
        "1000000_streaming",
    ]
    for scale_key in required_scales:
        assert scale_key in b, f"Scale {scale_key} missing from benchmark gate artifact!"
        entry = b[scale_key]
        assert entry["validation_passed"] is True
        assert entry["performance_metrics"]["throughput_detect_apply_rows_sec"] > 100_000
        assert entry["timings_sec"]["total_detect_apply"]["median"] > 0
        assert entry["memory_metrics"]["process_rss_mb"] > 0

    # 3. Scalability analysis verification
    assert "scalability_analysis" in data
    sa = data["scalability_analysis"]
    assert "5000000" in sa
    assert sa["5000000"]["throughput_rows_sec"] > 1_500_000


def test_benchmark_m25_artifact_exists_and_valid():
    """Verify that the M2.5 benchmark gate artifact exists with full metadata, comparison, and all scales."""
    assert BENCHMARK_GATE_M25_PATH.exists(), f"Gate artifact missing at {BENCHMARK_GATE_M25_PATH}"
    with open(BENCHMARK_GATE_M25_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert "metadata" in data
    meta = data["metadata"]
    assert meta["polars_version"] == "1.44.1"

    assert "benchmarks" in data
    b = data["benchmarks"]
    required_scales = [
        "10000",
        "100000",
        "500000",
        "1000000",
        "2000000",
        "5000000",
        "100000_composite",
        "1000000_composite",
        "100000_in_memory",
        "100000_streaming",
        "1000000_in_memory",
        "1000000_streaming",
    ]
    for scale_key in required_scales:
        assert scale_key in b, f"Scale {scale_key} missing from M2.5 benchmark gate artifact!"
        entry = b[scale_key]
        assert entry["validation_passed"] is True
        assert entry["performance_metrics"]["throughput_e2e_rows_sec"] > 500_000

    assert "validation_speedup_comparison" in data
    comp = data["validation_speedup_comparison"]
    assert "5000000" in comp
    assert comp["5000000"]["validation_speedup_x"] >= 20.0
    assert comp["5000000"]["end_to_end_speedup_x"] >= 4.0


def test_authoritative_reproducibility():
    """Verify deterministic generator reproduces identical frames, counts, and classifications."""
    s1, t1 = generate_synthetic_data(10_000)
    s2, t2 = generate_synthetic_data(10_000)

    assert_frame_equal(s1, s2)
    assert_frame_equal(t1, t2)

    b_key = ["customer_id"]
    tracked = ["name", "city"]
    p_date = date(2026, 6, 8)

    r1 = detect_changes(s1, t1, b_key, tracked, p_date)
    r2 = detect_changes(s2, t2, b_key, tracked, p_date)

    assert len(r1.new) == len(r2.new) == 500
    assert len(r1.changed) == len(r2.changed) == 900
    assert len(r1.unchanged) == len(r2.unchanged) == 8100
    assert len(r1.deleted) == len(r2.deleted) == 500


def test_execution_modes_produce_identical_results():
    """Verify that auto, in-memory, and streaming engines produce byte-identical transformed DataFrames."""
    source_df, target_df = generate_synthetic_data(10_000)
    b_key = ["customer_id"]
    tracked = ["name", "city"]
    p_date = date(2026, 6, 8)

    rep_auto = detect_changes(source_df, target_df, b_key, tracked, p_date, engine="auto")
    out_auto = apply_scd2(source_df, target_df, rep_auto, b_key, tracked, p_date, engine="auto")

    rep_inmem = detect_changes(source_df, target_df, b_key, tracked, p_date, engine="in-memory")
    out_inmem = apply_scd2(source_df, target_df, rep_inmem, b_key, tracked, p_date, engine="in-memory")

    rep_stream = detect_changes(source_df, target_df, b_key, tracked, p_date, engine="streaming")
    out_stream = apply_scd2(source_df, target_df, rep_stream, b_key, tracked, p_date, engine="streaming")

    assert_frame_equal(out_auto, out_inmem)
    assert_frame_equal(out_auto, out_stream)


def test_regression_throughput_thresholds():
    """Gate against performance regression: verify throughput and heap memory limits."""
    if not BENCHMARK_GATE_M25_PATH.exists():
        pytest.skip("M2.5 Benchmark gate artifact not generated yet")

    with open(BENCHMARK_GATE_M25_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 1. Throughput Gate: 100K must exceed 1,000,000 rows/s
    tp_100k = data["benchmarks"]["100000"]["performance_metrics"]["throughput_detect_apply_rows_sec"]
    assert tp_100k >= 1_000_000, f"Regression: 100K throughput {tp_100k:,.0f} rows/s below 1,000,000 rows/s threshold!"

    # 2. Throughput Gate: 1M must exceed 1,500,000 rows/s
    tp_1m = data["benchmarks"]["1000000"]["performance_metrics"]["throughput_detect_apply_rows_sec"]
    assert tp_1m >= 1_500_000, f"Regression: 1M throughput {tp_1m:,.0f} rows/s below 1,500,000 rows/s threshold!"

    # 3. Python Heap Gate: 1M detect and validate peak heap must be <= 1.0 MB
    heap_detect_1m = data["benchmarks"]["1000000"]["memory_metrics"]["python_heap_peak_detect_mb"]
    assert heap_detect_1m <= 1.0, f"Regression: 1M detect peak heap {heap_detect_1m} MB exceeds 1.0 MB threshold!"
    heap_val_1m = data["benchmarks"]["1000000"]["memory_metrics"]["python_heap_peak_validate_mb"]
    assert heap_val_1m <= 1.0, f"Regression: 1M validate peak heap {heap_val_1m} MB exceeds 1.0 MB threshold!"

    # 4. M2.5 Validation Latency Gate:
    # 1M validation latency must be <= 0.15s (was 3.94s in M2.4)
    val_1m = data["benchmarks"]["1000000"]["timings_sec"]["validate"]["median"]
    assert val_1m <= 0.15, f"Regression: 1M validation latency {val_1m:.4f}s exceeds 0.15s threshold!"

    # 5M validation latency must be <= 0.80s (was 19.47s in M2.4)
    val_5m = data["benchmarks"]["5000000"]["timings_sec"]["validate"]["median"]
    assert val_5m <= 0.80, f"Regression: 5M validation latency {val_5m:.4f}s exceeds 0.80s threshold!"

