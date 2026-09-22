# 02 — Canonical Data Contract

## 1. Purpose

This document defines the canonical customer data contract that all supported source systems must ultimately produce before data can enter downstream platform processing.

The canonical contract is the stable target of onboarding.

Source-specific differences must be handled through:

- source adapters
- mappings
- constrained transformations
- deterministic validation

They must not cause the canonical schema to change implicitly.

---

## 2. Contract Identity

Initial canonical contract:

```text
Schema name: customer
Schema version: 1
```

Every onboarding run must be associated with an explicit canonical schema version.

The initial version is:

```text
customer.v1
```

Future changes must create a new canonical schema version rather than silently modifying `customer.v1`.

---

## 3. Canonical Entity

The canonical business entity is:

```text
Customer
```

The stable business identifier is:

```text
customer_id
```

`customer_id` is the canonical entity key.

The same logical customer must not appear more than once in a canonical batch under the same `customer_id`.

---

## 4. Canonical Fields

| Field | Type | Required | Unique | Description |
|---|---|---:|---:|---|
| `customer_id` | string | yes | yes | Stable canonical identifier for a customer |
| `first_name` | string | yes | no | Customer's given name |
| `last_name` | string | yes | no | Customer's family/surname |
| `email` | string | yes | no* | Customer email address |
| `date_of_birth` | date | no | no | Customer date of birth |
| `status` | enum | yes | no | Current canonical customer status |
| `created_at` | datetime | yes | no | Canonical customer creation timestamp |

\* Email is not globally required to be unique in the initial contract. Duplicate-email behavior can be validated as a configurable business rule where needed, but it must not be confused with the primary identity key.

---

## 5. Field Semantics

### 5.1 `customer_id`

Type:

```text
string
```

Required:

```text
yes
```

Uniqueness:

```text
required within canonical dataset
```

Purpose:

The stable canonical identity of the customer.

Validation expectations:

- must not be null/blank
- must be unique within a canonical batch
- must be deterministic after transformation
- must preserve the intended source identifier semantics

The mapping layer should not invent a customer ID unless a separately approved deterministic derivation rule exists.

---

### 5.2 `first_name`

Type:

```text
string
```

Required:

```text
yes
```

Purpose:

Customer's given name.

Expected normalization may include:

```text
TRIM
```

Additional normalization must be explicit and deterministic.

---

### 5.3 `last_name`

Type:

```text
string
```

Required:

```text
yes
```

Purpose:

Customer's family/surname.

Expected normalization may include:

```text
TRIM
```

Do not infer or alter a person's name beyond an explicitly approved deterministic transformation.

---

### 5.4 `email`

Type:

```text
string
```

Required:

```text
yes
```

Purpose:

Customer email address.

Expected normalization may include:

```text
TRIM
LOWERCASE
```

Validation should verify canonical email formatting according to the deterministic validation rules defined for the project.

Do not use an LLM to decide whether an email is valid.

---

### 5.5 `date_of_birth`

Type:

```text
date
```

Required:

```text
no
```

Purpose:

Customer birth date.

Source dates may arrive in different formats.

Date parsing must be deterministic and based on an approved format policy.

Ambiguous dates must not be silently interpreted.

Example:

```text
01/02/2026
```

must not be arbitrarily interpreted as either:

```text
2026-02-01
```

or:

```text
2026-01-02
```

without a defined source-specific date-format rule or explicit approval.

---

### 5.6 `status`

Type:

```text
enum
```

Required:

```text
yes
```

Initial allowed values:

```text
ACTIVE
INACTIVE
```

Source systems may use different values.

Examples:

```text
ACTIVE
INACTIVE
SUSPENDED
CLOSED
ENABLED
DISABLED
```

Such source values cannot be silently rewritten into the canonical enum.

A deterministic mapping rule must define how a source value maps to:

```text
ACTIVE
```

or:

```text
INACTIVE
```

If no deterministic mapping is approved, the record should fail validation or remain unresolved.

---

### 5.7 `created_at`

Type:

```text
datetime
```

Required:

```text
yes
```

Purpose:

Canonical customer creation timestamp.

This field is semantically important and may be difficult to infer from source fields such as:

```text
signup_date
registered_on
created
joined
```

These source fields should be treated as potentially ambiguous.

