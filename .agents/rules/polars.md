# SCD2 Copilot — Polars Performance Rule

## Primary principle

Use Polars as a columnar data-processing engine, not as a wrapper around Python row loops.

Prefer:

- joins
- expressions
- `when/then/otherwise`
- projections
- aggregations
- window expressions
- `LazyFrame` where justified
- scan APIs where appropriate
- streaming execution where appropriate and measured

Avoid in the hot path:

- `iter_rows()`
- converting entire frames into Python dictionaries
- per-row Python loops
- Python object materialization for large datasets

## Correctness before vectorization

Do not replace working logic with a vectorized implementation until the existing semantics are captured by tests.

Use the old implementation as a behavioral reference while the new implementation is being verified.

## Joins and key assumptions

Make key cardinality assumptions explicit.

Duplicate business keys must be detected and handled deliberately.
Never rely on dictionary overwrite behavior.

## Lazy execution

Do not use `LazyFrame` merely for appearance.

Adopt lazy/streaming execution where it provides an actual benefit for the workload and can be demonstrated with measurements.

## Performance claims

Only report measured results.

At minimum benchmark:

- 10K
- 100K
- 500K
- 1M

and report:

- runtime
- throughput
- memory observation
- environment

The goal is a measured improvement, not a predetermined number.
