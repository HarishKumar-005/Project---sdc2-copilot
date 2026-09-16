# SCD2 Copilot V2 — Master Context

> PURPOSE: This document is the primary context contract for AI coding agents working on SCD2 Copilot V2.
> Read this document before planning or modifying V2.
> Repository code remains the ultimate source of truth; this document defines the intended architecture, boundaries, priorities, and non-negotiable rules.

---

# 1. PROJECT IDENTITY

Project name:

**SCD2 Copilot — AI-Assisted Data Change & Historical Analytics**

Current product:

A batch/snapshot-oriented, domain-agnostic SCD Type 2 data-change engine with a Streamlit application.

V2 product direction:

**SCD2 Copilot — Real-Time Data Change Guardrail**

Core product problem:

> Live operational data can change incorrectly even when the database and pipeline report success. SCD2 Copilot should continuously observe those changes, reconstruct the before/after business state, detect significant or suspicious state-transition patterns, and hold suspicious changes before they silently propagate downstream.

Important:

V2 is an evolution of the existing project, NOT a restart.

The existing V1 deterministic SCD2 engine is the foundation and must be reused.

---

# 2. CURRENT V1 — VERIFIED BASELINE

The current application is batch/snapshot oriented.

Input:

- Current/source CSV or Polars DataFrame
- Existing target SCD2 history CSV/DataFrame

Current pipeline:

```text
Current Source CSV/DataFrame
        +
Existing SCD2 Target History
        ↓
Ingestion / normalization
        ↓
Schema + business-key detection
        ↓
Data contract gate
        ↓
SHA-256 execution fingerprint
        ↓
Deterministic change detection
        ↓
SCD2 transformation
        ↓
Deterministic validation
        ↓
AI explanation
        ↓
Parquet / CSV / JSON artifacts
        ↓
Streamlit UI
```

Current classifications:

- NEW
- CHANGED
- UNCHANGED
- DELETED

Current temporal model:

```text
[effective_from, effective_to)
```

`effective_from` is inclusive.
`effective_to` is exclusive.
`effective_to = null` means the current/open version.

Current snapshot modes:

- FULL
- INCREMENTAL

Current delete policies:

- SOFT_DELETE
- IGNORE

---

# 3. CURRENT V1 ENGINEERING THAT MUST BE PRESERVED

The verified V1 system already contains:

- deterministic vectorized Polars SCD2 processing
- duplicate business-key validation
- datetime normalization
- temporal invariant validation
- FULL vs INCREMENTAL semantics
- SOFT_DELETE vs IGNORE semantics
- data contracts
- contract compatibility checks
- quarantine artifacts
- SHA-256 execution fingerprint/idempotency
- Prefect 3 orchestration
- retry/error categorization
- Gemini primary AI provider
- Groq fallback
- deterministic template fallback
- structured Pydantic AI outputs
- AI cost/token/latency tracking
- Google OIDC authentication
- Parquet/CSV/JSON artifacts
- Docker packaging
- extensive automated tests
- benchmark tests

The deterministic core is the highest-priority protected area.

Core files identified in V1 include:

```text
src/scd2_copilot/ingestion.py
src/scd2_copilot/schema.py
src/scd2_copilot/contracts.py
src/scd2_copilot/detect_changes.py
src/scd2_copilot/transform_scd2.py
src/scd2_copilot/validate.py
src/scd2_copilot/workflow.py
src/scd2_copilot/deployment.py
src/scd2_copilot/explain.py
src/scd2_copilot/artifacts.py
src/scd2_copilot/auth.py

app/streamlit_app.py
app/ui_components.py
```

Do not assume these paths are unchanged. Inspect the current repository before editing.

---

# 4. CUSTOMER DATA — IMPORTANT CLARIFICATION

Customer data currently appears heavily in tests and sample data.

This does NOT mean the engine is customer-specific.

The V1 audit established:

- customer references in core code are examples/documentation
- customer CSVs are sample/demo data
- customer fields are heavily used in regression fixtures
- the core SCD2 engine is domain-agnostic

Therefore:

**DO NOT delete customer fixtures or rewrite them merely to make the project “generic.”**

Customer tests are valuable regression assets.

V2 should prove domain generality by adding one additional operational domain, initially:

**Inventory / Warehouse**

Suggested live demo domain:

```text
inventory_source
----------------
sku_id
warehouse_id
quantity_on_hand
reorder_level
status
updated_at
```

Business key:

```text
(sku_id, warehouse_id)
```

