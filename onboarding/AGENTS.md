# Customer Data Onboarding — Agent Instructions

## 1. Purpose

This directory contains the **Customer Data Onboarding & Integration Guardrail** bounded context.

The product is a controlled onboarding platform for heterogeneous customer data sources. It profiles incoming source data, proposes semantic mappings with GenAI, requires human approval for uncertain mappings, versions approved mappings, executes transformations deterministically, validates canonical records deterministically, routes invalid records through a replayable exception workflow, detects schema drift and mapping impact, supports idempotent onboarding runs, and feeds validated canonical customer state into the existing SCD2 historical layer.

Primary flow:

Source → Profile → Mapping Proposal → Human Review → Versioned Mapping → Deterministic Transformation → Deterministic Validation → Valid / Exception → Canonical Customer Data → Existing SCD2 → Historical Guardrail

Core principle:

> **AI interprets. Human approves. Deterministic code executes. Validation verifies. SCD2 preserves history. Guardrail controls suspicious historical propagation.**

---

## 2. Scope Boundary

### This bounded context owns

- Customer source ingestion
- CSV and mock REST source adapters
- Schema discovery and profiling
- Data profiling
- Canonical customer contract
- Candidate mapping generation
- GenAI-assisted semantic mapping
- Mapping explanations
- Human mapping approval
- Mapping versioning
- Deterministic transformation
- Deterministic validation
- Validation error classification
- Exception queue
- Exception correction/reprocessing
- Source schema snapshots
- Schema drift detection
- Mapping impact analysis
- Onboarding run lifecycle
- Idempotency
- Onboarding-specific API/UI
- Onboarding-specific persistence and audit metadata

### Existing SCD2 capability owns

- Business-state change detection
- SCD Type 2 transformation
- Temporal correctness
- Historical version management
- SCD2 validation/invariants
- Historical persistence
- Suspicious-change guardrail evaluation
- Containment/hold semantics
- Controlled historical recovery

The onboarding bounded context must **not duplicate or reimplement** these capabilities.

Dependency direction:

**Onboarding → Canonical Data → Existing SCD2**

Do not reverse this dependency.

---

## 3. Source-of-Truth Hierarchy

The parent repository has evolved through multiple milestones and may contain historical documents describing superseded architecture.

When information conflicts, use this order:

1. **Actual current repository code**
2. **Current tests and executable behavior**
3. **Current database schema/migrations**
4. **Latest verified project-state documentation**
5. **Current files under `onboarding/docs/`**
6. **Historical project documents**

Never blindly copy an older architecture into onboarding.

Before deciding that a component exists, inspect the actual current implementation.

Before modifying an existing SCD2 component, determine whether the requirement can be satisfied entirely inside the onboarding boundary.

---

## 4. Existing SCD2 Project Is a Reusable Foundation

The parent repository already contains substantial infrastructure and proven capabilities.

Expected reusable areas, subject to confirmation from actual code:

- PostgreSQL connectivity
- Pydantic / settings conventions
- FastAPI application and routing patterns
- Streamlit infrastructure
- API client conventions
- repository/persistence patterns
- logging and structured error handling
- testing conventions
- AI provider/fallback infrastructure
- generic SCD2 engine
- generic historical persistence
- deterministic guardrail engine
- containment/recovery patterns
- deployment/container conventions

Reuse stable interfaces.

Prefer importing/invoking existing components over copying their implementation.

Do not convert the SCD2 application wholesale into an onboarding application.

---

## 5. Protected Existing Components

The following are protected unless a genuine defect or formally approved architecture change requires modification:

- deterministic SCD2 change-detection logic
- deterministic SCD2 transformation logic
- SCD2 temporal semantics
- SCD2 validation and invariants
- generic historical persistence contracts
- guardrail decision authority
- containment transaction semantics
- checkpoint semantics
- existing authentication/security boundaries
- existing AI fallback safety behavior

Do not modify protected components merely to make onboarding convenient.

When integration requires an adapter, build it in the onboarding boundary.

---

## 6. AI Authority Rules

GenAI is an **assistive component**, not an execution authority.

### AI may

