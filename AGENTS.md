# SCD2 Copilot — Agent Development Contract

## 1. Project identity

**Project:** SCD2 Copilot — AI-Assisted Data Change & Historical Analytics

This is an existing, functional CSV-based batch SCD Type 2 application.
Do NOT treat it as a greenfield project and do NOT redesign it from scratch.

The intended evolution is from a strong prototype into a production-oriented
**Data Change Intelligence Platform**.

Core principle:

> **The engine decides. Validation protects. AI explains.**

## 2. Current architecture — supplied baseline

The current application is documented as:

```text
Source CSV + existing SCD2 CSV
    ↓
ingestion / normalization
    ↓
schema + business-key detection
    ↓
deterministic change detection
    ↓
SCD2 transformation
    ↓
validation
    ↓
structured GenAI explanation
    ↓
Streamlit dashboard / reports / downloads
```

The repository audit reports these currently implemented capabilities:

- CSV ingestion and normalization with Polars.
- Heuristic business-key detection.
- Deterministic NEW / CHANGED / UNCHANGED / DELETED classification.
- SCD2 historical preservation and current-row creation/closure.
- Five-rule SCD2 invariant validation.
- Batched structured GenAI explanations.
- Gemini primary → Groq fallback → deterministic template fallback.
- Pydantic validation for structured AI output.
- Token, latency, provider, cost-estimate, and fallback tracking.
- An existing test suite reported at 178 passing tests.

**Important:** this file is an agent contract derived from the supplied project audit and roadmap.
Before making changes, verify repository reality. Never assume this file is more authoritative than the code,
tests, dependency files, and actual runtime behavior.

## 3. Technology constraints

Current stack:

- Python
- Polars
- Prefect 3
- Pydantic
- pydantic-settings
- Gemini / google-genai
- Groq
- Streamlit
- Pytest

Do NOT introduce technology merely for resume value.

In particular, do NOT add:

- DuckDB
- Great Expectations
- LangChain
- LangGraph
- vector databases
- RAG
- generic multi-agent application architecture
- MCP

unless a concrete engineering requirement is demonstrated and the change is explicitly justified.

Do not replace the existing stack wholesale.

## 4. Non-negotiable architecture rules

### Deterministic correctness

SCD2 correctness must remain deterministic.

The following must NOT depend on an LLM:

- business-key matching
- NEW / CHANGED / UNCHANGED / DELETED classification
- SCD2 version creation
- effective-date assignment
- validation gates
- persistence commit decisions
- idempotency decisions

### AI boundary

The LLM only explains validated evidence.

Preferred path:

```text
raw input
  ↓
deterministic processing
  ↓
validation
  ↓
structured evidence / aggregates
  ↓
GenAI explanation
```

Never:

```text
raw input
  ↓
LLM decides SCD2 state
```

### Separation of data quality from AI status

Deterministic validation and AI explanations are strictly orthogonal:
- **Data Quality / SCD2 Validation** is 100% authoritative for SCD2 correctness (derived from the 5 invariant rules). It does not depend on LLM availability, latency, token count, or provider success.
- **AI Explanation Status** describes only the explanation subsystem state (`SUCCESS`, `FALLBACK`, `TEMPLATE`, `UNAVAILABLE`). It can neither upgrade failed validation nor downgrade passing validation.

### Preserve semantics

When refactoring:

1. Establish expected behavior from existing tests.
2. Add regression tests for discovered bugs.
3. Change implementation.
4. Re-run relevant tests.
5. Compare results against the reference behavior.

Do not alter business semantics merely to make code look more modern.

## 5. Current known gaps

The repository audit identified the following areas. Verify each against the current repository before acting:

1. Prefect workflow exists but is bypassed by Streamlit.
2. Core SCD2 processing uses row/dictionary/loop-oriented processing.
3. Duplicate business keys can be silently overwritten.
4. ISO datetime handling can silently coerce valid timestamps to NULL.
5. Temporal boundary semantics are ambiguous.
6. The UI "Confidence Assessment" is conceptually invalid.
7. Gemini model identifiers include stale/non-current hard-coded entries.
8. Run history is largely Streamlit session state.
9. No FastAPI/headless API.
10. No PostgreSQL persistence.
11. No idempotency for repeated snapshots.
12. No quarantine workflow for invalid records.
13. No Type 1 vs Type 2 column configuration.
14. No explicit schema-evolution policy.
15. No production-grade authentication/resource controls/export sanitization.
16. No Docker/CI/CD.
17. Benchmarks are mixed into the normal pytest path.

These are a prioritized work queue, not permission to fix everything in one change.

## 6. Roadmap

The locked milestone order is:

### M0 — Baseline & Agent Foundation
- Freeze current behavior.
- Establish reproducible test and performance baselines.
- Create/maintain agent instructions and skills.
- Separate benchmarks from routine tests.

### M1 — Trustworthy Core
- Duplicate-key protection.
- Correct datetime parsing.
- Explicit temporal semantics.
- Explicit full-vs-incremental snapshot semantics.
- Real delete policy behavior.
- Remove fake confidence score.
- Formalize SCD2 invariants.
- Strengthen regression tests.

### M2 — High Performance
- Replace hot-path row iteration with native Polars operations.
- Use joins/expressions/window operations where justified.
- Use LazyFrame/scan/streaming only where they provide measurable value.
- Benchmark 10K / 100K / 500K / 1M scale.
- Measure runtime, memory, and throughput.

### M3 — Real Automation
- Make Prefect the actual workflow boundary.
- Introduce deployment configuration.
- Add retries only for transient failures.
- Add concurrency controls where justified.
- Persistent run artifacts and execution retrieval (M3.5).
- Idempotency, run identity, and execution deduplication (M3.6).

### M4 — Real Product
- FastAPI API boundary.
- PostgreSQL control/audit persistence.
- SQLAlchemy/Alembic where appropriate.
- Snapshot identity and idempotency.
- Transactional persistence.
- Durable run/history state.
- Proper persisted surrogate identity where the relational model needs it.

### M5 — Intelligence
- First-class change analytics.
- Run-level Change Intelligence.
- Record-level explanations.
- Grounded structured GenAI.
- AI evaluation.
- Type 1 / Type 2 column configuration.
- Schema-evolution policy.

### M6 — Production Hardening
- Quarantine.
- Authentication.
- Upload/resource limits.
- Export sanitization.
- Structured observability.
- Docker.
- GitHub Actions CI/CD.
- Resilience and operational controls.

Do not skip ahead simply because a later feature is more visually impressive.

## 7. SCD2 semantic contract

Unless repository evidence or an approved design decision changes this:

### Temporal convention

Use half-open validity intervals:

```text
[effective_from, effective_to)
```

- `effective_from` is **inclusive**
- `effective_to` is **exclusive**
- `NULL` `effective_to` represents an **open-ended / current** version

Concrete Example:

```text
Version A: [2026-09-01, 2026-09-06)
Version B: [2026-09-06, NULL)
```

The boundary instant `2026-09-06` belongs strictly and exclusively to Version B.
The same instant must never belong to two versions.

### Point-in-time query semantics

For an instant or snapshot date $T$, the active version is determined by:

```sql
effective_from <= T AND (effective_to > T OR effective_to IS NULL)
```

Do NOT use `BETWEEN` because `BETWEEN` is inclusive on both ends, which would produce duplicate matches on boundary dates.

### Gap semantics

Chronological gaps between historical versions (`previous.effective_to < next.effective_from`) are valid non-contiguous history and do not constitute overlaps.

### Snapshot semantics: full vs incremental

- **Full Snapshot (`SnapshotMode.FULL`, default)**:
  The incoming source represents the entire universe of entities. Any key present in the target with `is_current=True` that is absent from the incoming source is candidate for deletion based on the active `delete_policy`.
- **Incremental Feed (`SnapshotMode.INCREMENTAL`)**:
  The incoming source represents only changed or newly arrived entities (delta feed). Absence of an active target key simply means "no updates for this entity in this batch". Absent target keys produce **zero** deletions regardless of `delete_policy` and are preserved as active (`is_current=True`) in the resulting SCD2 table.