The engine must remain generic enough to process other entities such as:

- customer
- inventory
- product
- pricing
- employee
- financial account

without domain-specific changes to the core engine.

---

# 5. V2 PRODUCT GOAL

The V2 user should not have to manually upload snapshots to see what is happening.

The desired workflow is:

```text
LIVE OPERATIONAL DATABASE
        ↓
record/data changes
        ↓
automatic ingestion
        ↓
micro-batch
        ↓
existing deterministic SCD2 engine
        ↓
historical state update
        ↓
contract validation
        ↓
SCD2 validation
        ↓
state-transition significance analysis
        ↓
NORMAL / SUSPICIOUS
        ↓
NORMAL → commit/publish
SUSPICIOUS → hold downstream
        ↓
AI explanation
        ↓
live Streamlit visibility
```

The new product should feel like:

```text
WATCH → DETECT → ANALYZE → HANDLE → EXPLAIN
```

rather than:

```text
UPLOAD → RUN → VIEW
```

V1 batch mode must continue to work.

---

# 6. WHAT V2 IS NOT

Do NOT turn SCD2 Copilot into:

- a new database
- a generic data observability replacement
- a generic data-quality platform
- a generic data catalog
- a generic lineage platform
- a generic anomaly-detection platform
- a generic chatbot
- an autonomous AI data-integrity authority
- a Kafka showcase
- a distributed-systems showcase
- a custom authentication platform

Do not add technology merely because it sounds enterprise.

---

# 7. CORE V2 PRINCIPLE

## The engine decides. Validation protects. AI explains.

The deterministic engine remains authoritative for:

- business-key matching
- NEW / CHANGED / UNCHANGED / DELETED classification
- SCD2 transformation
- effective dates
- temporal semantics
- correctness validation
- significance rules where explicitly deterministic

AI may:

- summarize structured evidence
- explain why a deterministic rule fired
- describe historical context
- generate human-readable narratives

AI must NOT:

- decide whether a row is NEW / CHANGED / UNCHANGED / DELETED
- decide effective dates
- declare the authoritative database state correct/incorrect
- modify authoritative SCD2 state
- directly mutate source operational data
- directly decide to block source transactions

If all AI providers fail, the deterministic pipeline must still function.

---

# 8. REAL-TIME V2 ARCHITECTURE

Target architecture:

```text
                    LIVE SOURCE
              PostgreSQL / Supabase
                        │
                        ▼
               CHANGE INGESTION
                     WORKER
                        │
                 watermark / CDC
                        │
                        ▼
                  MICRO-BATCH
                        │
                        ▼
             EXISTING POLARS SCD2
                    ENGINE
                        │
                        ▼
              CONTRACT VALIDATION
                        │
                        ▼
                SCD2 VALIDATION
                        │
                        ▼
          STATE-TRANSITION ANALYSIS
                        │
                ┌───────┴───────┐
                ▼               ▼
             NORMAL         SUSPICIOUS
                │               │
                ▼               ▼
              COMMIT            HOLD
                                │
                                ▼
                        AI EXPLANATION
                                │
                                ▼
                     POSTGRESQL STATE
                                │
                      ┌─────────┴────────┐
                      ▼                  ▼
                   FastAPI           Streamlit
                                         │
                                         ▼
                                  LIVE MONITOR
```

The system should be modular enough that ingestion transport can change without rewriting the core SCD2 engine.

---

# 9. DATABASE DECISION

Primary database technology:

**PostgreSQL**

Initial hosted implementation for the project/demo:

**Supabase PostgreSQL**

Why:

- real PostgreSQL
- hosted
- suitable for a free/low-cost project/demo
- transactional state
- indexing
- concurrency controls
- native PostgreSQL change-capture foundation
- convenient Realtime capabilities

Important:

Supabase is infrastructure, not the product differentiator.

The core application must not become tightly coupled to Supabase-specific APIs.

---

# 10. INITIAL LIVE INGESTION STRATEGY

First implementation:

**PostgreSQL watermark polling**

Conceptually:

```sql
SELECT ...
FROM source_table
WHERE updated_at > :last_watermark
ORDER BY updated_at, stable_key;
```

Polling interval must be configurable.

Initial working range:

**15–30 seconds**

Do NOT describe 15 seconds as universally optimal.

The purpose of V2.1 is to establish a credible live system with minimum complexity.

Future option:

- PostgreSQL logical decoding / CDC
- Supabase Realtime transport
- other CDC transport only if measured requirements justify it

