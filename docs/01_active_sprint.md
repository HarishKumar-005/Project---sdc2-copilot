# Active Sprint — V2 Real-Time Data Change Guardrail

## Goal

Evolve SCD2 Copilot from a batch CSV application into an operational real-time data change guardrail on top of cloud Supabase PostgreSQL, preserving 100% of existing deterministic V1 capabilities.

## Current Phase

**V3 Phase 5 Completed — Real-World Demonstration, Validation & Project Freeze**  
**STATUS: FEATURE FREEZE** — No new features or architecture changes. Only blocking bugs, reproducibility issues, security defects, or incorrect documentation.

## Milestone Progress & Roadmap

### V2.0 — Architecture & Context Freeze [COMPLETED]

- Established master architecture contracts (`docs/07_v2_master_context.md`).
- Audited repository baseline and separated V1 batch from V2 real-time paths.

### V2.1 — Supabase/PostgreSQL Data-Access Layer [COMPLETED]

- Production connection management (`DatabaseManager` with connection pooling, statement timeouts, SSL mode).
- Strongly-typed Pydantic domain models: `InventorySourceRow`, `InventoryHistoryRow`, `ProcessingCheckpointRow`, `ProcessingRunRow`.
- Repositories with atomic transaction boundaries: `InventorySourceRepository`, `InventoryHistoryRepository`, `CheckpointRepository`, `ProcessingRunRepository`.
- 43 comprehensive unit and integration tests against live Supabase PostgreSQL.

### V2.2 — Incremental Ingestion Worker [COMPLETED]

- Dedicated worker package: `src/scd2_copilot/worker/` (isolated from `src/scd2_copilot/ingestion.py` to prevent import collisions).
- Deterministic micro-batching (`MicroBatch`) and conversion to engine-typed Polars DataFrames (`history_rows_to_target_df`).
- Continuous polling worker (`IngestionWorker`) with `run_once()` and `run_forever()` loops.
- Deterministic integration with Polars SCD2 engine (`detect_changes` + `apply_scd2` + `validate_scd2`) under `SnapshotMode.INCREMENTAL` and `DeletePolicy.IGNORE`.
- Atomic multi-table persistence in `DatabaseManager.transaction()`: historical version closure, new version insertion, processing run tracking, and monotonic watermark advancement.
- Rollback-safe failure semantics: checkpoint remains strictly unadvanced on compute, validation, or persistence failures.
- Thread-safe graceful shutdown handling (`SIGINT`/`SIGTERM`) and CLI runner (`python -m src.scd2_copilot.worker`).
- 21 automated tests (19 unit, 2 live integration against Supabase).

### V2.3 — Deterministic Change Significance & Guardrail Engine [COMPLETED]

- Dedicated guardrail package: `src/scd2_copilot/guardrail/` (`GuardrailEngine`, rules, and models).
- 7 deterministic business rules:
  1. `HIGH_CHANGE_VOLUME`: Threshold on mutated record counts.
  2. `HIGH_POPULATION_IMPACT`: Proportion of evaluated batch population mutated (with batch size noise guard).
  3. `LARGE_QUANTITY_SWING`: Relative quantity delta (> 300%) paired with minimum absolute noise threshold (>= 50 units).
  4. `MASS_DEACTIVATION`: Categorical status transitions from `ACTIVE` to `INACTIVE`/`DISCONTINUED`.
  5. `HIGH_CHANGE_VELOCITY`: Changes per event second across the micro-batch window.
  6. `WIDE_GEOGRAPHIC_IMPACT`: Multi-warehouse dispersion across a single batch.
  7. `SCD2_VALIDATION_FAILURE`: SCD2 invariant validation compliance.