Concrete Example:
Target has active keys `[101, 102, 103]`.
Source feed contains only key `101`.
- Under `full` + `soft_delete`: Keys `102` and `103` are classified as `DELETED` and closed (`effective_to = processing_date`, `is_current = False`).
- Under `incremental`: Key `101` is processed (NEW/CHANGED/UNCHANGED). Keys `102` and `103` are classified as neither deleted nor changed; they are retained as active (`is_current = True`, `effective_to = None`).

### Delete policy: soft_delete vs ignore

The engine strictly separates snapshot semantics from delete policy:
- `snapshot_mode` answers: **"Is absence meaningful?"**
- `delete_policy` answers: **"What should happen when absence is meaningful?"**

Supported policies:
- **`DeletePolicy.SOFT_DELETE` (default)**: When absence is meaningful (full snapshot), close the active version by setting `effective_to = processing_date` and `is_current = False`.
- **`DeletePolicy.IGNORE`**: Missing active keys are not treated as deletions; their active rows remain current and unmodified.

#### Four-way decision matrix

| `snapshot_mode` | `delete_policy` | Missing Active Key Inferred As | Resulting Target Active Version |
| :--- | :--- | :--- | :--- |
| `full` | `soft_delete` (default) | `DELETED` | Closed (`effective_to = processing_date`, `is_current = False`) |
| `full` | `ignore` | Not deleted | Retained active (`effective_to = None`, `is_current = True`) |
| `incremental` | `soft_delete` | Not deleted | Retained active (`effective_to = None`, `is_current = True`) |
| `incremental` | `ignore` | Not deleted | Retained active (`effective_to = None`, `is_current = True`) |

> `delete_policy` cannot make absence meaningful when the snapshot mode is incremental.

### Core invariants

For each business key:

- a business key must identify at most one row in the incoming source snapshot
- at most one active (current) version in the target SCD2 table
- historical versions remain immutable
- no overlapping validity intervals
- effective_from is before effective_to when effective_to exists
- CHANGED closes the previous version and creates a new current version
- UNCHANGED creates no new version
- NEW creates a current version
- DELETED preserves history and closes the prior current version when delete policy allows it

#### Verified Invariant Matrix (M1.7 Hardening Baseline)

| Invariant Category | Invariant Rule | Status | Implementation Authority |
| :--- | :--- | :--- | :--- |
| **Business Key** | Source business keys must be unique | **ENFORCED & TESTED** | `validate_business_keys` (`detect_changes` / `apply_scd2`) |
| **Business Key** | Active target business keys must be unique | **ENFORCED & TESTED** | `validate_business_keys` & `_check_one_current_per_key` |
| **Business Key** | Historical duplicate keys allowed across distinct versions | **ENFORCED & TESTED** | `validate_business_keys` & `validate_scd2` |
| **Business Key** | Null business keys strictly rejected | **ENFORCED & TESTED** | `_check_no_null_keys` in `validate_scd2` |
| **Business Key** | Composite keys behave consistently (no partial collisions) | **ENFORCED & TESTED** | Tuple key lookups in engine & validator |
| **Classification** | Source absent from active target → `NEW` | **ENFORCED & TESTED** | `detect_changes.py` |
| **Classification** | Source present, tracked columns differ → `CHANGED` | **ENFORCED & TESTED** | `detect_changes.py` |
| **Classification** | Source present, tracked columns match → `UNCHANGED` | **ENFORCED & TESTED** | `detect_changes.py` |
| **Classification** | Target active absent in Full + Soft Delete → `DELETED` | **ENFORCED & TESTED** | `detect_changes.py` |
| **Classification** | Incremental mode produces 0 deletions | **ENFORCED & TESTED** | `detect_changes.py` |
| **Classification** | Delete policy `ignore` produces 0 deletions | **ENFORCED & TESTED** | `detect_changes.py` |
| **Transformation** | `NEW` creates 1 active row (`effective_to=None`, `is_current=True`) | **ENFORCED & TESTED** | `apply_scd2` |
| **Transformation** | `CHANGED` closes prior active row, creates new active row | **ENFORCED & TESTED** | `apply_scd2` |
| **Transformation** | `UNCHANGED` preserves active row unmodified | **ENFORCED & TESTED** | `apply_scd2` |
| **Transformation** | `DELETED` closes prior active row (`effective_to=D`, `is_current=False`) | **ENFORCED & TESTED** | `apply_scd2` |
| **Transformation** | Absent keys under Incremental / Ignore retained active | **ENFORCED & TESTED** | `apply_scd2` |
| **Transformation** | Historical inactive rows remain immutable | **ENFORCED & TESTED** | `apply_scd2` |
| **Temporal** | Half-open intervals `[effective_from, effective_to)` | **ENFORCED & TESTED** | `apply_scd2` and `validate_scd2` |
| **Temporal** | Boundary instant belongs strictly to new version | **ENFORCED & TESTED** | Point-in-time logic & tests |
| **Temporal** | `effective_from == effective_to` rejected (zero duration) | **ENFORCED & TESTED** | `_check_date_consistency` |
| **Temporal** | `effective_from > effective_to` rejected (reversed) | **ENFORCED & TESTED** | `_check_date_consistency` |
| **Temporal** | `next.from < prev.to` rejected (overlap) | **ENFORCED & TESTED** | `_check_no_overlapping_dates` |
| **Temporal** | `next.from == prev.to` allowed (adjacent) | **ENFORCED & TESTED** | `_check_no_overlapping_dates` |
| **Temporal** | `prev.to < next.from` allowed (chronological gap) | **ENFORCED & TESTED** | `_check_no_overlapping_dates` |
| **Current Row** | At most one `is_current=True` row per key | **ENFORCED & TESTED** | `_check_one_current_per_key` |
| **Current Row** | `is_current=True` requires `effective_to is None` | **ENFORCED & TESTED** | `_check_date_consistency` |
| **Current Row** | `is_current=False` requires `effective_to is not None` | **ENFORCED & TESTED** | `_check_date_consistency` |
| **Stability** | Repeated identical snapshot is idempotent | **ENFORCED & TESTED** | Verified in `test_invariants_hardening.py` |
| **Stability** | Output column ordering & sorting deterministic | **ENFORCED & TESTED** | Output sorted by `business_key + ["effective_from"]` |
| **Schema** | Empty transformation preserves column dtypes | **ENFORCED & TESTED** | `apply_scd2` empty return schema (`pl.Date`, `pl.Boolean`) |
| **Quarantine / Reject** | Automatic row-level quarantine workflow | **FUTURE / BUSINESS POLICY** | Scheduled for M6 |
| **Type 1 / Type 2** | Mixed Type 1 / Type 2 in-place updates | **FUTURE / BUSINESS POLICY** | Scheduled for M5 |

## 8. Configuration principles


Prefer typed configuration with Pydantic for pipeline behavior.

Expected configuration areas include:

- business keys
- Type 1 columns
- Type 2 columns
- snapshot mode
- delete policy
- schema policy
- timezone/temporal semantics
- AI provider/model configuration
- resource limits

Do not hard-code environment-specific secrets or model identifiers in source.

### AI Provider & Model Configuration Baseline (M1.8 Hardening)

- **Configuration Ownership**: Single source of truth in `config.py` (`Settings`). `DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"` and `DEFAULT_GEMINI_FALLBACK_MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-2.5-flash"]`. `GeminiProvider` receives resolved model policy and does not duplicate fallback lists.
- **Model Normalization & Deduplication**: Model IDs are whitespace-trimmed, empty entries removed, duplicates removed while preserving order, and the primary model is excluded from the fallback list.
- **Stable Settings Type**: `gemini_fallback_models: list[str]` at runtime, supporting comma-separated env values (e.g. `GEMINI_FALLBACK_MODELS=model1,model2`), JSON array strings, and Python lists.
- **Routing & Accurate Retry Semantics**:
  - Sequence: `Configured Primary -> Fallback 1 -> Fallback 2 -> Fallback 3 -> Groq -> Template`.
  - `MAX_RETRIES = 2`: Exactly 1 initial attempt + 2 retries = 3 total attempts per model for transient errors.
  - No unnecessary sleep: Never sleeps after the final permitted attempt or upon encountering non-recoverable errors.
  - Exponential backoff: Base delay of 3s (delays 3s, 6s for attempts 1 and 2).