Do NOT start with Kafka, Flink, Spark, or a Debezium/Kafka stack.

---

# 11. REAL-TIME WORKER

The ingestion worker must be a separate continuously running process.

Do NOT put the continuous ingestion loop inside Streamlit.

Responsibilities:

1. load checkpoint
2. read new/changed source records
3. buffer records
4. form a micro-batch
5. invoke existing SCD2 engine
6. validate
7. write transactionally
8. advance checkpoint
9. expose observable status
10. recover safely after restart

The worker must be independently testable.

---

# 12. MICRO-BATCHING

Do not run the Polars engine once per individual live row.

Use micro-batches.

Initial design:

```text
incoming changes
      ↓
short buffer window
      ↓
Polars DataFrame
      ↓
existing vectorized engine
```

Make the window configurable.

Example configuration:

```text
MICRO_BATCH_WINDOW_SECONDS=15
```

The optimal window must eventually be measured rather than assumed.

---

# 13. POSTGRESQL OPERATIONAL STATE

V2 should move authoritative operational SCD2 state away from local Parquet files.

PostgreSQL should contain the authoritative live state.

Likely tables:

```text
source / source-facing tables
scd2_history
processing_run
processing_checkpoint
change_batch
held_change
```

Exact names must be decided after inspecting current repository conventions.

SCD2 history should include:

```text
business keys
tracked attributes
effective_from
effective_to
is_current
```

Keep:

```text
[effective_from, effective_to)
```

exactly.

Parquet/CSV/JSON remain useful for:

- exports
- downloads
- analytical artifacts
- reports
- reproducible snapshots

They are not the authoritative live transactional state.

---

# 14. TRANSACTION BOUNDARY

For each successfully processed micro-batch, the target transaction should conceptually cover:

```text
BEGIN

read required active target state
calculate transitions
validate transitions
close previous versions
insert new versions
record run metadata
advance checkpoint

COMMIT
```

If anything required fails:

```text
ROLLBACK
```

Checkpoint advancement must never indicate success for a batch whose state commit did not succeed.

---

# 15. CONCURRENCY

The existing batch filesystem architecture does not prove safe concurrent updates.

V2 must explicitly address:

- concurrent workers
- two runs touching the same business key
- simultaneous updates
- current-version uniqueness
- transaction isolation
- locking strategy
- conflict handling

Do not claim concurrency safety until it is implemented and tested.

---

# 16. EVENT / BATCH IDEMPOTENCY

The V1 SHA-256 fingerprint is valuable and must remain.

However:

**Batch fingerprint idempotency is not the same thing as streaming replay protection.**

V2 must additionally reason about:

- source watermark
- source event identity where available
- batch boundaries
- replay
- worker restart
- duplicate delivery
- checkpoint recovery

The future system must not create duplicate current SCD2 versions after a replay.

Do not claim “exactly once” unless technically proven.

---

# 17. LATE AND OUT-OF-ORDER EVENTS

Current V1 does NOT automatically support late/retro-dated event insertion into closed temporal intervals.

V2 must explicitly define behavior.

Possible future policy:

```text
ON-TIME EVENT
    ↓
normal processing

LATE EVENT
    ↓
special correction path
```

Do NOT silently force a late event through the normal append path.

A late event may require historical interval surgery.

This is a correctness-sensitive area.

---

# 18. STATE-TRANSITION SIGNIFICANCE

This is the main new intelligence layer.

The first implementation must be deterministic and explainable.

Initial signals to investigate:

- number of changed records
- percentage of population changed
- change velocity
- synchronized changes
- change magnitude
- historical baseline
- contract violations
- SCD2 validation failures

Example:

```text
Normal historical batch:
20–300 changed records

Current batch:
18,421 changed records
in 3 seconds
```

Possible result:

```text
HIGH-SIGNIFICANCE STATE TRANSITION
```

Important:

A changed value is NOT automatically bad.

Example:

```text
999 → 9999
```

may be completely legitimate.

The system should focus more on:

- breadth
- velocity
- synchronization
- unusual population behavior
- rule violations
- historical deviation

rather than arbitrary value-change judgments.

---

# 19. FIRST SIGNIFICANCE MODEL

Start simple.

Possible evidence vector:

```text
change_volume
population_percentage
change_velocity
synchronization_score
magnitude_signal
historical_deviation
contract_violation
validation_violation
```

The implementation should produce a structured evidence object.

Example conceptual result:

```json
{
  "severity": "HIGH",
  "records_changed": 18421,
  "population_percentage": 87.4,
  "change_velocity": 5756,
  "historical_baseline": {
    "min": 20,
    "max": 300
  },
  "signals": [
    "HIGH_CHANGE_VOLUME",
    "HIGH_CHANGE_VELOCITY",
    "SYNCHRONIZED_TRANSITION"
  ]
}
```

Actual schema should be designed and validated before implementation.

Do not start with an ML model.

---

# 20. SUSPICIOUS CHANGE HANDLING

The source database remains the source of truth.

Do NOT directly block or mutate the operational source because the model says something is suspicious.

Instead:

```text
NORMAL
   ↓
commit/process downstream

SUSPICIOUS
   ↓
hold downstream publication/processing
```

The held batch should preserve:

- evidence
- original event/change data
- reason
- run/batch ID
- timestamps
- processing state

V2 should provide a safe reprocess path eventually.

---

# 21. AI ARCHITECTURE IN V2

Reuse V1 provider structure:

```text
Gemini
  ↓ failure
Groq
  ↓ failure
Deterministic template
```

V2 AI input should be structured evidence, not raw full tables.

Example evidence:

```text
records_changed
population_percentage
historical_baseline
velocity
fields_affected
transition_patterns
contract_result
validation_result
hold_reason
```

AI output:

```text
structured explanation
```

Validate with Pydantic.

The AI must not be allowed to change authoritative state.

---

# 22. LIVE UI

V1 batch UI remains.

V2 adds a live monitor.

Minimum useful live view:

```text
SOURCE
● Connected

WORKER
● Running

LAST CHECKPOINT
...

EVENTS PROCESSED
...

PENDING
...

LAST PROCESSING RUN
...

LIVE CHANGES
────────────────────────
time
entity
before
after
significance
status
────────────────────────
```

A suspicious batch should visibly show:

```text
HIGH-SIGNIFICANCE
HELD
```

with evidence and AI explanation.

Do not turn the UI into a generic dashboard.

The goal is to visibly demonstrate the live event lifecycle.

---

# 23. FASTAPI

FastAPI should provide a headless application/query boundary once the underlying state/worker architecture is stable.

Potential minimal endpoints:

```text
GET /health
GET /live-status
GET /runs
GET /changes
GET /changes/{id}
```

Potential later endpoints:

```text
POST /reprocess
POST /release-held
```

Do not build unnecessary APIs.

Do not move the SCD2 engine into HTTP handlers.

Long-running processing should remain worker-oriented.

---

# 24. AUTHENTICATION

Existing Google OIDC remains.

Current V1 authentication pattern:

```text
st.login("google")
st.user
st.logout()
```

Authentication is not the V2 focus.

Do not build:

- custom passwords
- custom JWT issuance
- Firebase Auth
- Auth0
- custom identity infrastructure

unless a future requirement genuinely demands it.

Current V1 lacks:

- RBAC
- persistent user database
- multi-tenant isolation

That is known and should not distract from the V2 live-data goal.

---

# 25. SECURITY

Treat operational data as untrusted.

Must protect:

- database credentials
- service-role credentials
- OIDC secrets
- AI provider keys
- stored artifacts

Rules:

- never commit secrets
- never expose service-role credentials to UI/browser
- minimize data sent to LLMs
- consider prompt injection from cell values
- validate all external/source data
- use least privilege
- review RLS before exposing user-scoped database access
- do not log sensitive secrets/tokens

Authentication does not equal authorization.

---

# 26. DEPLOYMENT MODEL

Local development:

```text
Docker Compose
+
Supabase/PostgreSQL
+
worker
+
Streamlit
+
(optional) FastAPI
```

Public UI/demo:

Streamlit Community Cloud remains acceptable for the interactive UI.

Do NOT claim Community Cloud is an always-on real-time worker environment.

The real-time worker should be independently runnable.

Docker remains the reproducible deployment artifact.

---

# 27. COST PRINCIPLE

Development and demo should remain as close to $0 as practical.

Prefer:

- Supabase free tier where suitable
- open-source Python libraries
- existing Gemini/Groq usage within available limits
- Docker
- local development

Do not introduce paid infrastructure without explicit approval.

Free-tier limitations must be documented.

---

# 28. OBSERVABILITY

V2 must expose enough information to answer:

```text
Is the worker alive?
How far behind is it?
How many events are pending?
How long does processing take?
How many changes occurred?
How many batches were held?
How often do retries occur?
When was the checkpoint last advanced?
```

Metrics to measure:

- ingestion latency
- processing latency
- end-to-end detection latency
- records/events processed
- micro-batch size
- checkpoint lag
- worker failures
- held-batch count
- AI latency
- AI cost

Do not invent performance claims.

---

# 29. PERFORMANCE

The V1 engine is already benchmarked.

When comparing V2 performance:

- use a clearly defined benchmark
- do not mix historical benchmark methodologies
- measure end-to-end latency separately from engine throughput
- measure worker overhead
- measure PostgreSQL read/write cost
- measure micro-batch size effects
- measure memory

Important:

The Polars engine is intended to process batches efficiently.

Do not convert each event into an individual Polars execution.

---

# 30. V2 TESTING REQUIREMENTS

Preserve V1 tests.

Add V2 coverage for:

## Database
- migrations
- schema constraints
- indexes
- transactional commits
- rollback

## Worker
- polling
- checkpoint loading
- checkpoint advancement
- restart recovery
- empty batch
- duplicate batch
- replay

## Real-time correctness
- NEW
- CHANGED
- UNCHANGED
- DELETED
- composite keys
- concurrent updates
- ordering
- late event behavior

## Significance
- normal batch
- mass update
- mass deletion
- synchronized transition
- high velocity
- baseline comparison
- false-positive cases

## Containment
- normal commit
- suspicious hold
- held-state persistence
- reprocess/release

## AI
- valid evidence
- fallback
- malformed output
- prompt limits
- sensitive-data controls

## API/UI
- health
- live status
- live data display
- error states

## Performance
- 10K
- 100K
- 500K
- 1M
- V2 end-to-end latency

---

# 31. DOMAIN STRATEGY

Do not hard-code the product around inventory.

Use:

### Existing customer dataset
Regression/demo fixture.

### Inventory dataset
Primary V2 live demonstration.

This proves:

```text
generic engine
+
different business domain
+
real-time source
```

Possible future domains:

- pricing
- product catalog
- employee
- accounts

Do not implement all of them now.

---

# 32. DEFINITION OF “REAL-TIME” FOR V2

V2 is considered live when this works:

```text
1. Modify a PostgreSQL source record.
2. No CSV upload.
3. Worker automatically detects the change.
4. Change is placed into a micro-batch.
5. Existing SCD2 engine processes it.
6. Transaction commits historical state.
7. Checkpoint advances.
8. Streamlit displays the processed event.
```

For suspicious data:

```text
PostgreSQL change
      ↓
worker
      ↓
micro-batch
      ↓
SCD2
      ↓
significance detection
      ↓
HOLD
      ↓
live UI
      ↓
AI explanation
```

This is the core V2 acceptance demonstration.

---

# 33. V2 IMPLEMENTATION ORDER

Follow dependencies strictly.

```text
V2.0  Architecture/context freeze
  ↓
V2.1  Supabase/PostgreSQL foundation
  ↓
V2.2  PostgreSQL SCD2 state
  ↓
V2.3  Processing checkpoint
  ↓
V2.4  Watermark ingestion worker
  ↓
V2.5  Live SCD2 processing
  ↓
V2.6  Transaction + concurrency hardening
  ↓
V2.7  Live Streamlit monitor
  ↓
V2.8  State-transition significance
  ↓
V2.9  Suspicious hold/containment
  ↓
V2.10 AI evidence explanation
  ↓
V2.11 FastAPI boundary
  ↓
V2.12 CDC/Realtme transport evaluation
  ↓
V2.13 End-to-end reliability testing
  ↓
V2.14 Performance benchmarking
  ↓
V2.15 Deployment hardening
```

The exact ordering may be adjusted only after repository inspection shows a dependency difference.

---

# 34. V2 NON-NEGOTIABLE DEVELOPMENT RULES

Before coding:

1. Read AGENTS.md.
2. Read this file.
3. Read relevant V2 skill.
4. Inspect the actual repository.
5. Identify exact files to change.
6. Produce an implementation plan for the current task.
7. Do not modify unrelated files.

During coding:

- smallest correct change
- preserve V1 behavior
- add tests with every behavioral change
- migrations for database schema
- no secret commits
- no hidden architectural changes
- no new infrastructure without justification

After coding:

- run focused tests
- run relevant integration tests
- run V1 regression suite
- report failures honestly
- update documentation only after verification

---

# 35. CHANGE BOUNDARIES

## DO NOT CHANGE CASUALLY

```text
detect_changes.py
transform_scd2.py
validate.py
contracts.py
```

