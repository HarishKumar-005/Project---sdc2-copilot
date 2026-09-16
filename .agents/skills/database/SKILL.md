---
name: database
description: PostgreSQL schema, transactional state, concurrency, checkpoints and SCD2 persistence rules.
---

# Database Skill

Focus on:
- PostgreSQL schema design
- constraints
- indexes
- transactions
- locking
- checkpoints
- idempotency
- SCD2 history/current state

Rules:
- Preserve [effective_from, effective_to).
- Enforce business-key uniqueness where appropriate.
- Design current-state access for indexed lookups.
- Make processing checkpoint updates part of a clear transaction boundary.
- Explicitly design replay/restart behavior.
- Never claim exactly-once delivery without proof.
- Handle concurrent processing intentionally.
- Keep analytical/export storage separate from authoritative operational state.
