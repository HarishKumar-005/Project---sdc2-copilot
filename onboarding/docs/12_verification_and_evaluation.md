# 12 — Verification and Evaluation

## 1. Purpose

This document defines how the Customer Data Onboarding & Integration Guardrail will be verified and evaluated.

The project must produce evidence of correctness rather than relying only on a successful UI demonstration.

---

## 2. Verification Philosophy

The verification strategy follows:

```text
Unit correctness
      +
Contract correctness
      +
Integration correctness
      +
End-to-end behavior
      +
Evaluation metrics
```

A feature is not considered reliable merely because one happy-path demonstration works.

---

## 3. Test Data Principle

Use deterministic synthetic customer datasets.

The test corpus should include:

- clean source schema
- renamed fields
- alternative field names
- incomplete schema
- inconsistent date formats
- missing required values
- duplicate customer IDs
- duplicate records
- invalid emails
- invalid statuses
- ambiguous semantic fields
- source-schema changes

Do not use real production customer data for project evaluation.

---

# 4. Source Adapter Verification

## CSV

Verify:

- valid CSV ingestion
- empty CSV handling
- missing header handling
- malformed rows
- quoted values
- null/blank values
- duplicate records
- deterministic output

## Mock REST

Verify:

- successful response
- malformed response
- empty response
- connection failure
- deterministic normalized output

Acceptance:

```text
same source
→
same normalized representation
```

---

# 5. Profiling Verification

Verify:

- row count
- column count
- data-type observations
- null rate
- distinct count
- duplicate count
- categorical distributions
- date-format observations
- identifier-pattern observations

For fixed input, repeated profiling must produce equivalent results.

Do not use the LLM to calculate deterministic statistics.

---

# 6. Mapping Verification

## Ground Truth Dataset

Create a labeled mapping dataset:

```text
source field
→
canonical field
```

Example:

```text
cust_no → customer_id
fname → first_name
lname → last_name
email_address → email
dob → date_of_birth
acct_status → status
signup_date → created_at
```

Include intentionally ambiguous cases.

---

## Mapping Metrics

At minimum calculate:

```text
mapping accuracy
```

defined as:

```text
correct approved/accepted mapping decisions
/
total labeled mapping decisions
```

Also report:

```text
review-required rate
unresolved rate
```

Do not treat model confidence as ground truth.

---

## Structured Output Tests

Verify that malformed LLM output is rejected.

Examples:

```text
missing target
invalid mapping type
unsupported transformation
invalid confidence
unknown canonical field
duplicate mappings
```

These must not become executable mappings.

---

# 7. Human Approval Verification

Test:

```text
DRAFT → REVIEW
REVIEW → APPROVED
REVIEW → REJECTED
```

Verify:

- approved mapping is immutable
- edits create a new version
- approval identity is stored
- approval timestamp is stored
- source/canonical versions are stored
- unapproved mappings cannot execute

---

# 8. Transformation Verification

For every supported operation, create explicit deterministic tests.

Initial operations:

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

Test:

- normal values
- nulls
- invalid values
- boundary values
- unsupported operations

Required property:

```text
same input
+
same mapping
=
same output
```

No LLM invocation should be required during deterministic execution.

---

# 9. Validation Verification

Create labeled validation cases for:

```text
required-field failure
type failure
invalid email
invalid enum
duplicate customer_id
duplicate record
referential-integrity failure where configured
business-rule failure
```

Measure:

```text
validation detection coverage
```

Conceptually:

```text
detected injected violations
/
total injected violations
```

Also measure false positives on clean records where practical.

Do not report validation coverage without describing the injected-error dataset.

---

# 10. Exception Verification

Verify:

```text
VALIDATION FAILURE
      ↓
EXCEPTION CREATED
```

Then:

```text
OPEN
 ↓
CORRECTED
 ↓
REPROCESSED
 ↓
RESOLVED
```

Verify:

- original exception remains auditable
- correction is explicit
- reprocessing is deterministic
- successful reprocessing resolves the issue
- failed reprocessing keeps the issue visible
- exceptions cannot be silently deleted

---

# 11. Reprocessing Verification

Test at least:

### Single record

One exception is corrected and reprocessed.

