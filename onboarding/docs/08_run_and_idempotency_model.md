# 08 — Run and Idempotency Model

## 1. Purpose

This document defines the lifecycle and identity of an onboarding operation and establishes how retries can occur without accidentally creating duplicate canonical data.

---

## 2. Definition of an Onboarding Run

An onboarding run represents one logical attempt to process a specific source input using a specific configuration.

A run should reference:

```text
run_id
source_id
source_schema_version
canonical_schema_version
mapping_version
input fingerprint
idempotency key
status
timestamps
record counts
```

The exact persistence schema may evolve, but the lineage must remain explicit.

---

## 3. Run Lifecycle

Initial lifecycle:

```text
CREATED
   ↓
PROFILING
   ↓
MAPPING_PENDING
   ↓
APPROVAL_PENDING
   ↓
TRANSFORMING
   ↓
VALIDATING
   ↓
COMPLETED
```

Possible terminal/exception states include:

```text
FAILED
PARTIAL
CANCELLED
```

The precise status transitions must be enforced by the application.

Do not allow arbitrary state jumps from the UI.

---

## 4. Lifecycle Meaning

### CREATED

The run exists and has been accepted as a logical onboarding operation.

### PROFILING

Source schema and data characteristics are being inspected.

### MAPPING_PENDING

A usable source profile exists, but mapping is not yet ready for execution.

### APPROVAL_PENDING

A mapping proposal requires human review.

### TRANSFORMING

An approved mapping is being executed deterministically.

### VALIDATING

Canonical candidate records are undergoing deterministic validation.

### COMPLETED

The run completed according to its configured success policy.

### PARTIAL

The run processed records but contains unresolved record-level exceptions or other explicitly permitted partial outcomes.

### FAILED

A system-level or blocking processing failure prevented successful completion.

### CANCELLED

An operator or system policy explicitly stopped the run.

---

## 5. Batch Success Policy

The system must distinguish:

```text
run-level failure
```

from:

```text
record-level validation failures
```

Example:

```text
10,000 records
9,900 valid
100 invalid
```

This may produce:

```text
PARTIAL
```

rather than:

```text
FAILED
```

provided the configured onboarding policy allows valid records to proceed.

The final policy must be explicitly documented and tested.

---

## 6. Idempotency Principle

The core rule is:

> **A retry of the same logical onboarding request must not accidentally create duplicate side effects.**

Conceptually:

```text
same idempotency key
+
same logical request
=
same logical operation
```

---

## 7. Idempotency Key

The caller provides or the system derives an idempotency key for a logical onboarding request.

Examples:

```text
ONBOARD-CRM-2026-001
```

or a generated stable request identifier.

The key must identify the logical operation rather than merely the HTTP request instance.

---

## 8. Request Fingerprint

An idempotency key alone is not sufficient.

The system should also calculate a deterministic request/input fingerprint.

Potential inputs include:

```text
source identity
source schema version
mapping version
canonical schema version
input file/content hash
relevant execution options
```

The exact composition may be finalized during implementation.

---

## 9. Same Key + Same Request

Example:

```text
idempotency key:
RUN-1001

fingerprint:
ABC123
```

First request:

```text
RUN-1001
ABC123
```

→ creates run 1001

Second request:

```text
RUN-1001
ABC123
```

→ must return/use the existing logical operation rather than create another canonical load.

The implementation may return the original result, current state, or a deterministic conflict-free representation.

---

## 10. Same Key + Different Request

Example:

```text
RUN-1001
fingerprint:
ABC123
```

followed by:

```text
RUN-1001
fingerprint:
XYZ999
```

This must not silently reuse the first result.

Return an explicit idempotency conflict.

---

## 11. Idempotency Persistence

The system should be able to associate:

```text
idempotency_key
request fingerprint
run_id
status
result reference
created_at
```

with the logical onboarding operation.

The persistence mechanism must be concurrency-safe.

---

## 12. Concurrency

Two identical onboarding requests may arrive concurrently.

Correct behavior requires one logical operation rather than:

```text
request A → load data
request B → load same data again
```

The implementation should rely on deterministic uniqueness/transaction semantics rather than in-memory locks alone.

An in-memory cache may improve efficiency but cannot be the sole correctness mechanism.

---

## 13. Reprocessing

Reprocessing is a distinct operation referencing a previous run/exception.

It should retain lineage:

```text
original run
      ↓
correction
      ↓
reprocessing run/reference
```

Reprocessing must not corrupt the original run's audit information.

---

## 14. Mapping Version and Idempotency

Changing the mapping version generally changes the logical processing input.

Therefore:

```text
mapping v1
≠
mapping v2
```

even if the same source file is processed.

The resulting run must reference the mapping actually used.

---

## 15. Source Schema Version and Idempotency

Likewise:

```text
source schema v1
≠
source schema v2
```

unless the system explicitly determines that the normalized logical input is identical and the processing identity policy permits reuse.

The conservative default is to treat configuration/version changes as materially different processing identities.

---

## 16. Input Fingerprinting

For file-based input, a content fingerprint should be computed deterministically.

Do not use:

```text
filename only
```

as the content identity.

Two files with the same name can contain different content.

For API sources, the fingerprint should represent the logical input/request payload or source snapshot identity according to the source contract.

---

## 17. Run Lineage

Every run should answer:

```text
What source was processed?
Which schema version?
Which canonical schema?
Which mapping?
Which input?
Who initiated it?
When?
What happened?
How many records succeeded?
How many failed?
```

Where authentication is available, associate the run with the authenticated operator identity using the repository's existing identity conventions.

---

## 18. Failure and Retry

If a system-level failure occurs before canonical commit:

```text
do not claim success
do not silently advance processing state
```

A retry should be able to resume or repeat safely according to the selected transaction/idempotency design.

The implementation must explicitly define which side effects are:

```text
transactional
retriable
idempotent
```

---

## 19. Idempotency and SCD2

Downstream SCD2 processing must receive only the intended canonical operation.

The onboarding run must not trigger duplicate canonical changes simply because the same onboarding request was retried.

The actual SCD2 engine already has its own correctness/transactional guarantees; onboarding should integrate through its stable boundary rather than duplicate those mechanisms.

---

## 20. Audit Information

An onboarding run should preserve:

```text
run_id
source
source schema version
canonical schema version
mapping version
input fingerprint
idempotency key
operator identity where available
start time
end time
status
counts
error references
```

Do not store unnecessary raw customer data merely to maintain auditability.

---

## 21. Run Acceptance Criteria

The run/idempotency subsystem is correct when:

1. every run has a stable identifier
2. lifecycle transitions are controlled
3. source/mapping/canonical versions are traceable
4. identical retries do not duplicate side effects
5. same idempotency key with different input produces an explicit conflict
6. concurrent identical requests do not create duplicate operations
7. reprocessing retains lineage
8. failures are distinguishable from partial record-level outcomes
9. run results expose accurate record counts
10. downstream SCD2 receives only the intended canonical operation

---

## 22. Explicit Non-Goals

Do not introduce:

- Redis solely for idempotency
- distributed workflow infrastructure solely for demonstration
- a second queueing system
- duplicate execution engines

The implementation should first use the existing PostgreSQL/application infrastructure where it provides adequate correctness.
