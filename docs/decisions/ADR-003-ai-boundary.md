# ADR-003 — Deterministic SCD2 + Evidence-Grounded AI

## Decision
The deterministic engine remains authoritative for state and temporal correctness.

AI can:
- summarize structured evidence,
- explain significance,
- produce human-readable narratives.

AI cannot:
- classify NEW/CHANGED/UNCHANGED/DELETED,
- set SCD2 dates,
- decide database correctness,
- directly mutate authoritative state.
