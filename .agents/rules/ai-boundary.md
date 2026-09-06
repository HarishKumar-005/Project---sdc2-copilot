# SCD2 Copilot — AI Boundary Rule

## AI never decides business truth

Never ask an LLM to determine:

- whether a record is NEW
- whether a record is CHANGED
- whether a record is UNCHANGED
- whether a record is DELETED
- whether a temporal interval is valid
- whether a pipeline should commit
- whether a snapshot is a duplicate

Those are deterministic responsibilities.

## AI input contract

Prefer:

```text
raw data
→ deterministic diff
→ validation
→ structured evidence
→ AI
```

AI should receive only the minimum evidence required for the explanation task.

Do not send entire raw snapshots when aggregated/filtered evidence is sufficient.

## AI output contract

All structured AI responses must be validated against a typed schema.

Invalid output must be rejected or replaced by a deterministic fallback.

Never silently treat malformed AI output as authoritative.

## Groundedness

Whenever feasible, generated statements should be traceable to deterministic evidence such as:

- change counts
- changed attributes
- known entity keys
- before/after values
- validation results
- run-to-run metrics

Do not invent causes that the pipeline has not measured.

Avoid unsupported causal language such as "because" unless the data actually establishes causality.

## Provider resilience

Maintain the current conceptual fallback:

```text
Gemini
  ↓
Groq
  ↓
deterministic template
```

Provider/model selection should be configuration-driven and current.

AI availability must never affect the SCD2 correctness score.
