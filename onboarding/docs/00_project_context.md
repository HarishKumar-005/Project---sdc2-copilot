# 00 — Project Context

## 1. Project Identity

**Project name:** Customer Data Onboarding & Integration Guardrail

This project is a new bounded context created inside a fork of the existing SCD2 Copilot repository.

The purpose of this project is to solve a recurring enterprise data-onboarding problem:

> A customer needs to send data from one or more source systems into a platform whose canonical schema does not match the customer's source schema. The source may have inconsistent names, types, formats, identifiers, missing documentation, duplicates, missing required values, and future schema changes.

The system provides a controlled onboarding workflow that makes heterogeneous customer data safe and understandable before it enters the platform.

The product is intentionally **not** a generic ETL platform and **not** an autonomous AI data cleaner.

---

## 2. Product Definition

### One-sentence definition

> A controlled customer-data onboarding platform that profiles heterogeneous source data, proposes reviewable semantic mappings using GenAI, versions approved mappings, executes transformations deterministically, validates canonical records deterministically, routes invalid records through a replayable exception workflow, detects schema drift and mapping impact, supports idempotent onboarding runs, and feeds validated canonical customer state into the existing SCD2 historical layer.

### Core product flow

```text
Customer Source
    |
    |  CSV / Mock REST API
    v
Source Adapter
    |
    v
Schema + Data Profiler
    |
    v
Candidate Mapping
    |
    +----------------------+
    |                      |
    |                GenAI assistance
    |                      |
    +----------+-----------+
               |
               v
        Mapping Proposal
               |
               v
        Human Review
               |
               v
       Mapping Version
               |
               v
 Deterministic Transformation
               |
               v
   Deterministic Validation
           /         \
          /           \
       VALID        INVALID
         |             |
         v             v
 Canonical Data   Exception Queue
         |
         v
 Existing SCD2 Capability
         |
         v
 Historical Guardrail
```

The key idea is that the system does not trust a language model with unrestricted execution authority.

---

## 3. Core Engineering Principle

The governing principle of the project is:

> **AI interprets. Human approves. Deterministic code executes. Validation verifies. SCD2 preserves history. Guardrail controls suspicious historical propagation.**

This means:

- The LLM proposes semantic interpretations and mappings.
- Humans approve or modify uncertain mappings.
- Approved mappings become versioned configuration.
- Deterministic application code performs the transformation.
- Deterministic validation decides whether records satisfy the canonical contract.
- The existing SCD2 engine manages historical state.
- The existing guardrail decides whether suspicious changes may automatically become historical state.
- The LLM can explain evidence or propose corrections, but it cannot silently change data or system state.

---

## 4. Why This Project Exists

Enterprise customers rarely send data in exactly the structure expected by a receiving platform.

Typical differences include:

```text
cust_id        vs customer_id
fname          vs first_name
surname        vs last_name
email_addr     vs email
dob            vs date_of_birth
acct_status    vs status
signup_date    vs created_at
```

The problem becomes more difficult when source data also contains:

- inconsistent data types
- multiple date formats
- missing required fields
- duplicate identifiers
- duplicate records
- unexpected categorical values
- inconsistent casing
- incomplete or stale source documentation
- source-specific business terminology
- changing source schemas

A manual onboarding process can become repetitive, slow, difficult to audit, and fragile when source requirements change.

This project turns onboarding into an explicit, versioned workflow.

---

## 5. Target Outcome

The system should allow an operator to take a previously unknown or messy customer source and move it through:

```text
UNDERSTAND
    ↓
MAP
    ↓
REVIEW
    ↓
APPROVE
    ↓
TRANSFORM
    ↓
VALIDATE
    ↓
LOAD
```

with an explicit exception path:

```text
VALIDATION FAILURE
    ↓
EXCEPTION
    ↓
REVIEW / CORRECTION
    ↓
REPROCESS
    ↓
RESOLVE
```

and an explicit schema-change path:

```text
SOURCE SCHEMA V1
       ↓
SOURCE SCHEMA V2
       ↓
DRIFT DETECTION
       ↓
MAPPING IMPACT
       ↓
REVIEW
       ↓
NEW MAPPING VERSION
```

---

## 6. Canonicalization Boundary

The onboarding project is responsible for producing **canonical customer data**.

The canonical representation is intentionally small for the MVP:

```text
customer_id
first_name
last_name
email
date_of_birth
status
created_at
```

The canonical contract itself is versioned.

The complete field definitions, constraints, and semantics are defined in:

`onboarding/docs/02_canonical_data_contract.md`

The onboarding project should not invent additional canonical fields merely to accommodate a source.

Source-specific variation belongs in:

- source metadata
- mapping rules
- deterministic transformation rules
- validation rules

---

## 7. Source Model

The initial implementation should support:

1. **CSV source**
2. **Mock REST API source**

These represent different customer systems without introducing external vendor dependencies.

Examples of conceptual source systems:

```text
CRM
Billing
Support
```

These may be represented by generated CSV fixtures and mock HTTP endpoints.

The first version should **not** depend on real Salesforce, SAP, ServiceNow, Zendesk, or other production vendor integrations.

The abstraction should nevertheless leave room for future source adapters.

---

## 8. Data Profiling

Before mapping, the system should profile the source.

Useful profile information includes:

- source column name
- detected or declared data type
- null count / null rate
- uniqueness characteristics
- cardinality for categorical fields
- date-format patterns
- sample value patterns
- length characteristics
- possible identifier patterns
- possible enumerations

The profiler is deterministic.

Its output becomes evidence for the mapping process.

The profiler should minimize the amount of raw customer data that needs to be sent to an external LLM.

---

## 9. AI-Assisted Mapping

The LLM is used where semantic interpretation is useful.

For each candidate source field, the mapping process can consider:

```text
source field name
source data type
profile statistics
masked/minimized sample patterns
source documentation
canonical field name
canonical type
canonical description
canonical constraints
```

The output is a structured proposal containing at least:

```text
source field
target field
mapping type
transformation
confidence
reason / explanation
```

The proposal is reviewable.

The proposal is not execution authority.

---

## 10. Human Approval

Mappings have an explicit lifecycle:

```text
DRAFT
  ↓
REVIEW
  ↓
APPROVED
  ↓
IMMUTABLE
```

An approved mapping should be identified by:

- source
- source schema version
- canonical schema version
- mapping version
- approval metadata

An approved mapping should not be silently edited.

A change produces a new version.

---

## 11. Deterministic Transformation

After approval, transformation is performed by deterministic application code.

The initial transformation vocabulary should remain deliberately small and reviewable.

Potential operations:

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

No arbitrary Python or arbitrary SQL generated by an LLM should be executed.

Transformation definitions should be inspectable, testable, and reproducible.

---

## 12. Deterministic Validation

Validation operates against the canonical contract.

Initial validation categories include:

```text
REQUIRED
TYPE
FORMAT
ENUM
UNIQUENESS
DUPLICATE
REFERENTIAL_INTEGRITY
BUSINESS_RULE
```

The validator decides whether the resulting canonical record is valid.

The LLM does not override validation.

Each failure should have a deterministic rule identifier and a clear explanation.

Example:

```text
rule_id: EMAIL_FORMAT
field: email
status: INVALID
```

---

## 13. Exception Workflow

Invalid records should become explicit exceptions rather than disappearing or causing an opaque pipeline failure.

A logical exception should be associated with:

```text
exception_id
run_id
record_id
rule_id
field
observed_value / safe representation
reason
suggested_fix
status
timestamps
```

The preferred lifecycle is:

```text
OPEN
 ↓
CORRECTED
 ↓
REPROCESSED
 ↓
RESOLVED
```

AI-generated correction suggestions are proposals only.

The corrected record must still pass deterministic transformation and validation.

---

## 14. Schema Drift

Source schemas are versioned.

For example:

```text
CRM schema v1
CRM schema v2
CRM schema v3
```

Deterministic drift detection should identify at least:

```text
ADDED_COLUMN
REMOVED_COLUMN
TYPE_CHANGED
NULLABILITY_CHANGED
POSSIBLE_RENAME
```

The system should then determine which existing mappings are affected.

Example:

```text
signup_date removed
        ↓
mapping M-006 references signup_date
        ↓
M-006 is affected / broken
```

AI may suggest:

```text
registration_date
```

as a possible semantic replacement, but a new mapping still requires human approval.

---

## 15. Mapping Versioning vs SCD2

These are deliberately separate concepts.

### Mapping versioning

Answers:

> Which source-to-canonical transformation configuration was used for this onboarding run?

### SCD2

Answers:

> How did the canonical business entity's state change over time?

Therefore:

```text
Mapping Version
      ≠
SCD2 Version
```

Do not implement mapping history as a disguised SCD2 table.

---

## 16. Idempotent Onboarding

An onboarding run should have an explicit logical identity.

At minimum, the design should consider:

```text
idempotency_key
input fingerprint / request hash
run_id
source schema version
mapping version
```

The intended behavior is:

```text
same idempotency key
+
same logical request
=
same logical operation
```

A reused key with materially different input should produce an explicit conflict.

The system must not create duplicate canonical records because a request was retried.

---

## 17. Existing SCD2 Capability

The parent SCD2 Copilot repository already contains a deterministic SCD2 capability and generic historical persistence.

The onboarding project should reuse those capabilities after canonicalization.

The intended relationship is:

```text
validated canonical customer records
                ↓
        existing SCD2 engine
                ↓
        SCD2 validation
                ↓
        existing guardrail
                ↓
         historical state
```

The existing generic historical structure supports entity-specific keys and attributes through reusable persistence. The current implementation uses concepts including `source_name`, `entity_key`, `attributes`, `effective_from`, `effective_to`, and `is_current`. 

For the onboarding use case, the logical entity key is:

```text
customer_id
```

Do not create a second SCD2 engine.

---

## 18. Existing Guardrail Capability

The existing SCD2 guardrail is useful at the final historical-propagation boundary.

After the onboarding batch becomes canonical data:

```text
canonical batch
    ↓
SCD2 computation
    ↓
validation
    ↓
guardrail
```

The guardrail may produce:

```text
NORMAL
```

or:

```text
SUSPICIOUS
```

A suspicious batch should be held before historical propagation according to the existing guardrail/containment contract.

The source system itself is not transactionally blocked by this mechanism.

The guardrail protects downstream historical state.

This preserves the proven principle from the original SCD2 project:

> **The engine decides. Validation protects. AI explains.**

---

## 19. Existing Engineering Foundation

The parent repository has already established substantial engineering infrastructure, including, where confirmed in the current code:

- PostgreSQL/Supabase connectivity
- Pydantic configuration patterns
- FastAPI operational boundaries
- Streamlit UI infrastructure
- repository/persistence conventions
- structured error handling
- authentication/security boundaries
- AI provider/fallback infrastructure
- deterministic SCD2 processing
- generic SCD2 history persistence
- deterministic guardrails
- containment/recovery
- testing and integration-test patterns
- container/deployment conventions

This infrastructure should be reused where appropriate.

The onboarding project should not rewrite the parent application's stable core simply to obtain these capabilities.

---

## 20. Project Technology Direction

The project should remain compatible with the existing repository's established technology unless a real requirement justifies a change.

Expected primary technologies:

```text
Python
Pydantic
PostgreSQL
FastAPI
Streamlit
Pytest
Existing AI provider infrastructure
Existing deterministic SCD2 engine
```

For dataframe/data-processing operations, reuse the parent project's proven approach where appropriate rather than introducing another analytical engine without need.

The project should remain free/open-source or free-tier compatible for development and demonstration.

---

## 21. What This Project Is Not

This project is not intended to become:

- a full iPaaS
- a generic ETL replacement
- a generic CSV cleaner
- a chatbot
- a generic RAG application
- an autonomous data-cleaning agent
- an ML anomaly detector
- a CDC platform
- a message-broker system
- a real Salesforce/SAP integration suite
- a multi-cloud enterprise control plane
- a Kubernetes demonstration
- an arbitrary code-generation/execution system

The objective is controlled customer-data onboarding.

---

## 22. Primary Demo Story

The final demo should demonstrate one coherent story.

### Scenario A — New source onboarding

```text
Upload CRM sample
    ↓
Profile source
    ↓
Generate mapping
    ↓
Review ambiguous field
    ↓
Approve mapping v1
    ↓
Transform
    ↓
Validate
    ↓
Valid + exception records
```

### Scenario B — Reprocessing

```text
Open exception
    ↓
Apply/correct deterministic transformation
    ↓
Reprocess
    ↓
Validation succeeds
    ↓
Exception resolved
```

### Scenario C — Schema drift

```text
CRM v1
    ↓
customer changes source schema
    ↓
CRM v2
    ↓
drift detected
    ↓
affected mapping identified
    ↓
replacement suggested
    ↓
human approval
    ↓
mapping v2
```

### Scenario D — Historical protection

```text
Validated canonical customer batch
    ↓
SCD2
    ↓
Guardrail
    ├── NORMAL → historical commit
    └── SUSPICIOUS → hold
```

---

## 23. Success Definition

The project succeeds when it demonstrates that a previously unfamiliar customer dataset can move through a **reviewable, deterministic, traceable, repeatable onboarding workflow** without allowing GenAI to silently alter the customer data or bypass data-quality controls.

The strongest implementation is not the one with the most features.

It is the one with the clearest boundaries and strongest evidence that each boundary works.

---

## 24. Authoritative Follow-On Documents

Detailed rules are intentionally split into separate documents:

- `01_problem_and_scope.md`
- `02_canonical_data_contract.md`
- `03_source_and_profiling_contract.md`
- `04_mapping_contract.md`
- `05_transformation_and_validation_contract.md`
- `06_exception_and_reprocessing_model.md`
- `07_schema_drift_and_versioning.md`
- `08_run_and_idempotency_model.md`
- `09_scd2_integration.md`
- `10_target_architecture.md`
- `11_active_plan.md`
- `12_verification_and_evaluation.md`

This document explains **what the project is**.

The other documents define the exact contracts and implementation expectations.
