# ADR-004 — Deterministic Change Significance & Guardrail Engine

## Decision
Use a deterministic, rule-based guardrail engine to evaluate the operational significance of incoming micro-batches as `NORMAL` or `SUSPICIOUS` with deterministic severity (`LOW`, `MEDIUM`, `HIGH`, `CRITICAL`), completely decoupled from LLMs and machine-learning models.

## Context
As SCD2 Copilot evolves into a real-time data change guardrail, it must evaluate whether detected mutations represent routine operations or significant anomalies (e.g. mass deactivations, sudden volume spikes, runaway updates) before they silently propagate downstream.

## Rationale
- **Deterministic Correctness**: The engine must never rely on non-deterministic LLM hallucinations or black-box ML models to decide whether data is suspicious.
- **Explainable Evidence**: Generates structured machine-readable evidence vectors (volume, population ratio, quantity deltas, status transitions, velocity) that can be consumed directly by evidence-grounded GenAI explanation layers in subsequent milestones.
- **Strict Boundary Separation**:
  - The engine classifies SCD2 state.
  - Validation protects temporal/uniqueness invariants.
  - The guardrail evaluates operational significance.
  - Transactions commit or contain (`HELD`).
  - AI explains.
- **Safety**: `SUSPICIOUS` batches do not mutate target history, do not advance stream checkpoints, and do not alter source data.

## Rules Implemented
1. `HIGH_CHANGE_VOLUME`: Threshold on mutated record counts.
2. `HIGH_POPULATION_IMPACT`: Proportion of evaluated batch population mutated (with batch size noise guard).
3. `LARGE_QUANTITY_SWING`: Relative quantity delta (> 300%) paired with minimum absolute noise threshold (>= 50 units).
4. `MASS_DEACTIVATION`: Categorical status transitions from `ACTIVE` to `INACTIVE`/`DISCONTINUED`.
5. `HIGH_CHANGE_VELOCITY`: Changes per event second across the micro-batch window.
6. `WIDE_GEOGRAPHIC_IMPACT`: Multi-warehouse dispersion across a single batch.
7. `SCD2_VALIDATION_FAILURE`: SCD2 invariant validation compliance.

## Precedence-Based Severity Derivation
- `CRITICAL`: SCD2 invariant validation failure (`SCD2_VALIDATION_FAILURE`).
- `HIGH`: Multiple triggered rules (>= 2) or `MASS_DEACTIVATION`.
- `MEDIUM`: Single significance rule triggered.
- `LOW`: Zero rules triggered (`NORMAL`).

## Alternatives Considered
- **LLM-based anomaly detection**: Rejected due to non-determinism, latency, token costs, and safety contract violations.
- **Statistical ML models (Isolation Forest / Autoencoders)**: Deferred; requires training state, drift monitoring, and lacks explicit explainable business guarantees.