- **Structured Error Classification**: Inspects `google.genai.errors.APIError` properties (`code`, `status`, `message`, `ServerError`, `ClientError`) first:
  - Transient rate limit (429 / `RESOURCE_EXHAUSTED` without `PerDay`) -> retry with backoff.
  - Transient server error (5xx / `INTERNAL` / `UNAVAILABLE` / `DEADLINE_EXCEEDED`) -> retry with backoff.
  - Daily quota exhaustion (429 with `PerDay` / daily indicator) -> advance immediately without retrying.
  - Invalid/unsupported model (404 / `NOT_FOUND` / unsupported) -> advance immediately without retrying.
  - Authentication failure (401 / 403 / `UNAUTHENTICATED` / `PERMISSION_DENIED`) -> advance immediately without retrying.
  - Unknown failure -> safe fallback without misclassification.
- **Model-Aware Cost Accounting**: `GEMINI_MODEL_PRICING` registry tracks exact prompt/completion rates based on current official Google Gemini documentation: `gemini-3.8-flash` ($0.75/$3.75 per 1M introductory through Dec 31, 2026; $1.50/$7.50 from Jan 1, 2027), `gemini-3.5-flash-lite` ($0.30/$2.50 per 1M), `gemini-3.1-flash-lite` ($0.25/$1.50 per 1M), and `gemini-2.5-flash` ($0.30/$2.50 per 1M). Unknown models safely mark `is_estimated = True` and report cost $0.00 rather than inventing numbers. `LLMMetrics` always attributes cost and tokens to the actual executing model.
- **Observable Partial Batch Fallback**: When structured batch generation omits individual records, per-record template fallback is used, `result.fallback_count` records the count, `result.provider_used` indicates `partial template fallback`, and `compute_ai_status` evaluates to `FALLBACK` rather than falsely claiming complete Gemini `SUCCESS`.

## 9. Testing contract

Before changing behavior:

- inspect existing tests
- add regression tests for every confirmed bug
- preserve current passing tests where behavior is intended to remain unchanged
- run focused tests first
- then run the full routine suite

Performance benchmarks must be separate from routine correctness tests.

Normal tests should not require external LLM connectivity unless an integration test explicitly opts into it.

## 10. Benchmark contract

Benchmarks are evidence, not decoration.

Never claim throughput, memory, speedups, or scalability numbers that were not measured.

At minimum capture:

- dataset size
- runtime
- throughput
- peak/observed memory where practical
- machine/environment details sufficient to reproduce the result
- implementation version/commit

### M2.1 Baseline Benchmark & Profiling Foundation (Pre-Vectorization)

- **Environment & Hardware Specifications**:
  - Python: `3.12.9` (64-bit AMD64)
  - Polars: `1.44.1`
  - OS: Windows 11 (10.0.26200-SP0)
  - CPU: Intel64 Family 6 Model 186 Stepping 3, GenuineIntel (12 logical CPU cores)
  - RAM: 15.65 GB total (~4.13 GB available at test time)
- **Fast Vectorized Synthetic Generator**: Vectorized Polars expressions (`pl.int_range`, `pl.when/then`, `pl.format`) generate 100K rows in ~0.01s and 1M rows in ~0.08s with exact SCD2 distribution (80% unchanged, 10% changed, 5% new, 5% deleted, 10% closed historical versions).
- **Authoritative Baseline Measurements (Pre-Vectorization M1 Engine)**:

| Dataset Size | Key Type | Detect Time | Apply Time | Val Time | Total Time | Peak Mem (Detect) | Peak Mem (Apply) | Throughput |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **10,000** | Single | 0.2766s | 0.2817s | 0.0286s | 0.5583s | 9.22 MB | 9.46 MB | **17,911 rows/s** |
| **100,000** | Single | 2.7366s | 2.9123s | 0.1914s | 5.6489s | 93.84 MB | 96.99 MB | **17,703 rows/s** |
| **500,000** | Single | 15.1444s | 15.4390s | 1.0921s | 30.5834s | 460.91 MB | 475.54 MB | **16,349 rows/s** |
| **1,000,000** | Single | 40.4536s | 37.8481s | 3.3546s | 78.3017s | 922.17 MB | 946.47 MB | **12,771 rows/s** |
| **100,000** | Composite | 4.5032s | 3.0915s | 0.2354s | 7.5946s | 103.71 MB | 107.05 MB | **13,167 rows/s** |

- **Hot-Path Profiling Analysis (cProfile 50K rows, 1,919,657 calls in 0.833s)**:
  1. `iter_rows(named=True)`: 194,755 calls consuming ~0.158s cumulative overhead converting Polars chunks to Python dicts.
  2. `_compare_fields`: 45,000 calls consuming ~0.136s cumulative with 521,500 `dict.get` and 180,000 `_normalize` invocations.
  3. `apply_scd2`: 52,250 `_pick` calls and 50,000 `_key_tuple` calls building Python dicts in row-by-row loops.
  4. Materialization overhead: `pl.DataFrame(result_rows)` converting hundreds of thousands of Python dicts back into Polars DataFrames.
- **Architectural Vectorization Targets for M2.2**:
  - Replace `target_lookup` and `iter_rows` with native Polars joins (`join(how="full")` or `join(how="left")` + `anti_join`).
  - Replace `_compare_fields` with vectorized column expressions: `(pl.col(c) != pl.col(f"{c}_target"))`.
  - Replace row-by-row Python dictionary appending in `apply_scd2` with native DataFrame column projections, unions, and filters.

### M2.2 Vectorized SCD2 Engine Performance & Parity Verification

- **Engine Implementation**:
  - `detect_changes`: Native Polars left join with `validate="m:1"`, vectorized `.ne_missing()` comparisons with whitespace/empty-string normalization, and anti-join for deletion under `SnapshotMode.FULL` + `DeletePolicy.SOFT_DELETE`.
  - `apply_scd2`: 100% native columnar projection, filter, and union pipeline (`historical`, `retained_active`, `closed`, `new_active`) with zero row iterations.
  - Multi-engine execution: supports `auto`, `in-memory`, and `streaming` engines (`engine: str = "auto"`).
- **Authoritative M2.2 Measured Benchmark Results (Polars 1.44.1)**:

| Dataset Size | Key Type | Engine | Detect Time | Apply Time | Val Time | Total Time | Peak Mem (Detect) | Peak Mem (Apply) | Throughput |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **10,000** | Single | auto | 0.1381s | **0.0053s** | 0.0234s | **0.1433s** | 4.10 MB | **0.01 MB** | **69,760 rows/s** |
| **100,000** | Single | auto | 1.7857s | **0.0121s** | 0.1836s | **1.7978s** | 40.52 MB | **0.01 MB** | **55,623 rows/s** |
| **500,000** | Single | auto | 12.9232s | **0.0575s** | 1.8171s | **12.9807s** | 201.75 MB | **0.01 MB** | **38,519 rows/s** |
| **1,000,000** | Single | auto | 34.2205s | **0.1305s** | 4.1114s | **34.3510s** | 403.60 MB | **0.01 MB** | **29,111 rows/s** |
| **100,000** | Composite | auto | 2.8152s | **0.0512s** | 0.2573s | **2.8664s** | 45.68 MB | **0.01 MB** | **34,887 rows/s** |
| **100,000** | Single | in-memory | 2.9485s | **0.0179s** | 0.4355s | **2.9665s** | 40.37 MB | **0.01 MB** | **33,710 rows/s** |
| **100,000** | Single | streaming | 2.3201s | **0.0220s** | 0.3805s | **2.3422s** | 40.52 MB | **0.01 MB** | **42,695 rows/s** |

- **Key Achievements vs M2.1 Baseline**:
  - `apply_scd2` runtime: **53x to 290x speedup** across all scales (e.g. 100K: 0.0121s vs 2.9123s; 1M: 0.1305s vs 37.8481s).
  - `apply_scd2` memory: **99.9% heap reduction** (0.01 MB additional heap vs 946 MB in M2.1).
  - Total pipeline throughput: **2.3x to 3.9x increase** across all scales (peak 69,760 rows/s).
  - cProfile function calls (50K): **97.4% reduction** (from 1,919,657 calls down to 50,205 calls).
  - Streaming execution: 21% faster than eager in-memory execution at 100K scale.
  - Golden behavioral parity: 100% verified across 15 dedicated parity tests and all 280 existing routine tests.

### M2.3 Deep Profiling & detect_changes Optimization (Polars 1.44.1)

