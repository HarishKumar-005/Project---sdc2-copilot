---
name: testing
description: Regression, integration, replay, recovery and real-time testing discipline for SCD2 Copilot V2.
---

# Testing Skill

Every V2 feature must include tests.

Required categories over time:
- unit
- integration
- migration
- transaction rollback
- concurrency
- checkpoint/restart
- duplicate/replay
- ordering/late-arrival
- live worker
- API
- UI
- AI fallback
- performance

Rules:
- Preserve V1 customer regression tests.
- Do not delete tests to make the suite pass.
- Separate benchmark gates from routine CI.
- Report actual counts and failures.
