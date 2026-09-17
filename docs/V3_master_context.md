# SCD2 Copilot V3 — Master Context

> **Purpose:** This document is the authoritative product, architecture, scope, and implementation contract for the V3 evolution of SCD2 Copilot.
>
> **Instruction to Antigravity:** Read this document before planning or modifying V3. The actual repository code remains the ultimate implementation source of truth. If repository reality differs from this document, inspect the code, tests, and current documentation first; do not silently redesign the project.

---

# 1. V3 PRODUCT IDENTITY

## Product name

**SCD2 Copilot — Data Change Guardrail**

## Product statement

> SCD2 Copilot is a configurable PostgreSQL data-change guardrail that incrementally processes operational table changes, reconstructs historical state using a deterministic SCD2 engine, detects suspicious change patterns using deterministic rules, holds suspicious batches before historical propagation, preserves evidence, and uses GenAI to explain the evidence to an operator.

## Product workflow

```text
CONFIGURE → WATCH → DETECT → VALIDATE → DECIDE →

NORMAL ─────────────→ COMMIT

SUSPICIOUS ──────────→ HOLD → EVIDENCE → EXPLAIN →
                         RELEASE / REPROCESS / DISCARD
```

The product is **not** a generic data observability platform, generic anomaly detection platform, generic chatbot, or autonomous AI data-integrity authority.

---

# 2. WHY V3 EXISTS

The current V2 implementation already provides the important operational building blocks: PostgreSQL state, incremental ingestion, deterministic SCD2 processing, validation, deterministic guardrails, suspicious-batch containment, AI explanations, FastAPI APIs, and a live Streamlit monitoring interface.

The remaining product gap is that the source is too closely tied to the current inventory/Supabase demonstration. V3 must turn the existing capabilities into a **configurable PostgreSQL monitoring product** without rewriting the deterministic SCD2 core.

The goal is not to add more technologies. The goal is to make the existing capability usable against a configured PostgreSQL table and demonstrate a complete real-world operational workflow.

---

# 3. ABSOLUTE SCOPE DECISION

## V3 supports

**PostgreSQL-compatible source databases only.**

The source is defined by configuration:

```yaml
monitor:
  name: warehouse_inventory

  source:
    type: postgresql
    schema: public
    table: inventory

  keys:
    - sku_id
    - warehouse_id

  change_timestamp:
    column: updated_at

  tracked_columns:
    - quantity_on_hand
    - reorder_level
    - status
```

## V3 does NOT support

- MySQL
- SQL Server
- Oracle
- MongoDB
- Kafka
- Debezium
- Spark
- Redis
- Kubernetes
- distributed event infrastructure
- generic CDC infrastructure
- vector databases
- RAG
- embeddings
- autonomous agents
- AI anomaly detection
- automatic source-database rollback
- multi-tenant SaaS architecture
- enterprise RBAC redesign

Do not add infrastructure merely because it sounds more enterprise.

---

# 4. CURRENT SYSTEM — PRESERVE

The V1/V2 deterministic core is protected.

Preserve existing verified behavior for:

- NEW / CHANGED / UNCHANGED / DELETED classification
- deterministic Polars processing
- SCD2 effective-date semantics
- `[effective_from, effective_to)` temporal intervals
- `effective_to = null` for the current version
- FULL vs INCREMENTAL semantics
- SOFT_DELETE vs IGNORE semantics
- data contracts
- deterministic validation
- execution fingerprint/idempotency where still applicable
- deterministic guardrail decisions
- suspicious hold semantics
- evidence persistence
- RELEASE / REPROCESS / DISCARD semantics
- evidence-grounded AI explanation
- FastAPI authentication/authorization boundary
- existing V1 batch CSV mode

## Core principle

```text
THE ENGINE DECIDES.
VALIDATION PROTECTS.
THE GUARDRAIL EVALUATES SIGNIFICANCE.
CONTAINMENT HOLDS.
THE WORKER ORCHESTRATES.
TRANSACTIONS COMMIT.
AI EXPLAINS.
```

AI must not become the business-decision authority.

---

# 5. TARGET ARCHITECTURE

```text
                    STREAMLIT UI
                         │
                         ▼
                    FASTAPI API
                         │
                         ▼
                MONITOR CONFIGURATION
                         │
                         ▼
             POSTGRES SOURCE ADAPTER
                         │
                         ▼
              INCREMENTAL CURSOR
                         │
                         ▼
                    MICRO-BATCH
                         │
                         ▼
               CHANGE DETECTION
                         │
                         ▼
                   SCD2 ENGINE
                         │
                         ▼
                    VALIDATION
                         │
                         ▼
                GUARDRAIL RULES
                    /       \
                   /         \
               NORMAL     SUSPICIOUS
                 │             │
                 ▼             ▼
              COMMIT          HOLD
                               │
                     ┌─────────┴─────────┐
                     ▼                   ▼
                  EVIDENCE        AI EXPLANATION
                     │                   │
                     └─────────┬─────────┘
                               ▼
                      OPERATOR ACTION
                 RELEASE / REPROCESS / DISCARD
```

