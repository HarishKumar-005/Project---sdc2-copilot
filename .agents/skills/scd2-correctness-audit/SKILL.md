---
name: scd2-correctness-audit
description: Analyze and harden SCD Type 2 correctness, including duplicate keys, temporal intervals, snapshot semantics, deletion policy, and deterministic invariants, while preserving existing behavior.
---

# SCD2 Correctness Audit Skill

## Goal

Improve correctness without redesigning the project.

## Investigation checklist

Verify the actual implementation of:

- business-key detection
- duplicate-key handling
- active target filtering
- NEW / CHANGED / UNCHANGED / DELETED classification
- historical-row preservation
- changed-row closure
- changed-row replacement
- delete semantics
- effective_from / effective_to
- is_current
- null-key handling
- schema compatibility

## Temporal contract

Prefer half-open intervals:

```text
[effective_from, effective_to)
```

This means an instant equal to `effective_to` is outside the closed version and may belong to the next version.

Do not change the project's temporal representation without first checking compatibility with existing fixtures/tests and documenting the migration.

## Snapshot semantics

Distinguish:

- full snapshot
- incremental snapshot

Only a complete snapshot can safely infer deletion from absence.

## Duplicate keys

Never silently overwrite duplicate source or active-target rows.

Use an explicit quality gate.

Affected duplicate groups should be rejected or quarantined according to the configured policy.

## Delete policy

If a delete policy exists in the UI/configuration, ensure it actually reaches the deterministic engine.

A visible control that does not affect behavior is a defect.

## Validation invariants

Confirm:

- at most one current row per business key
- no overlapping validity intervals
- valid effective-date ordering
- unchanged records do not generate new versions
- changed records close the prior version and create the new one
- new records get a current version
- deletes preserve history

## Test-first requirement

Before changing engine logic, add regression coverage for the exact failure mode.

Run focused tests first, then the full routine suite.

## Output

Report:

- confirmed current behavior
- invariant violation or risk
- minimal implementation change
- tests added/updated
- measured result
