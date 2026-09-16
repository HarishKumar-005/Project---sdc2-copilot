---
name: api
description: Headless FastAPI boundary for the live SCD2 Copilot service.
---

# API Skill

FastAPI is a boundary around processing/state, not a replacement for the core engine.

Rules:
- Keep endpoints small and resource-oriented.
- Validate request schemas with Pydantic.
- Never expose service-role/database secrets.
- Separate UI concerns from processing concerns.
- Add health/readiness endpoints.
- Keep synchronous long-running work out of request handlers when a worker is appropriate.
- Do not introduce FastAPI until the worker/state architecture is ready.
