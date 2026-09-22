# 11 — Active Implementation Plan

## 1. Plan Purpose

This is the active implementation plan for the Customer Data Onboarding & Integration Guardrail.

The implementation must proceed in dependency order.

The objective is to build a complete, trustworthy vertical slice rather than a large number of loosely connected features.

---

# M0 — Context and Architecture Freeze

## Objective

Finalize the onboarding contracts and establish the boundary between onboarding and the existing SCD2 capability.

## Required work

- Read all onboarding contract documents.
- Inspect the actual parent repository.
- Identify reusable infrastructure from the existing SCD2 project.
- Confirm the actual current database/API/UI conventions.
- Confirm the integration boundary to the existing SCD2 engine.
- Confirm the canonical customer schema.
- Confirm the source adapter abstraction.
- Confirm mapping lifecycle.
- Confirm run/idempotency semantics.

## Deliverables

```text
onboarding/AGENTS.md
docs/00_project_context.md
docs/01_problem_and_scope.md
docs/02_canonical_data_contract.md
docs/03_source_and_profiling_contract.md
docs/04_mapping_contract.md
docs/05_transformation_and_validation_contract.md
docs/06_exception_and_reprocessing_model.md
docs/07_schema_drift_and_versioning.md
docs/08_run_and_idempotency_model.md
docs/09_scd2_integration.md
docs/10_target_architecture.md
docs/11_active_plan.md
docs/12_verification_and_evaluation.md
```

## Gate

**No production onboarding implementation begins until the domain boundaries are clear and the actual reusable parent components have been inspected.**

---

# M1 — Source Adapters and Profiling

## Objective

Accept heterogeneous source data and produce deterministic source/schema/data profiles.

## Scope

Support:

```text
CSV
Mock REST API
```

Implement:

- source abstraction
- CSV adapter
- mock REST adapter
- schema snapshot
- schema fingerprint
- data profiler
- profile persistence/artifact
- deterministic profiling tests

## Demo

```text
Upload CRM-like source
        ↓
Schema detected
        ↓
Profile displayed
```

Expected profile information:

```text
columns
types
null rates
uniqueness
value patterns
date patterns
sample metadata
```

## Gate

Given the same source:

```text
same input
+
same profiling configuration
```

the profile should be reproducible.

No mapping or AI implementation should be required to pass this milestone.

---

# M2 — Mapping Proposal Engine

## Objective

Produce structured, reviewable source-to-canonical mapping proposals.

## Scope

Implement:

- deterministic candidate generation
- mapping context construction
- LLM mapping proposal
- structured model output
- output schema validation
- confidence
- explanation
- unsupported-output rejection

## Critical constraints

The model cannot:

- execute code
- mutate data
- write to the database
- approve itself
- bypass mapping validation

## Demo

Example:

```text
cust_no       → customer_id       98%
fname         → first_name        97%
email_address → email             99%
signup_date   → created_at        74% REVIEW
```

## Gate

A model-generated proposal must remain a proposal until explicit approval.

Malformed/unsafe model output must not become an executable mapping.

---

# M3 — Human Approval and Mapping Versioning

## Objective

Convert proposals into immutable executable mapping versions through explicit human review.

## Scope

Implement:

- mapping review UI/API
- approve
- reject
- edit
- mapping version creation
- immutable approved version
- approval audit
- source/canonical version references

## Gate

An unapproved mapping cannot execute.

An approved mapping cannot be silently modified.

A modification creates a new mapping version.

---

# M4 — Deterministic Transformation and Validation

## Objective

Transform approved source data into canonical customer records and validate them deterministically.

## Scope

Implement the constrained transformation DSL:

```text
TRIM
LOWERCASE
UPPERCASE
CAST
PARSE_DATE
NORMALIZE_EMAIL
MAP_ENUM
CONCAT
```

Implement validation:

