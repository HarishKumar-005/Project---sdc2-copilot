# SCD2 Copilot — Agent Development Contract

## 1. Project identity

**Project:** SCD2 Copilot — AI-Assisted Data Change & Historical Analytics

This is an existing, functional CSV-based batch SCD Type 2 application.
Do NOT treat it as a greenfield project and do NOT redesign it from scratch.

The intended evolution is from a strong prototype into a production-oriented
**Data Change Intelligence Platform**.

Core principle:

> **The engine decides. Validation protects. AI explains.**

## 2. Current architecture — supplied baseline

The current application is documented as:

```text
Source CSV + existing SCD2 CSV
    ↓
ingestion / normalization
    ↓
schema + business-key detection
    ↓
deterministic change detection
    ↓
SCD2 transformation
    ↓
validation
    ↓
structured GenAI explanation
    ↓
Streamlit dashboard / reports / downloads
```

The repository audit reports these currently implemented capabilities:

- CSV ingestion and normalization with Polars.
- Heuristic business-key detection.
- Deterministic NEW / CHANGED / UNCHANGED / DELETED classification.
- SCD2 historical preservation and current-row creation/closure.
- Five-rule SCD2 invariant validation.
- Batched structured GenAI explanations.
- Gemini primary → Groq fallback → deterministic template fallback.
- Pydantic validation for structured AI output.
- Token, latency, provider, cost-estimate, and fallback tracking.
- An existing test suite reported at 178 passing tests.

**Important:** this file is an agent contract derived from the supplied project audit and roadmap.
Before making changes, verify repository reality. Never assume this file is more authoritative than the code,
tests, dependency files, and actual runtime behavior.

## 3. Technology constraints

Current stack:

- Python
- Polars
- Prefect 3
- Pydantic
- pydantic-settings
- Gemini / google-genai
- Groq
- Streamlit
- Pytest

Do NOT introduce technology merely for resume value.

In particular, do NOT add:

- DuckDB
- Great Expectations
- LangChain
- LangGraph
- vector databases
- RAG
- generic multi-agent application architecture
- MCP

unless a concrete engineering requirement is demonstrated and the change is explicitly justified.

Do not replace the existing stack wholesale.

## 4. Non-negotiable architecture rules

### Deterministic correctness

SCD2 correctness must remain deterministic.

The following must NOT depend on an LLM:

- business-key matching
- NEW / CHANGED / UNCHANGED / DELETED classification
- SCD2 version creation
- effective-date assignment
- validation gates
- persistence commit decisions
- idempotency decisions

### AI boundary

The LLM only explains validated evidence.

Preferred path:

```text
raw input
  ↓
deterministic processing
  ↓
validation
  ↓
structured evidence / aggregates
  ↓
GenAI explanation
```

Never:

```text
raw input
  ↓
LLM decides SCD2 state
```

### Preserve semantics

When refactoring:

1. Establish expected behavior from existing tests.
2. Add regression tests for discovered bugs.
3. Change implementation.
4. Re-run relevant tests.
5. Compare results against the reference behavior.

Do not alter business semantics merely to make code look more modern.

## 5. Current known gaps

The repository audit identified the following areas. Verify each against the current repository before acting:

1. Prefect workflow exists but is bypassed by Streamlit.
2. Core SCD2 processing uses row/dictionary/loop-oriented processing.
3. Duplicate business keys can be silently overwritten.
4. ISO datetime handling can silently coerce valid timestamps to NULL.
5. Temporal boundary semantics are ambiguous.
6. The UI "Confidence Assessment" is conceptually invalid.
7. Gemini model identifiers include stale/non-current hard-coded entries.
8. Run history is largely Streamlit session state.
9. No FastAPI/headless API.
10. No PostgreSQL persistence.
11. No idempotency for repeated snapshots.
12. No quarantine workflow for invalid records.
13. No Type 1 vs Type 2 column configuration.
14. No explicit schema-evolution policy.
15. No production-grade authentication/resource controls/export sanitization.
16. No Docker/CI/CD.
17. Benchmarks are mixed into the normal pytest path.

These are a prioritized work queue, not permission to fix everything in one change.

## 6. Roadmap

The locked milestone order is:

### M0 — Baseline & Agent Foundation
- Freeze current behavior.
- Establish reproducible test and performance baselines.
- Create/maintain agent instructions and skills.
- Separate benchmarks from routine tests.

### M1 — Trustworthy Core
- Duplicate-key protection.
- Correct datetime parsing.
- Explicit temporal semantics.
- Explicit full-vs-incremental snapshot semantics.
- Real delete policy behavior.
- Remove fake confidence score.
- Formalize SCD2 invariants.
- Strengthen regression tests.