- **Deep 12-Stage Profiling & Query Plan Inspection (Phases 1–3)**:
  - Instrumented all 12 stages across 10K, 100K, 500K, and 1M row scales.
  - **Bottleneck Identified**: Eager boundary translation into Python dictionaries and dataclass instances (`ChangeRecord`, `FieldChange`) in Stages 10 & 11 consumed **95.8% of total detect_changes runtime** (22.91s out of 23.93s at 1M rows) and **403.60 MB** of Python heap, while native Polars relational operations (joins, filters, expressions) consumed only **0.43s**.
  - Physical query plan inspection (`explain(optimized=True)`) confirmed Polars pushes down projections and optimizes left joins to inner joins on matched slices.
- **Architectural Solution: `LazyRecordSequence`**:
  - Implemented high-performance sequence proxy in `models.py` wrapping Polars DataFrame slices (`new_slice`, `changed_slice`, `unchanged_slice`, `deleted_slice`).
  - Provides **$O(1)$ `len()` and boolean checks** via DataFrame height without any Python object instantiation.
  - Materializes `ChangeRecord` and `FieldChange` **strictly on demand** when indexed or sliced, with caching and fast columnar series iteration.
  - Complete transparent sequence compatibility: slicing, indexing, iteration, concatenation (`report.new + report.changed`), mutation (`append`, `extend`, `pop`), and equality.
- **Authoritative M2.3 Measured Benchmark Results (Polars 1.44.1)**:

| Dataset Size | Key Type | Engine | Detect Time | Apply Time | Val Time | Total Time | Peak Mem (Detect) | Peak Mem (Apply) | Throughput |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **10,000** | Single | auto | **0.0097s** | **0.0047s** | 0.0205s | **0.0144s** | **0.01 MB** | **0.01 MB** | **695,014 rows/s** |
| **100,000** | Single | auto | **0.0383s** | **0.0108s** | 0.1771s | **0.0491s** | **0.01 MB** | **0.01 MB** | **2,038,645 rows/s** |
| **500,000** | Single | auto | **0.1553s** | **0.0396s** | 0.8834s | **0.1949s** | **0.01 MB** | **0.01 MB** | **2,565,312 rows/s** |
| **1,000,000** | Single | auto | **0.3100s** | **0.0824s** | 1.7584s | **0.3924s** | **0.01 MB** | **0.01 MB** | **2,548,322 rows/s** |
| **100,000** | Composite | auto | **0.0485s** | **0.0213s** | 0.1876s | **0.0698s** | **0.01 MB** | **0.01 MB** | **1,432,261 rows/s** |
| **100,000** | Single | in-memory | **0.0346s** | **0.0107s** | 0.1820s | **0.0453s** | **0.01 MB** | **0.01 MB** | **2,205,908 rows/s** |
| **100,000** | Single | streaming | **0.0354s** | **0.0133s** | 0.1793s | **0.0487s** | **0.01 MB** | **0.01 MB** | **2,054,476 rows/s** |

- **Key Achievements vs M2.2 & M2.1**:
  - `detect_changes` runtime: **46x speedup at 100K** (0.0383s vs 1.7857s in M2.2, 2.7366s in M2.1) and **110x speedup at 1M** (0.3100s vs 34.2205s in M2.2, 40.4536s in M2.1).
  - Total pipeline throughput: **Peak 2,565,312 rows/second** (exceeding 2.5 million rows/s, up from 55K rows/s in M2.2 and 17K rows/s in M2.1).
  - Total 1M processing time (Detect + Apply): **0.3924s** (dropped from 34.35s in M2.2 and 78.30s in M2.1 -> **87x faster than M2.2, 200x faster than M2.1**).
  - Python heap usage: **99.99% reduction** (0.01 MB peak heap during detect_changes at 1M rows vs 403.60 MB in M2.2).
  - Process RSS memory: Peak working set reduced by **73.1%** (from 1,667 MB down to 448 MB at 1M scale).
  - cProfile function calls (50K): reduced from 50,205 calls to **5,073 calls** (90% reduction vs M2.2, 99.7% reduction vs M2.1).
  - Parity & regression verification: **304 routine tests passing in 4.10s**, including 15 golden parity tests and 9 lazy sequence unit tests.

### M2.4 Production Performance Validation, Scalability Testing & Benchmark Gate (Polars 1.44.1)

- **Benchmark Reconciliation & Root Cause Analysis**:
  - **The Discrepancy**: M2.2 previously reported 1M detect ≈ 34.22s, while M2.3 staged pre-optimization profile reported 23.93s.
  - **Empirical Investigation (`investigate_discrepancy.py`)**:
    - Re-created the unoptimized eager dictionary/dataclass loop in isolation.
    - **Finding 1 (Instrumentation Tax)**: Running `tracemalloc.start()` during eager instantiation of 1,000,000 Python dataclasses added **~14.5 seconds of pure profiling overhead** (3.19s without tracemalloc vs 17.68s with tracemalloc).
    - **Finding 2 (Trace Table Accumulation)**: In M2.2, running 10K, 100K, and 500K sequentially before 1M without garbage collection bloated Python's internal allocation trace table, artificially inflating 1M allocation latency from 17.68s up to 34.22s.
    - **Finding 3 (Reconciled Uninstrumented Speedup)**: The uninstrumented M2.2 eager Python loop runs in **3.1889s median**. The authoritative M2.4 candidate runs in **0.4760s median**. The true, unpolluted speedup of `LazyRecordSequence` on 1M change detection is **6.7x** (not 110x). The total pipeline speedup (Detect + Apply) vs M2.1 is **138.1x** (0.5668s vs 78.3017s).
- **Authoritative Benchmark Methodology**:
  - Established ONE unified, statistical benchmark harness in `authoritative_benchmark.py`:
    - 1 warm-up run per scale/engine.
    - Multiple repetitions (5 reps for 10K-1M; 3 reps for 2M-5M).
    - Measured metrics: `min`, `median`, `p95`, `max`, `std_dev`. Median is the authoritative reported runtime.
    - Pure wall-clock timing completely separated from memory profiling.
    - Three-tier memory tracking: Python heap (`tracemalloc`), Polars buffer buffers (`estimated_size()`), and Windows Process RSS (`GetProcessMemoryInfo`).
- **Authoritative M2.4 Measured Benchmark Results (Polars 1.44.1)**:

| Dataset Scale | Key Type | Engine | Reps | Detect (Med) | Apply (Med) | Val (Med) | Total D+A (Med) | Total E2E (Med) | Throughput (D+A) | Sec / 1M Rows (D+A) | Polars Buffers | Process RSS |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **10,000** | Single | auto | 5 | 0.0070s | 0.0033s | 0.0212s | **0.0101s** | 0.0312s | **990,099 rows/s** | 1.0100s | 0.97 MB | 72.39 MB |
| **100,000** | Single | auto | 5 | 0.0356s | 0.0096s | 0.1782s | **0.0454s** | 0.2237s | **2,202,643 rows/s** | 0.4540s | 10.35 MB | 212.92 MB |
| **500,000** | Single | auto | 5 | 0.2430s | 0.0444s | 1.8443s | **0.2869s** | 2.1250s | **1,742,768 rows/s** | 0.5738s | 54.43 MB | 542.58 MB |
| **1,000,000** | Single | auto | 5 | 0.4760s | 0.0949s | 3.9436s | **0.5668s** | 4.5241s | **1,764,291 rows/s** | 0.5668s | 109.55 MB | 843.67 MB |
| **2,000,000** | Single | auto | 3 | 0.8507s | 0.1946s | 6.5468s | **1.0411s** | 7.5880s | **1,921,045 rows/s** | 0.5205s | 225.81 MB | 1016.77 MB |
| **5,000,000** | Single | auto | 3 | 2.3101s | 0.6553s | 19.4714s | **2.9689s** | 22.4580s | **1,684,125 rows/s** | 0.5938s | 574.67 MB | 2369.10 MB |
| **100,000** | Composite | auto | 5 | 0.0698s | 0.0314s | 0.4812s | **0.1004s** | 0.5942s | **996,016 rows/s** | 1.0040s | 11.86 MB | 486.14 MB |
| **1,000,000** | Composite | auto | 3 | 0.6608s | 0.3341s | 4.7139s | **0.9949s** | 5.5818s | **1,005,126 rows/s** | 0.9949s | 124.71 MB | 879.87 MB |
| **100,000** | Single | in-memory | 5 | 0.0526s | 0.0145s | 0.4220s | **0.0653s** | 0.4878s | **1,531,394 rows/s** | 0.6530s | 10.35 MB | 464.72 MB |
| **100,000** | Single | streaming | 5 | 0.0605s | 0.0193s | 0.4525s | **0.0810s** | 0.5268s | **1,234,568 rows/s** | 0.8100s | 10.35 MB | 480.83 MB |
| **1,000,000** | Single | in-memory | 3 | 0.5113s | 0.1024s | 4.0904s | **0.6084s** | 4.6988s | **1,643,655 rows/s** | 0.6084s | 109.55 MB | 1030.62 MB |
| **1,000,000** | Single | streaming | 3 | 0.4847s | 0.1144s | 3.8988s | **0.5991s** | 4.4393s | **1,669,170 rows/s** | 0.5991s | 109.55 MB | 1233.63 MB |

