# SCD2 Copilot — Testing Rule

## Required workflow

For every non-trivial change:

```text
inspect existing tests
→ identify affected invariants
→ add/update regression tests
→ implement
→ run focused tests
→ run full routine suite
→ review diff
```

## Never weaken tests to make a change pass

If a test fails after an implementation change:

1. determine whether behavior intentionally changed
2. update the contract and test if the change is correct
3. otherwise fix the implementation

Do not delete or loosen assertions simply to obtain green CI.

## SCD2 regression coverage

Changes affecting the engine should consider:

- NEW
- CHANGED
- UNCHANGED
- DELETED
- empty source/target
- null business keys
- duplicate business keys
- composite keys
- historical rows
- current rows
- temporal boundary cases
- date inputs
- datetime inputs
- invalid dates
- full snapshots
- incremental snapshots
- delete policy
- Type 1/Type 2 configuration when implemented
- schema evolution when implemented

## External AI

Routine correctness tests must not require a real external LLM.

Use mocks/fakes for provider behavior and explicit integration tests for real-provider connectivity.

Validate:

- structured schema
- fallback behavior
- token/latency metadata
- missing explanation fallback
- provider failure paths

## Benchmarks

Benchmarks are NOT normal tests.

Keep them isolated and explicitly invoked/marked.
Routine `pytest` should remain suitable for fast correctness verification.