- Precedence-based deterministic severity derivation (`CRITICAL`, `HIGH`, `MEDIUM`, `LOW`).
- Integrated into `IngestionWorker.run_once()` between SCD2 validation and database transaction commit.
- `NORMAL` batches commit to `inventory_history` and advance watermark.
- `SUSPICIOUS` batches are marked `HELD` in `processing_run`, target history is protected, and watermark is strictly preserved.
- 22 automated tests (18 unit, 4 worker integration tests).

### V2.4 — Suspicious Batch Containment & Recovery [COMPLETED]

- Dedicated containment package: `src/scd2_copilot/containment/` (`ContainmentService`, exceptions, models).
- Durable atomic hold persistence: `processing_run` (HELD) + `held_change_batch` (HELD) with frozen source records in `evidence["batch_records"]`.
- Duplicate hold storm prevention: deterministic SHA-256 `batch_fingerprint` suppresses duplicate hold inserts on repeated polling of unadvanced watermark slices.
- Head-of-stream ordering: watermark remains at $T_0$, preventing downstream corruption.
- Three idempotent resolution operations:
  - `release_held_batch`: Operator override, replays frozen records through SCD2 engine, commits history, advances watermark, updates hold to `RELEASED`.
  - `reprocess_held_batch`: Re-evaluates guardrail; commits if normal, preserves hold if still suspicious.
  - `discard_held_batch`: Operator rejection, marks hold `DISCARDED`, does NOT touch history, advances watermark to unblock stream.
- Zero source mutations: `inventory_source` remains completely untouched.
- 20 new tests (18 unit, 2 live integration against Supabase); total suite passing at 563 tests.

### V2.5 — Evidence-Grounded AI Explanation [COMPLETED]

- Dedicated explanation package: `src/scd2_copilot/explanation/` (`ExplanationService`, providers, prompt, models, grounding).
- Immutable, trusted evidence context: `ExplanationContext` (constructed exclusively from trusted deterministic models: `GuardrailDecision`, `TriggeredRule`, `GuardrailEvidence`, `HeldChangeBatchRow`).
- Versioned, constrained prompt builder: `build_explanation_prompt` (version `v1`) with zero credential exposure and strict negative instructions.
- Grounding verification: `validate_explanation_grounding` enforces decision and severity immutability, untriggered rule rejection, validation status alignment, and containment status alignment.
- Provider fallback routing: Gemini (`gemini-3.8-flash` -> `gemini-3.5-flash-lite` -> `gemini-3.1-flash-lite` -> `gemini-2.5-flash`) -> Groq (`openai/gpt-oss-120b`) -> Deterministic Template.
- 100% offline functionality: `DeterministicExplanationProvider` provides identical structured operational narrative without requiring external credentials or network connectivity.
- Additive persistence: `HeldChangeBatchRepository.attach_explanation` additively appends explanation to `held_change_batch.evidence["explanation"]`, preserving frozen source snapshots and guardrail metrics intact.
- Fault isolation: Ingestion worker wraps explanation generation in safe exception handlers; explanation failures never crash worker cycles, never rollback holds, and never mutate data.
- 25 automated tests (23 unit, 2 live integration against Supabase); total suite passing at 588 tests.

### V2.6 — Operational API + Live Monitoring Interface [COMPLETED]

- Headless FastAPI operational boundary (`/health`, `/ready`, `/api/v1/runs`, `/api/v1/holds`, `/api/v1/history`, `/api/v1/inventory`, `/api/v1/metrics`).
- Supabase Auth as identity authority with asymmetric JWT verification (`ES256` via JWKS endpoint) using `pyjwt[crypto]`.
- Enforces RBAC on state-mutating recovery endpoints (`/release`, `/reprocess`, `/discard`), failing closed (401 Unauthorized / 403 Forbidden).
- Strongly typed `ApiClient` with automated Bearer token attachment and typed HTTP error hierarchy.
- Streamlit dual-mode interface:
  - `⚡ Live Guardrail Monitor (V2)`: Connects strictly via `ApiClient` over HTTP (no direct PostgreSQL connections, zero SQL in Streamlit). 4 operational tabs:
    1. Containment Queue & Recovery Console (interactive hold resolution: Release, Reprocess, Discard with operator audit attribution)
    2. Processing Runs (system health strip, run metrics, timeline, and audit logs)
    3. Point-in-time SCD2 History Explorer (temporal query inspector)
    4. Upstream Source Inventory (raw feed monitor)
  - `📁 Batch CSV Analysis (V1)`: 100% preserved offline CSV batch execution with Polars SCD2 engine, validation, and AI explanations.
