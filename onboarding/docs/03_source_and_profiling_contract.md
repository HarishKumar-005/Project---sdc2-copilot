# 03 — Source and Profiling Contract

## 1. Purpose

This document defines how external customer sources are represented, ingested, inspected, and profiled before semantic mapping.

The initial source boundary intentionally supports:

```text
CSV
Mock REST API
```

These represent heterogeneous customer systems without requiring real third-party vendor integrations.

---

## 2. Source Abstraction

Every source must expose a normalized internal representation regardless of transport.

Conceptually:

```text
Source
  ↓
Source Adapter
  ↓
Normalized Source Dataset
  ↓
Schema Snapshot + Data Profile
```

The adapter is responsible for:

- reading source data
- identifying source schema
- normalizing transport-specific differences
- producing source metadata

The adapter is not responsible for:

- semantic mapping
- canonical transformation
- validation
- SCD2
- guardrail decisions

---

## 3. Supported Sources

### 3.1 CSV

A CSV source may contain:

- header row
- heterogeneous column names
- strings representing multiple logical types
- inconsistent date formats
- missing values
- duplicate records

The adapter must treat CSV input as untrusted external data.

It must not mutate the user's original source file.

---

### 3.2 Mock REST API

A mock REST API represents a source system such as:

```text
CRM
Billing
Support
```

The purpose is to demonstrate API integration while keeping the project deterministic and free of external vendor dependencies.

The mock API should return source records and source metadata using a controlled schema.

Do not create separate business logic for each simulated vendor.

---

## 4. Source Identity

A source should have a stable logical identity.

Conceptual fields:

```text
source_id
source_name
source_type
```

Examples:

```text
crm_customer
billing_customer
support_customer
```

A source identity identifies the system, not a particular upload.

Individual uploads/runs are separately identified.

---

## 5. Source Schema Version

Every recognized source schema must be versionable.

Conceptual representation:

```text
source_id
schema_version
schema_hash
schema_definition
observed_at
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

A schema version should be reproducibly derived from the observed schema definition.

The exact hashing strategy may be selected during implementation, but equivalent schemas must produce the same normalized schema representation before hashing.

---

## 6. Source Schema Definition

A source schema snapshot should capture, at minimum:

```text
column name
source type
nullability or observed null characteristics
column order where relevant
```

Additional metadata may include:

```text
semantic description
source documentation
maximum observed length
example patterns
```

Do not treat inferred semantics as equivalent to declared source semantics.

---

## 7. Data Profile

The profiler should calculate deterministic statistics useful for mapping and validation.

At minimum, consider:

### Completeness

```text
row count
null count
null rate
```

### Uniqueness

```text
distinct count
duplicate count
uniqueness rate
```

### Value distribution

For categorical/low-cardinality fields:

```text
distinct values
frequency distribution
```

### String characteristics

```text
minimum length
maximum length
typical length
whitespace patterns
```

### Temporal characteristics

```text
parseable date rate
observed date formats
minimum observed date
maximum observed date
```

### Identifier characteristics

Potential patterns such as:

```text
CUST-1024
ACC-1001
USR-9482
```

may be recorded as evidence.

The profiler should not declare the semantic meaning of an identifier solely from its pattern.

---

## 8. Profiling Must Be Deterministic

Given the same source data and profiling configuration, the profiler should produce the same profile.

Do not use an LLM to calculate:

- row counts
- null rates
- duplicate counts
- type statistics
- frequency distributions
- schema hashes

These are deterministic responsibilities.

---

## 9. Type Inference

Type inference may inspect source values to suggest:

```text
string
integer
decimal
boolean
date
datetime
```

However, inferred type is evidence, not automatically the canonical type.

Example:

```text
dob
```

may appear as:

```text
string
```

while semantically representing a date.

The mapping/transformation layer handles the semantic relationship.

---

## 10. Date Detection

The profiler should detect observed date patterns where practical.

Examples:

```text
YYYY-MM-DD
DD/MM/YYYY
MM/DD/YYYY
YYYY/MM/DD
```

It must report ambiguity rather than silently choose one interpretation when multiple formats are plausible.

The source may be assigned a source-specific date-format configuration after human review.

---

## 11. Sample Values

Profiles may include a small, representative set of sample values.

Prefer:

```text
masked
minimized
pattern-oriented
```

samples for any field that may contain customer data.

For example:

```text
email:
h***@example.com
```

is preferable to sending large collections of real customer email addresses to the LLM.

Sample selection should be deterministic where practical.

---

## 12. LLM Input Boundary

The mapping model should receive the minimum information needed to propose a semantic mapping.

Preferred inputs:

```text
source field name
source type
profile statistics
masked sample patterns
source description/documentation
canonical field definition
canonical type
canonical constraints
```

Do not send an entire source dataset to the LLM solely because it is convenient.

The profiler should reduce raw-data dependency before the mapping call.

---

## 13. Source Schema Drift Baseline

The first accepted schema snapshot becomes the comparison baseline.

For example:

```text
CRM v1
```

A later observation:

```text
CRM v2
```

is compared against the previous relevant schema version.

Schema comparison should identify:

```text
added
removed
type changed
nullability changed
possible rename
```

The exact impact model is defined in:

`07_schema_drift_and_versioning.md`

---

## 14. Source Data Immutability

The original source input must be treated as read-only.

The onboarding pipeline may create:

```text
normalized copy
profile artifact
transformed data
validation result
exception records
```

but must not modify the original source merely to make it easier to process.

---

## 15. Source Adapter Failure Categories

The source boundary should distinguish at least:

```text
SOURCE_NOT_FOUND
SOURCE_UNREADABLE
SCHEMA_INVALID
EMPTY_SOURCE
MALFORMED_RECORD
UNSUPPORTED_TYPE
API_CONNECTION_FAILURE
API_RESPONSE_INVALID
```

Failures should be machine-readable and safe to expose through the onboarding workflow.

Do not expose secrets or raw connection credentials in error messages.

---

## 16. Empty and Malformed Input

An empty source should produce an explicit onboarding outcome rather than silently succeeding.

A malformed source should identify enough information for the operator to understand the problem.

For example:

```text
CSV contains no header
```

or:

```text
Required source field metadata is missing
```

should be actionable errors.

---

## 17. Source Profile Acceptance

A profile is considered usable for mapping when:

- the source schema can be identified
- records can be read
- field metadata can be represented
- profile statistics can be produced
- no blocking source-format error remains

A usable profile does not imply that the source is semantically compatible with the canonical customer schema.

Semantic compatibility is established by mapping and validation.

---

## 18. Source Examples

Use intentionally heterogeneous examples.

### CRM-like

```text
cust_no
fname
lname
email_address
dob
acct_status
signup_date
```

### Billing-like

```text
account_id
given_name
surname
email
birth_date
customer_state
registered_on
```

### Support-like

```text
user_ref
first
family
contact
birthdate
state
created
```

The examples are test/demo schemas, not claims about exact real vendor schemas.

---

## 19. Source Profiling Acceptance Criteria

Given a deterministic source dataset:

1. the adapter can ingest it
2. the schema can be represented
3. profiling statistics are reproducible
4. null/duplicate/value-pattern information is available
5. date-format observations are available where applicable
6. sensitive sample values are minimized/masked for AI usage
7. the original source remains unchanged

Only then should the dataset proceed to semantic mapping.
