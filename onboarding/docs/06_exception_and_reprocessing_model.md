# 06 — Exception and Reprocessing Model

## 1. Purpose

This document defines how the onboarding system represents, reviews, corrects, and reprocesses records that cannot satisfy the canonical customer contract.

The exception workflow exists so that data-quality failures become:

- visible
- attributable
- actionable
- replayable
- auditable

rather than being silently discarded or hidden inside a failed batch.

---

## 2. Core Principle

The exception system follows:

> **Detect deterministically. Explain clearly. Correct explicitly. Revalidate deterministically.**

GenAI may suggest a possible correction or explain an error, but it must not silently resolve the exception.

---

## 3. Exception Granularity

Exceptions are primarily **record-level**.

A single onboarding run may therefore contain:

```text
10,000 input records
9,850 valid
150 exceptions
```

The valid records can continue according to the batch policy.

The 150 invalid records remain individually inspectable.

A batch-level failure may still occur for infrastructure, contract, mapping, or other processing errors. Such a failure is distinct from ordinary record-level validation exceptions.

---

## 4. Exception Categories

Initial exception categories include:

```text
SOURCE_ERROR
MAPPING_ERROR
TRANSFORMATION_ERROR
VALIDATION_ERROR
DUPLICATE_ERROR
REFERENTIAL_INTEGRITY_ERROR
BUSINESS_RULE_ERROR
SCHEMA_DRIFT_ERROR
```

These categories describe the processing stage.

The precise validation rule should be represented by a stable rule identifier.

---

## 5. Exception Record

An exception should contain, at minimum:

```text
exception_id
run_id
record_id
rule_id
category
field
observed_value / safe representation
reason
suggested_fix
status
created_at
updated_at
resolved_at
```

Where appropriate, also retain:

```text
source_id
source_schema_version
canonical_schema_version
mapping_version
```

These references provide lineage without requiring the exception record to duplicate the entire input dataset.

---

## 6. Sensitive Data Handling

Exception records may contain values derived from customer data.

Therefore:

- store only the value needed to diagnose the issue
- avoid unnecessary raw-record duplication
- mask sensitive values where possible
- never store secrets or credentials
- do not place complete customer records in logs merely to simplify debugging

The exact persistence mechanism must follow the parent repository's existing security and storage conventions.

---

## 7. Exception Lifecycle

Preferred lifecycle:

```text
OPEN
  ↓
CORRECTED
  ↓
REPROCESSED
  ↓
RESOLVED
```

An optional path may exist:

```text
OPEN
  ↓
DISMISSED
```

only when the business rule explicitly permits dismissal.

A record cannot be considered resolved solely because a user clicked a UI button.

---

## 8. OPEN

An exception enters `OPEN` when deterministic processing identifies a blocking issue.

Examples:

```text
CUSTOMER_ID_REQUIRED
EMAIL_FORMAT
STATUS_ENUM
CUSTOMER_ID_DUPLICATE
INVALID_DATE
```

The exception remains linked to the onboarding run that produced it.

---

## 9. CORRECTED

A correction means an explicit change has been proposed or applied to the problematic record or processing configuration.

The system should preserve enough information to distinguish:

```text
original observed value
corrected value
who/what proposed the correction
when it occurred
```

AI suggestions are not automatically corrections.

A correction may originate from:

- operator input
- approved deterministic transformation change
- source-data correction
- approved mapping change
- AI suggestion followed by explicit human confirmation

---

## 10. REPROCESSED

After correction, the affected record is sent through the deterministic pipeline again:

```text
corrected record
    ↓
approved mapping
    ↓
deterministic transformation
    ↓
deterministic validation
```

If validation succeeds, the exception can move to `RESOLVED`.

If validation fails again, the system should retain a traceable retry/reprocessing history rather than silently replacing the original exception.

---

## 11. RESOLVED

An exception becomes `RESOLVED` only after the record successfully satisfies the configured processing requirements.

Resolution should preserve:

```text
exception_id
original rule
resolution action
reprocessing run/reference
resolution timestamp
```

Do not delete resolved exception history merely because the record eventually succeeded.

---

## 12. DISMISSED

If dismissal is supported, it must require an explicit deterministic policy.

Example:

```text
optional field unavailable
```

might be dismissible if the canonical contract allows the field to remain absent.

The user must not be allowed to dismiss:

```text
required-field failure
duplicate primary identity
invalid mandatory status
```

unless a separately documented policy explicitly permits it.

---

## 13. Suggested Fixes

AI may generate a suggested fix.

Example:

```text
Rule:
EMAIL_FORMAT

Observed:
alice@@example.com

Suggested fix:
Correct the source email before reprocessing.
```

