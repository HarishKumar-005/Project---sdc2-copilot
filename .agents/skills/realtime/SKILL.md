---
name: realtime
description: Live ingestion, watermarking, micro-batching, event lifecycle, replay and recovery for SCD2 Copilot.
---

# Realtime Skill

Initial model:
PostgreSQL source -> watermark worker -> micro-batch -> existing SCD2 engine.

Rules:
- Start with watermark polling.
- Keep the polling interval configurable.
- Do not assume a fixed latency is optimal.
- Persist checkpoints.
- Make restart/replay behavior explicit.
- Never silently lose events.
- Never silently duplicate SCD2 versions.
- Buffer events into micro-batches rather than one-row Polars executions.
- Distinguish source event capture from downstream processing state.
- Treat CDC/logical decoding as a later transport option, not the first implementation.

Each batch should have an observable lifecycle:
RECEIVED -> BUFFERED -> PROCESSING -> VALIDATED -> COMMITTED
or FAILED/HOLD.
