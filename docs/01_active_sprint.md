# Active Sprint — V2 Foundation

## Goal
Prepare and implement the smallest credible live operational architecture without breaking V1.

## Current phase
V2.0 — Architecture and environment setup.

## Ordered milestones

### V2.0
Freeze V1 and establish project governance/context.

### V2.1
Supabase/PostgreSQL foundation:
- local Supabase CLI stack
- remote Supabase project
- migrations
- seed data
- inventory source schema
- SCD2 target schema
- processing checkpoint schema

### V2.2
Transactional PostgreSQL SCD2 target.

### V2.3
Watermark worker:
- checkpoint
- polling
- micro-batch
- restart recovery

### V2.4
Live SCD2 processing.

### V2.5
Live Streamlit monitor.

### V2.6
Deterministic state-transition significance.

### V2.7
Suspicious-change hold/containment.

### V2.8
Evidence-grounded AI explanation.

### V2.9
FastAPI headless/query boundary.

### V2.10
Reliability/performance benchmarking.

## Rule
Only implement the current milestone unless explicitly instructed to move forward.
