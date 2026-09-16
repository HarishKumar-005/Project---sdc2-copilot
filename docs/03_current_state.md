# SCD2 Copilot — Verified V1 Current State

## Purpose
This document freezes the verified V1 baseline before the real-time upgrade.

## Verified product today
SCD2 Copilot is a batch/snapshot-oriented, domain-agnostic SCD Type 2 data-change engine with a Streamlit UI.

## Current input
- Current/source CSV or Polars DataFrame
- Existing target SCD2 history CSV/DataFrame

## Core behavior
- NEW
- CHANGED
- UNCHANGED
- DELETED
- FULL and INCREMENTAL snapshot modes
- SOFT_DELETE and IGNORE delete policies
- Half-open temporal intervals: [effective_from, effective_to)
- Deterministic SCD2 transformation and validation

## Implemented engineering
- Polars vectorized change detection/transformation
- Data contracts and quarantine
- SHA-256 execution fingerprint/idempotency
- Prefect 3 orchestration
- Gemini → Groq → deterministic explanation fallback
- Structured Pydantic AI output
- Google OIDC authentication
- Parquet/CSV/JSON run artifacts
- Docker packaging
- Extensive automated tests

## Domain coupling
The core engine is domain-agnostic. Customer data is used primarily for examples, fixtures, and regression tests. Do not remove customer tests.

## Not yet implemented
- Continuous real-time ingestion
- PostgreSQL operational target
- Event/checkpoint based streaming semantics
- Concurrency/transactional live-state handling
- Live push UI
- Business-state significance engine
- Suspicious-change hold/containment
- Headless FastAPI boundary

## Important baseline rules
- Do not rewrite the deterministic SCD2 core unless a verified requirement forces it.
- Do not make AI authoritative for SCD2 correctness.
- Do not claim production-grade capabilities that are not verified.
- Do not reuse stale benchmark numbers without checking the benchmark definition.