- Streamlit 1.44+ styling compliant (uses `width="stretch"` for modern buttons/tables, zero deprecated `use_container_width`).
- 43 automated tests (21 API unit, 9 client/UI unit, 6 E2E auth integration, 5 auth/session unit, 2 Streamlit AppTest); total suite passing at 631 routine tests.

### P0 — Security Hardening [COMPLETED]

- Fixed PKCE verifier exposure: removed `spkce` URL parameter, eliminated global singleton verifiers, implemented thread-safe, bounded (max 500), TTL-based (10 min) `OAuthTransactionStore` keyed by cryptographic `state` nonce with atomic single-use pop.
- Removed hardcoded secrets & masked sensitive config: implemented `get_redacted_database_url()`, defaulted `recovery_operator_emails` to `[]` (fail-closed), sanitized `.env.example` and `.streamlit/secrets.toml.example`.
- Hardened token & session handling: added `repr=False` to `access_token` and `refresh_token` in `SupabaseSession`, sanitized all provider HTTP error logging, eliminated manual token fallback in UI session state.
- Hardened CORS & operator authorization boundaries: forbade wildcard `*` with credentials, restricted allowed headers to explicit allowlist (`Authorization`, `Content-Type`, `Accept`, `X-Request-ID`), enforced cryptographic JWT claims verification.
- Purged obsolete authentication paths: removed legacy Streamlit OIDC artifacts (`st.login`, `st.user`, `st.logout`), unified all auth strictly under Supabase Auth.
- 34 new security and auth tests (27 in `test_security_hardening.py`, 7 new in `test_auth.py`); total routine test suite passing at **665 tests (100%)**.

### V3 Phase 1 — Configurable PostgreSQL Source [COMPLETED]

- Removed hardcoded inventory schema dependency; established reusable PostgreSQL source configuration boundary (`MonitorConfig`, `PostgresSourceDefinition`, `ChangeTimestampDefinition`).
- Strict configuration validation: SQL identifier format regex, disjoint business keys and tracked columns, forbidden change timestamp in keys/tracked, rejection of reserved SCD2 columns (`effective_from`, `effective_to`, `is_current`).
- Dedicated PostgreSQL source adapter (`PostgresSourceAdapter`): metadata inspection (`information_schema.columns` & `table_constraints`), live schema discovery, configuration validation against database, and deterministic snapshot/incremental queries with deterministic ordering (`ORDER BY <change_timestamp> ASC, <key_1> ASC...`).
- Generalized ingestion worker: `MicroBatch` supports arbitrary records with dynamic column projection; `IngestionWorker` accepts `MonitorConfig` while preserving canonical warehouse inventory behavior as default.
- Headless FastAPI operational endpoints: `GET /api/v1/monitors`, `GET /api/v1/monitors/{name}`, and `POST /api/v1/monitors/validate`.
- Streamlit UI operational interface: added "⚙️ Source Configuration" tab with live config inspection, schema metadata viewer, and on-demand live database validation.
- 22 new automated tests (`tests/test_source_config.py`) covering all configuration validation, adapter discovery, error conditions, and worker integration; total routine test suite passing at **687 tests (100%)**.

### V3 Phase 2 — Live Monitoring Pipeline [COMPLETED]

