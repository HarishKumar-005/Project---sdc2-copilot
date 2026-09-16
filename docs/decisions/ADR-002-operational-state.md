# ADR-002 — PostgreSQL as Authoritative Operational State

## Decision
Use PostgreSQL for live SCD2 state, checkpoints and processing metadata.

## Reason
The live system needs transactional updates, concurrency control, indexed current-state lookups and durable checkpoints.

## Keep
Parquet and JSON for exports, reports and analytical artifacts.

## Rule
Do not treat local filesystem artifacts as authoritative live state.