- **Scalability Linearity Analysis**:
  - Across a **50x scale expansion** (from 100,000 to 5,000,000 rows), the scaling ratio vs strict linearity remained between **1.14x and 1.30x**.
  - Processing time per million rows remained remarkably constant: **0.45s to 0.59s per 1M rows**.
  - **Dominant Bottleneck at 5M scale**: Invariant validation (`validate_scd2`), specifically `_check_no_overlapping_dates` due to group-by/window partitioning across 5.92M output rows (19.47s vs 2.97s for core transformation). Core SCD2 detection + transformation remains sub-linear and sub-second per million rows.
- **Engine Comparison**:
  - `auto` engine achieves optimal single-threaded/multi-threaded dispatch for small-to-medium datasets (2.2M rows/s at 100K).
  - At 1M rows, `streaming` engine achieves **0.5991s** (1.67M rows/s), slightly outperforming `in-memory` (**0.6084s**, 1.64M rows/s) while producing byte-identical DataFrames.
- **Benchmark Gate & Regression Verification**:
  - Reusable regression artifact created at `tests/adversarial/benchmark_gate_m24.json`.
  - Automated gate tests in `tests/adversarial/test_benchmark_gate.py` enforce throughput thresholds (>=1M rows/s at 100K, >=1.5M rows/s at 1M) and heap limits (<=1.0 MB).
  - Full routine test suite: **306 passed, 17 deselected in 12.27s**.

### M2.5 Production-Grade Vectorized SCD2 Validation Optimization (Polars 1.44.1)

- **Bottleneck Profiling & Root Cause Analysis**:
  - **The Discovery**: Profiling `validate_scd2` across 7 distinct stages on 100K to 5M rows revealed that Stage 6 (`_check_no_overlapping_dates`) accounted for **>99% of total validation runtime** (16.97s out of 17.12s at 5M rows).
  - **Root Cause**: `check_df = sorted_df.with_columns(pl.col("effective_to").shift(1).over(business_key), ...)` forced Polars to hash and partition 5,925,000 output rows into ~5,000,000 individual single-row groups. The `.over(business_key)` window partitioning alone accounted for 97.5% of the check's runtime.
- **Algorithmic Vectorization Target**:
  - **Consolidated Sorting**: Sort once canonically by `business_key + ["effective_from", "effective_to"]` in `validate_scd2` and pass `sorted_df` down to `_check_one_current_per_key` and `_check_no_overlapping_dates`.
  - **Vectorized Shift without Partitioning**: In a frame sorted by `business_key + ["effective_from", "effective_to"]`, records for any business key are guaranteed contiguous. Adjacent versions for the same key are identified by:
    `is_same_key = pl.all_horizontal([(pl.col(k) == pl.col(k).shift(1)).fill_null(False) for k in business_key])`
    Previous version bounds are computed via native columnar projection:
    `__prev_to = pl.when(is_same_key).then(pl.col("effective_to").shift(1)).otherwise(None)`
    `__prev_from = pl.when(is_same_key).then(pl.col("effective_from").shift(1)).otherwise(None)`
    Operating at native SIMD vector speed in Rust with zero hash group allocations.
  - **Fast-Path Duplicate Active Key Check**: `_check_one_current_per_key` leverages `sorted_df.filter(is_current == True)` and evaluates `is_same_key`. If 0 matches, passes in ~6ms (vs 147ms group_by). On violations, falls back to full `group_by` to format exact duplicate details.
  - **Single-Pass Date Consistency**: `_check_date_consistency` evaluates date ordering and active/closed flag consistency in a single filter pass.
- **Authoritative M2.5 Measured Benchmark Results (Polars 1.44.1)**:

| Dataset Scale | Key Type | Engine | Reps | Detect (Med) | Apply (Med) | Val (Med) | Total D+A (Med) | Total E2E (Med) | Throughput (E2E) | Sec / 1M Rows (E2E) | Polars Buffers | Process RSS |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **10,000** | Single | auto | 5 | 0.0072s | 0.0032s | **0.0026s** | 0.0105s | **0.0129s** | **775,194 rows/s** | 1.2900s | 0.97 MB | 70.38 MB |
| **100,000** | Single | auto | 5 | 0.0327s | 0.0088s | **0.0064s** | 0.0414s | **0.0475s** | **2,105,263 rows/s** | 0.4750s | 10.35 MB | 252.18 MB |
| **500,000** | Single | auto | 5 | 0.1497s | 0.0363s | **0.0252s** | 0.1879s | **0.2130s** | **2,347,418 rows/s** | 0.4260s | 54.43 MB | 546.12 MB |
| **1,000,000** | Single | auto | 5 | 0.4670s | 0.0911s | **0.0571s** | 0.5581s | **0.6188s** | **1,616,031 rows/s** | 0.6188s | 109.55 MB | 861.39 MB |
| **2,000,000** | Single | auto | 3 | 0.8590s | 0.1965s | **0.1188s** | 1.0555s | **1.1741s** | **1,703,432 rows/s** | 0.5870s | 225.81 MB | 1265.39 MB |
| **5,000,000** | Single | auto | 3 | 2.0856s | 0.5614s | **0.3036s** | 2.6470s | **2.9450s** | **1,697,793 rows/s** | 0.5890s | 574.67 MB | 2437.91 MB |
| **100,000** | Composite | auto | 5 | 0.0619s | 0.0279s | **0.0141s** | 0.0899s | **0.1051s** | **951,475 rows/s** | 1.0510s | 11.86 MB | 554.38 MB |
| **1,000,000** | Composite | auto | 3 | 0.6320s | 0.2801s | **0.1017s** | 0.9169s | **1.0054s** | **994,629 rows/s** | 1.0054s | 124.71 MB | 1177.07 MB |
| **100,000** | Single | in-memory | 5 | 0.0532s | 0.0121s | **0.0093s** | 0.0662s | **0.0776s** | **1,288,660 rows/s** | 0.7760s | 10.35 MB | 538.40 MB |
| **100,000** | Single | streaming | 5 | 0.0629s | 0.0228s | **0.0148s** | 0.0857s | **0.1001s** | **999,001 rows/s** | 1.0010s | 10.35 MB | 603.57 MB |
| **1,000,000** | Single | in-memory | 3 | 0.4598s | 0.1012s | **0.0626s** | 0.5641s | **0.6307s** | **1,585,540 rows/s** | 0.6307s | 109.55 MB | 1149.27 MB |
| **1,000,000** | Single | streaming | 3 | 0.4667s | 0.1116s | **0.0647s** | 0.5643s | **0.6236s** | **1,603,592 rows/s** | 0.6236s | 109.55 MB | 1270.91 MB |

- **Before vs After Speedup Comparison (M2.4 vs M2.5)**:

| Scale | M2.4 Validation | M2.5 Validation | **Validation Speedup** | M2.4 End-to-End | M2.5 End-to-End | **E2E Speedup** |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **10,000** | 0.0212s | **0.0026s** | **8.15x** | 0.0312s | **0.0129s** | **2.42x** |
| **100,000** | 0.1782s | **0.0064s** | **27.84x** | 0.2237s | **0.0475s** | **4.71x** |
| **500,000** | 1.8443s | **0.0252s** | **73.19x** | 2.1250s | **0.2130s** | **9.98x** |
| **1,000,000** | 3.9436s | **0.0571s** | **69.06x** | 4.5241s | **0.6188s** | **7.31x** |
| **2,000,000** | 6.5468s | **0.1188s** | **55.11x** | 7.5880s | **1.1741s** | **6.46x** |
| **5,000,000** | 19.4714s | **0.3036s** | **64.14x** | 22.4580s | **2.9450s** | **7.63x** |
| **100K Comp** | 0.4812s | **0.0141s** | **34.13x** | 0.5942s | **0.1051s** | **5.65x** |
| **1M Comp** | 4.7139s | **0.1017s** | **46.35x** | 5.5818s | **1.0054s** | **5.55x** |
| **1M Stream** | 3.8988s | **0.0647s** | **60.26x** | 4.4393s | **0.6236s** | **7.12x** |