- Interpret source column names
- Compare source metadata with canonical field definitions
- Propose semantic mappings
- Provide confidence estimates
- Explain why a mapping was proposed
- Suggest constrained transformations
- Suggest possible mappings for schema drift
- Suggest possible exception corrections

### AI must not

- Directly write customer data
- Modify source systems
- Commit canonical records
- Decide deterministic data validity
- Override validation failures
- Decide referential integrity
- Automatically approve ambiguous mappings
- Generate arbitrary executable Python for runtime execution
- Generate arbitrary SQL that is executed without deterministic controls
- Decide SCD2 state
- Decide guardrail state
- Decide recovery authorization or outcome

Required rule:

> **AI proposes; deterministic application code executes.**

---

## 7. Human-in-the-Loop Rules

A model-generated mapping is a **proposal** until explicitly approved.

Recommended lifecycle:

DRAFT → REVIEW → APPROVED → IMMUTABLE

Rejected or superseded mappings remain auditable.

Ambiguous or low-confidence mappings must not silently enter transformation.

Mapping approval should identify:

- mapping version
- approving user
- approval time
- changed rules, if any
- source schema version
- canonical schema version

Never silently mutate an approved mapping. Create a new version.

---

## 8. Deterministic Transformation

Use a constrained, inspectable transformation representation.

Initial operations may include:

- TRIM
- LOWERCASE
- UPPERCASE
- CAST
- PARSE_DATE
- NORMALIZE_EMAIL
- MAP_ENUM
- CONCAT

Do **not** execute arbitrary model-generated Python, SQL, shell commands, or other code.

Transformation definitions must be reproducible and testable.

---

## 9. Deterministic Validation

Validation is separate from GenAI.

Validation owns decisions such as:

- required-field checks
- type checks
- format checks
- allowed-value checks
- uniqueness/duplicate checks
- referential-integrity checks where configured
- business-rule checks

Validation results must be machine-readable and traceable to rule identifiers.

Example:

`EMAIL_FORMAT`

is preferable to an unstructured message such as:

`Email looks wrong.`

Never make validation dependent on an LLM response.

---

## 10. Customer Data Handling

Treat input as potentially sensitive customer data.

Prefer giving the model:

- column names
- declared/source types
- canonical field definitions
- profiling statistics
- masked/minimized sample values
- relevant documentation

Avoid sending whole raw customer datasets to the LLM when metadata is sufficient.

Do not log:

- secrets
- API keys
- tokens
- unnecessary PII
- complete raw customer records unless explicitly required and protected

Follow existing secret-management and redaction conventions.

---

## 11. Source Boundary

Initial MVP sources:

- CSV
- Mock REST API

Do not begin with real Salesforce/SAP/ServiceNow connectors.

Source adapters normalize source data into an internal representation. They should not contain mapping, validation, or SCD2 business logic.

---

## 12. Canonical Customer Contract

Initial canonical contract:

- `customer_id` — string, required, unique
- `first_name` — string, required
- `last_name` — string, required
- `email` — string, required
- `date_of_birth` — date, optional
- `status` — enum, required
- `created_at` — datetime, required

Initial status values:

- `ACTIVE`
- `INACTIVE`

Canonical schema is versioned.

Mapping, validation, transformation, and downstream processing must reference an explicit canonical schema version.

Do not silently change the canonical contract.

---

## 13. Mapping Contract

A mapping should explicitly represent at least:

- source field
- target field
- mapping type
- transformation definition
- confidence/proposal metadata
- explanation
- status
- mapping version

Initial mapping types:

- direct mapping
- transformed mapping

Keep derived/complex mappings out of the first implementation unless explicitly approved.

---

## 14. Schema Drift

Schema drift detection must be deterministic.

At minimum detect:

- added column
- removed column
- type change
- nullability change
- possible rename

Separate facts from interpretations.

Example:

- Fact: `signup_date` was removed.
- Fact: `registration_date` was added.
- AI interpretation: `registration_date` may replace `signup_date`.

AI may suggest replacements, but human approval is required before activating a new mapping.

The system must identify which approved mappings are affected by a drift event.

---

## 15. Mapping Versioning

Mapping versioning is **not** SCD2.

Mapping versioning answers:

> Which mapping/transformation configuration was used for this onboarding run?

