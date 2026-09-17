# ADR-005 — Operational API Boundary & Live Monitoring Interface

## Decision
Expose backend V2 capabilities through a dedicated, headless **FastAPI service** and an interactive **Streamlit Live Monitoring Interface**, secured with **Supabase Auth asymmetric JWT verification (ES256)** for operational recovery actions.

## Context
Milestones V2.1 through V2.5 established the live Supabase PostgreSQL data layer, incremental micro-batch worker, deterministic change significance guardrail, durable containment workflow, and evidence-grounded AI explanations. To transform these capabilities into an observable, controllable operational product, the platform requires an external API boundary and an operator monitoring interface.

## Core Architecture Invariants
> **The engine decides. Validation protects. The guardrail evaluates significance. The containment layer holds. The worker orchestrates. Transactions commit. AI explains. The API exposes. The UI monitors.**

## Key Principles & Architectural Guarantees

1. **Strict Separation of Concerns**:
   - **FastAPI Routes**: Thin HTTP adaptors handling deserialization, correlation ID assignment, and status codes. No business logic in handlers. All actions delegate directly to existing repositories (`ProcessingRunRepository`, `HeldChangeBatchRepository`, `InventoryHistoryRepository`, `InventorySourceRepository`, `CheckpointRepository`) and `ContainmentService`.
   - **Streamlit Monitoring Interface**: Communicates **strictly via HTTP** (`ApiClient`). Never imports `psycopg`, never opens direct database connections, and never executes raw SQL queries.

2. **Supabase Auth & Asymmetric Cryptographic Verification**:
   - Identity Authority: **Supabase Auth**.
   - Signature Scheme: Asymmetric elliptic curve **`ES256`** (`P-256`) verified against Supabase's live public JWKS endpoint (`/.well-known/jwks.json`). No shared HS256 secrets.
   - Verification Pipeline: `Google OAuth / Supabase Auth` $\to$ Supabase access JWT $\to$ Streamlit session $\to$ `Authorization: Bearer <token>` $\to$ FastAPI `SupabaseJWTVerifier` $\to$ `AuthenticatedOperator`.
   - Client-supplied identity headers (`X-Operator-*`) are **untrusted**; operator identity and permissions derive exclusively from verified cryptographic JWT claims (`email`, `app_metadata.role`, `user_id`).
   - Authorization Policy: Recovery operations (`RELEASE`, `REPROCESS`, `DISCARD`) fail closed:
     - `401 Unauthorized`: Missing, expired, or invalid JWT.
     - `403 Forbidden`: Authenticated user lacking operator authorization (email not in `recovery_operator_emails` and role != `operator`).

3. **Request Correlation & Secret Sanitization**:
   - Every request is tagged with an `X-Request-ID` header for distributed tracing.
   - Global exception handlers sanitize all database connection strings, passwords, and raw stack traces before responding to clients.

4. **Preserved V1 Batch CSV & Prefect Workflows**:
   - The Streamlit application provides a top-level mode selector in the sidebar:
     - `⚡ Live Guardrail Monitor (V2)`
     - `📁 Batch CSV Analysis (V1)`
   - All existing CSV upload, heuristic schema detection, Polars vectorized transformation, 5-rule validation, Prefect orchestration, and Parquet persistence capabilities remain 100% operational.

## Consequences & Operational Topology
- The system operates reproducibly across 3 decoupled processes:
  1. **Incremental Ingestion Worker**: Polls upstream mutations and evaluates guardrails.
  2. **Headless Operational API**: Exposes telemetry, audit logs, and authorized recovery transitions on port 8000.
  3. **Streamlit Monitoring Dashboard**: Renders live KPIs, containment queues, historical queries, and recovery consoles on port 8501.
