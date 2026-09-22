# 09 — SCD2 Integration

## 1. Purpose

This document defines exactly how the Customer Data Onboarding bounded context leverages the existing SCD2 Copilot capability.

The purpose is not to rewrite SCD2.

The purpose is to place the existing deterministic historical capability downstream of canonical customer-data onboarding.

---

## 2. Core Relationship

The system is split into two responsibilities:

```text
ONBOARDING
"Make heterogeneous external data canonical and valid"

        ↓

SCD2
"Preserve how canonical customer state changes over time"

        ↓

GUARDRAIL
"Control suspicious historical propagation"
```

This boundary must remain explicit.

---

## 3. Onboarding Output

The onboarding pipeline produces validated canonical customer records:

```text
customer_id
first_name
last_name
email
date_of_birth
status
created_at
```

The records have already passed:

```text
approved mapping
deterministic transformation
deterministic validation
```

Only then should the downstream historical capability be invoked.

---

## 4. SCD2 Input Boundary

The SCD2 integration consumes canonical customer state.

Conceptually:

```text
Validated Canonical Batch
          ↓
Existing SCD2 Change Detection
          ↓
Existing SCD2 Transformation
          ↓
Existing SCD2 Validation
```

The onboarding system must not reimplement:

```text
detect_changes
apply_scd2
validate_scd2
```

or create a second version of their temporal logic.

---

## 5. Business Key

The canonical SCD2 entity key is:

```text
customer_id
```

This is a single business key for the initial project.

The identity must remain stable across onboarding source mappings.

A source field can map to `customer_id`, but onboarding should not create a different downstream identity for the same logical customer without an explicitly defined identity-resolution feature.

---

## 6. Historical State

The downstream SCD2 layer should maintain versions conceptually using:

```text
effective_from
effective_to
is_current
```

with the existing repository's half-open temporal semantics:

```text
[effective_from, effective_to)
```

The onboarding layer must not reinterpret these temporal rules.

---

## 7. Initial Load

For a previously unseen customer:

```text
canonical customer
      ↓
SCD2
      ↓
NEW
      ↓
initial historical version
```

The existing implementation should determine how the active version is persisted.

Onboarding is responsible only for providing the valid canonical record.

---

## 8. Normal Change

Example:

```text
customer CUST-1001

status:
ACTIVE → INACTIVE
```

The expected downstream behavior is:

```text
canonical change
      ↓
SCD2 change detection
      ↓
CHANGED
      ↓
new SCD2 version
```

The previous version is closed according to the existing SCD2 temporal semantics and the new version becomes current.

Onboarding does not calculate these dates itself.

---

## 9. Unchanged Customer

If the canonical representation is unchanged:

```text
source update
      ↓
same canonical business state
      ↓
UNCHANGED
```

The SCD2 engine should retain the existing current history without creating an unnecessary version.

---

## 10. Multiple Source Systems

The same canonical customer model may be produced by multiple sources:

```text
CRM
Billing
Support
```

The onboarding layer standardizes them before the SCD2 boundary.

The SCD2 layer therefore operates on a canonical representation rather than source-specific naming.

Example:

```text
CRM:
cust_no → customer_id

Billing:
account_id → customer_id

Support:
user_ref → customer_id
```

The source-specific mapping disappears at the canonical boundary.

---

## 11. Mapping Version vs SCD2 Version

These are distinct:

```text
Mapping v1
Mapping v2
```

describe transformation configuration.

SCD2 versions describe business history:

```text
Customer version 1
Customer version 2
Customer version 3
```

A mapping change does not itself mean that the customer's business state changed.

Do not create SCD2 history merely because a mapping version changed.

---

## 12. Canonical Schema Version vs SCD2

A canonical schema version change does not automatically mean every customer has changed.

For example:

```text
customer.v1
→
customer.v2
```

may add an optional field.

That is a schema evolution event.

It is not necessarily a business-state change for every customer.

The system must keep schema evolution and business-state history conceptually separate.

---

## 13. Guardrail Placement

The existing deterministic guardrail should operate after canonicalization and SCD2 calculation/validation.

Sequence:

```text
validated canonical batch
        ↓
SCD2 computation
        ↓
SCD2 validation
        ↓
Guardrail
```

Then:

```text
NORMAL
   ↓
historical commit
```

or:

