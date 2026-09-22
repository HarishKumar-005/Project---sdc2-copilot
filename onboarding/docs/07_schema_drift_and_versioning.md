# 07 — Schema Drift and Versioning

## 1. Purpose

This document defines how the onboarding platform represents schema versions, detects source changes, identifies affected mappings, and safely evolves mappings without silently changing previously approved behavior.

The core problem is:

> A source that was successfully onboarded can change later, potentially invalidating an approved mapping.

The system must detect the change and explain its impact before silently processing incompatible data.

---

## 2. Versioning Domains

The system intentionally separates three version concepts:

```text
Source Schema Version
        ≠
Canonical Schema Version
        ≠
Mapping Version
```

They evolve independently.

---

## 3. Source Schema Version

A source schema version represents the structure observed from one source system.

Conceptual identity:

```text
source_id
schema_version
schema_hash
observed_at
schema_definition
```

Example:

```text
crm_customer
schema v1
```

Later:

```text
crm_customer
schema v2
```

---

## 4. Canonical Schema Version

A canonical schema version represents the receiving platform's contract.

Initial:

```text
customer.v1
```

A canonical contract change creates a new version.

For example:

```text
customer.v1
→ customer.v2
```

A source schema change does not automatically change the canonical schema.

The canonical contract is defined in:

`02_canonical_data_contract.md`

---

## 5. Mapping Version

A mapping version describes how a particular source schema is translated into a particular canonical schema.

A mapping version references:

```text
source_id
source_schema_version
canonical_schema_version
mapping_version
mapping_rules
approval metadata
```

Example:

```text
CRM v1
+
Customer v1
+
Mapping v1
```

---

## 6. Schema Fingerprint

The system should derive a deterministic schema representation before calculating a fingerprint/hash.

The normalized representation should include at least:

```text
column name
normalized data type
nullability information where relevant
```

Additional structural information may be included where useful.

Column ordering should not cause two semantically identical schemas to be incorrectly classified as different unless ordering is explicitly meaningful to the source contract.

The hashing implementation is an engineering detail, but equivalent normalized schemas should produce the same fingerprint.

---

## 7. Drift Detection

Compare the latest source schema with the previous accepted/observed version.

Initial drift categories:

```text
ADDED_COLUMN
REMOVED_COLUMN
TYPE_CHANGED
NULLABILITY_CHANGED
POSSIBLE_RENAME
```

A drift event should identify:

```text
source
previous schema
new schema
change category
affected field(s)
```

---

## 8. Added Column

Example:

```text
V1:
cust_no
fname
lname

V2:
cust_no
fname
lname
segment
```

The change is:

```text
ADDED_COLUMN: segment
```

An added column does not automatically require a mapping change.

If the field is not needed by the canonical contract, it may remain unmapped.

---

## 9. Removed Column

Example:

```text
V1:
signup_date

V2:
registration_date
```

The system detects:

```text
REMOVED_COLUMN: signup_date
ADDED_COLUMN: registration_date
```

It then checks approved mappings.

If:

```text
signup_date → created_at
```

exists in mapping v1, the mapping is affected.

---

## 10. Type Changed

Example:

```text
dob
TEXT
→
DATE
```

This is a schema change.

The mapping might still be compatible.

The system should classify whether:

```text
type change is compatible
```

or:

```text
type change affects transformation assumptions
```

Do not automatically mark every type change as catastrophic.

---

## 11. Nullability Changed

Example:

```text
email
nullable = true
→
nullable = false
```

or vice versa.

A nullability change may affect validation or source expectations.

The drift engine should make the effect visible.

---

## 12. Possible Rename

A rename is difficult because a source often reports only:

```text
one field removed
one field added
```

A possible rename is therefore an interpretation.

Deterministic evidence may include:

```text
type compatibility
similar profile
similar value patterns
similar documentation
name similarity
```

GenAI may then propose:

```text
registration_date
```

as a possible replacement for:

```text
signup_date
```

but this is a proposal, not a fact.

---

## 13. Drift Impact Analysis

The key output of drift detection is:

> Which approved mappings are affected?