### M2 — High Performance
- Replace hot-path row iteration with native Polars operations.
- Use joins/expressions/window operations where justified.
- Use LazyFrame/scan/streaming only where they provide measurable value.
- Benchmark 10K / 100K / 500K / 1M scale.
- Measure runtime, memory, and throughput.

### M3 — Real Automation
- Make Prefect the actual workflow boundary.
- Introduce deployment configuration.
- Add retries only for transient failures.
- Add concurrency controls where justified.

### M4 — Real Product
- FastAPI API boundary.
- PostgreSQL control/audit persistence.
- SQLAlchemy/Alembic where appropriate.
- Snapshot identity and idempotency.
- Transactional persistence.
- Durable run/history state.
- Proper persisted surrogate identity where the relational model needs it.

### M5 — Intelligence
- First-class change analytics.
- Run-level Change Intelligence.
- Record-level explanations.
- Grounded structured GenAI.
- AI evaluation.
- Type 1 / Type 2 column configuration.
- Schema-evolution policy.

### M6 — Production Hardening
- Quarantine.
- Authentication.
- Upload/resource limits.
- Export sanitization.
- Structured observability.
- Docker.
- GitHub Actions CI/CD.
- Resilience and operational controls.

Do not skip ahead simply because a later feature is more visually impressive.

## 7. SCD2 semantic contract

Unless repository evidence or an approved design decision changes this:

### Temporal convention

Use half-open validity intervals:

```text
[effective_from, effective_to)
```

Example:

```text
Version A: [2026-06-01, 2026-06-08)
Version B: [2026-06-08, ∞)
```

The same instant must not belong to two versions.

### Full snapshot deletion semantics

A missing target key may be classified as DELETED only when the input is explicitly treated as a complete snapshot.

For incremental input, absence means "not provided", not necessarily "deleted".

### Core invariants

For each business key:

- at most one current version
- historical versions remain immutable
- no overlapping validity intervals
- effective_from is before effective_to when effective_to exists
- CHANGED closes the previous version and creates a new current version
- UNCHANGED creates no new version
- NEW creates a current version
- DELETED preserves history and closes the prior current version when delete policy allows it

## 8. Configuration principles

Prefer typed configuration with Pydantic for pipeline behavior.

Expected configuration areas include:

- business keys
- Type 1 columns
- Type 2 columns
- snapshot mode
- delete policy
- schema policy
- timezone/temporal semantics
- AI provider/model configuration
- resource limits

Do not hard-code environment-specific secrets or model identifiers in source.

## 9. Testing contract

Before changing behavior:

- inspect existing tests
- add regression tests for every confirmed bug
- preserve current passing tests where behavior is intended to remain unchanged
- run focused tests first
- then run the full routine suite

Performance benchmarks must be separate from routine correctness tests.

Normal tests should not require external LLM connectivity unless an integration test explicitly opts into it.

## 10. Benchmark contract

Benchmarks are evidence, not decoration.

Never claim throughput, memory, speedups, or scalability numbers that were not measured.

At minimum capture:

- dataset size
- runtime
- throughput
- peak/observed memory where practical
- machine/environment details sufficient to reproduce the result
- implementation version/commit

## 11. Safe agent operating procedure

For non-trivial changes:

```text
EXPLORE
  ↓
PLAN
  ↓
REVIEW
  ↓
IMPLEMENT
  ↓
TEST
  ↓
INSPECT DIFF
  ↓
REPORT
```

Do not make broad architectural changes without first producing a plan.

Before implementing a requested change, identify:

- current behavior
- affected files
- invariants
- compatibility risks
- tests that must change
- measurable success criteria

## 12. Scope discipline

Do not:

- rewrite the repository because a cleaner architecture is possible
- rename unrelated files during feature work
- change dependencies without a reason
- silently alter external behavior
- remove tests because they are inconvenient
- hide failures by weakening assertions
- claim production readiness before the relevant milestone is actually complete

## 13. Definition of done

A code change is not complete merely because it compiles.

A meaningful change should leave behind:

- implementation
- appropriate tests
- updated documentation/configuration
- verification output
- a concise summary of measurable results
- no unrelated modifications

## 14. Repository truth hierarchy

When sources disagree, prefer:

1. Actual running code
2. Existing tests and fixtures
3. Dependency/configuration files
4. Runtime/build output
5. Project documentation
6. This agent-context document

Treat this document as guidance, not as proof.