SCD2 answers:

> How did the canonical business entity's state change over time?

Keep these concepts separate.

Every onboarding run references its exact mapping version.

Approved mapping versions are immutable.

---

## 16. Exception Workflow

Invalid records must not disappear or fail through an opaque error.

An exception should identify, where appropriate:

- exception id
- onboarding run id
- record id
- rule id
- field
- observed value or safe representation
- reason
- suggested fix
- status
- timestamps

Preferred lifecycle:

OPEN → CORRECTED → REPROCESSED → RESOLVED

A dismissal state may exist where the business rule permits it.

AI-suggested fixes are proposals only.

Reprocessing must use deterministic transformation and validation.

The existing SCD2 containment/replay design is an architectural inspiration for controlled recovery, but do not copy its semantics blindly into onboarding.

---

## 17. Idempotency

Define an explicit idempotency boundary.

At minimum consider:

- idempotency key
- request/input fingerprint
- run id
- mapping version
- source schema version

Required behavior:

**same idempotency key + same logical request → same logical onboarding operation**

Reuse of the same key with materially different input must produce an explicit conflict rather than duplicate work.

Do not claim end-to-end idempotency until tested.

---

## 18. SCD2 Integration

The onboarding system ends at:

**validated canonical customer records**

The existing SCD2 capability then consumes those records:

Canonical Customer
→ Existing SCD2 Change Detection
→ Existing SCD2 Transformation
→ Existing SCD2 Validation
→ Existing Guardrail
→ Historical State

Canonical entity key:

`customer_id`

Reuse the existing generic historical mechanism where the current repository supports it.

Do not create a competing SCD2 engine.

Do not duplicate historical semantics inside onboarding.

---

## 19. Guardrail Integration

The guardrail belongs after canonicalization and SCD2 calculation.

Sequence:

Validated canonical batch
→ SCD2 calculation/validation
→ Guardrail
→ NORMAL or SUSPICIOUS

NORMAL:

`commit historical state`

SUSPICIOUS:

`hold historical propagation`

The source transaction is not blocked.

The guardrail protects downstream historical propagation.

AI may explain deterministic guardrail evidence but must not make the guardrail decision.

---

## 20. Architecture Rules

Prefer explicit boundaries:

- source
- profiling
- mapping
- transformation
- validation
- exceptions
- drift/versioning
- orchestration
- SCD2 integration
- API/UI

Avoid microservices unless a real requirement demands them.

Avoid speculative abstractions.

Do not add infrastructure merely to increase apparent complexity.

Use free/open-source or free-tier-compatible tooling consistent with the existing project constraints.

---

## 21. Explicit Non-Goals

Unless separately approved, do not add:

- Kafka
- Debezium
- Spark
- Dask/Ray
- Kubernetes
- a second database technology
- vector databases
- RAG
- agent frameworks
- ML anomaly detection
- arbitrary AI-generated code execution
- real SaaS OAuth integrations
- enterprise multi-tenant billing
- full iPaaS functionality
- unnecessary production PII
- large cloud architecture purely for demonstration

Complexity is not a success criterion.

---

## 22. Repository/Code Workflow

Before implementing any milestone:

1. Inspect the actual repository.
2. Read the relevant onboarding contract documents.
3. Inspect existing parent-repository implementations that will be reused.
4. Verify assumptions against code/tests.
5. Make the smallest correct change.
6. Avoid unrelated refactors.
7. Do not alter working SCD2 behavior to make onboarding convenient.
8. Preserve backward compatibility where shared components are reused.

If a capability already exists, prefer using it through a stable interface.

---

## 23. Testing Rules

Meaningful onboarding capabilities require deterministic tests covering, where applicable:

- source ingestion
- source profiling
- mapping schema validation
- structured AI output validation
- human approval state transitions
- mapping immutability/versioning
- transformation operations
- validation rules
- exception creation
- exception reprocessing
- schema drift detection
- mapping impact analysis
- idempotency
- SCD2 integration
- guardrail integration

Use synthetic, deterministic test data.

For GenAI behavior, evaluate structured outputs against labeled cases.

Never use "AI worked" as a test criterion.

---

## 24. Evaluation Rules

