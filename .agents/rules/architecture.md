# SCD2 Copilot — Architecture Rule

## Core separation

The system has two fundamentally different concerns:

1. deterministic data engineering
2. probabilistic language generation

Never merge them.

### Deterministic layer owns

- ingestion
- normalization
- business-key validation
- duplicate detection
- schema validation
- change classification
- SCD2 transformation
- temporal semantics
- validation gates
- snapshot identity/idempotency
- persistence decisions
- change metrics

### AI layer owns

- natural-language explanation
- run summaries
- interpretation of deterministic statistics
- presentation-oriented insights

AI output must never be treated as a source of truth for SCD2 correctness.

## Layering target

Preferred direction:

```text
Presentation
  ↓
API / application service
  ↓
workflow orchestration
  ↓
deterministic data engine
  ↓
validation
  ↓
persistence / artifacts
  ↓
analytics
  ↓
AI explanation
```

Avoid circular dependencies between layers.

## Refactoring rule

Do not perform a whole-system rewrite when a local refactor can solve the problem.

Preserve public behavior unless the task explicitly changes the contract.

When introducing a new layer (FastAPI, PostgreSQL, Prefect integration, etc.), migrate responsibility incrementally and keep old behavior testable until the cutover is verified.

## Data ownership

The deterministic engine owns truth.

PostgreSQL, when introduced, should primarily provide durable operational/control/audit state unless there is a justified relational requirement for storing the dynamic SCD2 dataset itself.

Do not force arbitrary user-defined dimension schemas into an unnecessarily rigid relational model.
