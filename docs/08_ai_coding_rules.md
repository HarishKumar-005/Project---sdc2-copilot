# AI Coding Rules — SCD2 Copilot V2

Before changing code:

1. Read AGENTS.md.
2. Read docs/03_current_state.md.
3. Read docs/07_v2_master_context.md.
4. Read docs/04_realtime_upgrade_plan.md.
5. Read the applicable skill in .agents/skills/.
6. Inspect existing code and tests.
7. Produce a short plan and identify files to change.
8. Do not modify unrelated modules.

Preservation rules:
- Preserve V1 batch behavior.
- Preserve customer regression fixtures.
- Preserve [effective_from, effective_to) semantics.
- Preserve deterministic SCD2 decisions.
- Preserve AI fallback behavior.
- Preserve existing tests unless a behavior change is explicitly approved.

Implementation rules:
- Prefer the smallest change satisfying the current milestone.
- Add migrations for database schema changes.
- Add tests for every new persistence/worker behavior.
- Never hardcode production secrets.
- Keep source DB credentials server-side.
- Never expose service-role credentials to browser/UI code.
- Treat all source data as untrusted input.
- Do not add heavyweight infrastructure without an architecture decision.

After implementation:
- run targeted tests,
- run relevant integration tests,
- run full routine suite when practical,
- report failures honestly,
- update docs/active sprint only after the milestone is actually verified.
