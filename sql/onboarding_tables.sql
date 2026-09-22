-- SCD2 Copilot Customer Data Onboarding DDL
-- Table: public.onboarding_run
-- Enforces durable execution auditing, lineage tracking, and concurrency-safe idempotency.

CREATE TABLE IF NOT EXISTS public.onboarding_run (
    run_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_schema_fingerprint TEXT,
    canonical_schema_version INTEGER NOT NULL DEFAULT 1,
    mapping_version_id TEXT,
    status TEXT NOT NULL CHECK (
        status IN (
            'CREATED', 'PROFILING', 'MAPPING_PENDING', 'APPROVAL_PENDING',
            'TRANSFORMING', 'VALIDATING', 'COMPLETED', 'PARTIAL', 'FAILED', 'CANCELLED'
        )
    ),
    input_fingerprint TEXT,
    total_records INTEGER NOT NULL DEFAULT 0,
    valid_records INTEGER NOT NULL DEFAULT 0,
    exception_records INTEGER NOT NULL DEFAULT 0,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    artifact_paths JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message TEXT,
    error_details JSONB,
    operator_id TEXT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Unique index guaranteeing database-level concurrency protection for idempotency keys
CREATE UNIQUE INDEX IF NOT EXISTS uq_onboarding_run_idempotency_key
    ON public.onboarding_run (idempotency_key);

-- Secondary index for fast source history lookup
CREATE INDEX IF NOT EXISTS idx_onboarding_run_source_id
    ON public.onboarding_run (source_id, created_at DESC);

-- Secondary index for operational status filtering
CREATE INDEX IF NOT EXISTS idx_onboarding_run_status
    ON public.onboarding_run (status);

-- Table: public.schema_drift_report
-- Enforces durable tracking of source schema drift and mapping impact evaluations.

CREATE TABLE IF NOT EXISTS public.schema_drift_report (
    report_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    prior_schema_version INTEGER NOT NULL DEFAULT 1,
    current_schema_version INTEGER NOT NULL DEFAULT 2,
    prior_fingerprint TEXT NOT NULL,
    current_fingerprint TEXT NOT NULL,
    mapping_version_id TEXT NOT NULL,
    canonical_schema_version INTEGER NOT NULL DEFAULT 1,
    overall_compatibility TEXT NOT NULL CHECK (
        overall_compatibility IN ('COMPATIBLE', 'COMPATIBLE_WITH_REVIEW', 'BROKEN')
    ),
    review_required BOOLEAN NOT NULL DEFAULT FALSE,
    drift_events JSONB NOT NULL DEFAULT '[]'::jsonb,
    impacted_mappings JSONB NOT NULL DEFAULT '[]'::jsonb,
    summary_reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_schema_drift_report_source_id
    ON public.schema_drift_report (source_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_schema_drift_report_mapping_version_id
    ON public.schema_drift_report (mapping_version_id);