Measure evidence rather than presentation quality.

Recommended metrics:

- mapping accuracy
- mapping review rate
- validation detection coverage
- exception correction/reprocessing success
- schema-drift detection accuracy
- idempotency correctness
- SCD2 correctness
- guardrail correctness
- processing latency

For every benchmark document:

- dataset
- sample size
- ground truth
- measurement method
- limitations

must be explicit.

Do not generalize synthetic benchmark results into universal enterprise claims.

---

## 25. Documentation Rules

Authoritative onboarding documents:

- `onboarding/docs/00_project_context.md`
- `onboarding/docs/01_problem_and_scope.md`
- `onboarding/docs/02_canonical_data_contract.md`
- `onboarding/docs/03_source_and_profiling_contract.md`
- `onboarding/docs/04_mapping_contract.md`
- `onboarding/docs/05_transformation_and_validation_contract.md`
- `onboarding/docs/06_exception_and_reprocessing_model.md`
- `onboarding/docs/07_schema_drift_and_versioning.md`
- `onboarding/docs/08_run_and_idempotency_model.md`
- `onboarding/docs/09_scd2_integration.md`
- `onboarding/docs/10_target_architecture.md`
- `onboarding/docs/11_active_plan.md`
- `onboarding/docs/12_verification_and_evaluation.md`

Read the relevant contract before implementing its domain.

Do not duplicate authoritative rules unnecessarily.

When implementation and documentation disagree, inspect code/tests first and then update the stale documentation.

---

## 26. Milestone Direction

### M0 — Context and architecture
No production code changes.
Finalize contracts and boundaries.

### M1 — Sources and profiling
CSV + mock REST.
Schema/data profiling.
Source snapshots.

### M2 — Mapping
Candidate mappings.
GenAI proposal.
Structured output.
Confidence/explanation.

### M3 — Approval/versioning
Human review.
Approve/reject/edit.
Immutable mapping versions.

### M4 — Transformation/validation
Constrained transformation engine.
Deterministic validation.
Canonical output.

### M5 — Exceptions
Persist exceptions.
Correction.
Reprocessing.
Resolution.

### M6 — Runs/API/idempotency
Run lifecycle.
Idempotency.
Operational API.

### M7 — Schema drift
Schema snapshots.
Diff.
Mapping impact.
Review workflow.

### M8 — SCD2 integration
Feed validated canonical customer data into the existing SCD2 capability.

### M9 — Historical guardrail
Connect the existing guardrail/containment semantics at the historical propagation boundary.

### M10 — Evaluation/deployment/freeze
Benchmark.
Demo.
Documentation.
Deployment.
Final verification.
Freeze.

Do not jump ahead when milestone dependencies are not satisfied.

---

## 27. Final Product Boundary

The completed product must answer two separate questions.

### Onboarding

> **Can we safely understand, normalize, and validate heterogeneous customer data before it enters the platform?**

### Historical control

> **Once canonical customer data enters the platform, can we preserve historical state and prevent suspicious changes from silently becoming historical truth?**

Architectural relationship:

External Customer Data
→ Controlled Onboarding
→ Canonical Customer Data
→ Existing SCD2
→ Historical Guardrail

This relationship is the core reason the existing SCD2 project is being leveraged.

---

## 28. Agent Stop Conditions

Stop and request clarification or create an architecture decision when:

- a requirement conflicts with a protected SCD2 component
- an assumed reusable component cannot be found
- a requirement needs arbitrary LLM code execution
- persistent customer-data exposure to an external model lacks a defined policy
- a new external infrastructure dependency is required
- mapping semantics are ambiguous
- idempotency semantics are ambiguous
- canonical schema semantics are ambiguous
- schema drift would silently change an approved mapping
- implementation scope materially exceeds the current milestone

Do not resolve major architectural ambiguity by guessing.

---

## 29. Completion Standard

A milestone is complete only when:

- intended behavior is implemented
- contracts remain consistent
- deterministic boundaries are enforced
- AI authority is constrained
- persistence is correct
- tests pass
- required integration behavior is verified
- documentation reflects implementation
- no known critical behavior remains unverified

The goal is **trustworthy engineering, not feature count**.