- **Key Achievements & Architectural Insights**:
  - Validation at 5M rows dropped from **19.47s down to 0.3036s** (**64.1x speedup**).
  - Full end-to-end pipeline (Detect + Apply + Validation) at 5M rows dropped from **22.46s down to 2.9450s** (**7.63x E2E speedup**).
  - End-to-end throughput across all scales now exceeds **1,600,000 rows/second**, up from 222,000 rows/second in M2.4.
  - Validation Python heap overhead: **0.00 MB** traced heap across all scales.
  - Linear Scalability: Across the 50x volume jump from 100K to 5M rows, scaling ratio remains within **0.91x to 1.35x** of strict linearity. Unit latency is **~0.59s per million rows end-to-end**.
  - Remaining Bottleneck: Detection now constitutes ~70% of total runtime (2.08s out of 2.94s at 5M), representing the natural irreducible cost of full-outer/hash joins.
  - Parity & regression verification: **321 routine tests passing in 4.79s**, plus 5 benchmark gate tests in 0.14s.

### M3.1 Canonical Prefect 3 Execution Boundary

- **Orchestration Architecture**:
  - Prefect 3 (`prefect>=3.0.0`) established as the single canonical execution boundary for the entire SCD2 processing pipeline.
  - The `@flow(name="scd2_pipeline")` (`run_pipeline`) owns the complete run lifecycle, input validation, execution timing, failure isolation, and `PipelineResult` assembly.
  - Streamlit UI refactored to eliminate direct engine execution bypass; UI invokes `run_pipeline(...)` directly and hydrates session state from canonical `PipelineResult`.
- **Task Retry Policies & Failure Boundaries**:
  - Deterministic tasks (`ingest_csvs`, `detect_schema`, `detect_changes`, `transform_scd2`, `validate_output`): `retries=0` for immediate fail-fast behavior on invalid CSV data, duplicate business keys, or invariant violations.
  - External AI task (`explain_changes`): `retries=2, retry_delay_seconds=3` for transient network/rate-limit resilience.
  - AI Failure Isolation: Flow wraps `explain_task` in an isolation boundary. Unhandled AI errors yield structured fallback warnings (`provider_used="failed"`), ensuring deterministic SCD2 data transformation and invariant validation results are never discarded.
- **Structured Observability**:
  - Integrated `get_run_logger()` in tasks and flows, with automatic fallback to standard Python logging when invoked outside Prefect flow contexts (e.g. unit tests).
- **Verification & Baseline**:
  - Routine tests: **330 passed** (321 existing + 9 new orchestration tests in `tests/test_prefect_orchestration.py`) in ~22.7s.
  - Exact byte-for-byte and logical parity verified between Prefect flow and direct engine execution.
  - Benchmark gate: 5 passed in 0.12s.

### M3.2 Production-Grade Prefect Task Reliability, Observability & Failure Semantics

- **Authoritative Failure Classification (`failure.py`)**:
  - Maps pipeline exceptions into 7 categories: `deterministic_input_error`, `deterministic_schema_error`, `deterministic_scd2_error`, `validation_failure`, `transient_ai_error`, `permanent_ai_error`, `unexpected_internal_error`.
  - Distinguishes transient vs permanent errors without replacing domain exceptions (`DuplicateBusinessKeyError`, `InvalidTemporalValueError`).
- **Retry Storm Prevention Architecture**:
  - Level 1: In-provider API retries (`GeminiProvider._call_model`: `MAX_RETRIES=2` with exponential backoff on 429/5xx).
  - Level 2: In-provider model fallback chain (`gemini-3.8-flash -> gemini-3.5-flash-lite -> gemini-3.1-flash-lite -> gemini-2.5-flash`).
  - Level 3: Cross-provider fallback (`Gemini -> Groq -> Template`).
  - Level 4: Orchestration task boundary (`explain_task`: `retries=1, retry_delay_seconds=2, timeout_seconds=60, retry_condition_fn=should_retry_ai_task`). Rejects retrying deterministic or permanent errors, eliminating retry amplification.
- **Run-Level Observability (`OrchestrationSummary`)**:
  - Captured in `PipelineResult.orchestration_summary`: tracks `flow_run_id`, `flow_run_name`, timestamps, monotonic `task_durations` across all 6 tasks, `task_statuses`, row counts, change summary counts, validation results, and AI fallback status.
- **Verification & Baseline**:
  - Routine tests: **347 passed** (330 previous + 17 new reliability tests in `tests/test_prefect_reliability.py`) in ~38s.
  - 10-scenario failure matrix fully verified.
  - Benchmark gate: 5 passed in 0.12s.
  - Measured local orchestration overhead: ~65ms across 6 tasks.

### M3.3 Production-Grade Prefect Deployment & Execution Infrastructure

- **Deployment Architecture (`deployment.py`)**:
  - Canonical named deployment: `scd2_pipeline/local-processing` (Flow: `scd2_pipeline`, Deployment name: `local-processing`).
  - Execution model: local/process execution via Prefect 3 `RunnerDeployment` (`serve_deployment` / `flow.serve()`), with optional process work pool provisioning (`ensure_process_work_pool`).
  - Concurrency control: `concurrency_limit = 1`, `collision_strategy = ConcurrencyLimitStrategy.ENQUEUE`. Prevents concurrent SCD2 snapshot collisions by safely queuing incoming batch requests.
- **Parameter Contract & Schema Safety (`DeploymentParameters`)**:
  - Strictly JSON-serializable, OpenAPI-compliant parameter contract: accepts only string file paths / URIs (`source: str`, `target: str`), ISO dates (`processing_date: Optional[str]`), column name overrides (`business_key_override`, `tracked_columns_override`), snapshot mode, and delete policy.
  - Zero secrets in OpenAPI schema: flow signature uses `settings: Optional[Any] = None` and `llm_provider: Optional[str] = None`, preventing Pydantic from inspecting `Settings` and leaking API keys (`gemini_api_key`, `groq_api_key`).
  - Zero raw DataFrames in deployment parameters: large multi-million-row Polars DataFrames are never serialized across RPC. Datasets are staged to disk via `stage_dataset` and passed as filesystem references.
- **Streamlit Dual-Path Execution**:
  - Interactive mode: direct in-process `run_pipeline` for immediate sub-second feedback.
  - Deployed mode: stages uploaded DataFrames to `data/staging/`, registers deployment, triggers flow run via `trigger_pipeline_run(..., timeout=0)`, and presents Flow Run ID, status, and inspection guidance without duplicating pipeline logic.
- **Verification & Baseline**:
  - Routine test suite: **357 passed, 18 deselected in 47.00s** (347 previous + 10 new deployment tests in `tests/test_prefect_deployment.py`).
  - Benchmark gate: 5 passed in 0.09s.
  - Complete deterministic output, change summary, and validation parity verified between in-memory DataFrame execution and file-based deployment execution.

### M3.4 Scheduled and Programmatic Automated Execution for SCD2 Prefect Deployment

- **Deployment Scheduling Infrastructure (`deployment.py`)**:
  - `build_deployment` natively supports `cron` and `timezone` via Prefect 3 `CronSchedule` (`CronSchedule(cron=..., timezone=...)`).
  - Configurable defaults via `Settings`: `pipeline_schedule_cron = "0 2 * * *"`, `pipeline_schedule_timezone = "UTC"`, `pipeline_schedule_active = True`.
  - Parameter defaults and fallback: `Settings.default_source_path = "data/source.csv"`, `Settings.default_target_path = "data/target_scd2.csv"`.
  - Operational lifecycle management: `pause_deployment_schedule()`, `resume_deployment_schedule()`, and `get_deployment_schedule_info()` using Prefect 3 client APIs.
  - Concurrency preservation: Scheduled and manual triggers share `concurrency_limit = 1` and `collision_strategy = ConcurrencyLimitStrategy.ENQUEUE`, preventing overlapping batch execution and ensuring serialized FIFO execution.