The architecture must be an incremental evolution, not a rewrite.

---

# 6. FIVE ESSENTIAL V3 PHASES

Only these five phases are required for the V3 product target.

Anything outside them is optional and should not be implemented unless a blocking defect makes it necessary.

---

## PHASE 1 — CONFIGURABLE POSTGRES SOURCE

### Objective

Remove source-specific coupling from ingestion and make the existing pipeline configurable for a PostgreSQL table.

### Required modules

#### 1.1 MonitorConfig

Create a typed configuration model containing at minimum:

- monitor name
- source type
- database connection reference
- schema
- table
- business key columns
- change timestamp column
- tracked columns

Do not store plaintext secrets in persisted monitor configuration.

#### 1.2 PostgreSQL Source Adapter

Create a single source boundary responsible for PostgreSQL access.

Minimum capability:

```text
connect()
discover_schema()
validate_configuration()
read_initial_snapshot()
read_incremental_changes()
```

The adapter must return data in the normalized shape expected by the existing processing pipeline.

#### 1.3 Source Configuration Validation

Before activating a monitor, validate:

- schema exists
- table exists
- key columns exist
- timestamp column exists
- tracked columns exist
- business key values are usable/non-null
- timestamp is usable for incremental processing
- configuration can be processed by the existing engine

#### 1.4 Remove Hard-Coded Inventory Coupling

Inventory-specific names may remain in the demo dataset, but the core processing path must not require:

- `inventory_source`
- `sku_id`
- `warehouse_id`
- `quantity_on_hand`
- `status`

Those values must come from `MonitorConfig` or domain configuration.

### Exit criteria

A configured PostgreSQL table can enter the existing SCD2 pipeline without rewriting the engine for that table.

---

## PHASE 2 — LIVE PROCESSING CORRECTNESS

### Objective

Make the configured PostgreSQL source reliably drive the existing incremental pipeline.

### Required modules

#### 2.1 Config → Ingestion → Engine Integration

The configured source must flow through:

```text
MonitorConfig
  → PostgresSourceAdapter
  → normalized micro-batch
  → existing change detection
  → existing SCD2 engine
  → validation
```

Do not duplicate the SCD2 engine for V3.

#### 2.2 Deterministic Incremental Cursor

Do not use timestamp alone as the only ordering boundary when equal timestamps are possible.

Use a deterministic cursor concept such as:

```text
(updated_at, business_key)
```

The exact representation may follow the repository's existing implementation after code inspection.

#### 2.3 Correct Checkpoint Ordering

The required logical order is:

```text
READ
  ↓
PROCESS
  ↓
COMMIT or HOLD
  ↓
ADVANCE CHECKPOINT only when legally allowed
```

A checkpoint must not advance before a batch outcome is durably known.

#### 2.4 Basic Replay Safety

A failed/restarted processing attempt must not silently corrupt historical state or skip the source position.

Do not redesign the entire worker architecture. Fix only the correctness needed for the V3 workflow.

#### 2.5 Concurrency Guard

Prevent two processing instances from independently consuming and committing the same monitor cursor at the same time.

Use the smallest database-backed mechanism that fits the existing codebase.

### Exit criteria

A real PostgreSQL UPDATE can be detected automatically, processed by the existing engine, and result in a correct committed or held batch without source-position ambiguity.

---

## PHASE 3 — DETERMINISTIC GUARDRAIL + CONTAINMENT

### Objective

Turn the change-processing pipeline into a true safety workflow.

### Required modules

#### 3.1 Deterministic Change Metrics

For each micro-batch calculate the available generic evidence, such as:

- changed rows
- new rows
- deleted rows
- unchanged rows
- change ratio
- affected columns
- numeric deltas where applicable
- configured categorical transitions where applicable

#### 3.2 Guardrail Rule Evaluation

Preserve the deterministic rule architecture.

Rules may include the existing concepts such as:

- HIGH_CHANGE_VOLUME
- HIGH_POPULATION_IMPACT
- LARGE_QUANTITY_SWING
- MASS_DEACTIVATION
- HIGH_CHANGE_VELOCITY
- WIDE_GEOGRAPHIC_IMPACT

Only rules supported by the configured source fields should participate.

