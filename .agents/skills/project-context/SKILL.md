---
name: project-context
description: Establish and preserve the verified V1/V2 project context before changing SCD2 Copilot.
---

# Project Context Skill

Always read:
- docs/03_current_state.md
- docs/07_v2_master_context.md
- docs/04_realtime_upgrade_plan.md

Rules:
- Treat repository code as the source of truth.
- Do not assume a documented feature is implemented without verification.
- Preserve V1 compatibility.
- Treat customer data as fixtures/demo data unless code proves otherwise.
- Do not redesign the core SCD2 engine without explicit evidence.
- Separate FACT, INFERENCE, and UNKNOWN in reports.