```text
REQUIRED
TYPE
FORMAT
ENUM
UNIQUE
DUPLICATE
REFERENTIAL_INTEGRITY where configured
BUSINESS_RULE
```

## Demo

```text
10,000 input records

9,842 valid
158 invalid
```

## Gate

Same:

```text
input
+
mapping version
+
canonical schema version
```

must produce the same canonical result.

No arbitrary LLM-generated code may execute.

---

# M5 — Exception Queue and Reprocessing

## Objective

Provide an actionable record-level exception workflow.

## Scope

Implement:

- exception persistence
- exception details
- correction representation
- deterministic reprocessing
- resolution
- audit trail

Lifecycle:

```text
OPEN
 ↓
CORRECTED
 ↓
REPROCESSED
 ↓
RESOLVED
```

## Demo

```text
EMAIL_FORMAT
    ↓
exception
    ↓
operator correction
    ↓
reprocess
    ↓
valid
    ↓
resolved
```

## Gate

A failed record cannot be marked resolved without successful deterministic reprocessing or an explicitly supported dismissal path.

---

# M6 — Onboarding Runs, API, and Idempotency

## Objective

Turn the individual processing stages into a reliable onboarding operation.

## Scope

Implement:

- run lifecycle
- run persistence
- idempotency key
- input/request fingerprint
- conflict detection
- onboarding operational API
- run history

Expected API categories:

```text
sources
profiles
mappings
runs
exceptions
```

Exact paths should follow repository conventions.

## Gate

The same logical request repeated with the same idempotency identity must not create duplicate side effects.

The same idempotency key with different logical input must produce an explicit conflict.

Concurrent identical requests must not create duplicate logical operations.

---

# M7 — Schema Drift and Mapping Impact

## Objective

Detect source-schema changes and show exactly which approved mappings are affected.

## Scope

Implement:

- schema snapshot comparison
- added-column detection
- removed-column detection
- type-change detection
- nullability-change detection where represented
- possible-rename evidence
- mapping impact analysis
- compatibility state
- review workflow
- new mapping version creation

## Demo

```text
CRM v1
   ↓
CRM v2

Removed:
signup_date

Added:
registration_date

Affected:
signup_date → created_at

Status:
BROKEN / REVIEW_REQUIRED
```

AI may suggest a replacement but cannot silently modify mapping v1.

## Gate

A broken approved mapping cannot execute against an incompatible source schema.

---

# M8 — SCD2 Integration

## Objective

Feed validated canonical customer records into the existing SCD2 capability.

## Scope

Implement:

- onboarding → canonical batch boundary
- integration adapter
- customer_id business-key mapping
- use of existing generic SCD2 capability
- historical inspection

## Demonstration

```text
Customer
CUST-1001

status:
ACTIVE → INACTIVE

        ↓

SCD2 version 1 closed
SCD2 version 2 current
```

## Gate

No duplicate SCD2 implementation is introduced.

Existing temporal semantics remain unchanged.

Mapping changes are not incorrectly interpreted as customer business-state changes.

---

# M9 — Historical Guardrail Integration

## Objective

Use the existing SCD2 guardrail to prevent suspicious canonical batches from silently becoming historical truth.

## Scope

Integrate:

```text
validated canonical batch
      ↓
existing SCD2
      ↓
existing guardrail
```

Expected outcomes:

```text
NORMAL
    ↓
historical commit
```

or:

```text
SUSPICIOUS
    ↓
hold
    ↓
evidence
    ↓
existing recovery workflow
```

## Gate

A suspicious batch must not mutate downstream historical state before recovery.

AI remains explanation-only.

Do not create a second onboarding-specific anomaly detector.

---

# M10 — Evaluation, Demo, Deployment, and Freeze

## Objective

Produce evidence that the system works and is understandable to an interviewer/operator.

## Evaluation

Measure at minimum:

