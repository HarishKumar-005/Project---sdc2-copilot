# SCD2 Copilot — Data Contracts & Monitor Configuration

## 1. Overview

This document formalizes the data contracts and configuration interfaces introduced in **V3 Phase 1 — Configurable PostgreSQL Source**.

The core principle remains:
> **The engine decides. Validation protects. AI explains.**

To decouple the SCD2 pipeline from any hard-coded domain schema (such as warehouse inventory), source definitions are formalized as typed data contracts validated both structurally at configuration time and semantically against live PostgreSQL metadata.

---

## 2. Configuration Schema (`MonitorConfig`)

A monitor configuration defines how an operational PostgreSQL table is watched, which columns constitute its business identity, what timestamp governs incremental watermarking, and which columns are tracked for SCD2 historical preservation.

### Specification

```yaml
monitor:
  name: warehouse_inventory

  source:
    type: postgresql
    schema: public
    table: inventory_source

  keys:
    - sku_id
    - warehouse_id

  change_timestamp:
    column: updated_at

  tracked_columns:
    - quantity_on_hand
    - reorder_level
    - status

  batch_size: 500
```

### Pydantic Contract Models (`src.scd2_copilot.source.models`)

- **`PostgresSourceDefinition`**:
  - `type`: `Literal["postgresql"]` (default `"postgresql"`)
  - `schema_name`: Valid SQL identifier (aliased as `schema`, default `"public"`)
  - `table_name`: Valid SQL identifier (aliased as `table`)
- **`ChangeTimestampDefinition`**:
  - `column`: Valid SQL identifier representing the monotonic event/update timestamp
- **`MonitorConfig`**:
  - `name`: Monitor identifier string
  - `source`: `PostgresSourceDefinition`
  - `business_keys`: Non-empty list of valid SQL identifiers (aliased as `keys`)
  - `change_timestamp`: `ChangeTimestampDefinition`
  - `tracked_columns`: Non-empty list of valid SQL identifiers
  - `batch_size`: Integer between 1 and 50,000 (default 500)

---

## 3. Structural Validation Rules

The configuration schema enforces strict validation before any database connection or execution occurs:

1. **SQL Identifier Format**:
   All schema names, table names, and column identifiers must match:
   `^[a-zA-Z_][a-zA-Z0-9_]{0,62}$`
   Preventing SQL injection and syntax ambiguity.
2. **Disjoint Sets**:
   Business keys and tracked columns must be completely disjoint:
   `set(business_keys) & set(tracked_columns) == ∅`
3. **Change Timestamp Independence**:
   The change timestamp column cannot be included in `business_keys` or `tracked_columns`.
4. **Reserved SCD2 Columns Forbidden**:
   The reserved metadata column names (`effective_from`, `effective_to`, `is_current`) are strictly forbidden across `business_keys`, `tracked_columns`, and `change_timestamp`.
5. **No Column Duplication**:
   Columns cannot appear multiple times within `business_keys` or `tracked_columns`.

---

## 4. PostgreSQL Source Adapter Contract (`PostgresSourceAdapter`)

The `PostgresSourceAdapter` mediates between configuration and the physical database.

### Responsibilities
1. **Schema Discovery (`discover_schema`)**:
   - Queries `information_schema.columns` for data types, nullability, and ordinal positioning.
   - Queries `information_schema.table_constraints` to discover primary key definitions.
2. **Configuration Validation (`validate_configuration`)**:
   - Tests live database connectivity.
   - Verifies target schema existence.
   - Verifies target table existence.
   - Verifies all `business_keys` exist and warns/errors if nullable.
   - Verifies `change_timestamp` exists and is a valid temporal type (`timestamp`, `timestamptz`, `date`).
   - Verifies all `tracked_columns` exist.
   - Produces a structured `SourceValidationResult`.