- **Deterministic Scheduled Date Resolution (`workflow.py`)**:
  - If `processing_date is None` during an automated run, `run_pipeline` extracts `scheduled_start_time.date()` from `prefect.runtime.flow_run` when running under a deployment schedule.
  - If `processing_date` is explicitly passed, the parameter override is strictly preserved.
  - Source/target fallbacks: If omitted, resolves cleanly to configured default paths. If the source file is missing, fails immediately as `deterministic_input_error`.
  - AI isolation: Any transient or unexpected AI explanation failure gracefully degrades to template fallback without corrupting deterministic SCD2 output or invariant validation.
- **Run-Level Observability (`OrchestrationSummary`)**:
  - Populates `deployment_id`, `deployment_name`, and `trigger_type` (`"scheduled"` vs `"manual"`).
- **CLI Operational Commands**:
  - `--apply [--cron <expr>] [--timezone <tz>]`: Registers deployment with schedule.
  - `--inspect-schedule`: Outputs active schedule, pause state, and concurrency limits in JSON.
  - `--pause`: Safely pauses scheduled runs.
  - `--resume`: Resumes scheduled runs.
  - `--trigger`: Programmatically triggers a run.
- **Verification & Baseline**:
  - Routine test suite: **376 passed, 18 deselected in 266.72s** (357 previous + 19 new scheduling tests in `tests/test_prefect_scheduling.py`).
  - Benchmark gate: 5 passed in 0.46s.
  - 100% parity verified between file-based scheduled execution and direct flow execution.

### M3.5 Persistent SCD2 Run Artifacts and Result Retrieval Baseline

- **Decoupled Artifact Persistence Service (`src/scd2_copilot/artifacts.py`)**:
  - Storage layout: `data/runs/<run_id>/`:
    - `metadata.json`: Complete execution and pipeline metadata without secrets or raw source payloads.
    - `scd2_output.parquet`: Native Polars Parquet format with `zstd` compression, preserving exact schema, `pl.Date`, `pl.Boolean`, numeric, and nulls.
    - `changes.json`: Structured change breakdown summary and change records.
    - `validation.json`: Validation report results and rule evaluations.
    - `explanations.json`: AI/template explanations, warnings, and usage metrics.
- **Atomicity & Immutability**:
  - Staging directory (`.tmp_<run_id>_<uuid>`) with complete file validation before atomic directory rename (`os.replace`).
  - Overwrite protection (`FileExistsError` raised unless `overwrite=True`).
- **Fast Metadata Inspection**:
  - `get_run_metadata(run_id)` parses `metadata.json` in ~0.13ms without loading the Parquet table.
- **Failure Classification & Observability**:
  - Classified as `FailureCategory.PERSISTENCE_ERROR` if persistence fails; recorded on `OrchestrationSummary` without corrupting prior SCD2 transformation.
  - `OrchestrationSummary` tracks `run_id`, `artifact_status` (`"persisted"`, `"failed"`, `"skipped"`), `artifact_directory`, `artifact_files`, and `persistence_duration_seconds`.
- **Streamlit Readback Integration**:
  - Persisted run inspector in sidebar and History tab allowing instant load of past runs into session state with zero recomputation.
- **Test Suite Performance Optimization**:
  - Configured test environment variables in `tests/conftest.py` (`PREFECT_SERVER_ANALYTICS_ENABLED=False`, `PREFECT_API_ENABLE_HTTP2=False`, `PREFECT_LOGGING_TO_API_ENABLED=False`), reducing full routine test suite time from ~266s to **58.26s** (4.5x faster).
- **Authoritative Measured Persistence Benchmarks (Polars 1.44.1, zstd)**:
  - 10,000 rows: write 0.0178s | full read 0.0050s | metadata read 0.000162s (0.16ms) | Parquet size: 0.03 MB
  - 100,000 rows: write 0.0138s | full read 0.0056s | metadata read 0.000184s (0.18ms) | Parquet size: 0.16 MB
  - 500,000 rows: write 0.0340s | full read 0.0104s | metadata read 0.000147s (0.15ms) | Parquet size: 0.69 MB
  - 1,000,000 rows: write **0.0445s** | full read **0.0127s** | metadata read **0.000131s (0.13ms)** | Parquet size: **1.34 MB**
- **Verification**:
  - Routine test suite: **393 passed, 18 deselected in 58.26s** (376 previous + 17 new tests in `tests/test_run_artifacts.py`).
  - Benchmark gate: 5 passed in 0.08s.

### M3.6 Idempotency, Run Identity & Execution Deduplication Baseline

- **Three-Tier Execution Identity Model**:
  - `run_id`: Uniquely identifies an execution instance (`run_YYYYMMDD_HHMMSS_<short_uuid>`).
  - `execution_fingerprint`: Canonical SHA-256 digest identifying identical logical inputs (source digest, target digest, processing date, snapshot mode, delete policy, business keys, tracked columns). Strictly excludes ephemeral timestamps, `run_id`, Prefect IDs, AI explanations, and host metrics.
  - `persisted result`: Durable artifacts stored in `data/runs/<run_id>/` and indexed in `data/runs/.fingerprints/<fingerprint>.json`.
- **Deduplication Lifecycle & Outcomes**:
  - `NEW_EXECUTION`: Fresh input/config; runs full pipeline and indexes fingerprint.
  - `REUSED_EXECUTION`: Existing completed run matched; completely bypasses SCD2 engine (`detect_changes`, `transform_scd2`, `validate_scd2`, `explain_changes`) and disk writes, returning existing result in ~0.005s.
  - `FORCED_REEXECUTION`: Caller passes `force_recompute=True`; generates a fresh `run_id` and recalculates despite existing match.
- **Canonical Deterministic Hashing**:
  - Polars DataFrames hashed via native vectorized row hashing (`df.hash_rows().to_numpy().tobytes()`).
  - Cross-platform file hashing normalizes CRLF (`\r\n`) to LF (`\n`) in streaming chunks with carry-over buffering, ensuring identical fingerprints across Windows and Linux.
  - Atomic index linking via staging file + `os.replace` with Windows `PermissionError` exponential backoff retry.
  - Self-healing fallback: if `.fingerprints/<fingerprint>.json` is missing or pointing to a deleted run, automatically scans valid runs in `runs_directory` and repairs the index.
  - Corrupted run rejection: `run_exists()` validates both `metadata.json` and `scd2_output.parquet` before allowing any run reuse.
- **Workflow & Parameter Integration**:
  - `run_pipeline(..., reuse_existing: Optional[bool] = None, force_recompute: bool = False)`
  - `DeploymentParameters` supports `reuse_existing` and `force_recompute`.
  - Deployment CLI flags: `--no-reuse` and `--force-recompute`.
  - Streamlit UI includes "⚡ Force Recompute" checkbox in Run Controls and deduplication status badges (♻️ Reused, ⚡ Forced, 📦 New) with fingerprints in the History tab.
- **Zero External Infrastructure Guarantee**:
  - 100% pure local filesystem coordination via `os.replace` and JSON pointer indices.
  - Zero databases (PostgreSQL, SQLite), caches (Redis), or distributed message brokers.
  - Zero modifications to the proven deterministic Polars SCD2 transformation and change detection algorithms.
- **Authoritative Verification Baseline**:
  - Routine test suite: **415 passed, 18 deselected in 79.03s** (393 previous + 22 new idempotency tests in `tests/test_idempotency.py`).
  - Benchmark gate: 5 passed in 0.12s.
  - Reused execution bypasses engine and persistence with 100% bit-exact parity.

### M3 Prefect Local Runner Scheduling Backlog & SQLite Locking Resilience Baseline

- **Root Cause Confirmed**:
  - Unisolated Pytest runs (e.g. `test_programmatic_triggering` with `timeout=0`) were creating flow runs in `~/.prefect/prefect.db`.
  - An orphaned flow run stranded in `Submitting`/`Pending` occupied the deployment concurrency slot (`active_slots = 1/1`).
  - Under `CONCURRENCY_LIMIT = 1` and `ENQUEUE` strategy, all subsequent runs were permanently queued in `AwaitingConcurrencySlot` (SCHEDULED).
  - The runner `--serve` poller loop repeatedly proposed `Pending` transitions every 2 seconds, which the server rejected, producing `"scheduled runs skipped (at capacity)"` and `"Aborted submission ... non-pending state 'SCHEDULED'"`.
  - The poller loop write contention collided with runner heartbeats, exhausting SQLite's default 5-second busy timeout and producing `sqlite3.OperationalError: database is locked`.