```text
mapping accuracy
mapping review rate
validation detection coverage
exception correction/reprocessing success
schema-drift detection accuracy
idempotency correctness
SCD2 correctness
guardrail correctness
processing latency
```

Use deterministic synthetic datasets with known ground truth.

Document methodology and limitations.

## Demo

Final demo sequence:

```text
1. Select CRM-like messy source
2. Profile
3. Generate mapping proposal
4. Review uncertain mapping
5. Approve mapping v1
6. Run transformation
7. Show validation results
8. Show exception
9. Reprocess corrected exception
10. Show canonical customer state
11. Show SCD2 historical version
12. Demonstrate schema drift
13. Show affected mapping
14. Approve mapping v2
15. Demonstrate suspicious historical change → HOLD
```

## Deployment

Use the simplest working deployment path supported by the existing repository.

Retain Docker as a reproducible artifact where already supported.

Do not claim live production deployment without live verification.

## Final Gate

The project is frozen only when:

- all critical flows are verified
- tests are passing
- AI authority boundaries are enforced
- mappings are immutable after approval
- exceptions reprocess correctly
- idempotency is tested
- schema drift is tested
- SCD2 integration is tested
- guardrail integration is tested
- documentation reflects implementation
- no critical verification item remains unverified

### Status: COMPLETE & FROZEN (2026-09-19)
- Comprehensive evaluation engine: `src/scd2_copilot/onboarding/evaluation/`
- Labeled ground truth datasets: Dataset A (Clean), Dataset B (Moderately Messy), Dataset C (Highly Messy)
- Authoritative evaluation artifact: `data/onboarding_runs/m10_evaluation_report.json`
- Security and privacy verification: Credentials sanitized, customer PII masked, pure DSL code execution resistance, SQL parameterization.
- Complete 18-step demonstration verified end-to-end.
- Entire onboarding test suite: 197 passed with 0 failures. Full suite clean.


---

# 2. Dependency Order

The implementation dependencies are:

```text
M0
 ↓
M1
 ↓
M2
 ↓
M3
 ↓
M4
 ↓
M5
 ↓
M6
 ↓
M7
 ↓
M8
 ↓
M9
 ↓
M10
```

Some test/documentation work may run in parallel, but milestone gates should not be skipped.

---

# 3. Feature Priority

## P0 — Required

```text
CSV source
profiling
canonical schema
mapping proposal
human approval
mapping version
deterministic transformation
deterministic validation
exception queue
reprocessing
idempotency
schema drift
SCD2 integration
guardrail integration
```

## P1 — Useful

```text
mock REST source
stronger profiling
additional validation rules
mapping metrics
drift visualizations
run analytics
```

## P2 — Defer Unless Required

```text
additional vendor connectors
advanced derived mappings
complex referential-integrity graphs
bulk automation features
enterprise multi-tenancy
```

---

# 4. Scope Protection

Do not add features merely because they appear "enterprise."

A proposed feature must have a clear connection to:

```text
onboarding ambiguity
data quality
schema evolution
reliability
historical correctness
```

Otherwise defer it.

---

# 5. Parent SCD2 Protection

The following existing capabilities are not to be rewritten as part of ordinary onboarding milestones:

```text
SCD2 change detection
SCD2 transformation
SCD2 temporal semantics
SCD2 validation/invariants
generic history persistence
guardrail decision logic
containment semantics
checkpoint semantics
```

Integration should happen at an explicit boundary.

---

# 6. Milestone Reporting Format

At the completion of every milestone, report:

```text
Milestone:
Status:

Files added:
Files modified:

Capabilities implemented:

Reusable parent components used:

Tests executed:
Exact results:

Manual/live verification:

Known limitations:

Remaining work:
```

Never report unexecuted verification as complete.

---

# 7. Definition of Done

A milestone is done only when:

```text
implementation
+
tests
+
integration verification where required
+
documentation
```

are aligned.

Passing tests alone are not sufficient if the intended end-to-end behavior has not been verified.
