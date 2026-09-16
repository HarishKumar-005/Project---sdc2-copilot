# ADR-001 — Initial Live Ingestion Strategy

## Decision
Use PostgreSQL watermark polling as the first live ingestion implementation.

## Context
The V1 engine is batch-oriented and highly reusable. The project needs a live path without introducing unnecessary distributed infrastructure.

## Rationale
- Small implementation surface
- Reuses existing Polars batch engine
- Easy to test and debug
- Works with hosted PostgreSQL
- Makes micro-batching explicit
- Avoids prematurely coupling to Kafka/Debezium/Flink

## Alternatives considered
- PostgreSQL logical decoding/CDC
- Debezium/Kafka
- Webhooks

## Revisit conditions
Use CDC/logical decoding when measured requirements show that polling latency, source load, or change semantics are insufficient.
