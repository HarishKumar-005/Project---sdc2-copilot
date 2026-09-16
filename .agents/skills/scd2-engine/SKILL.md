---
name: scd2-engine
description: Protect the existing deterministic Polars SCD2 engine while adding new ingestion paths.
---

# SCD2 Engine Skill

The deterministic core is authoritative.

Preserve:
- NEW
- CHANGED
- UNCHANGED
- DELETED
- FULL
- INCREMENTAL
- SOFT_DELETE
- IGNORE
- half-open temporal semantics
- null-safe comparison
- business-key validation

Rules:
- Never let AI classify changes.
- Never let AI set effective dates.
- Prefer adapting inputs to the existing engine over rewriting engine internals.
- Every behavior change requires regression tests.
- Keep customer fixtures as regression assets.