Example:

```text
Source:
CRM

Schema change:
signup_date removed

Affected mapping:
M-006

Mapping:
signup_date → created_at

Status:
BROKEN
```

This is more useful than a generic:

```text
SCHEMA DRIFT DETECTED
```

---

## 14. Mapping Compatibility States

A mapping associated with a new source schema may be:

```text
COMPATIBLE
COMPATIBLE_WITH_REVIEW
BROKEN
```

### COMPATIBLE

No meaningful mapping dependency was broken.

### COMPATIBLE_WITH_REVIEW

The source changed, but existing mappings may remain usable and should be reviewed according to policy.

### BROKEN

At least one required mapping dependency is no longer valid.

The exact state must be determined deterministically.

---

## 15. Drift Handling Workflow

Preferred workflow:

```text
Observe source
      ↓
Build schema snapshot
      ↓
Compare with previous version
      ↓
Detect drift
      ↓
Impact analysis
      ↓
Existing mapping:
    compatible / review / broken
      ↓
Human review where needed
      ↓
AI assistance for semantic replacement
      ↓
Create new mapping version
      ↓
Approve
      ↓
Resume onboarding
```

Do not automatically mutate an approved mapping in place.

---

## 16. Mapping Version Creation

A source-schema change requiring mapping modification creates a new mapping version.

Example:

```text
CRM v1
Customer v1
Mapping v1
```

becomes:

```text
CRM v2
Customer v1
Mapping v2
```

The original mapping remains auditable.

---

## 17. Canonical Schema Change

If the receiving canonical contract changes:

```text
Customer v1
→
Customer v2
```

a compatible mapping must be created against the new canonical version.

This should not rewrite history for older onboarding runs.

Historical runs remain associated with:

```text
their source schema version
their canonical schema version
their mapping version
```

---

## 18. Run Lineage

Every completed onboarding run should be traceable to:

```text
source_id
source_schema_version
canonical_schema_version
mapping_version
```

Optionally also:

```text
input fingerprint
```

This allows an operator to answer:

> Which mapping transformed this customer's data?

---

## 19. Drift and Active Processing

Before processing a source whose schema has changed:

1. observe the source schema
2. calculate its version/fingerprint
3. compare against the expected schema
4. determine mapping compatibility
5. stop or route for review when the required mapping is broken

The system must not continue using an obsolete mapping against an incompatible source schema simply because that mapping was previously approved.

---

## 20. No Silent Mapping Mutation

This is non-negotiable.

Bad:

```text
signup_date removed
registration_date added
↓
automatically edit Mapping v1
```

Correct:

```text
Mapping v1 remains immutable

Possible replacement proposed
↓
Mapping v2 created
↓
Human approval
↓
Mapping v2 becomes active
```

---

## 21. Drift Evidence

A drift record should preserve:

```text
source_id
previous_schema_version
new_schema_version
changes
affected_mappings
compatibility state
review status
created_at
```

This evidence should be sufficient to reproduce why the system required review.

---

## 22. AI Role in Drift

AI can assist with semantic interpretation.

Example:

```text
Removed:
signup_date

Added:
registration_date
```

AI proposal:

```text
registration_date may replace signup_date
confidence: 0.86
reason: naming and observed date patterns are similar
```

This does not automatically activate the mapping.

Human approval remains required.

---

## 23. Drift Acceptance Criteria

The schema-drift subsystem is correct when:

1. schemas can be captured as versions
2. equivalent schemas produce stable fingerprints
3. added columns are detected
4. removed columns are detected
5. type changes are detected
6. nullability changes are detected where represented
7. affected mappings are identified
8. compatible changes are not unnecessarily blocked
9. broken mappings cannot silently execute
10. AI replacement suggestions remain proposals
11. approved mappings are immutable
12. new mapping versions are auditable

---

## 24. Explicit Non-Goals

Do not implement:

- automatic schema migration without approval
- automatic mapping mutation
- a generic database migration framework
- probabilistic-only drift detection
- AI-only drift detection

Schema facts and mapping dependency analysis must remain deterministic.
