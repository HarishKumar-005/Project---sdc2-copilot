---
name: polars-performance
description: Profile and refactor the SCD2 hot path toward native Polars joins, expressions, lazy execution, and streaming where justified, with correctness-preserving benchmarks.
---

# Polars Performance Skill

## Goal

Replace avoidable Python row-oriented processing with native Polars execution while preserving exact SCD2 semantics.

## Phase 1 — profile

Identify:

- `iter_rows`
- `to_dicts`
- Python dictionaries built from full frames
- nested loops
- Python row construction
- repeated scans/conversions
- unnecessary eager materialization

Do not refactor before identifying the actual hot path.

## Phase 2 — capture reference behavior

Use existing tests and representative fixtures as the behavioral reference.

The optimized path must produce equivalent deterministic classifications and SCD2 state.

## Phase 3 — vectorize

Prefer:

- joins
- expressions
- conditional expressions
- projections
- aggregations
- window operations
- `LazyFrame`
- scan APIs
- streaming execution when justified

Keep small Python control flow for orchestration/configuration if it is not data-volume dependent.

## Phase 4 — verify

For representative datasets compare:

- row counts
- classifications
- key sets
- effective dates
- `is_current`
- output schema
- deterministic ordering where part of the contract

## Phase 5 — benchmark

Measure:

- 10K
- 100K
- 500K
- 1M

Capture:

- wall-clock runtime
- throughput
- memory observation
- Python/Polars versions
- machine/environment
- implementation version

Do not promise a target throughput before measurement.

## Acceptance rule

The hot path should no longer depend on Python row iteration for large datasets unless an explicit, documented exception exists.

## Output

Report:

1. baseline bottleneck
2. new Polars strategy
3. correctness verification
4. benchmark results
5. remaining bottlenecks
6. next optimization, if any
