# SCD2 Copilot — Verified Limitations

> **Purpose**: This document records only verified, confirmed limitations of the V3 system.
> Do not add speculative limitations. Every item listed here has been confirmed through implementation review or testing.

---

## 1. Source Database Support

- **PostgreSQL only.** The `PostgresSourceAdapter` and `SourceCursor` are implemented exclusively for PostgreSQL-compatible databases.
- No MySQL, SQL Server, Oracle, MongoDB, Kafka, or Debezium support exists.

## 2. Concurrency Model

- **Single concurrent worker per monitor.** Concurrency is governed by PostgreSQL advisory locks (`pg_try_advisory_lock(hashtext('scd2_worker_' || source_name))`). A second worker instance attempting the same monitor receives `status="LOCKED"` and does not process.
- No multi-worker parallelism or partitioned processing.

## 3. Monitor Registry

- **In-memory singleton.** `MonitorRegistry` is not persisted across application restarts. Workers auto-register on startup, and the canonical inventory monitor is pre-registered.
- No persistent monitor catalog exists in the database.

## 4. Source Mutations

- **Zero source mutations.** The `PostgresSourceAdapter` never executes `INSERT`, `UPDATE`, `DELETE`, `DROP`, or `ALTER` on the source table. There is no automatic source-database rollback capability.
- Operator recovery actions (`RELEASE`, `REPROCESS`, `DISCARD`) affect only the target history and checkpoint state.

## 5. SCD2 Column Configuration

- **No Type 1 / Type 2 column distinction.** All tracked columns are treated as Type 2 (full history preservation). Type 1 in-place updates are not supported.
- **No schema-evolution policy.** If the source table schema changes (columns added, removed, or renamed), the monitor configuration must be manually updated.

## 6. Quarantine & Data Quality

- **No automatic row-level quarantine.** Invalid records that fail SCD2 validation cause the entire batch to fail. There is no per-record quarantine workflow.
- Batch-level containment (HOLD) exists for suspicious batches, but individual record rejection within a batch is not supported.

## 7. Authentication & Authorization

- **Supabase Auth only.** Authentication uses Supabase Auth with asymmetric JWT verification (ES256 via JWKS). No alternative identity providers are configured.
- **Operator email allowlist.** State-mutating recovery endpoints (`/release`, `/reprocess`, `/discard`) are restricted by `recovery_operator_emails` configuration. No enterprise RBAC, role hierarchy, or group-based authorization exists.

## 8. Notifications & Alerting

- **No external notifications.** When a suspicious batch is held, no email, Slack, PagerDuty, or webhook notification is sent. The operator must check the UI or API.

## 9. AI Explanation

- **Explanation failure is non-blocking.** If all AI providers (Gemini → Groq → Deterministic Template) fail, the deterministic workflow continues. The explanation is marked as unavailable but the hold/commit decision is unaffected.
- **Legacy field names in stored evidence.** Existing `held_change_batch.evidence` JSONB in PostgreSQL uses inventory-specific field names (`warehouses_affected`, `skus_affected`, etc.) for backward compatibility. These are populated with zero values for non-inventory monitors.

## 10. Scalability

- **Single-process architecture.** The ingestion worker runs as a single Python process with a polling loop. No horizontal scaling, distributed processing, or message queue decoupling exists.
- **Micro-batch size limit.** `MonitorConfig.batch_size` caps each polling cycle at 1–50,000 records. Very large source tables with high change velocity may require tuning.

## 11. Deployment

- **No CI/CD pipeline.** Docker packaging exists but no GitHub Actions, automated testing pipeline, or deployment automation is configured.
- **Environment variables for configuration.** All runtime configuration (database URL, API keys, model identifiers) is supplied via environment variables or `.env` file. No configuration management service integration exists.

## 12. Data Persistence

- **Supabase PostgreSQL.** All operational state (checkpoints, processing runs, held batches, history) is stored in a single Supabase PostgreSQL instance. No replication, backup automation, or disaster recovery is configured by the application.
- **No data retention policy.** Historical versions accumulate indefinitely. No automatic archival, partitioning, or TTL-based cleanup exists.