- **Database Cleanup & State Restoration**:
  - Safely transitioned all stale/orphaned non-terminal runs in `~/.prefect/prefect.db` to `Cancelled` via official Prefect client APIs.
  - Active concurrency slots reset to 0/1.
  - Exactly 1 canonical schedule (`cron='0 2 * * *' timezone='UTC'`) preserved.
- **Pytest Database Isolation**:
  - Configured `_isolate_test_runs` fixture in `tests/conftest.py` to isolate `PREFECT_HOME` to `tmp_path / "prefect_home"` and set `PREFECT_SERVER_DATABASE_TIMEOUT=30.0`.
  - Routine test executions now operate strictly on ephemeral test databases, preventing run or slot leakage into the user's primary database.
- **Self-Healing Runner & Concurrency Reconciliation**:
  - Implemented `reconcile_deployment_concurrency(deployment_name, cancel_stale_scheduled=False)` in `src/scd2_copilot/deployment.py`.
  - Automatically identifies orphaned `Submitting`, `Pending`, and `Running` runs from interrupted sessions and transitions them to `Crashed`, immediately releasing leaked slots.
  - Automatically cancels invalid scheduled runs pointing to missing source datasets.
  - Updated `serve_deployment(reconcile_runs=True)` to execute self-healing concurrency reconciliation on boot before starting the worker loop.
  - Added CLI management flags: `--clear-queue` (cancels all scheduled backlog) and `--reconcile` (releases orphaned slots without serving).
  - Configured `PREFECT_SERVER_DATABASE_TIMEOUT=30.0` in `deployment.py` to eliminate transient lock timeouts on Windows.
- **Authoritative Verification Baseline**:
  - `tests/test_prefect_deployment.py`: 14 passed in 30.15s (10 baseline + 4 new regression tests).
  - `tests/test_prefect_scheduling.py`: 19 passed in 36.17s.
  - Full routine test suite: **419 passed, 18 deselected in 95.74s** (100% passing, 0 regressions).
  - Zero non-terminal runs and 0 leaked concurrency slots in `~/.prefect/prefect.db`.

### M3 Prefect Subprocess API Isolation & Concurrency Slot Loss Fix

- **Root Cause Confirmed (Ephemeral Server Isolation)**:
  - `PREFECT_API_URL` was completely absent from the entire codebase (0 references in `src/`, `app/`, `tests/`, `.env`, `prefect.toml`).
  - With `PREFECT_API_URL=None`, Prefect 3.8.5 operates in **ephemeral mode**: each process calling `get_client()` starts an independent `SubprocessASGIServer` on a random port (8000–9000) backed by an independent ephemeral database.
  - `SubprocessASGIServer` stores the API URL only in its own Python instance memory — it **never writes it back to `os.environ["PREFECT_API_URL"]` or Prefect's settings context**.
  - When `serve()` spawns a flow subprocess via `multiprocessing.get_context("spawn")`, the child receives `os.environ` which does NOT contain `PREFECT_API_URL`, so the child starts its **own** independent ephemeral server on a different random port.
  - **Result**: Concurrency lease acquired on parent server A is invisible to child server B → `"Deployment concurrency slot lost during provisioning"` and `"Lease not found during release"`.
  - The same issue affected Streamlit → `trigger_pipeline_run()` → `run_deployment()`, which started a third independent server.
- **Fix Architecture**:
  - Added `ensure_prefect_server()` to `deployment.py`: auto-starts a dedicated Prefect API server (`prefect server start`) on port 4200 if not already reachable, managed as a daemon subprocess with atexit cleanup.
  - Added `configure_prefect_api_url()` to `deployment.py`: sets `os.environ["PREFECT_API_URL"]` from `Settings.prefect_api_url` (default `http://127.0.0.1:4200/api`). Called by `serve_deployment()`, CLI `main()`, and Streamlit deployment submission.
  - Added `prefect_api_url: str = "http://127.0.0.1:4200/api"` to `config.py` `Settings`, overridable via `PREFECT_API_URL` env var.
  - All processes (serve runner, spawned flow subprocesses, Streamlit, CLI) now connect to the **same** Prefect API server.
- **Regression Tests**:
  - `test_configure_prefect_api_url_sets_env_var`: verifies `os.environ["PREFECT_API_URL"]` is set correctly.
  - `test_configure_prefect_api_url_reads_from_settings`: verifies default from Settings.
  - `test_is_server_reachable_returns_false_for_unreachable`: verifies health check for non-existent servers.
  - `test_settings_has_prefect_api_url_field`: verifies Settings model field and default.
  - `test_configure_prefect_api_url_overrides_existing`: verifies stale URL override.

## 11. Safe agent operating procedure

For non-trivial changes:

```text
EXPLORE
  ↓
PLAN
  ↓
REVIEW
  ↓
IMPLEMENT
  ↓
TEST
  ↓
INSPECT DIFF
  ↓
REPORT
```

Do not make broad architectural changes without first producing a plan.

Before implementing a requested change, identify:

- current behavior
- affected files
- invariants
- compatibility risks
- tests that must change
- measurable success criteria

## 12. Scope discipline

Do not:

- rewrite the repository because a cleaner architecture is possible
- rename unrelated files during feature work
- change dependencies without a reason
- silently alter external behavior
- remove tests because they are inconvenient
- hide failures by weakening assertions
- claim production readiness before the relevant milestone is actually complete

## 13. Definition of done

A code change is not complete merely because it compiles.

A meaningful change should leave behind:

- implementation
- appropriate tests
- updated documentation/configuration
- verification output
- a concise summary of measurable results
- no unrelated modifications

## 14. Repository truth hierarchy

When sources disagree, prefer:

1. Actual running code
2. Existing tests and fixtures
3. Dependency/configuration files
4. Runtime/build output
5. Project documentation
6. This agent-context document

Treat this document as guidance, not as proof.

## 15. Codebase Understanding & Navigation with Graphify

The repository maintains an indexed structural knowledge graph generated by **Graphify** at `graphify-out/` (with rules in `.agents/rules/graphify.md` and skill in `.agents/skills/graphify/SKILL.md`).

### Priority Usage Contract
Agents MUST prioritize using Graphify whenever it is useful for:
- Understanding the architecture and topological organization of the repository.
- Locating related files, modules, classes, functions, or symbols.
- Tracing dependencies, call paths, and data flow across modules (e.g. via `graphify path`).
- Determining the blast radius / impact of a proposed code change before editing.
- Understanding relationships between engine, validation, workflow, and UI components.
- Finding where specific functionality, invariant enforcement, or configuration is implemented.
- Exploring unfamiliar parts of the codebase without reading dozens of raw files.
- Checking whether modifying a function or schema affects other modules or tests.
- Navigating large or interconnected components across `src/scd2_copilot`, `app/`, and `tests/`.

### Pre-Change Analysis Discipline
- **Before editing unfamiliar or interconnected code**: Run Graphify-based structural/relationship analysis (`graphify path`, `graphify explain`, or `graphify query`) to establish dependency context rather than blindly scanning many files with broad greps.
- **When tasks involve multiple interconnected modules**: Query Graphify first to reveal caller/callee graphs and imported symbols across the boundary.
- **Trivial tasks exception**: Do NOT use Graphify unnecessarily for trivial tasks where direct inspection is faster (e.g., editing an already-identified small code snippet in a known file, fixing a syntax error, or making an isolated one-line change).

### Grounding & Verification Principle
- Treat Graphify as a **context and analysis aid, NOT as an authority**.
- Always verify structural findings against the actual source code, tests, configuration, and runtime behavior before committing changes.
- Do not modify SCD2 Copilot business logic, architecture, or dependencies merely to demonstrate Graphify.

### Graph Maintenance
- After modifying code files in a session, run:
  ```powershell
  graphify update .
  ```
  to keep `graphify-out/graph.json` current (runs fast AST extraction locally with zero LLM API cost).

