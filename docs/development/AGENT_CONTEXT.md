# Agent Context — Supplied Project Baseline

This file records the project context supplied for initial Antigravity setup.

## Important

This is NOT a repository scan.

The local repository remains the source of truth.

At the beginning of work, the agent should run the `repository-audit` skill and verify every claim against the actual codebase.

## Verified baseline from supplied audit

The project is described as an existing CSV-based batch SCD2 application with:

- Python
- Polars
- Prefect 3
- Pydantic
- pydantic-settings
- Gemini / google-genai
- Groq
- Streamlit
- Pytest

The reported baseline was 178 passing tests.

## Reported current capabilities

- CSV ingestion/normalization
- heuristic business-key detection
- deterministic NEW / CHANGED / UNCHANGED / DELETED classification
- SCD2 transformation
- five-rule validation
- structured batched GenAI explanations
- Gemini → Groq → deterministic template fallback
- Pydantic structured AI output
- token/latency/provider/cost/fallback tracking

## Reported issues to verify

- Prefect flow bypassed by Streamlit
- row/dictionary/loop-heavy SCD2 implementation
- duplicate-key silent overwrite risk
- ISO datetime null-coercion bug
- ambiguous temporal boundaries
- invalid confidence score
- stale Gemini model IDs
- ephemeral run history
- missing API layer
- missing persistent database
- missing idempotency
- missing quarantine
- missing Type 1/Type 2 configuration
- missing schema evolution policy
- missing security/resource controls
- missing Docker/CI/CD
- benchmark pollution of routine tests

## Locked product direction

Target:

```text
Data Source
→ ingestion
→ quality/schema gate
→ snapshot identity/idempotency
→ high-performance Polars change detection
→ deterministic SCD2
→ validation gate
→ persistent storage/audit
→ analytics
→ structured GenAI interpretation
→ API/dashboard
→ observability
→ deployment/CI/CD/security
```

Product principle:

> The engine decides. Validation protects. AI explains.

## Technology anti-goals

Do not introduce a framework merely to appear more advanced.

Specifically, do not add:

- DuckDB
- LangChain
- LangGraph
- vector DB
- RAG
- MCP
- generic multi-agent architecture

unless a real requirement emerges.

## Roadmap

M0 Baseline & Agent Foundation
→ M1 Trustworthy Core
→ M2 High Performance
→ M3 Real Automation
→ M4 Real Product
→ M5 Intelligence
→ M6 Production Hardening