### Multiple exceptions

Several exceptions are processed independently.

### Mapping correction

A mapping-level problem is fixed through a new mapping version and affected data is reprocessed.

Acceptance:

```text
reprocessing
does not create duplicate canonical records
```

---

# 12. Run and Idempotency Verification

Test:

### Same key + same input

Expected:

```text
same logical operation
```

### Same key + different input

Expected:

```text
explicit conflict
```

### Concurrent duplicate requests

Expected:

```text
one logical operation
```

### Different keys + same input

Expected:

```text
distinct logical operations
```

### Same input + different mapping version

Expected:

```text
distinct processing identity
```

The final implementation must make the chosen idempotency semantics explicit.

---

# 13. Schema Drift Verification

Create controlled schema transitions.

### Scenario A — Added field

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

Expected:

```text
ADDED_COLUMN
```

### Scenario B — Removed mapped field

```text
V1:
signup_date

V2:
registration_date
```

Expected:

```text
REMOVED_COLUMN
ADDED_COLUMN
affected mapping identified
```

### Scenario C — Type change

```text
dob:
TEXT → DATE
```

Expected:

```text
TYPE_CHANGED
```

### Scenario D — Possible rename

Use semantically similar new/removed fields.

Expected:

```text
possible replacement suggestion
```

but not automatic mapping mutation.

---

# 14. Drift Impact Verification

Verify the system can answer:

> Which approved mapping rules are affected?

Example:

```text
Removed field:
signup_date

Mapping:
signup_date → created_at

Result:
mapping M-006 affected
```

A broken required mapping must prevent unsafe execution until resolved.

---

# 15. SCD2 Integration Verification

Only validated canonical records may cross into the SCD2 integration boundary.

Verify:

### New customer

```text
NEW
→ initial historical state
```

### Changed customer

```text
CHANGED
→ prior version closed
→ new current version
```

### Unchanged customer

```text
UNCHANGED
→ no unnecessary new version
```

### Mapping version change

Verify that changing mapping configuration alone does not create a false business-state change.

---

# 16. SCD2 Temporal Verification

Preserve the existing SCD2 semantics:

```text
[effective_from, effective_to)
```

Verify:

- no overlapping versions
- correct current-row state
- correct boundary dates
- historical ordering
- point-in-time behavior where the existing engine supports it

Do not replace the existing SCD2 test suite with onboarding-specific approximations.

---

# 17. Guardrail Verification

Use a controlled canonical dataset to exercise the existing deterministic guardrail.

### Normal scenario

```text
normal canonical change
→ SCD2
→ NORMAL
→ historical commit
```

### Suspicious scenario

Use a deterministic batch known to trigger an existing guardrail rule.

Expected:

```text
SUSPICIOUS
→ HELD
→ historical state unchanged before recovery
```

Verify:

- evidence persisted
- no historical leakage
- no inappropriate checkpoint advancement where that existing semantics apply
- recovery remains authorized and traceable

Do not change the guardrail merely to make the demo pass.

---

# 18. End-to-End Scenario

The primary end-to-end test should be:

```text
Messy CRM source
      ↓
Profile
      ↓
Generate mapping
      ↓
Human approval
      ↓
Mapping v1
      ↓
Deterministic transform
      ↓
Validation
      ↓
Valid + exceptions
      ↓
Correct/reprocess exceptions
      ↓
Canonical customer
      ↓
SCD2
      ↓
Guardrail
```

Then:

```text
Source schema changes
      ↓
Drift
      ↓
Affected mapping
      ↓
Mapping v2
      ↓
Repeat onboarding
```

---

# 19. AI Reliability Verification

Test:

- valid model response
- malformed model response
- missing fields
- invalid mapping
- unsupported transformation request
- timeout
- rate limit
- provider failure
- fallback behavior where supported

Expected property:

```text
AI failure
≠
unsafe data execution
```

Where manual/deterministic processing is available, the system should fail safely rather than fabricate a mapping.

---

# 20. Security Verification

Verify:

- secrets are not committed
- secrets are not logged
- customer data is minimized in model prompts
- sensitive values are masked where practical
- unauthorized protected actions fail
- API authentication follows existing project conventions
- input validation prevents malformed configuration
- model output is structurally validated before use