- Replaced timestamp-only incremental polling with deterministic composite cursor: `SourceCursor(timestamp, keys, key_columns)`.
- Solved same-timestamp edge cases: records sharing identical change timestamps across micro-batches are never skipped or duplicated using SQL row-value comparison `WHERE (ts > %s) OR (ts = %s AND (k1, k2) > (%s, %s))`.
- Rich lexicographical cursor comparison (`__lt__`, `__le__`, `__gt__`, `__ge__`, `__eq__`) with UTC normalization.
- Bounded micro-batch boundary derivation: `first_cursor` and `last_cursor` computed on `MicroBatch`, tracked through worker cycles, frozen in containment holds, and restored during hold resolution.
- PostgreSQL schema evolution: migrated `processing_checkpoint` with `cursor_keys JSONB` column on live Supabase PostgreSQL.
- Checkpoint ordering guarantee: strictly preserved on failed compute, validation errors, or guardrail holds (`HELD`); advanced if and only if the micro-batch commits successfully (`COMMITTED`).
- Mutual exclusion concurrency: session-level PostgreSQL advisory locks `pg_try_advisory_lock(hashtext('scd2_worker_' || source_name))` preventing race conditions and concurrent double-processing across workers (`WorkerCycleResult(status="LOCKED")`).
- Containment & recovery alignment: `release_held_batch`, `reprocess_held_batch`, and `discard_held_batch` properly advance both `watermark_value` and `cursor_keys`.
- 17 new automated tests (7 in `test_source_cursor.py`, 6 in `test_adapter_cursor.py`, 4 live end-to-end scenarios in `test_live_monitoring_pipeline.py`); total routine test suite passing at **704 tests (100%)**.

### V3 Phase 3 — Generic Data-Change Guardrail & Containment [COMPLETED]

- Removed all inventory-specific hardcoding from `GuardrailEngine.extract_evidence()`:
  - Velocity extraction now uses `batch.timestamp_column` (was hardcoded `hasattr(r, "updated_at")`).
  - Numeric delta analysis runs over ALL numeric-compatible tracked columns (was hardcoded `quantity_on_hand`).
  - Categorical transition analysis runs over ALL non-numeric tracked columns (was hardcoded `status`).
  - Key dispersion uses the highest-cardinality business key column (was hardcoded `warehouse_id`).
- `batch.py` `to_source_df()` empty-batch fallback is now generic (any columns → String schema).
- `to_source_df()` cast map is now dynamic: `quantity_on_hand` / `reorder_level` remain Int64 via explicit override; all other columns detected at runtime by attempting `int()` conversion on sample values.
- `GuardrailEvidence` extended with generic fields: `key_value_dispersion`, `dispersion_key_column`, `numeric_column_analyzed`, `categorical_column_analyzed`, `per_column_change_counts`.
- All legacy inventory-specific fields (`warehouses_affected`, `skus_affected`, `max_quantity_relative_change`, `status_deactivations_count`) **preserved** for backward compatibility with existing held batch evidence in PostgreSQL.
- `NORMAL`/`SUSPICIOUS` decision contract, severity derivation logic, containment lifecycle, recovery paths, and AI authority boundary unchanged.
- `IngestionWorker._execute_cycle()` passes `monitor_config` to `guardrail.evaluate()`.
- Data contract document updated: Section 7 "Generic Guardrail Evidence Model".
- 15 new focused tests (`tests/test_guardrail_generic.py`) verifying domain-agnostic behavior across generic numeric columns, generic categorical columns, custom timestamp columns, non-`warehouse_id` key dispersion, and JSON evidence round-trip; total routine test suite passing at **719 tests (100%)**.

### V3 Phase 4 — End-to-End Configured Monitoring & Operator Experience [COMPLETED]