These are protected deterministic core components.

## LIKELY NEW/ADAPTED AREAS

```text
source ingestion abstraction
PostgreSQL repository/state layer
checkpoint subsystem
worker
micro-batch coordinator
significance engine
hold/containment state
live UI
FastAPI
V2 integration tests
```

Actual paths must be determined from the repository.

---

# 36. WHAT SUCCESS LOOKS LIKE

At the end of V2, a user should be able to see:

```text
LIVE SOURCE
    ↓
database change happens
    ↓
automatic detection
    ↓
historical SCD2 transition
    ↓
validation
    ↓
significance analysis
    ↓
NORMAL or SUSPICIOUS
    ↓
COMMIT or HOLD
    ↓
AI explanation
    ↓
live UI
```

The system should remain fully usable in V1 batch mode.

---

# 37. REQUIRED LIVE DEMO SCENARIO

Use inventory as the primary live demonstration.

Example:

Normal:

```text
SKU-101
quantity: 100 → 98
```

Expected result:

```text
NORMAL
COMMITTED
```

Then create a controlled test bulk event.

Example:

```text
thousands of inventory records
quantity_on_hand → 0
within a very short interval
```

Expected behavior:

```text
HIGH-SIGNIFICANCE STATE TRANSITION
        ↓
HELD
        ↓
evidence displayed
        ↓
AI explanation
```

The exact record count and latency must be measured in the real implementation.

This demonstration is for controlled testing only.

---

# 38. FINAL V2 PRODUCT STATEMENT

Use this wording consistently:

> **SCD2 Copilot is a real-time data-change guardrail that continuously observes operational data, reconstructs historical business-state transitions using a deterministic SCD2 engine, identifies significant state-change patterns, holds suspicious downstream changes, and uses GenAI to explain the evidence.**

Do NOT shorten this into:

- “AI SCD2”
- “real-time database”
- “data observability platform”
- “AI anomaly detector”

unless the actual product scope is deliberately changed later.

---

# 39. FINAL ARCHITECTURAL PHILOSOPHY

The project should evolve by adding layers around the current engine:

```text
             LIVE INGESTION
                   ↓
             EXISTING SCD2
                   ↓
              VALIDATION
                   ↓
        TRANSITION SIGNIFICANCE
                   ↓
          HOLD / COMMIT LOGIC
                   ↓
            AI EXPLANATION
                   ↓
              LIVE UI/API
```

The existing engine remains the center.

The new functionality should be added around it.

This is an incremental evolution, not a rewrite.

---

# 40. CURRENT V2 STATUS

At the beginning of V2:

```text
✅ V1 deterministic engine
✅ V1 validation
✅ V1 contracts
✅ V1 idempotency
✅ V1 AI explanation
✅ V1 orchestration
✅ V1 authentication
✅ V1 Docker
✅ V1 tests

❌ PostgreSQL operational state
❌ continuous worker
❌ checkpoint/recovery
❌ live ingestion
❌ live UI
❌ significance engine
❌ hold/containment
❌ V2 API
❌ V2 end-to-end real-time tests
```

Therefore:

**V2 is not implemented yet.**

The next task is environment/database setup, not feature coding.

---

# 41. FIRST IMPLEMENTATION MILESTONE

## V2.1 — Supabase/PostgreSQL Foundation

This milestone should establish:

- Supabase CLI/local environment
- remote Supabase project
- version-controlled migrations
- seed data
- inventory source table
- SCD2 target table
- processing checkpoint table
- processing run metadata table
- local reset/reproducibility
- basic database tests

V2.1 must NOT implement:

- live worker
- CDC
- significance engine
- AI changes
- hold logic
- FastAPI
- live UI

Those belong to later milestones.

---

# 42. AI AGENT STOP CONDITION

If a requested implementation would require changing:

- core SCD2 semantics
- temporal interval semantics
- AI authority boundary
- V1 batch behavior
- customer regression tests
- database architecture
- ingestion architecture

without the change being part of the approved milestone:

**STOP and ask for clarification rather than silently redesigning the system.**

---

# 43. MOST IMPORTANT INSTRUCTION

Do not optimize the project for the number of technologies it contains.

Optimize for:

```text
REAL PROBLEM
+
REAL LIVE DATA
+
CORRECT STATE TRANSITIONS
+
AUTOMATION
+
RELIABILITY
+
MEASURABLE PERFORMANCE
+
USEFUL GENAI
```

The project becomes stronger through measurable engineering behavior, not technology accumulation.
