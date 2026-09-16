# SCD2 Copilot V2 — Real-Time Upgrade Plan

## Product goal

Evolve SCD2 Copilot into a live data-change guardrail:

> Continuously observe operational business-data changes, reconstruct before/after state, identify abnormal/significant state-transition patterns, and hold suspicious changes before downstream propagation. AI explains deterministic evidence.

This is NOT:
- a new database
- a generic data-observability replacement
- a generic chatbot
- an autonomous AI data-integrity authority

## V2 architecture

```text
PostgreSQL / Supabase source
        |
        v
Change ingestion worker
        |
        v
Micro-batch
        |
        v
Existing Polars SCD2 engine
        |
        v
Contract validation
        |
        v
SCD2 validation
        |
        v
State-transition significance
        |
   +----+----+
   |         |
NORMAL   SUSPICIOUS
   |         |
COMMIT      HOLD
             |
             v
       AI explanation
             |
             v
      PostgreSQL state
             |
       +-----+------+
       |            |
    FastAPI      Streamlit
                     |
                     v
                Live monitor
```

## First domain
Inventory/warehouse data for the live demonstration.

Customer data remains a regression/demo domain.

## First ingestion approach
PostgreSQL watermark polling.

Initial target interval: configurable 15–30 seconds.
Do not claim 15 seconds is universally optimal.

## Future ingestion
Evaluate PostgreSQL logical decoding/CDC only after the first live workflow is proven.

## Persistence
PostgreSQL becomes the authoritative operational state.
Parquet/JSON remain useful for exports and analytical run artifacts.

## Processing
The existing Polars SCD2 engine remains the reusable deterministic core.
Use micro-batching rather than treating every live event as a one-row batch.

## Transaction boundary
A successful processing cycle should atomically:
1. read required active state,
2. compute transitions,
3. validate,
4. close old versions,
5. insert new versions,
6. update checkpoint,
7. commit.

Failure must roll back the transaction.

## Live containment
The source database is not directly blocked or modified by the guardrail.
Suspicious batches are held at the downstream processing/publication boundary.

## AI boundary
The deterministic engine and validators decide state/correctness.
AI only explains structured evidence.

## V1 compatibility
Batch/CSV mode remains supported.
V2 live mode is an additional input path into the same core.
