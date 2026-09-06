"""SCD2 Copilot — Master Saturation Audit Runner.

Runs programmatic property tests, composite key checks, schema drift simulations,
and performance stress tests up to 100K rows.
"""

from __future__ import annotations

import random
import time
import tracemalloc
from datetime import date, timedelta
import polars as pl

from src.scd2_copilot.detect_changes import detect_changes
from src.scd2_copilot.transform_scd2 import apply_scd2
from src.scd2_copilot.validate import validate_scd2
from src.scd2_copilot.models import ValidationStatus
from src.scd2_copilot.ingestion import validate_csv_columns

# Seed for reproducibility
random.seed(42)

def generate_random_string(length: int = 8) -> str:
    chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return "".join(random.choice(chars) for _ in range(length))

def run_property_based_tests(num_datasets: int = 1000):
    print(f"--- Running {num_datasets} Property-Based Tests ---")
    invariants_passed = 0
    total_records_checked = 0
    
    for i in range(num_datasets):
        # Generate random size (between 5 and 50)
        n = random.randint(5, 50)
        
        # Build source
        ids = list(range(1, n + 1))
        names = [generate_random_string() for _ in ids]
        cities = [generate_random_string() for _ in ids]
        source = pl.DataFrame({"id": ids, "name": names, "city": cities})
        
        # Build target (yesterday's SCD2)
        # Randomly choose some records to be in target already
        target_rows = []
        for id_val in ids:
            # 70% chance of record being in target
            if random.random() < 0.7:
                # 30% chance of having an old history row
                if random.random() < 0.3:
                    target_rows.append({
                        "id": id_val,
                        "name": generate_random_string(),
                        "city": generate_random_string(),
                        "effective_from": date(2026, 6, 1),
                        "effective_to": date(2026, 6, 5),
                        "is_current": False
                    })
                # The current row
                target_rows.append({
                    "id": id_val,
                    "name": names[id_val - 1] if random.random() < 0.5 else generate_random_string(),
                    "city": cities[id_val - 1] if random.random() < 0.5 else generate_random_string(),
                    "effective_from": date(2026, 6, 5),
                    "effective_to": None,
                    "is_current": True
                })
        
        if target_rows:
            target = pl.DataFrame(target_rows, schema_overrides={
                "effective_from": pl.Date,
                "effective_to": pl.Date,
                "is_current": pl.Boolean
            })
        else:
            target = pl.DataFrame({
                "id": pl.Series([], dtype=pl.Int64),
                "name": pl.Series([], dtype=pl.Utf8),
                "city": pl.Series([], dtype=pl.Utf8),
                "effective_from": pl.Series([], dtype=pl.Date),
                "effective_to": pl.Series([], dtype=pl.Date),
                "is_current": pl.Series([], dtype=pl.Boolean)
            })
            
        # Run pipeline
        pd = date(2026, 6, 8)
        report = detect_changes(source, target, ["id"], ["name", "city"], pd)
        output = apply_scd2(source, target, report, ["id"], ["name", "city"], pd)
        v = validate_scd2(output, ["id"])
        
        # Verify invariants
        assert v.passed, f"Validation failed: {v}"
        
        # Check current row rules
        current_rows = output.filter(pl.col("is_current") == True)
        assert current_rows["id"].n_unique() == current_rows.height, "Duplicate current records found!"
        
        # Check that no records disappeared from source
        source_keys = set(source["id"])
        target_current_keys = set(target.filter(pl.col("is_current") == True)["id"])
        expected_output_keys = source_keys | target_current_keys
        output_keys = set(output["id"])
        assert expected_output_keys == output_keys, "Records disappeared or added incorrectly!"
        
        invariants_passed += 1
        total_records_checked += output.height
        
    print(f"[OK] All {invariants_passed} property tests passed! Inspected {total_records_checked} rows.\n")
    return invariants_passed, total_records_checked

def run_composite_key_audit():
    print("--- Running Composite Key Audit ---")
    # Define keys & schemas
    composite_configs = [
        {"keys": ["customer_id", "source_system"], "tracked": ["name"]},
        {"keys": ["customer_id", "region"], "tracked": ["name"]},
        {"keys": ["emp_id", "dept"], "tracked": ["salary"]},
        {"keys": ["product_id", "warehouse"], "tracked": ["qty"]}
    ]
    
    for config in composite_configs:
        bk = config["keys"]
        tc = config["tracked"]
        
        # Create a source and target with mixed scenarios (new, changed, unchanged, deleted)
        source = pl.DataFrame({
            bk[0]: [1, 1, 2, 2],
            bk[1]: ["A", "B", "A", "B"],
            tc[0]: ["Val_New", "Val_Changed_Src", "Val_Unchanged", "Val_New2"]
        })
        
        target = pl.DataFrame({
            bk[0]: [1, 2, 3],
            bk[1]: ["B", "A", "A"],
            tc[0]: ["Val_Changed_Tgt", "Val_Unchanged", "Val_Deleted"],
            "effective_from": [date(2026, 6, 1)] * 3,
            "effective_to": [None] * 3,
            "is_current": [True] * 3
        })
        
        pd = date(2026, 6, 8)
        report = detect_changes(source, target, bk, tc, pd)
        
        # Verify categorizations
        assert len(report.new) == 2  # (1, A) and (2, B)
        assert len(report.changed) == 1  # (1, B)
        assert len(report.unchanged) == 1  # (2, A)
        assert len(report.deleted) == 1  # (3, A)
        
        output = apply_scd2(source, target, report, bk, tc, pd)
        v = validate_scd2(output, bk)
        assert v.passed
        
        print(f"[OK] Composite key config {bk} validated successfully.")
    print("Composite key readiness: 100/100\n")

