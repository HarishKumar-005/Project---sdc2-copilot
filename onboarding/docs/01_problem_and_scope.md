# 01 — Problem and Scope

## 1. Problem Statement

Enterprise customer onboarding often requires integrating data from systems that were designed independently.

A customer may provide data from CRM, billing, support, or other operational systems while the receiving platform expects a different canonical schema.

A representative mismatch looks like:

```text
Customer source                 Canonical platform
----------------------------------------------------
cust_no                         customer_id
fname                           first_name
lname                           last_name
email_address                   email
dob                             date_of_birth
acct_status                     status
signup_date                     created_at
```

Schema differences alone are manageable. The operational problem becomes harder when the incoming data also contains:

- missing fields
- inconsistent identifiers
- duplicate records
- inconsistent types
- different date formats
- invalid categorical values
- missing required values
- incomplete documentation
- source-specific terminology
- source schema changes after onboarding

The onboarding process therefore has two related problems:

### Problem A — Initial onboarding

How can a platform understand a customer's source data and transform it into a canonical structure without requiring extensive manual implementation for every customer?

### Problem B — Ongoing onboarding stability

After a source has been integrated, how can the platform detect when the source changes in a way that invalidates previously approved mappings or produces unsafe downstream historical changes?

---

## 2. Product Goal

Build a controlled onboarding workflow that makes heterogeneous customer data:

```text
understandable
reviewable
transformable
validatable
traceable
reprocessable
versioned
```

The system should reduce the amount of repetitive manual integration work while preserving deterministic control over data execution.

The target is not "zero human involvement."

The target is:

> **Use automation and GenAI to reduce repetitive interpretation work while keeping important data decisions reviewable and deterministic.**

---

## 3. Core User

The primary user is an operator, implementation engineer, data engineer, or similar technical user responsible for onboarding a customer into a receiving data platform.

The user needs to:

1. provide a representative source
2. understand the source structure
3. map it to the canonical customer model
4. review uncertain mappings
5. approve a versioned mapping
6. run the transformation
7. inspect validation results
8. resolve failed records
9. reprocess corrected records
10. understand future source-schema changes
11. preserve historical customer state safely

---

## 4. User Pain Points

### 4.1 Manual field mapping

Different customers use different names for the same business concept.

Examples:

```text
cust_id
customer_no
acct_ref
user_id
```

may all represent some form of customer identifier.

The mapping work is repetitive but still requires contextual reasoning.

### 4.2 Incomplete requirements

Customer documentation may not fully describe:

- field meanings
- data formats
- allowed values
- requiredness
- historical semantics

The system should make uncertainty visible rather than hiding it.

### 4.3 Data-quality failures

A source may look structurally valid but contain records that cannot satisfy the canonical contract.

Examples:

```text
missing customer_id
duplicate customer_id
invalid email
invalid date
unknown status
```

These should become actionable exceptions.

### 4.4 Schema changes

A source that worked yesterday may change tomorrow.

Examples:

```text
cust_no → customer_number
signup_date removed
registration_date added
status type changed
```

A system that only checks the initial source misses this ongoing integration risk.

### 4.5 Retry/replay behavior

Integration requests can be retried.

A repeated request must not silently create duplicate canonical data.

---

## 5. Product Scope

### In scope

#### Source ingestion

- CSV input
- Mock REST API input
- source metadata capture
- source schema snapshots

#### Profiling

- schema inspection
- type information
- nullability/null rates
- uniqueness characteristics
- categorical/value patterns
- date-format detection
- sample/value-pattern profiling

#### Mapping

- candidate source-to-canonical mappings
- GenAI semantic mapping assistance
- mapping confidence
- mapping explanation
- structured mapping output
- human review
- mapping approval
- immutable mapping versions

#### Transformation

- deterministic transformation execution
- constrained transformation operations
- reproducible canonicalization

#### Validation

- required-field validation
- type validation
- format validation
- enumeration validation
- duplicate/uniqueness checks
- referential-integrity checks where configured
- business rules where configured

#### Exceptions