3. **Deterministic Snapshot & Incremental Extraction**:
   - All queries use strictly parameterized SQL.
   - Deterministic ordering: `ORDER BY <change_timestamp> ASC, <key_1> ASC, <key_2> ASC...`
   - Initial snapshot extraction: `read_initial_snapshot(limit)`
   - Incremental batch extraction: `read_incremental_records(after_timestamp, limit)`

### Immutability & Safety
- **Zero Source Mutations**: `PostgresSourceAdapter` never executes `INSERT`, `UPDATE`, `DELETE`, `DROP`, or `ALTER` on the source table. The source is strictly read-only.
- **Connection Borrowing**: Borrows connections from the managed `DatabaseManager` pool without connection leaks.

---

## 5. Runtime Data Flow

```text
PostgreSQL Source Table
    │
    ▼ (PostgresSourceAdapter.read_incremental_records)
NormalizedSourceRecord / dict records
    │
    ▼ (MicroBatch)
Source Polars DataFrame (key_columns + tracked_columns)
    │
    ▼ (IngestionWorker)
SCD2 Transformation Engine (detect_changes + apply_scd2)
    │
    ▼
Validation Engine (validate_scd2)
    │
    ├─ NORMAL ───► Atomic Target DB Commit (history + watermark)
    │
    └─ SUSPICIOUS ─► Containment Hold (processing_run + held_change_batch)
                     AI Operational Explanation
```

The deterministic SCD2 engine (`detect_changes.py`, `transform_scd2.py`, `validate.py`) remains 100% agnostic to domain types, operating entirely on dynamically projected Polars DataFrames defined by `MonitorConfig`.

---

## 6. Composite Cursor Contract (`SourceCursor`)

Introduced in **V3 Phase 2**, incremental stream tracking uses a deterministic composite cursor `(change_timestamp, business_key_tuple)` rather than timestamp-alone polling.

### Model Definition (`src.scd2_copilot.source.models.SourceCursor`)
- `timestamp`: UTC-normalized event timestamp (`datetime`).
- `keys`: Mapping of `{key_column: value}` for all business key columns.
- `key_columns`: Ordered list of key column names defining lexicographical sort precedence.

### SQL Row-Value Query Semantics
When polling incrementally from cursor `(T, K)`:
- For single-column business keys:
  ```sql
  WHERE ({ts} > %s) OR ({ts} = %s AND {key} > %s)
  ```
- For composite business keys:
  ```sql
  WHERE ({ts} > %s) OR ({ts} = %s AND ({k1}, {k2}) > (%s, %s))
  ```
- Guaranteed deterministic order:
  ```sql
  ORDER BY {ts} ASC, {k1} ASC, {k2} ASC... LIMIT {batch_size}
  ```

### Invariants
1. **No Skipped or Duplicated Records**: Even when 10,000 records share the exact same millisecond timestamp $T$, pagination across micro-batches deterministically picks up at $K_{last} + 1$.
2. **Batch Boundaries**: Every `MicroBatch` derives `first_cursor` and `last_cursor` from the ordered record slice.
3. **Checkpoint Monotonicity**: `processing_checkpoint` persists both `watermark_value` and `cursor_keys` (as `JSONB`). The checkpoint advances if and only if the batch successfully commits (`COMMITTED`). On `HELD` or error, the checkpoint is strictly preserved.
4. **Mutual Exclusion**: Concurrency is governed by PostgreSQL advisory locks:
   ```sql
   SELECT pg_try_advisory_lock(hashtext('scd2_worker_' || %s))
   ```
   preventing parallel workers from processing overlapping stream slices.

---

## 7. Generic Guardrail Evidence Model (`GuardrailEvidence`)

Introduced in **V3 Phase 3**, the guardrail evidence model is now fully domain-agnostic.

### Evidence Fields