The system should clearly distinguish:

```text
deterministic reason
```

from:

```text
AI suggestion
```

A suggested fix does not change the record.

---

## 14. Deterministic Correction

Where possible, corrections should be represented using the same constrained transformation vocabulary used by the normal pipeline.

Examples:

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

Do not allow an exception-repair mechanism to bypass the transformation engine.

---

## 15. Mapping-Related Exceptions

A mapping error is distinct from a record-level data-quality error.

Example:

```text
source field:
signup_date

target:
created_at

problem:
mapping unresolved
```

This should not produce 10,000 independent record exceptions if the actual problem is a source-to-canonical mapping definition.

Prefer:

```text
mapping issue
    ↓
fix mapping
    ↓
create new mapping version
    ↓
reprocess affected run
```

This distinction prevents noisy exception queues.

---

## 16. Schema-Drift Exceptions

If a source removes a mapped field:

```text
cust_no removed
```

and an approved mapping still references it, this is primarily a schema/mapping compatibility problem.

The correct lifecycle is:

```text
drift detected
    ↓
mapping impact identified
    ↓
mapping review
    ↓
new mapping version
    ↓
reprocess
```

Do not pretend that every affected record independently has a data-quality defect.

---

## 17. Partial Success

The pipeline may support:

```text
valid records
+
invalid records
```

in the same onboarding run.

The final run result should distinguish:

```text
records_seen
records_valid
records_invalid
records_skipped
```

The exact run status vocabulary is defined in:

`08_run_and_idempotency_model.md`

Do not report a partially successful run as a clean success.

---

## 18. Reprocessing Scope

Reprocessing should be able to target:

1. a corrected individual record
2. a set of related exceptions
3. an entire affected onboarding run when the mapping or source-schema issue is corrected

The implementation should prefer the smallest safe reprocessing scope.

A mapping-level correction may require reprocessing many records.

A single invalid email should not require replaying an unrelated source dataset.

---

## 19. Reprocessing and Idempotency

Reprocessing must remain subject to the onboarding run's idempotency rules.

Do not let:

```text
retry
reprocess
refresh
```

create duplicate canonical records.

Every reprocessing operation should be traceable to:

```text
original run
original exception
correction
reprocessing run/reference
mapping version
```

---

## 20. Relationship to Existing SCD2 Recovery

The parent SCD2 project contains a controlled containment/recovery pattern in which held batches preserve evidence and can later be released or reprocessed without leaking historical state before recovery.

That architecture provides a useful reliability pattern:

```text
preserve evidence
+
explicit operator action
+
replay through deterministic logic
+
atomic state transition
```

However, onboarding exceptions are **not identical** to SCD2 containment.

Do not copy:

```text
SCD2 hold states
checkpoint semantics
historical recovery transactions
```

into onboarding unless the onboarding requirement actually needs them.

The onboarding exception system should remain responsible for:

```text
record validation failures
mapping failures
transformation failures
schema/mapping incompatibility
```

---

## 21. Operator Experience

The exception UI should make the following immediately understandable:

```text
What failed?
Why did it fail?
Which field is affected?
Which rule failed?
What value was observed?
What is the expected condition?
What correction is suggested?
What can the operator do next?
```

Example:

```text
Exception: EX-1042
Record: CUST-1842

Rule:
EMAIL_FORMAT

Field:
email

Observed:
alice@@example.com

Expected:
valid canonical email format

Suggested action:
Correct the source value and reprocess.

Status:
OPEN
```

Avoid a UI that only shows:

```text
Validation failed
```

---

## 22. Audit Requirements

Exception actions should retain:

```text
who
what
when
why/reference
```

where appropriate.

At minimum, approval/correction/resolution actions should reference the relevant:

```text
run_id
mapping_version
schema_version
exception_id
```

---

## 23. Exception Acceptance Criteria

The exception subsystem is correct when:

1. deterministic failures create identifiable exceptions
2. every exception has a stable rule/category
3. operators can inspect the failure reason
4. AI suggestions are visibly distinguished from deterministic facts
5. corrections are explicit
6. reprocessing uses deterministic transformation/validation
7. successful reprocessing resolves the exception
8. unresolved records remain visible
9. exception history is not silently deleted
10. reprocessing does not create duplicates
11. mapping/schema issues can be handled at the correct scope instead of generating misleading record-level noise

---

## 24. Explicit Non-Goals

Do not turn the exception queue into:

- a generic CRM ticketing system
- an autonomous correction agent
- a chatbot-based support system
- a hidden data-cleaning service
- a mechanism for bypassing validation

The exception queue is a controlled data-quality recovery workflow.
