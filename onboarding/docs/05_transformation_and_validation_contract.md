# 05 — Transformation and Validation Contract

## 1. Purpose

This document defines how approved mappings are executed and how resulting canonical customer records are validated.

The architecture deliberately separates:

```text
mapping
transformation
validation
```

and keeps all runtime data execution deterministic.

---

## 2. Execution Principle

The transformation path is:

```text
Approved Mapping
      ↓
Deterministic Transformation Engine
      ↓
Canonical Candidate Records
      ↓
Deterministic Validation
      ↓
VALID / INVALID
```

The LLM does not execute the transformation.

The LLM does not decide whether a record is valid.

---

## 3. Transformation Vocabulary

The initial deterministic transformation vocabulary is intentionally constrained.

Supported operations may include:

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

Only operations implemented and tested by the transformation engine are allowed.

Do not silently add an operation merely because an LLM requested it.

---

## 4. Transformation Definitions

A transformation should be represented as structured data.

Example:

```json
{
  "source_field": "email_address",
  "target_field": "email",
  "operations": [
    {"op": "TRIM"},
    {"op": "LOWERCASE"}
  ]
}
```

The exact schema is an implementation detail, but it must remain:

- structured
- inspectable
- deterministic
- serializable
- versioned through the mapping

---

## 5. TRIM

Removes leading/trailing whitespace according to the project's deterministic string policy.

Example:

```text
" alice@example.com "
```

becomes:

```text
"alice@example.com"
```

Do not remove meaningful internal whitespace.

---

## 6. LOWERCASE / UPPERCASE

These operations change character casing deterministically.

Use carefully.

Example:

```text
EMAIL → LOWERCASE
```

is reasonable as an explicit normalization rule.

Do not apply blanket upper/lower casing to names unless the mapping specifically requires it.

---

## 7. CAST

CAST converts a value to a supported canonical type.

Examples:

```text
"42" → integer
"2026-09-18" → date
```

A failed cast is a deterministic transformation/validation failure.

Do not silently substitute an unrelated default value.

---

## 8. PARSE_DATE

Date parsing must use explicitly configured formats.

Example:

```text
31/12/2026
```

may be parsed using:

```text
DD/MM/YYYY
```

only when that format is defined by the source/mapping configuration.

If the date is ambiguous and no deterministic policy exists:

```text
INVALID / REVIEW_REQUIRED
```

Do not guess.

---

## 9. NORMALIZE_EMAIL

The initial email normalization may include:

```text
TRIM
LOWERCASE
```

More complex email correction must not be inferred by an LLM at runtime.

Validation must still verify final format.

---

## 10. MAP_ENUM

Source categorical values may be mapped to canonical values using explicit deterministic mappings.

Example:

```text
source:
ENABLED → ACTIVE
DISABLED → INACTIVE
```

An unresolved source value should remain invalid.

Do not silently convert unknown values.

---

## 11. CONCAT

Concatenation may combine explicitly selected fields.

Example:

```text
first + " " + last
```

The operation must define:

- source fields
- ordering
- separator
- null handling

Do not infer complex name decomposition automatically in the runtime transformation path.

---

## 12. Transformation Errors

Every transformation failure should produce a deterministic reason.

Possible categories:

```text
INVALID_CAST
INVALID_DATE
UNSUPPORTED_OPERATION
INVALID_ENUM_MAPPING
MISSING_SOURCE_VALUE
TRANSFORMATION_CONFIGURATION_ERROR
```

The transformation engine must never swallow a failed operation silently.

---

## 13. Determinism

Given:

```text
same input
+
same mapping version
+
same canonical schema version
+
same transformation implementation
```

the transformation result should be reproducible.

Avoid runtime dependence on:

- current time unless explicitly part of a defined field rule
- random values
- model responses
- uncontrolled external services

---

## 14. Validation Responsibility

Validation determines whether transformed canonical data satisfies the canonical contract.

Initial categories:

```text
REQUIRED
TYPE
FORMAT
ENUM
UNIQUE
DUPLICATE
REFERENTIAL_INTEGRITY
BUSINESS_RULE
```

---

## 15. REQUIRED Validation

Required fields:

```text
customer_id
first_name
last_name
email
status
created_at
```

must be:

```text
present
non-null
non-blank where string
```

`date_of_birth` may be null.

---

## 16. TYPE Validation

Validate canonical types:

```text
customer_id   → string
first_name    → string
last_name     → string
email         → string
date_of_birth → date
status        → enum
created_at    → datetime
```

