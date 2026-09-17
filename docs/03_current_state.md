# SCD2 Copilot — Current System State (V3)

## Purpose
This document captures the verified V3 system state after completing all five V3 phases.

## Product Identity

**SCD2 Copilot — Data Change Guardrail**

> A configurable PostgreSQL data-change guardrail that incrementally processes operational table changes, reconstructs historical state using a deterministic SCD2 engine, detects suspicious change patterns using deterministic rules, holds suspicious batches before historical propagation, preserves evidence, and uses GenAI to explain the evidence to an operator.

## Current Input

- **Primary (V3):** Configured PostgreSQL operational tables monitored via `MonitorConfig` with incremental watermark-based polling
- **Legacy (V1):** Batch CSV or Polars DataFrame upload (fully preserved)

## Core Behavior

- NEW / CHANGED / UNCHANGED / DELETED classification
- FULL and INCREMENTAL snapshot modes
- SOFT_DELETE and IGNORE delete policies
- Half-open temporal intervals: `[effective_from, effective_to)`
- Deterministic SCD2 transformation and validation
- Configurable PostgreSQL source (arbitrary table, keys, tracked columns)
- Deterministic composite cursor `(timestamp, business_key)` for incremental polling
- PostgreSQL advisory lock concurrency guard

## Guardrail & Containment

- 7 deterministic guardrail rules (HIGH_CHANGE_VOLUME, HIGH_POPULATION_IMPACT, LARGE_QUANTITY_SWING, MASS_DEACTIVATION, HIGH_CHANGE_VELOCITY, WIDE_GEOGRAPHIC_IMPACT, SCD2_VALIDATION_FAILURE)
- Domain-agnostic evidence extraction (generic numeric, categorical, key dispersion)
- NORMAL → COMMIT → checkpoint advance
- SUSPICIOUS → HOLD → evidence preserved → checkpoint preserved
- Operator recovery: RELEASE / REPROCESS / DISCARD (authenticated, idempotent)

## AI Explanation

- Gemini → Groq → Deterministic Template fallback chain
- Evidence-grounded: AI explains validated evidence, never decides SCD2 state
- Grounding validator detects hallucinated rules, contradicted status, modified decisions
- 100% offline functionality via DeterministicExplanationProvider

## API & UI

- FastAPI headless operational boundary with Supabase Auth JWT verification
- Streamlit dual-mode interface:
  - ⚡ Live Guardrail Monitor (V2): Containment Queue, Processing Runs, SCD2 History Explorer, Source Inventory, Source Configuration
  - 📁 Batch CSV Analysis (V1): Preserved offline batch execution
- Monitor-aware history: dynamic monitor selector with structured key inputs
- Generic entity history API: `GET /history/{source_name}/entity?key=<json>`

## Operational Persistence

- `inventory_source` — Canonical inventory demo source
- `inventory_history` — Canonical inventory SCD2 target
- `monitored_entity_history` — Generic SCD2 target (any configured monitor)
- `processing_checkpoint` — Watermark state with cursor_keys JSONB
- `processing_run` — Run audit log
- `held_change_batch` — Containment queue with frozen evidence

## Engineering

- Polars vectorized change detection and SCD2 transformation
- Pydantic typed configuration and data contracts
- Prefect 3 orchestration (available, bypassed by direct worker)
- SHA-256 execution fingerprint / batch deduplication
- Google OIDC + Supabase Auth authentication
- Docker packaging
- Extensive automated test suite (737 routine tests passing)

## Domain Coupling

The core engine is domain-agnostic. Warehouse inventory is a **demo domain** exercised through the canonical `inventory` monitor configuration. The same engine processes arbitrary PostgreSQL tables via `MonitorConfig`.

## Verified Limitations

See [limitations.md](./limitations.md) for the complete verified limitations list.

## Baseline Rules

- The engine decides. Validation protects. AI explains.
- Do not rewrite the deterministic SCD2 core unless a verified requirement forces it.
- Do not make AI authoritative for SCD2 correctness.
- Do not claim capabilities that are not verified.
