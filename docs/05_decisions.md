# Architecture Decision Record Index

## ADR-001 — PostgreSQL as live operational database
Status: Accepted

Reason:
PostgreSQL provides strong transactional semantics and a native CDC foundation. It also fits the intended hosted demo path through Supabase.

## ADR-002 — Watermark polling as first live ingestion
Status: Accepted for V2.1

Reason:
Smallest credible real-time upgrade. Reuses the existing batch SCD2 engine and avoids premature streaming infrastructure.

Revisit when:
- latency requirements become stricter,
- volume requires log-based capture,
- polling overhead becomes material,
- or measured correctness requires source-log semantics.

## ADR-003 — SCD2 engine remains deterministic
Status: Accepted

AI is explanatory only.

## ADR-004 — PostgreSQL is authoritative operational state
Status: Accepted for V2

Parquet/JSON remain export/artifact formats.

## ADR-005 — Suspicious changes are held downstream, not blocked at source
Status: Accepted

Reason:
The guardrail should not become a source-database control plane. It protects downstream publication/processing boundaries.

## ADR-006 — Keep V1 batch mode
Status: Accepted

V2 live mode must enter the same reusable core without removing the existing CSV/DataFrame workflow.
