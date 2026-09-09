"""SCD2 transformation: apply change report to produce updated table.

Takes the existing target SCD2 table and the detected changes, then
produces the new SCD2 table with closed rows, new rows, and preserved
historical rows using native Polars relational operations.

Temporal Contract:
Validity intervals follow the half-open convention [effective_from, effective_to):
- effective_from is inclusive.
- effective_to is exclusive.
- When an active version is changed on processing_date D:
    old version: effective_to = D, is_current = False
    new version: effective_from = D, effective_to = None, is_current = True
  The boundary date D belongs strictly to the new version.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

import polars as pl

from .models import ChangeRecord, ChangeReport, DeletePolicy
from .validate import validate_business_keys


def _keys_to_df(
    records: list[ChangeRecord],
    business_key: list[str],
    fallback_schema: pl.Schema,
) -> pl.DataFrame:
    """Convert a list of ChangeRecord objects into a Polars DataFrame of business keys."""
    if not records:
        sub_schema = {k: fallback_schema.get(k, pl.String) for k in business_key if k in fallback_schema}
        return pl.DataFrame(schema=sub_schema)
    return pl.DataFrame([r.business_key_values for r in records]).select(business_key)


def _get_output_schema(
    source_df: pl.DataFrame,
    target_df: pl.DataFrame,
    business_key: list[str],
    tracked_columns: list[str],
) -> dict[str, pl.DataType]:
    """Determine the authoritative typed schema for SCD2 output."""
    schema: dict[str, pl.DataType] = {}
    for col in business_key + tracked_columns:
        if col in source_df.columns:
            schema[col] = source_df.schema[col]
        elif col in target_df.columns:
            schema[col] = target_df.schema[col]
        else:
            schema[col] = pl.String
    schema["effective_from"] = pl.Date
    schema["effective_to"] = pl.Date
    schema["is_current"] = pl.Boolean
    return schema


def apply_scd2(
    source_df: pl.DataFrame | pl.LazyFrame,
    target_df: pl.DataFrame | pl.LazyFrame,
    change_report: ChangeReport,
    business_key: list[str],
    tracked_columns: list[str],
    processing_date: date,
    delete_policy: Optional[DeletePolicy | str] = None,
    engine: str = "auto",
) -> pl.DataFrame:
    """Generate the updated SCD2 table under half-open [from, to) semantics.

    Vectorized implementation using native Polars operations:
    1. historical: existing non-current historical versions (is_current=False)
    2. retained_active: current target rows NOT in closing_keys (unchanged / incremental retained)
    3. closed: current target rows in closing_keys (CHANGED + DELETED under SOFT_DELETE),
       updated with effective_to=processing_date, is_current=False
    4. new_active: source rows for new_active_keys (NEW + CHANGED),
       inserted with effective_from=processing_date, effective_to=None, is_current=True

    Args:
        source_df: Today's source data.
        target_df: Yesterday's SCD2 table.
        change_report: The output of detect_changes().
        business_key: Business key column(s).
        tracked_columns: Tracked attribute columns.
        processing_date: Date to stamp on new/changed rows.
        delete_policy: Optional override. Must match change_report.delete_policy if provided.
        engine: Execution engine: "auto", "in-memory", or "streaming" (default: "auto").

    Returns:
        Updated SCD2 Polars DataFrame.
    """
    report_policy = getattr(change_report, "delete_policy", DeletePolicy.SOFT_DELETE)

    if delete_policy is not None:
        if isinstance(delete_policy, str):
            try:
                delete_policy = DeletePolicy(delete_policy.lower().strip())
            except ValueError:
                raise ValueError(
                    f"Invalid delete_policy: '{delete_policy}'. Must be 'soft_delete' or 'ignore'."
                )
        elif not isinstance(delete_policy, DeletePolicy):
            raise ValueError(
                f"Invalid delete_policy: {delete_policy!r}. Must be a DeletePolicy enum or string ('soft_delete', 'ignore')."
            )

        if report_policy != delete_policy:
            raise ValueError(
                f"Conflict in delete_policy: apply_scd2 received {delete_policy.value!r} but change_report has {report_policy.value!r}."
            )
        effective_delete_policy = delete_policy
    else:
        effective_delete_policy = report_policy

    if effective_delete_policy == DeletePolicy.IGNORE and change_report.deleted:
        raise ValueError(
            "Conflict: delete_policy is 'ignore' but change_report contains DELETED records."
        )

    # Collect LazyFrames if provided to ensure business key validation and schema access
    src_df = source_df.collect(engine=engine) if isinstance(source_df, pl.LazyFrame) else source_df
    tgt_df = target_df.collect(engine=engine) if isinstance(target_df, pl.LazyFrame) else target_df

    # Guard against duplicate business keys before transformation
    validate_business_keys(src_df, business_key, dataset_name="source")
    validate_business_keys(tgt_df, business_key, dataset_name="target")

    output_columns = business_key + tracked_columns + [
        "effective_from", "effective_to", "is_current"
    ]

    # Fast path: both datasets are empty
    if src_df.is_empty() and tgt_df.is_empty():
        return pl.DataFrame(schema=_get_output_schema(src_df, tgt_df, business_key, tracked_columns))

    # Fast path: target is empty (Day 1 initial load)
    if tgt_df.is_empty():
        if src_df.is_empty():
            return pl.DataFrame(schema=_get_output_schema(src_df, tgt_df, business_key, tracked_columns))
        result = (
            src_df.lazy()
            .with_columns([
                pl.lit(processing_date).alias("effective_from"),
                pl.lit(None).cast(pl.Date).alias("effective_to"),
                pl.lit(True).alias("is_current"),
            ])
            .select(output_columns)
            .sort(by=business_key + ["effective_from"])
            .collect(engine=engine)
        )
        return result

    # ── Resolve Key DataFrames ─────────────────────────────────────────
    has_vec_keys = (
        getattr(change_report, "new_keys_df", None) is not None
        and getattr(change_report, "changed_keys_df", None) is not None
        and getattr(change_report, "deleted_keys_df", None) is not None
    )

    if has_vec_keys:
        new_keys = change_report.new_keys_df
        changed_keys = change_report.changed_keys_df
        deleted_keys = change_report.deleted_keys_df
    else:
        new_keys = _keys_to_df(change_report.new, business_key, src_df.schema)
        changed_keys = _keys_to_df(change_report.changed, business_key, src_df.schema)
        deleted_keys = _keys_to_df(change_report.deleted, business_key, tgt_df.schema)

    closing_keys = pl.concat([changed_keys, deleted_keys]) if (changed_keys.height > 0 or deleted_keys.height > 0) else changed_keys.clear()
    new_active_keys = pl.concat([new_keys, changed_keys]) if (new_keys.height > 0 or changed_keys.height > 0) else new_keys.clear()

    # ── Relational Partitions ───────────────────────────────────────────
    s_lf = src_df.lazy()
    t_lf = tgt_df.lazy()

    # 1. Historical records (already closed in target)
    if "is_current" in tgt_df.columns:
        historical = (
            t_lf.filter(pl.col("is_current") == False)  # noqa: E712
            .with_columns([
                pl.col("effective_from").cast(pl.Date),
                pl.col("effective_to").cast(pl.Date),
                pl.col("is_current").cast(pl.Boolean),
            ])
            .select(output_columns)
        )
        target_current = t_lf.filter(pl.col("is_current") == True)  # noqa: E712
    else:
        historical = (
            t_lf.clear()
            .with_columns([
                pl.lit(None).cast(pl.Date).alias("effective_from"),
                pl.lit(None).cast(pl.Date).alias("effective_to"),
                pl.lit(False).alias("is_current"),
            ])
            .select(output_columns)
        )
        target_current = t_lf

    # 2. Retained Active (target current rows that are NOT in closing_keys)
    if closing_keys.height > 0:
        retained_active = (
            target_current
            .join(closing_keys.lazy(), on=business_key, how="anti")
            .with_columns([
                pl.col("effective_from").cast(pl.Date),
                pl.col("effective_to").cast(pl.Date),
                pl.col("is_current").cast(pl.Boolean),
            ])
            .select(output_columns)
        )
    else:
        retained_active = (
            target_current
            .with_columns([
                pl.col("effective_from").cast(pl.Date),
                pl.col("effective_to").cast(pl.Date),
                pl.col("is_current").cast(pl.Boolean),
            ])
            .select(output_columns)
        )

    # 3. Closed records (target current rows that ARE in closing_keys)
    if closing_keys.height > 0:
        closed = (
            target_current
            .join(closing_keys.lazy(), on=business_key, how="inner")
            .with_columns([
                pl.col("effective_from").cast(pl.Date),
                pl.lit(processing_date).alias("effective_to"),
                pl.lit(False).alias("is_current"),
            ])
            .select(output_columns)
        )
    else:
        closed = (
            target_current.clear()
            .with_columns([
                pl.lit(None).cast(pl.Date).alias("effective_from"),
                pl.lit(processing_date).alias("effective_to"),
                pl.lit(False).alias("is_current"),
            ])
            .select(output_columns)
        )

    # 4. New Active records (source rows for NEW + CHANGED keys)
    if new_active_keys.height > 0:
        new_active = (
            s_lf
            .join(new_active_keys.lazy(), on=business_key, how="inner")
            .with_columns([
                pl.lit(processing_date).alias("effective_from"),
                pl.lit(None).cast(pl.Date).alias("effective_to"),
                pl.lit(True).alias("is_current"),
            ])
            .select(output_columns)
        )
    else:
        new_active = (
            s_lf.clear()
            .with_columns([
                pl.lit(processing_date).alias("effective_from"),
                pl.lit(None).cast(pl.Date).alias("effective_to"),
                pl.lit(True).alias("is_current"),
            ])
            .select(output_columns)
        )

    # ── Concat, Sort & Collect ──────────────────────────────────────────
    combined = pl.concat([historical, retained_active, closed, new_active])
    sorted_plan = combined.sort(by=business_key + ["effective_from"])
    result_df = sorted_plan.collect(engine=engine)

    if result_df.is_empty():
        return pl.DataFrame(schema=_get_output_schema(src_df, tgt_df, business_key, tracked_columns))

    return result_df


# ── Backward Compatibility Helpers ─────────────────────────────────────


def _key_tuple(key_dict: dict, business_key: list[str]) -> tuple:
    """Convert a key dict to a hashable tuple (legacy helper)."""
    return tuple(key_dict[k] for k in business_key)


def _pick(row: dict, columns: list[str]) -> dict:
    """Pick only the specified columns from a row dict (legacy helper)."""
    return {c: row.get(c) for c in columns}


def _build_source_lookup(
    source_df: pl.DataFrame, business_key: list[str]
) -> dict[tuple, dict]:
    """Build a key → row lookup from the source DataFrame (legacy helper)."""
    lookup: dict[tuple, dict] = {}
    for row in source_df.iter_rows(named=True):
        key = tuple(row[k] for k in business_key)
        lookup[key] = row
    return lookup