A value that cannot satisfy its canonical type is invalid.

---

## 17. FORMAT Validation

Initial format validation should include at least:

### Email

Verify deterministic canonical email formatting.

Do not use an LLM to classify email validity.

Additional formats may be added only when the canonical contract defines them.

---

## 18. ENUM Validation

For:

```text
status
```

allowed values are:

```text
ACTIVE
INACTIVE
```

Anything else is invalid unless an approved deterministic mapping converts the source representation before validation.

---

## 19. UNIQUE / DUPLICATE Validation

`customer_id` must be unique within the canonical dataset.

Example:

```text
CUST-1001
CUST-1001
```

must produce an explicit duplicate error.

The system must not silently keep one record and discard the other.

Other fields such as email may have configurable uniqueness rules, but such a rule must be explicitly enabled.

---

## 20. Referential Integrity

Referential-integrity validation may be supported when a configured reference dataset exists.

Example:

```text
customer.account_manager_id
```

would require a reference dataset.

The initial canonical customer contract does not require a complex foreign-key model, so referential integrity should remain a configurable validation capability rather than a mandatory demo dependency.

If implemented, the check must be deterministic.

---

## 21. Business Rules

Business rules must be deterministic and explicit.

Examples:

```text
status must be ACTIVE or INACTIVE
created_at must not violate configured temporal constraints
```

Do not ask an LLM whether a business rule is satisfied.

The rule engine decides.

---

## 22. Validation Result

Validation should produce a structured result containing at least:

```text
record identity
overall status
failed rules
warnings where applicable
field-level findings
```

Conceptual result:

```text
record_id: CUST-1001
status: INVALID

errors:
  - rule_id: EMAIL_FORMAT
  - rule_id: STATUS_ENUM
```

---

## 23. Warning vs Blocking Error

The implementation should distinguish:

```text
BLOCKING ERROR
```

from:

```text
WARNING
```

A warning should not be silently treated as a success.

The exact warning policy must be defined before enabling warning-driven continuation.

Required-field/type/identity violations are blocking by default.

---

## 24. Valid vs Invalid Records

The pipeline must support record-level outcomes.

Example:

```text
10,000 input records

9,842 VALID
158 INVALID
```

Valid records may proceed to the canonical downstream path.

Invalid records enter the exception workflow.

The presence of invalid records does not require discarding all valid records unless a configured batch-level rule requires that behavior.

---

## 25. Validation Rule Identity

Every deterministic validation rule should have a stable identifier.

Examples:

```text
CUSTOMER_ID_REQUIRED
CUSTOMER_ID_DUPLICATE
FIRST_NAME_REQUIRED
EMAIL_REQUIRED
EMAIL_FORMAT
STATUS_REQUIRED
STATUS_ENUM
CREATED_AT_REQUIRED
DATE_OF_BIRTH_TYPE
```

Stable rule IDs enable:

- exception tracking
- metrics
- reproducibility
- testing
- reporting

---

## 26. AI Suggested Fixes

AI may suggest a possible correction after a deterministic failure.

Example:

```text
Rule:
EMAIL_FORMAT

Observed:
alice@@example.com

AI suggestion:
Correct the source email address before reprocessing.
```

The suggestion does not resolve the exception.

A correction must pass the deterministic transformation and validation pipeline again.

---

## 27. Validation and SCD2 Boundary

Only validated canonical records may enter the downstream SCD2 processing path, subject to the chosen batch-level processing policy.

Conceptually:

```text
Canonical candidate
      ↓
Validation
      ↓
VALID
      ↓
Existing SCD2
```

The onboarding validation engine does not implement SCD2 semantics.

---

## 28. Transformation/Validation Acceptance Criteria

The implementation is correct when:

1. approved mappings execute without LLM runtime dependence
2. supported transformations are deterministic
3. unsupported transformations are rejected
4. canonical type rules are enforced
5. required fields are enforced
6. enum and format rules are enforced
7. duplicates are surfaced
8. validation failures have stable rule IDs
9. invalid records can enter the exception workflow
10. valid records can proceed downstream
11. repeated execution with the same input and mapping produces the same canonical result

---

## 29. Explicit Non-Goals

Do not add:

- arbitrary Python generated by the LLM
- arbitrary SQL generated by the LLM
- autonomous data correction
- probabilistic record validity decisions
- hidden transformations
- silent default-value insertion
- silent record deletion

The deterministic pipeline is the execution authority.
