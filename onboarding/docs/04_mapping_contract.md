# 04 — Mapping Contract

## 1. Purpose

This document defines how source fields are mapped to the canonical customer schema.

The mapping system combines:

```text
deterministic candidate evidence
+
GenAI semantic assistance
+
human approval
+
versioned mapping configuration
```

The system does not permit an LLM to silently establish executable truth.

---

## 2. Mapping Principle

The governing rule is:

> **AI proposes. Human approves. Deterministic code executes.**

A model response is never itself an approved mapping.

The lifecycle is:

```text
DRAFT
  ↓
REVIEW
  ↓
APPROVED
  ↓
IMMUTABLE
```

---

## 3. Mapping Direction

Mappings are directed from:

```text
source field
```

to:

```text
canonical field
```

Example:

```text
cust_no → customer_id
```

A mapping must identify the source schema version and canonical schema version to which it applies.

---

## 4. Mapping Record

A mapping record should represent, at minimum:

```text
mapping_id
source_id
source_schema_version
canonical_schema_version
mapping_version
source_field
target_field
mapping_type
transformation
confidence/proposal metadata
explanation
status
created_at
approved_at
approved_by
```

The final data model may normalize these fields into related tables/entities.

---

## 5. Initial Mapping Types

The initial supported mapping types are:

```text
DIRECT
TRANSFORMED
```

### DIRECT

Source value maps directly to target value.

Example:

```text
cust_no → customer_id
```

### TRANSFORMED

Source value must pass through one or more constrained deterministic operations.

Example:

```text
email_address → email
operations:
    TRIM
    LOWERCASE
```

Do not introduce complex derived mappings into the MVP unless explicitly approved.

---

## 6. Candidate Mapping Generation

Candidate mappings may be generated from deterministic evidence before the LLM is called.

Useful evidence includes:

```text
name similarity
type compatibility
profile characteristics
canonical field semantics
documentation similarity
value-pattern compatibility
```

The exact candidate-scoring algorithm can evolve.

The candidate generator must not claim semantic certainty from name similarity alone.

---

## 7. GenAI Mapping Role

GenAI may evaluate a candidate or propose a mapping using:

```text
source field metadata
source profile
canonical field definition
canonical constraints
available transformation vocabulary
```

The output should be structured rather than free-form.

Conceptual result:

```json
{
  "source_field": "cust_no",
  "target_field": "customer_id",
  "mapping_type": "DIRECT",
  "confidence": 0.98,
  "reason": "The source field name and identifier pattern are consistent with the canonical customer identifier."
}
```

The exact production schema should be implemented with the project's structured-output mechanism.

---

## 8. Confidence

Confidence is a workflow signal, not proof of correctness.

Illustrative policy:

```text
>= 0.90
high confidence

0.70–0.89
medium confidence

< 0.70
low confidence / unresolved
```

The exact thresholds are configurable application policy.

They must not be presented as universal statistical guarantees.

A confidence score cannot override a human approval requirement or deterministic validation.

---

## 9. Mapping Explanation

Every AI-assisted proposal should contain a short reason explaining the proposed mapping.

Example:

```text
Source:
cust_no

Target:
customer_id

Reason:
The source name indicates a customer identifier and the observed values follow an identifier-like pattern.
```

Explanations should describe evidence, not invent source documentation.

---

## 10. Uncertain Mapping Example

Suppose the source contains:

```text
signup_date
```

and the canonical contract contains:

```text
created_at
```

The system may propose:

```text
signup_date → created_at
```

but should explicitly identify semantic uncertainty if the field could instead represent:

```text
account activation
subscription start
registration
```

The operator must be able to review the proposal.

---

## 11. Mapping Approval

A reviewer should be able to:

- inspect source field
- inspect canonical field
- inspect source profile
- inspect confidence
- inspect reasoning
- inspect transformation
- approve
- reject
- change target field
- change allowed transformation

An approved mapping becomes immutable.

A later modification produces a new version.

---

## 12. Mapping Version

A mapping version is tied to:

```text
source schema version
canonical schema version
mapping configuration
approval state
```

Example:

```text
CRM schema v1
customer schema v1
mapping v1
```

If the source schema changes and requires a new mapping:

```text
CRM schema v2
customer schema v1
mapping v2
```

A canonical schema change may produce:

```text
CRM schema v2
customer schema v2
mapping v3
```

Version numbers should not be treated as globally unique across unrelated sources.

---

## 13. Mapping Immutability

Once a mapping version is approved:

```text
APPROVED
```

its executable mapping definition must not be silently modified.

To change:

```text
source field
target field
transformation
canonical field meaning
```

create a new mapping version.

Past onboarding runs must continue to reference the exact version they used.

---

## 14. Mapping Validation

Before a mapping can become executable, validate:

- source field exists
- target field exists in the referenced canonical schema
- mapping type is supported
- transformation operations are allowed
- required canonical fields have a mapping or explicitly approved derivation
- incompatible source/target type relationships are rejected or require an explicit deterministic cast
- no unsupported transformation is present

A syntactically valid LLM response is not necessarily a valid executable mapping.

---

## 15. Required Canonical Field Coverage

The mapping review process must identify whether all required canonical fields are covered:

```text
customer_id
first_name
last_name
email
status
created_at
```

`date_of_birth` is optional.

If a required canonical field has no approved mapping, the onboarding run cannot proceed to successful canonicalization.

---

## 16. Mapping Conflicts

The mapping system should detect conflicts such as:

```text
source field A → target field X
source field B → target field X
```

where multiple source fields are assigned to one canonical field without an explicitly supported transformation.

It should also detect:

```text
same source field → multiple canonical fields
```

unless a supported derived/structured transformation explicitly permits it.

The system must not silently choose one mapping.

---

## 17. Transformation Restrictions

Mapping proposals may reference only transformations supported by the deterministic transformation engine.

Initial operations are defined in:

`05_transformation_and_validation_contract.md`

The LLM must not return arbitrary Python, SQL, shell commands, regular execution code, or external commands.

---

## 18. Human Override

A human reviewer may change an AI-proposed mapping.

Example:

```text
AI:
signup_date → created_at

Reviewer:
Reject

Reviewer decision:
No mapping; source semantics are insufficient.
```

or:

```text
AI:
acct_status → status
```

Reviewer changes:

```text
acct_status → status
mapping:
SUSPENDED → INACTIVE
CLOSED → INACTIVE
```

provided the transformation is represented using an approved deterministic mapping rule.

---

## 19. Mapping Evidence

For auditability, retain evidence sufficient to understand why a mapping was approved.

Useful evidence:

```text
source profile reference
source schema version
canonical schema version
AI proposal
confidence
reason
human decision
approval identity
approval time
final transformation definition
```

Do not retain unnecessary raw customer records solely to explain mapping decisions.

---

## 20. Mapping Failure States

The mapping workflow may encounter:

```text
UNRESOLVED
CONFLICT
INVALID_TARGET
UNSUPPORTED_TRANSFORMATION
MISSING_REQUIRED_MAPPING
REVIEW_REQUIRED
REJECTED
```

These states should be machine-readable.

They must not be collapsed into a generic "AI error."

---

## 21. Mapping Approval Acceptance Criteria

A mapping version is approved only when:

1. source schema version is known
2. canonical schema version is known
3. every executable mapping is structurally valid
4. transformations use the allowed vocabulary
5. required canonical fields are covered
6. conflicts are resolved
7. ambiguous mappings have been reviewed according to policy
8. the final mapping is stored as an immutable version

Only an approved mapping version may execute against customer data.