---

# 21. Performance Verification

Measure at least:

```text
profiling latency
mapping proposal latency
transformation throughput
validation throughput
end-to-end onboarding time
```

Do not report benchmark numbers without specifying:

```text
dataset size
hardware/environment
measurement method
warm-up/repetition strategy where relevant
```

Do not claim scalability beyond the measured test conditions.

---

# 22. Evaluation Dataset

Create at least:

### Dataset A — Clean

Minimal inconsistencies.

### Dataset B — Moderately messy

Renamed columns, formatting differences, some invalid records.

### Dataset C — Highly messy

Ambiguous names, multiple date representations, duplicates, missing fields, invalid categories.

Each dataset should have known ground truth.

---

# 23. Required Evaluation Metrics

The final evaluation report should include:

| Metric | Purpose |
|---|---|
| Mapping accuracy | Measures source-to-canonical mapping correctness |
| Review-required rate | Measures how often human intervention is needed |
| Validation detection coverage | Measures detection of injected data-quality errors |
| Exception correction success | Measures whether failures can be corrected and reprocessed |
| Schema-drift detection accuracy | Measures structural-change detection against labeled changes |
| Idempotency correctness | Measures duplicate-side-effect prevention |
| SCD2 correctness | Measures historical-state correctness |
| Guardrail correctness | Measures expected NORMAL/SUSPICIOUS behavior |
| Processing latency | Measures operational performance |

---

# 24. Time-to-Onboard Evaluation

Where a meaningful manual baseline can be defined, record:

```text
manual mapping time
```

versus:

```text
system-assisted mapping/review time
```

The measurement should be conducted using the same source scenarios.

Do not invent a time-saving percentage without an actual baseline.

---

# 25. Final Demonstration Verification

Before final presentation, verify the exact demo sequence against a clean/reset dataset.

The final demo should show:

```text
1. Upload/select source
2. Profile data
3. Mapping proposal
4. Uncertain mapping review
5. Approval
6. Mapping version
7. Transformation
8. Validation
9. Exception
10. Reprocessing
11. Canonical output
12. SCD2 history
13. Schema drift
14. Mapping impact
15. New mapping version
16. Suspicious historical change
17. HOLD
18. Recovery
```

Every displayed result must correspond to real executed system behavior.

---

# 26. Regression Verification

Because the project lives in a fork of the SCD2 Copilot repository, run the relevant existing parent test suites after significant integration milestones.

At minimum, when integration touches shared code:

- existing SCD2 unit tests
- existing SCD2 integration tests
- existing guardrail tests
- existing containment/recovery tests
- API/security tests
- onboarding tests

Do not replace parent tests with onboarding tests.

---

# 27. Definition of Verified

A capability is **VERIFIED** only when:

1. the intended behavior is implemented
2. a deterministic test covers its critical logic
3. integration behavior is tested where required
4. end-to-end behavior is demonstrated where required
5. failures are handled according to contract
6. the result is documented accurately

A capability is **UNVERIFIED** when evidence is missing.

Do not convert "implemented" into "verified" without executing the relevant check.

---

# 28. Final Evaluation Report

The final report should include:

```text
Project:
Evaluation date:

Dataset(s):

Environment:

Mapping results:
- accuracy
- review rate
- unresolved rate

Validation results:
- injected violations
- detected violations
- coverage
- false positives where measured

Exception results:
- created
- corrected
- reprocessed
- resolved

Schema drift results:
- changes injected
- changes detected
- mappings impacted
- false/missed detections where measured

Idempotency:
- duplicate request test
- conflict test
- concurrency test

SCD2:
- NEW
- CHANGED
- UNCHANGED
- temporal correctness

Guardrail:
- NORMAL scenario
- SUSPICIOUS scenario
- containment result
- recovery result

Performance:
- dataset size
- latency/throughput
- measurement method

Limitations:

Unverified items:
```

---

# 29. Evaluation Integrity Rule

Never claim:

```text
production accuracy
enterprise scale
universal schema understanding
complete autonomous onboarding
zero human effort
```

from a synthetic student benchmark.

Report exactly what the experiment measures.

The objective is credible evidence, not inflated claims.