```text
SUSPICIOUS
   ↓
hold before historical propagation
```

The exact guardrail rules remain owned by the parent SCD2 implementation.

---

## 14. Historical Protection

The important property is:

```text
suspicious canonical batch
        ↓
guardrail
        ↓
HOLD
```

must not mutate the downstream historical state before recovery.

The original SCD2 Copilot already implements this concept through deterministic guardrail evaluation and evidence/containment workflows.

The onboarding integration should reuse that stable behavior rather than create a separate "onboarding guardrail."

---

## 15. AI Role

AI remains downstream of deterministic evidence.

For example:

```text
SCD2 engine:
status changed for 4,200 customers

Guardrail:
MASS_DEACTIVATION triggered

AI:
Explain evidence in human-readable form
```

The AI does not:

```text
decide mass deactivation
decide historical state
approve recovery
modify history
```

This preserves the existing principle:

> **The engine decides. Validation protects. AI explains.**

---

## 16. Integration Adapter

The onboarding domain should expose a clear boundary for handing validated canonical data to the existing SCD2 capability.

Prefer an integration service/adapter over direct coupling to internal SCD2 implementation details.

Conceptually:

```text
OnboardingService
      ↓
ValidatedCanonicalCustomerBatch
      ↓
SCD2IntegrationAdapter
      ↓
Existing SCD2 capability
```

The adapter should translate only what is necessary.

Do not duplicate SCD2 business logic in the adapter.

---

## 17. Failure Semantics

A failure before SCD2 processing:

```text
mapping failure
transformation failure
validation failure
```

belongs to onboarding.

A failure during SCD2 historical processing belongs to the SCD2 integration boundary and must preserve the existing SCD2 failure/transaction semantics.

Do not collapse both failure classes into one generic error.

---

## 18. Transaction Boundary

The exact transaction boundary must follow the existing SCD2 implementation.

The onboarding project must not pretend that source ingestion and SCD2 historical persistence are one atomic transaction unless the actual implementation provides that guarantee.

At minimum, the system should guarantee that:

```text
invalid canonical records
```

do not enter the SCD2 historical path.

The SCD2 guardrail must preserve its existing no-history-leakage behavior for suspicious batches.

---

## 19. Historical Persistence

The parent repository already provides generic historical persistence designed for configurable monitored entities.

Where the existing code supports reuse, the logical customer identity can be represented as:

```text
entity_key:
{
  "customer_id": "CUST-1001"
}
```

and canonical business attributes can be represented as the tracked historical attributes.

Use the existing persistence contract rather than creating a second history subsystem.

The actual current database schema and repository code are authoritative.

---

## 20. Point-in-Time History

The SCD2 layer should continue to support historical inspection according to its existing semantics.

The onboarding project should not create its own alternate temporal query model.

A useful final workflow is:

```text
Customer
 ↓
Onboarding run
 ↓
Canonical state
 ↓
SCD2 historical versions
 ↓
point-in-time inspection
```

---

## 21. Cross-Source Identity Limitation

The initial MVP does not include sophisticated entity resolution.

Therefore:

```text
cust_no = CUST-1001
account_id = 582194
user_ref = USER-11
```

cannot be assumed to refer to the same customer merely because an operator believes they do.

The onboarding platform maps source identifiers into the canonical `customer_id` when explicitly configured.

A future identity-resolution capability would require a separate architecture decision.

---

## 22. SCD2 Integration Acceptance Criteria

The integration is correct when:

1. only validated canonical records cross the onboarding/SCD2 boundary
2. `customer_id` is used as the canonical SCD2 business key
3. existing SCD2 change detection is reused
4. existing SCD2 temporal semantics are preserved
5. unchanged customers do not receive artificial historical versions
6. genuine customer changes produce new historical versions according to existing SCD2 rules
7. suspicious changes can be held by the existing guardrail
8. held suspicious batches do not mutate history before recovery
9. AI never controls SCD2 decisions
10. mapping/schema-version changes are not confused with business-state changes
11. no second SCD2 engine is introduced

---

## 23. Explicit Non-Goals

This integration does not add:

- a new SCD2 algorithm
- entity-resolution/record-linkage ML
- a second historical database
- a separate guardrail engine
- source-transaction blocking
- true event-driven CDC
- automatic cross-system identity merging

The existing SCD2 capability remains the historical-control subsystem.