- record-level exceptions
- rule-level reasons
- suggested fixes
- correction workflow
- deterministic reprocessing
- resolution tracking

#### Schema evolution

- source schema versioning
- deterministic drift detection
- mapping impact analysis
- proposed replacement mappings
- human re-approval

#### Reliability

- onboarding run tracking
- idempotency
- input/request fingerprinting
- retry-safe processing

#### Downstream historical state

- integration with the existing SCD2 engine
- use of existing historical persistence where appropriate
- existing guardrail integration
- existing containment/recovery semantics where appropriate

#### Interfaces

- onboarding API
- operator-oriented Streamlit workflow
- run/exceptions/mapping/drift inspection

---

## 6. Explicitly Out of Scope

The following are not part of the initial product:

### External enterprise connectors

Do not implement real:

- Salesforce
- SAP
- ServiceNow
- Zendesk
- Workday
- Oracle SaaS

Use CSV and mock REST APIs to represent source systems.

### Streaming/CDC infrastructure

Do not introduce:

- Kafka
- Debezium
- Kafka Connect
- event-stream infrastructure

The project is an onboarding/integration workflow, not a CDC platform.

### Full iPaaS functionality

Do not implement:

- arbitrary connector marketplace
- workflow marketplace
- customer billing
- enterprise tenancy model
- massive connector catalog

### Autonomous AI execution

Do not implement:

- autonomous agents with unrestricted tools
- LLM-generated Python execution
- LLM-generated SQL execution without deterministic control
- automatic approval of ambiguous mappings

### ML anomaly detection

Do not build an ML anomaly detector.

The existing SCD2 guardrail remains deterministic.

### Real private-cloud deployment

The project may document how the design could move into a customer VPC/private cloud, but the student/demo implementation does not need to provision a full enterprise private-cloud environment.

---

## 7. Non-Negotiable Boundaries

### Boundary 1 — AI

```text
AI = semantic assistance
```

Not:

```text
AI = data execution authority
```

### Boundary 2 — Transformation

```text
approved mapping
       ↓
deterministic transformation
```

Not:

```text
LLM-generated code
       ↓
execution
```

### Boundary 3 — Validation

```text
deterministic rules
       ↓
valid / invalid
```

Not:

```text
LLM opinion
       ↓
valid / invalid
```

### Boundary 4 — Historical state

```text
Onboarding
       ↓
canonical data
       ↓
existing SCD2
```

Do not implement another SCD2 engine inside onboarding.

### Boundary 5 — Guardrail

The guardrail protects downstream historical propagation.

It does not block the original customer source transaction.

---

## 8. MVP Canonical Customer Scope

The first canonical customer contract contains:

```text
customer_id
first_name
last_name
email
date_of_birth
status
created_at
```

Initial status values:

```text
ACTIVE
INACTIVE
```

The canonical schema is versioned.

More fields can be added later through an explicit schema-version change, not by silently modifying the initial contract.

---

## 9. MVP Source Scenarios

Use at least three intentionally different source schemas.

### CRM-like source

```text
cust_no
fname
lname
email_address
dob
acct_status
signup_date
```

### Billing-like source

```text
account_id
given_name
surname
email
birth_date
customer_state
registered_on
```

### Support-like source

```text
user_ref
first
family
contact
birthdate
state
created
```

The purpose is not to simulate the exact schemas of real vendors.

The purpose is to create controlled heterogeneous source structures that demonstrate the onboarding problem.

---

## 10. Intentional Data Problems

The demo/test data should include deliberately messy conditions such as:

```text
missing customer identifiers
duplicate customer identifiers
duplicate emails
invalid dates
multiple date formats
invalid status values
mixed casing
leading/trailing whitespace
inconsistent source naming
ambiguous date semantics
```

The data problems should be deterministic so that tests and demonstrations are reproducible.

---

## 11. Ambiguity Policy

Not every semantic mapping should be automatically accepted.

Example:

```text
signup_date → created_at
```

may be less certain than:

```text
cust_no → customer_id
```

The system should be capable of routing mappings based on configured confidence/review policy.

Illustrative policy:

```text
high confidence
    → eligible for straightforward review/approval

medium confidence
    → explicit human review

low confidence
    → unresolved/manual mapping
```

The exact confidence thresholds are application policy and must not be treated as universal truth about LLM confidence.

---

## 12. Scope of Human Review

Human review is required when the system cannot establish a sufficiently clear mapping or transformation under the configured policy.

The reviewer must be able to:

- inspect source field
- inspect target field
- inspect explanation
- inspect confidence/proposal metadata
- approve
- reject
- modify the target
- modify the constrained transformation

Approved mappings are versioned.

---

## 13. Exception Scope

The exception queue is record-level.

A validation failure should identify:

```text
record
field
rule
reason
```

The initial system should support:

```text
view exception
understand reason
make deterministic correction
reprocess
resolve
```

The project does not need a sophisticated case-management system.

---

## 14. Schema Drift Scope

Schema drift focuses on changes that can affect mappings.

Initial drift categories:

```text
column added
column removed
type changed
nullability changed
possible rename
```

The system should answer:

> Which existing mapping rules are affected by this change?

That question is more important than simply displaying:

```text
SCHEMA CHANGED
```

---

## 15. SCD2 Scope

The SCD2 integration should demonstrate one clear downstream use case:

```text
validated canonical customer
        ↓
historical state
```

For example:

```text
customer status:
ACTIVE
    ↓
INACTIVE
```

produces a new historical version using the existing SCD2 implementation.

The project should preserve:

```text
effective_from
effective_to
is_current
```

semantics from the parent implementation.

---

## 16. Guardrail Scope

The onboarding project should demonstrate that validated canonical changes can still be operationally significant.

Example:

```text
normal change
    ↓
SCD2
    ↓
guardrail
    ↓
NORMAL
    ↓
historical commit
```

and:

```text
large/suspicious batch
    ↓
SCD2
    ↓
guardrail
    ↓
SUSPICIOUS
    ↓
hold before historical propagation
```

Use the existing guardrail rules and semantics.

Do not invent a new ML risk system.

---

## 17. What the Project Should Prove

The implementation should prove five things.

### Proof 1 — Heterogeneous input can be interpreted

Different source schemas can be mapped to the same canonical contract.

### Proof 2 — AI assistance is controlled

The system uses GenAI for semantic suggestions but keeps the final mapping decision reviewable.

### Proof 3 — Data execution is deterministic

Approved mappings produce reproducible transformation results.

### Proof 4 — Failures are operationally manageable

Invalid records become visible exceptions that can be corrected and reprocessed.

### Proof 5 — Future source changes are detectable

The system identifies schema changes and shows which mappings are affected.

The downstream SCD2/guardrail integration proves an additional property:

### Proof 6 — Canonical changes can be historically controlled

Validated customer changes can be preserved in historical state without allowing suspicious batches to silently propagate.

---

## 18. Scope Control Rule

When considering a new feature, ask:

1. Does it directly reduce onboarding ambiguity, data-quality risk, schema-change risk, or reliability risk?
2. Can the requirement be satisfied using an existing component?
3. Can the capability be implemented inside the current bounded context?
4. Does it materially improve the final demo or evaluation?
5. Does it introduce a new external dependency?

If the feature does not materially improve the core workflow, defer it.

The project is judged by the quality of the workflow, not by feature count.

---

## 19. Final Product Boundary

The complete system should be understandable as:

```text
HETEROGENEOUS CUSTOMER DATA
          ↓
       ONBOARDING
          ↓
   CANONICAL CUSTOMER
          ↓
        SCD2
          ↓
     HISTORICAL STATE
          ↓
      GUARDRAIL
          ↓
 NORMAL / SUSPICIOUS
```

with two control flows:

```text
SCHEMA CHANGE
    ↓
DRIFT
    ↓
MAPPING IMPACT
    ↓
REVIEW
```

and:

```text
VALIDATION FAILURE
    ↓
EXCEPTION
    ↓
CORRECTION
    ↓
REPROCESS
```

This is the final scope boundary for the first complete implementation.