- In-memory neutral `MonitorRegistry` (`src/scd2_copilot/source/registry.py`) enabling worker and API monitor discovery without database migrations or circular FastAPI dependencies.
- Reusable `monitored_entity_history` table on PostgreSQL (`history_id`, `source_name`, `entity_key JSONB`, `attributes JSONB`, `effective_from`, `effective_to`, `is_current`, `created_at`) with btree and jsonb indexes.
- `MonitoredEntityHistoryRow` domain model with temporal invariant validation, and `MonitoredEntityHistoryRepository` with atomic version closure, entity history queries, and multi-key current version fetching.
- Generic target DataFrame conversion (`generic_history_rows_to_target_df`) with dynamic type casting matching source schema.
- Worker target routing (`IngestionWorker._is_inventory_monitor()`): cleanly branches READ and COMMIT between canonical `inventory_history` and generic `monitored_entity_history`.
- Containment service routing: `_reconstruct_batch_from_hold`, `_execute_release`, and `_execute_reprocess` properly route target-history operations for generic non-inventory batches.
- FastAPI history endpoint: `GET /api/v1/history/{source_name}/entity?key=<json>` returning typed chronological version history (`EntityHistoryQueryResponse`), placed before parameterized paths to prevent route shadowing.
- API client extensions: `ApiClient.get_entity_history(source_name, entity_key)` and `ApiClient.list_monitors()`.
- Streamlit UI History Explorer: dynamic monitor selector and structured input fields dynamically rendered per business-key column from monitor metadata.
- 15 new automated tests (14 unit in `tests/test_generic_history_repo.py`, 1 comprehensive live integration in `tests/test_live_generic_monitor.py` verifying full 7-step lifecycle); total routine test suite passing at **734 tests (100%)**.

### V3 Phase 5 — Real-World Demonstration, Validation & Project Freeze [COMPLETED]

- **Preflight audit**: All 8 verification items confirmed passing (generic PG source config, incremental SourceCursor, generic historical persistence, generic history API/UI, generic containment replay, deterministic guardrail, inventory compatibility).
- Fixed `ExplanationContext.from_held_batch` hardcoded inventory field filter (`sku_id`, `warehouse_id`, `quantity_on_hand`, `status`) that produced empty `records_sample` for non-inventory monitors. Now includes all non-secret record keys.
- Fixed `DeterministicExplanationProvider` inventory-specific labels: `what_changed` now uses generic `key_value_dispersion`/`dispersion_key_column` with fallback to legacy warehouse/SKU labels; `evidence_points` conditionally includes categorical deactivations, numeric changes, and per-column change counts only when populated; `containment_summary` uses generic "target history" instead of hardcoded "inventory_history".
- Created `docs/demo_script.md`: Reproducible demonstration sequences for Demo A (inventory: normal + suspicious + recovery) and Demo B (generic product-price: normal + suspicious + history lookup + recovery), with exact SQL, API, and UI walkthrough steps.
- Created `docs/limitations.md`: 12 verified limitation categories covering source database support, concurrency, monitor registry, source mutations, SCD2 column configuration, quarantine, authentication, notifications, AI explanation, scalability, deployment, and data persistence.
- Updated `docs/03_current_state.md`: Rewritten from V1 baseline to reflect complete V3 system state including configurable PG sources, guardrail, containment, recovery, AI explanation, FastAPI API, and Streamlit UI.
- **Verification results**: 39/39 explanation tests passed, 5/5 live integration tests passed (4 inventory + 1 generic lifecycle), full routine suite **734 passed, 18 deselected, 0 failed** (409s).
- **FEATURE FREEZE declared.** No new features, no architecture expansion. Only blocking bugs, reproducibility issues, security defects, or incorrect documentation.

## Governance Rules

- Strict separation: Engine decides, validation protects, AI explains.
- Ingestion worker uses `SnapshotMode.INCREMENTAL` and `DeletePolicy.IGNORE` (absence in micro-batch does not imply deletion).
- Checkpoints advance monotonically if and only if the micro-batch transaction commits successfully.
- Zero breaking changes to pre-existing V1 CSV batch workflows or tests.