#### Change Classification (domain-agnostic)
| Field | Type | Description |
| :--- | :--- | :--- |
| `evaluated_records` | `int` | Total source records in micro-batch |
| `changed_records` | `int` | SCD2 CHANGED record count |
| `new_records` | `int` | SCD2 NEW record count |
| `unchanged_records` | `int` | SCD2 UNCHANGED record count |
| `affected_population_ratio` | `float` | `(changed + new) / evaluated` |
| `event_window_seconds` | `float` | Time span of event timestamps in batch |
| `velocity_changes_per_second` | `float` | Changed records per event second |
| `validation_passed` | `bool` | SCD2 invariant validation outcome |
| `validation_failures` | `list[str]` | Validation failure messages if any |

#### Generic Key Dispersion (V3 Phase 3)
| Field | Type | Description |
| :--- | :--- | :--- |
| `key_value_dispersion` | `int` | Distinct values of the highest-cardinality business key column across changed + new records |
| `dispersion_key_column` | `Optional[str]` | Which business key column was selected as highest-cardinality |

The `WIDE_GEOGRAPHIC_IMPACT` rule threshold is applied against `key_value_dispersion`. For the inventory demo, when `warehouse_id` is the highest-cardinality key, this equals `warehouses_affected`.

#### Generic Numeric Analysis (V3 Phase 3)
| Field | Type | Description |
| :--- | :--- | :--- |
| `max_quantity_relative_change` | `float` | Worst-case relative change across ALL numeric-compatible tracked columns |
| `max_quantity_absolute_change` | `int` | Worst-case absolute change across all numeric columns |
| `total_quantity_absolute_change` | `int` | Cumulative absolute numeric change across all records and columns |
| `numeric_column_analyzed` | `Optional[str]` | Which tracked column produced the worst-case relative change |

The `LARGE_QUANTITY_SWING` rule threshold is applied against `max_quantity_relative_change` and `max_quantity_absolute_change`. Any numeric-convertible tracked column feeds these metrics, not just `quantity_on_hand`.

#### Generic Categorical Analysis (V3 Phase 3)
| Field | Type | Description |
| :--- | :--- | :--- |
| `status_deactivations_count` | `int` | Count of `ACTIVE → INACTIVE/DISCONTINUED/…` transitions in the `status` column (inventory-compat) |
| `categorical_column_analyzed` | `Optional[str]` | Column with the most categorical transitions across all changed records |

The `MASS_DEACTIVATION` rule remains tied to `status_deactivations_count` to preserve backward compatibility with existing held batch evidence in PostgreSQL.  The `categorical_column_analyzed` field records which non-numeric column saw the most transitions, for use in AI explanations and future rules.

#### Generic Per-Column Counts (V3 Phase 3)
| Field | Type | Description |
| :--- | :--- | :--- |
| `per_column_change_counts` | `dict[str, int]` | Count of `FieldChange` records keyed by column name across all changed records |

#### Legacy Inventory-Compat Fields (preserved for backward compatibility)
| Field | Type | Description |
| :--- | :--- | :--- |
| `warehouses_affected` | `int` | Distinct `warehouse_id` values — populated only when `warehouse_id` is a configured key |
| `skus_affected` | `int` | Distinct `sku_id` values — populated only when `sku_id` is a configured key |

These fields are preserved in `to_dict()` output for backward compatibility with existing `held_change_batch.evidence` JSONB already stored in PostgreSQL.

### Velocity Timestamp Source

`velocity_changes_per_second` is derived from timestamps read via `batch.timestamp_column`, not a hardcoded `updated_at` attribute. This makes the guardrail correct for monitors where the change timestamp column has any name (e.g., `last_modified`, `event_time`).

### Domain Isolation Contract

> Inventory-specific field names (`sku_id`, `warehouse_id`, `quantity_on_hand`, `status`, `reorder_level`) are **not required** by the generic guardrail core. They appear only in the demo configuration and in backward-compat legacy evidence fields. All rule evaluation logic reads from generic evidence fields derived from `MicroBatch.key_columns`, `MicroBatch.tracked_columns`, and `MicroBatch.timestamp_column`.