def run_schema_evolution_audit():
    print("--- Running Schema Evolution Audit ---")
    
    # 1. Column added to source (should error out as source columns are not subset of target data columns)
    source = pl.DataFrame({"id": [1], "name": ["A"], "new_col": ["X"]})
    target = pl.DataFrame({
        "id": [1], "name": ["A"],
        "effective_from": [date(2026, 6, 1)], "effective_to": [None], "is_current": [True]
    })
    errors = validate_csv_columns(source, target)
    assert any("Source columns not found in target" in e for e in errors), "Schema drift (column added) not caught!"
    print("[OK] Schema evolution: Column addition correctly caught.")

    # 2. Column removed from source (valid subset, but dropped from target outputs)
    source = pl.DataFrame({"id": [1]})
    target = pl.DataFrame({
        "id": [1], "name": ["A"],
        "effective_from": [date(2026, 6, 1)], "effective_to": [None], "is_current": [True]
    })
    errors = validate_csv_columns(source, target)
    assert not errors, "Schema drift (column removed) raised error incorrectly!"
    # When columns are missing in source, they are excluded from tracked_columns
    report = detect_changes(source, target, ["id"], [], date(2026, 6, 8))
    output = apply_scd2(source, target, report, ["id"], [], date(2026, 6, 8))
    assert "name" not in output.columns, "name column should be omitted from output schema"
    print("[OK] Schema evolution: Column removal successfully processed.")

    # 3. Column renamed in source
    source = pl.DataFrame({"id": [1], "name_new": ["A"]})
    target = pl.DataFrame({
        "id": [1], "name": ["A"],
        "effective_from": [date(2026, 6, 1)], "effective_to": [None], "is_current": [True]
    })
    errors = validate_csv_columns(source, target)
    assert any("Source columns not found in target" in e for e in errors)
    print("[OK] Schema evolution: Column rename in source caught as missing column.")
    
    # 4. Mixed types in same column
    source = pl.DataFrame({"id": [1], "val": ["100"]}) # String
    target = pl.DataFrame({
        "id": [1], "val": [100], # Integer
        "effective_from": [date(2026, 6, 1)], "effective_to": [None], "is_current": [True]
    })
    # String "100" vs Int 100 will compare as equal (normalized to string "100")
    report = detect_changes(source, target, ["id"], ["val"], date(2026, 6, 8))
    assert len(report.unchanged) == 1
    print("[OK] Schema evolution: Mixed types normalized and compared correctly (detected as unchanged).\n")

def run_performance_benchmarks():
    print("--- Running Performance Stress Tests ---")
    sizes = [1000, 10000, 25000, 50000, 100000]
    
    results = []
    
    for size in sizes:
        # Generate synthetic datasets
        ids = list(range(size))
        names_source = [f"Name_{i}" for i in ids]
        names_target = [f"Name_{i}" if i % 10 != 0 else f"OldName_{i}" for i in ids]
        
        # Half of target rows are historical, half are current
        # Target size = 1.5 * size (0.5 size historical, 1 size current)
        target_rows = []
        for i in ids:
            # 50% chance of historical record
            if i % 2 == 0:
                target_rows.append({
                    "id": i, "name": f"HistName_{i}",
                    "effective_from": date(2026, 6, 1), "effective_to": date(2026, 6, 5), "is_current": False
                })
            target_rows.append({
                "id": i, "name": names_target[i],
                "effective_from": date(2026, 6, 5), "effective_to": None, "is_current": True
            })
            
        source = pl.DataFrame({"id": ids, "name": names_source})
        target = pl.DataFrame(target_rows, schema_overrides={
            "effective_from": pl.Date,
            "effective_to": pl.Date,
            "is_current": pl.Boolean
        })
        
        pd = date(2026, 6, 8)
        
        # Start memory tracking
        tracemalloc.start()
        start_time = time.perf_counter()
        start_cpu = time.process_time()
        
        # Pipeline execution
        report = detect_changes(source, target, ["id"], ["name"], pd)
        output = apply_scd2(source, target, report, ["id"], ["name"], pd)
        v = validate_scd2(output, ["id"])
        
        end_time = time.perf_counter()
        end_cpu = time.process_time()
        _, peak_mem = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        
        elapsed_time = end_time - start_time
        cpu_time = end_cpu - start_cpu
        peak_mem_mb = peak_mem / (1024 * 1024)
        
        assert v.passed
        
        print(f"Size: {size:6d} | Time: {elapsed_time:7.4f}s | CPU: {cpu_time:7.4f}s | Peak Mem: {peak_mem_mb:7.3f} MB")
        results.append({
            "size": size,
            "time": elapsed_time,
            "cpu": cpu_time,
            "mem": peak_mem_mb
        })
    print()
    return results

if __name__ == "__main__":
    print("====================================================")
    print("            SCD2 COPILOT SATURATION RUNNER          ")
    print("====================================================\n")
    
    run_property_based_tests()
    run_composite_key_audit()
    run_schema_evolution_audit()
    run_performance_benchmarks()
    
    print("====================================================")
    print("          SATURATION RUN COMPLETED SUCCESSFULLY     ")
    print("====================================================")
