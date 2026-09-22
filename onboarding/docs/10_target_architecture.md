# 10 — Target Architecture

## 1. Purpose

This document defines the target architecture for the Customer Data Onboarding & Integration Guardrail bounded context.

The architecture is intentionally layered so that:

- source-specific concerns remain at the source boundary
- semantic interpretation remains reviewable
- transformation and validation remain deterministic
- exceptions are replayable
- schema changes are detectable
- onboarding remains idempotent
- the existing SCD2 capability remains a separate downstream subsystem

---

## 2. Architectural Principle

The primary dependency direction is:

```text
External Source
      ↓
Source Adapter
      ↓
Profiler
      ↓
Mapping
      ↓
Human Approval
      ↓
Deterministic Transformation
      ↓
Deterministic Validation
      ↓
Canonical Customer Data
      ↓
Existing SCD2 Capability
      ↓
Historical Guardrail
```

Cross-cutting control flows:

```text
Schema Snapshot
      ↓
Schema Drift
      ↓
Mapping Impact
      ↓
Review / New Mapping Version
```

and:

```text
Validation Failure
      ↓
Exception
      ↓
Correction
      ↓
Reprocess
```

---

## 3. High-Level Architecture

```text
┌──────────────────────────────────────────────────────────────┐
│                     SOURCE SYSTEMS                           │
│                                                              │
│       CSV                    Mock REST API                    │
│        │                          │                          │
└────────┼──────────────────────────┼──────────────────────────┘
         │                          │
         └──────────────┬───────────┘
                        ▼
┌──────────────────────────────────────────────────────────────┐
│                    SOURCE ADAPTER LAYER                      │
│                                                              │
│  CSVSourceAdapter              MockAPISourceAdapter          │
│                                                              │
│  Responsibilities: read + normalize + describe source       │
└────────────────────────┬─────────────────────────────────────┘
                         ▼
┌──────────────────────────────────────────────────────────────┐
│                 SCHEMA / DATA PROFILING                     │
│                                                              │
│  Schema snapshot                                             │
│  Data profile                                                 │
│  Null/duplicate statistics                                    │
│  Type/date/value patterns                                     │
└────────────────────────┬─────────────────────────────────────┘
                         ▼
┌──────────────────────────────────────────────────────────────┐
│                 MAPPING PROPOSAL LAYER                      │
│                                                              │
│  Deterministic candidate evidence                            │
│                 +                                            │
│             GenAI assistance                                 │
│                                                              │
│  Output: structured mapping proposal                         │
└────────────────────────┬─────────────────────────────────────┘
                         ▼
┌──────────────────────────────────────────────────────────────┐
│                    HUMAN REVIEW                              │
│                                                              │
│  Approve / Reject / Edit                                     │
│                                                              │
│  Approved Mapping Version becomes immutable                  │
└────────────────────────┬─────────────────────────────────────┘
                         ▼
┌──────────────────────────────────────────────────────────────┐
│               DETERMINISTIC TRANSFORMATION                  │
│                                                              │
│  Constrained transformation vocabulary                        │
│  No arbitrary model-generated code                           │
└────────────────────────┬─────────────────────────────────────┘
                         ▼
┌──────────────────────────────────────────────────────────────┐
│                  DETERMINISTIC VALIDATION                    │
│                                                              │
│   REQUIRED / TYPE / FORMAT / ENUM / DUPLICATE                │
│   REFERENTIAL INTEGRITY / BUSINESS RULE                      │
└───────────────┬──────────────────────────────┬───────────────┘
                │                              │
             VALID                          INVALID
                │                              │
                ▼                              ▼
┌──────────────────────────┐      ┌────────────────────────────┐
│ Canonical Customer Data  │      │ Exception Queue            │
└─────────────┬────────────┘      │ correction + reprocessing  │
              │                   └────────────────────────────┘
              ▼
┌──────────────────────────────────────────────────────────────┐
│                 EXISTING SCD2 CAPABILITY                    │
│                                                              │
│  Change Detection → SCD2 Transformation → Validation        │
│                              ↓                               │
│                         Guardrail                            │
│                         /      \                              │
│                    NORMAL    SUSPICIOUS                      │
│                      │          │                            │
│                      ▼          ▼                            │
│                  Commit       Hold                            │
└──────────────────────────────────────────────────────────────┘
```