The rule engine decides:

```text
NORMAL
SUSPICIOUS
```

and may assign the deterministic severity already supported by the repository.

AI must not make this decision.

#### 3.3 Normal Path

For a normal batch:

```text
NORMAL
  ↓
SCD2 historical state updated
  ↓
processing run committed
  ↓
checkpoint advances
```

#### 3.4 Suspicious Path

For a suspicious batch:

```text
SUSPICIOUS
  ↓
SCD2 historical state NOT updated
  ↓
held batch persisted
  ↓
evidence persisted
  ↓
checkpoint preserved
```

This is the central safety behavior of V3.

#### 3.5 Recovery

Preserve the existing operator recovery actions:

```text
RELEASE
REPROCESS
DISCARD
```

Recovery must remain authenticated, authorized, idempotent, and state-safe according to the repository's existing containment implementation.

### Exit criteria

The product can demonstrate both:

```text
normal change    → COMMIT
suspicious change → HOLD
```

and the suspicious batch can later be resolved through the existing recovery lifecycle.

---

## PHASE 4 — OPERATOR VALUE UI + AI EXPLANATION

### Objective

Make the operational value immediately visible to someone using the system.

Do not build a large analytics dashboard.

### Required modules

#### 4.1 Monitor Screen

Show only the information needed to answer:

- What source/table am I monitoring?
- Is it connected?
- Is monitoring active?
- When was the last successful processing activity?
- What is the current source position?

#### 4.2 Change Activity Screen

Show recent runs/events with at minimum:

- timestamp
- batch/run identifier
- number of changed records
- decision
- severity where applicable

#### 4.3 Suspicious Batch / Incident Screen

This is the primary demo surface.

It must show:

```text
SUSPICIOUS CHANGE

What changed?
Why was it considered suspicious?
Which rules fired?
What evidence supports that decision?
What historical state was protected?
What can the operator do next?
```

Minimum actions:

```text
RELEASE
REPROCESS
DISCARD
```

#### 4.4 AI Explanation

Feed the model only structured deterministic evidence.

AI may:

- summarize evidence
- explain triggered rules
- describe affected records/columns
- produce human-readable narrative

AI must not:

- change the decision
- invent evidence
- assign unsupported severity
- mutate source data
- mutate historical state
- bypass containment

If all AI providers fail, the deterministic workflow must continue with a deterministic fallback explanation.

### Exit criteria

A person unfamiliar with the code can inspect one suspicious batch and understand:

```text
WHAT CHANGED
→ WHY IT MATTERED
→ WHAT WAS HELD
→ WHAT EVIDENCE EXISTS
→ WHAT THE OPERATOR CAN DO
```

---

## PHASE 5 — REAL-WORLD DEMO VALIDATION + FREEZE

### Objective

Prove the complete workflow end-to-end and stop feature development.

### Required scenarios

#### Scenario A — Normal operational change

Example:

```text
SKU-001
quantity: 200 → 198
```

Expected:

```text
NORMAL
→ SCD2 history updated
→ checkpoint advances
```

#### Scenario B — Large individual change

Example:

```text
quantity: 200 → 20
```

Expected:

```text
LARGE_CHANGE rule
→ SUSPICIOUS
→ HOLD
→ evidence preserved
```

#### Scenario C — Broad operational change

Example:

```text
many ACTIVE records
→ INACTIVE
```

Expected:

```text
MASS_DEACTIVATION / related population rule
→ SUSPICIOUS
→ HOLD
→ evidence + AI explanation
→ operator recovery
```

#### Scenario D — Configuration change

Configure the same engine against another realistic PostgreSQL table shape if practical.

The goal is to demonstrate that the engine is configuration-driven, not inventory-hard-coded.

### Final validation

Run:

- focused unit tests
- relevant integration tests
- V1 regression tests
- live PostgreSQL workflow
- API checks
- UI/browser validation for affected screens

Record actual measured results.

### Freeze rule

Once all acceptance criteria pass:

**STOP FEATURE DEVELOPMENT.**

Only fix:

- blocking bugs
- reproducibility issues
- security defects
- incorrect claims/documentation
- demo-breaking UI issues

Do not add new architecture after the freeze.

---

# 7. V3 ACCEPTANCE CRITERIA — THE REAL DEFINITION OF DONE

V3 is complete only when the following complete workflow works:

```text
1. Configure a PostgreSQL table.
2. Validate the configuration.
3. Start monitoring.
4. Modify a source record directly in PostgreSQL.
5. Worker detects the change without CSV upload.
6. Existing SCD2 engine processes the batch.
7. Normal change is committed into historical state.
8. Suspicious change is detected deterministically.
9. Suspicious change is held before historical propagation.
10. Evidence is persisted and inspectable.
11. AI explains the deterministic evidence.
12. Operator can RELEASE, REPROCESS, or DISCARD.
13. Recovery is safe and idempotent.
14. The complete flow is demonstrable from the UI.
```

If these work, the product has reached the intended V3 outcome.

---

# 8. PRIMARY DEMO STORY

Use warehouse inventory as the primary demonstration domain.

Inventory is a **demo domain**, not a hard-coded product dependency.

### Demo narrative

```text
Warehouse operational database
          ↓
SCD2 Copilot monitors configured table
          ↓
Normal update
          ↓
Historical state updated

Then an abnormal/broad update occurs
          ↓
Deterministic guardrail detects significance
          ↓
Batch is held
          ↓
Historical state remains protected
          ↓
Evidence is shown
          ↓
AI explains the evidence
          ↓
Operator resolves the batch
```

This is the core product demonstration.

---

# 9. WHAT THE PRODUCT SOLVES

The user persona is:

> **A data engineer / data platform engineer responsible for operational datasets that feed historical or analytical state.**

The concrete problem is:

> A source change can be technically valid from the database/pipeline perspective while being unexpectedly broad, extreme, or operationally significant. A pipeline reporting success does not by itself prove that the resulting historical state is safe to propagate.

SCD2 Copilot provides a deterministic guardrail between source change and historical propagation.

The product value is therefore:

```text
OBSERVE
+ DETECT
+ PROTECT
+ EXPLAIN
+ RECOVER
```

Not simply:

```text
RUN SCD2
```

---

# 10. AI BOUNDARY

AI is a supporting layer, not the control mechanism.

```text
Deterministic evidence
        ↓
      AI
        ↓
Human-readable explanation
```

Never:

```text
Raw data
  ↓
LLM decides
  ↓
Database changes
```

AI explanations must be grounded in values already produced by deterministic processing and must pass the repository's existing grounding validation.

---

# 11. SECURITY PREREQUISITE

Before modifying authentication or OAuth-related code in V3:

1. Inspect the actual current authentication implementation.
2. Verify whether the previously identified PKCE verifier-in-URL/global-verifier issue still exists.
3. If it still exists, fix only that security defect before continuing with V3 feature work.
4. Do not redesign the authentication system unless the current implementation requires it.

No secrets may be committed or exposed through URLs, logs, persisted monitor configuration, or API responses.

---

# 12. ARCHITECTURAL PROTECTION RULES

## Never casually rewrite

```text
SCD2 change detection
SCD2 transformation
SCD2 temporal semantics
Validation contracts
Deterministic guardrail semantics
Existing containment state machine
AI authority boundary
```

## Before changing a protected component

- inspect the current code
- inspect its tests
- identify the exact reason for the change
- make the smallest change that satisfies the active phase
- run relevant regression tests

---

# 13. ANTIGRAVITY IMPLEMENTATION RULES

For every V3 task:

### Step 1 — Explore

Read:

- `AGENTS.md`
- this file
- relevant project docs
- relevant skill files
- actual current source code
- relevant tests
- current git diff

### Step 2 — Plan

Create the smallest implementation plan for the **current phase only**.

Do not implement future phases early.

### Step 3 — Implement

- minimal focused changes
- reuse existing services
- no speculative abstractions
- no unrelated refactors
- no new infrastructure unless required by an acceptance criterion

### Step 4 — Verify

Run the smallest useful verification first, then the relevant regression suite.

For UI work, verify the actual rendered UI.

### Step 5 — Report

Report:

- changed files
- behavior implemented
- tests/checks executed
- actual measured result
- remaining risks

Do not claim success without verification.

---

# 14. NO-SLOP / NO-HALLUCINATION RULES

Do not introduce:

- placeholder production implementations
- fake API responses
- fake database connections
- hard-coded “success” states
- fake monitoring activity
- fake AI explanations presented as live results

Demo data may be synthetic, but the workflow must exercise the real implementation.

Never claim:

- "production-ready"
- "real-time"
- "lossless"
- "exactly-once"
- "hallucination-free"
- "supports any database"
- "enterprise scale"

unless the repository contains measured/tested evidence for that exact claim.

---

# 15. V3 OUTCOME IN ONE SENTENCE

> **Configure a PostgreSQL table, watch it change, deterministically classify the change, commit safe changes into SCD2 history, hold suspicious changes before historical propagation, explain the evidence with AI, and let an authenticated operator resolve the held batch.**

That is the complete V3 target.

Anything that does not directly help make that sentence true is not essential V3 work.