The mapping system may propose a mapping with confidence and explanation, but the approved mapping must explicitly define the source field and transformation semantics.

---

## 6. Canonical Nullability

Required fields:

```text
customer_id
first_name
last_name
email
status
created_at
```

Optional field:

```text
date_of_birth
```

A missing value in a required field is a validation failure.

A missing `date_of_birth` is not automatically an exception solely because it is null.

Do not substitute synthetic values for missing required fields.

---

## 7. Canonical Type Rules

Canonical output must conform to these types:

```text
customer_id   → string
first_name    → string
last_name     → string
email         → string
date_of_birth → date
status        → enum
created_at    → datetime
```

Source values may initially have other representations.

Type conversion must happen through deterministic transformation.

An LLM may suggest a conversion, but it must not perform runtime type conversion itself.

---

## 8. Canonical Normalization

Initial normalization rules should remain conservative.

Allowed examples:

```text
string whitespace:
TRIM

email:
TRIM
LOWERCASE
```

Normalization must not alter business meaning.

For example:

```text
John
```

must not become:

```text
JOHN
```

unless that operation is explicitly configured.

Names should not be semantically rewritten by AI.

---

## 9. Canonical Validation

The contract defines the expected target state.

Validation should include at minimum:

### Required-field validation

Check:

```text
customer_id
first_name
last_name
email
status
created_at
```

### Type validation

Check:

```text
date_of_birth is date
created_at is datetime
```

and appropriate string representations.

### Format validation

At minimum:

```text
email
```

### Enumeration validation

At minimum:

```text
status ∈ {ACTIVE, INACTIVE}
```

### Identity validation

At minimum:

```text
customer_id is non-empty
customer_id is unique
```

### Business-rule validation

Additional deterministic rules may be configured in the project.

---

## 10. Canonical Output Contract

A successfully transformed record should contain exactly the canonical business fields:

```text
customer_id
first_name
last_name
email
date_of_birth
status
created_at
```

Implementation metadata such as:

```text
run_id
source_id
mapping_version
schema_version
validation_status
```

must be stored as processing metadata, not embedded into the canonical business record unless explicitly required.

---

## 11. Canonical Schema Evolution

A schema change requires a new version.

For example:

```text
customer.v1
```

may become:

```text
customer.v2
```

if a new required canonical field is introduced.

A canonical schema change must document:

- added fields
- removed fields
- type changes
- requiredness changes
- enum changes
- compatibility status
- migration/transform requirements

Never modify the semantics of an existing canonical field without increasing the schema version.

---

## 12. Compatibility Principle

A source schema may change independently of the canonical schema.

Therefore:

```text
Source version
      ≠
Canonical schema version
      ≠
Mapping version
```

A source change normally creates or modifies a source schema version and potentially a new mapping version.

A canonical contract change creates a new canonical schema version.

This separation must remain explicit.

---

## 13. Relationship to Mapping

The mapping layer translates:

```text
source schema
       ↓
canonical schema
```

Each approved mapping must identify:

```text
source schema version
canonical schema version
mapping version
```

A mapping is invalid if it targets a canonical field that no longer exists in the referenced canonical schema version.

---

## 14. Relationship to SCD2

The canonical customer contract is the input to the downstream historical state layer.

Conceptually:

```text
Validated Canonical Customer
            ↓
       Existing SCD2
            ↓
Historical Customer State
```

The canonical key:

```text
customer_id
```

is the downstream SCD2 business key.

The onboarding contract must not reproduce the SCD2 versioning fields as customer source fields.

---

## 15. Contract Acceptance Criteria

A canonical record is eligible for downstream processing only when:

1. all required canonical fields are present
2. canonical types are satisfied
3. configured formats are valid
4. status uses an allowed canonical value
5. `customer_id` satisfies identity/uniqueness rules
6. deterministic transformations have completed successfully
7. no blocking validation exception remains

Only after these conditions can the record enter the downstream SCD2 workflow.

---

## 16. Explicit Non-Goals

The canonical contract does not attempt to model every enterprise customer attribute.

Do not add fields such as:

```text
phone
address
company
country
loyalty_tier
revenue
```

to the initial contract merely to make the demo appear more enterprise-ready.

Additional fields should be added only when required by a clearly defined onboarding use case and a new canonical schema version.