---

## 4. Logical Components

### 4.1 Source Adapter

Responsible for:

- reading the source
- source-specific parsing
- source schema extraction
- transport-specific error handling
- normalization into an internal source representation

Not responsible for:

- semantic mapping
- business validation
- SCD2
- guardrail decisions

---

### 4.2 Schema Profiler

Responsible for:

- source schema snapshot
- deterministic data statistics
- nullability/completeness characteristics
- uniqueness characteristics
- value distributions
- date-format observations
- useful source patterns

The profiler produces metadata used by mapping and diagnosis.

It must not silently modify source data.

---

### 4.3 Candidate Mapping Layer

This layer identifies plausible source-to-canonical relationships.

It may combine:

```text
deterministic candidate evidence
+
LLM semantic reasoning
```

The result is:

```text
mapping proposal
```

not:

```text
approved mapping
```

---

### 4.4 Mapping Approval

Human review converts:

```text
PROPOSAL
```

into:

```text
APPROVED MAPPING VERSION
```

The approved version becomes an immutable processing contract.

---

### 4.5 Transformation Engine

Consumes:

```text
source data
+
approved mapping version
```

and produces:

```text
canonical candidate data
```

Only supported deterministic operations may execute.

---

### 4.6 Validation Engine

Consumes canonical candidate records and produces:

```text
VALID
```

or:

```text
INVALID
```

with deterministic findings.

The validation layer is independent of LLM availability.

---

### 4.7 Exception Service

Consumes validation failures and maintains:

```text
exception lifecycle
```

including:

```text
OPEN
CORRECTED
REPROCESSED
RESOLVED
```

It should preserve lineage to the originating onboarding run.

---

### 4.8 Schema Drift Service

Compares source schema versions.

Responsibilities:

- detect structural changes
- classify changes
- identify affected mappings
- determine mapping compatibility
- request review when required

Semantic replacement suggestions may use GenAI but cannot silently change the active mapping.

---

### 4.9 Run/Idempotency Service

Responsible for:

- onboarding run identity
- lifecycle
- input fingerprint
- idempotency behavior
- source/mapping/schema references
- audit metadata

Persistent uniqueness/transactional semantics must provide the correctness boundary.

---

### 4.10 SCD2 Integration Adapter

The integration adapter is responsible for translating:

```text
ValidatedCanonicalCustomerBatch
```

into the interface required by the existing SCD2 capability.

It must not implement:

- SCD2 change detection
- SCD2 interval calculation
- SCD2 validation
- guardrail decisions

---

## 5. Data Flow

### Initial onboarding

```text
source
 ↓
profile
 ↓
mapping proposal
 ↓
human approval
 ↓
mapping version
 ↓
transform
 ↓
validate
 ↓
canonical
 ↓
SCD2
```

### Exception

```text
validate
 ↓
invalid
 ↓
exception
 ↓
correct
 ↓
reprocess
 ↓
validate
```

### Schema drift

```text
source schema v1
       ↓
source schema v2
       ↓
schema diff
       ↓
mapping impact
       ↓
review
       ↓
mapping v2
```

### Historical guardrail

```text
canonical batch
      ↓
SCD2 calculation
      ↓
SCD2 validation
      ↓
guardrail
   /        \
NORMAL    SUSPICIOUS
  │           │
  ▼           ▼
commit       hold
```

---

## 6. Persistence Model

The onboarding domain is expected to require persistent representations for:

```text
source
source_schema_version
canonical_schema_version
mapping
mapping_version
onboarding_run
exception
```

The exact database schema must be designed during the appropriate implementation milestone.

Do not create duplicate persistence structures where existing parent-repository capabilities can be safely reused.

The existing SCD2 historical persistence remains downstream and conceptually separate.

---

## 7. API Boundary

The onboarding API should expose operational actions without exposing internal implementation details.

Logical API groups may include:

```text
/sources
/profiles
/mappings
/runs
/exceptions
/drift
```

Possible operations:

```text
create/onboard source
profile source
generate mapping proposal
retrieve mapping
approve mapping
start onboarding run
retrieve run
list exceptions
retrieve exception
reprocess exception
inspect schema drift
```

Exact endpoint names should be selected during implementation based on the repository's existing FastAPI conventions.

---

## 8. UI Boundary

The Streamlit interface should support the operational workflow:

```text
1. Select/upload source
2. Inspect profile
3. Review mappings
4. Approve mapping version
5. Run onboarding
6. Inspect validation results
7. Review exceptions
8. Reprocess corrections
9. Inspect schema drift
10. Inspect downstream historical outcome
```

The UI must consume stable service/API boundaries rather than embed business logic.

---

## 9. AI Boundary

The LLM should sit behind a dedicated mapping/explanation interface.

Conceptually:

```text
Profiler
   ↓
Mapping Context
   ↓
LLM
   ↓
Structured Proposal
   ↓
Schema Validation
   ↓
Human Review
```

The LLM must not directly reach:

- database write functions
- source mutation functions
- SCD2 persistence
- guardrail decisions
- recovery actions

---

## 10. Security Boundary

The source data path must respect the existing repository authentication, secret-management, and redaction conventions.

At minimum:

- credentials are configuration, not code
- secrets are never logged
- AI prompts minimize sensitive content
- raw customer data is not unnecessarily duplicated
- operator actions are attributable where authentication is available

---

## 11. Reliability Boundary

The system should make these boundaries explicit:

```text
source read
schema/profile
mapping approval
transformation
validation
canonical persistence
SCD2 integration
```

A failure in one stage must not falsely report success in another.

Example:

```text
validation failure
```

must not appear as:

```text
successful onboarding
```

Likewise:

```text
SCD2 guardrail HOLD
```

must not appear as:

```text
historical commit
```

---

## 12. AI Failure Behavior

LLM failures should not destroy deterministic functionality where the workflow can reasonably continue.

Possible model failures:

```text
timeout
rate limit
invalid structured output
provider error
unavailable provider
```

The system should:

- validate model output
- fail closed for unsafe mapping execution
- allow deterministic/manual mapping where the workflow supports it
- preserve the run's state
- avoid silently inventing mappings

The project's existing AI provider/fallback conventions should be reused where applicable.

---

## 13. Deployment Architecture

The MVP should use the simplest deployment compatible with the project.

Expected logical deployment:

```text
Streamlit UI
    │
    ├── onboarding services/API
    │
    └── existing SCD2 capability
            │
            ▼
        PostgreSQL
```

The project may retain the existing Docker artifact as a reproducible deployment option.

A future customer private-cloud/VPC deployment can be documented without implementing enterprise networking infrastructure for the demo.

---

## 14. Architectural Reuse from SCD2 Copilot

Reuse from the parent project where confirmed by code:

```text
database connectivity
configuration conventions
FastAPI conventions
Streamlit infrastructure
AI provider/fallback handling
repository patterns
logging/error conventions
testing conventions
generic SCD2 engine
generic history persistence
deterministic guardrails
containment/recovery patterns
```

The reuse boundary should be through explicit interfaces where practical.

Do not couple onboarding to internal SCD2 implementation details unnecessarily.

---

## 15. Architectural Non-Goals

Do not introduce:

- microservices solely for presentation
- a second persistence technology
- message brokers
- CDC infrastructure
- arbitrary code execution
- agentic orchestration
- vector databases
- RAG
- ML anomaly detection

unless a future architecture decision explicitly changes this scope.

---

## 16. Architecture Acceptance Criteria

The target architecture is considered established when:

1. every major capability has a clear owner
2. onboarding and SCD2 have a one-way dependency
3. AI is restricted to assistive tasks
4. transformation is deterministic
5. validation is deterministic
6. mappings are human-approved and versioned
7. exceptions are replayable
8. schema drift identifies mapping impact
9. runs are idempotency-aware
10. historical control remains delegated to the existing SCD2 capability
11. no duplicated SCD2/guardrail implementation is introduced
