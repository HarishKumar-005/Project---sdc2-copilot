"""Query Plan and Lazy Execution Inspector for detect_changes (Phase 2 of M2.3).

Inspects the optimized and unoptimized logical/physical execution plans for
detect_changes under Polars 1.44.1, evaluating:
- Projection pushdown
- Predicate pushdown
- Common subplan caching
- Expression reuse
- Streaming plan behavior
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
import sys
import polars as pl

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.adversarial.run_profiler import generate_synthetic_data


def _normalize_expr(col_expr: pl.Expr) -> pl.Expr:
    s = col_expr.cast(pl.String).str.strip_chars()
    return pl.when(s == "").then(None).otherwise(s)


def inspect_plans():
    source_df, target_df = generate_synthetic_data(1000)
    business_key = ["customer_id"]
    tracked_columns = ["name", "city"]
    processing_date = date(2026, 6, 8)

    src_lf = source_df.lazy()
    tgt_lf = target_df.lazy()

    target_current = tgt_lf.filter(pl.col("is_current") == True).with_columns(
        pl.lit(True).alias("_target_match")
    )

    joined = src_lf.join(
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
    is_changed = pl.any_horizontal(diff_exprs)

    new_keys = joined.filter(pl.col("_target_match").is_null()).select(business_key)
    changed_keys = joined.filter(pl.col("_target_match").is_not_null() & is_changed).select(business_key)
    unchanged_keys = joined.filter(pl.col("_target_match").is_not_null() & (~is_changed)).select(business_key)
    deleted_keys = target_current.join(src_lf, on=business_key, how="anti").select(business_key)

    out_lines = []
    out_lines.append("=" * 80)
    out_lines.append(f"POLARS VERSION: {pl.__version__}")
    out_lines.append("=" * 80)

    out_lines.append("\n--- UNOPTIMIZED JOIN & CLASSIFICATION PLAN ---")
    out_lines.append(joined.explain(optimized=False))

    out_lines.append("\n--- OPTIMIZED JOIN & CLASSIFICATION PLAN ---")
    out_lines.append(joined.explain(optimized=True))

    out_lines.append("\n--- OPTIMIZED DELETED ANTI-JOIN PLAN ---")
    out_lines.append(deleted_keys.explain(optimized=True))

    out_lines.append("\n--- OPTIMIZED CHANGED KEYS PLAN ---")
    out_lines.append(changed_keys.explain(optimized=True))

    plan_text = "\n".join(out_lines)
    
    # Save plan output with safe ascii encoding for Windows
    out_file = Path(__file__).resolve().parent / "query_plan_inspection.txt"
    out_file.write_text(plan_text, encoding="utf-8")
    print(f"Query plans successfully saved to {out_file}")


if __name__ == "__main__":
    inspect_plans()
