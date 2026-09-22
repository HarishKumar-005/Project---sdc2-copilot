# SCD2 Copilot — Demo Script

> **Purpose**: Reproducible demonstration sequences proving SCD2 Copilot works end-to-end as a configurable PostgreSQL data-change guardrail and historical analytics platform.

---

## Prerequisites

### Environment Variables

The following must be set in `.env` before starting services:

```bash
DATABASE_URL=postgresql://...          # Supabase PostgreSQL connection string
SUPABASE_URL=https://...               # Supabase project URL
SUPABASE_ANON_KEY=...                  # Supabase anon/publishable key
SUPABASE_SERVICE_ROLE_KEY=...          # Supabase service role key (server-side only)
GEMINI_API_KEY=...                     # Google Gemini API key (primary explanation provider)
GROQ_API_KEY=...                       # Groq API key (secondary fallback)
```

### Database Tables

The following tables must exist in Supabase PostgreSQL:

- `public.product_master` — **Demo 1** primary source table (seeded with 240 products across 8 categories)
- `monitored_entity_history` — Generic SCD2 target table (stores versioned history for `product_master`)
- `inventory_source` — **Demo 2** backward-compatibility source table (warehouse inventory)
- `inventory_history` — Demo 2 SCD2 target table
- `processing_checkpoint` — Watermark state and cursor position per stream
- `processing_run` — Run audit log
- `held_change_batch` — Containment queue for suspicious batches

### Database Seeding

Seed the `product_master` catalog baseline (240 deterministic rows, 30 per category):

```bash
uv run python scripts/seed_product_master.py
```

### Starting Services

```bash
# Terminal 1 — FastAPI Server (Headless API)
uv run uvicorn src.scd2_copilot.api.app:app --reload --port 8000

# Terminal 2 — Streamlit Application (Operator Dashboard)
uv run streamlit run app/streamlit_app.py

# Terminal 3 — Ingestion Worker (Continuous Polling / Micro-batch)
uv run python -m src.scd2_copilot.worker
```

---

## Demo 1 — Product Master Monitor (Primary Demo Narrative)

The active default monitor tracks `public.product_master`:
- **Business Key**: `["product_id"]` (e.g. `PRD-0001` through `PRD-0240`)
- **Change Timestamp**: `updated_at` (managed automatically via PostgreSQL `BEFORE UPDATE` trigger)
- **Tracked Columns**: `product_name`, `category`, `supplier_id`, `price`, `status`
- **SCD2 Target**: `monitored_entity_history` (stores JSON attributes with half-open temporal validity `[effective_from, effective_to)`)

---

### Scenario 1.1: Baseline Bootstrap

When the ingestion worker starts with no prior checkpoint:

1. Polling discovers all 240 baseline records (`updated_at = '2026-09-01 00:00:00+00'`).
2. Deterministic change detection identifies all 240 records as `NEW`.
3. Guardrail evaluates baseline onboarding as **NORMAL** (bootstrap phase).
4. All 240 records are committed to `monitored_entity_history` with `is_current = true`, `effective_from = '2026-09-01'`, `effective_to = NULL`.
5. Checkpoint advances to `2026-09-01 00:00:00+00`.

**Verification**:
```sql
SELECT COUNT(*) AS active_products
FROM monitored_entity_history
WHERE source_name = 'product_master' AND is_current = true;
-- Returns: 240
```

---

### Scenario 1.2: Normal Operational Price Adjustment

In day-to-day operations, a merchandising analyst makes a small operational price adjustment on a workstation in the Computers category:

```sql
UPDATE public.product_master
SET price = price + 15.00
WHERE product_id = 'PRD-0031';
```

**Execution**:
1. Worker polling detects 1 updated record via incremental cursor (`updated_at > watermark`).
2. Deterministic SCD2 engine detects `CHANGED` on `price` (`499.00 -> 514.00`).
3. Guardrail evaluates batch significance:
   - Volume: 1 record (< threshold 25)
   - Population impact: 1 / 240 = 0.4% (< threshold 10%)
   - Quantity swing: +15.00 / 3.0% (< threshold 300%)
   - Decision: **NORMAL** (`LOW` severity).
4. **SCD2 Transformation**:
   - Closes prior active version: `effective_to = processing_date`, `is_current = false`.
   - Inserts new active version: `effective_from = processing_date`, `effective_to = NULL`, `is_current = true`.
5. Checkpoint advances to the new timestamp.
6. Processing run recorded with `status = COMMITTED`.

**Verification**:
```sql
SELECT history_id, is_current, effective_from, effective_to, attributes->>'price' AS price
FROM monitored_entity_history
WHERE source_name = 'product_master' AND entity_key->>'product_id' = 'PRD-0031'
ORDER BY effective_from ASC;
-- Returns 2 rows: Version 1 (closed) and Version 2 (active with new price)
```

---

### Scenario 1.3: Suspicious Catalog Mass Deactivation

A rogue script or faulty batch job attempts to deactivate all 30 products in the `Electronics` category simultaneously:

```sql
UPDATE public.product_master
SET status = 'INACTIVE'
WHERE category = 'Electronics' AND status = 'ACTIVE';
```

**Execution**:
1. Worker polling discovers a micro-batch of 30 changed records.
2. Deterministic change detection identifies 30 `CHANGED` records (`status: ACTIVE -> INACTIVE`).
3. SCD2 invariant validation passes in-memory (`effective_from < effective_to`, zero overlaps).
4. Guardrail evaluates batch significance against deterministic rules:
   - `HIGH_CHANGE_VOLUME`: 30 changed records >= threshold 25.
   - `HIGH_POPULATION_IMPACT`: 30 / 240 = 12.5% >= threshold 10%.
   - `MASS_DEACTIVATION`: 30 deactivations >= safe limit 5.
   - Decision: **SUSPICIOUS** (`HIGH` severity).
5. **Containment Protection Active**:
   - Batch is **HELD** — `monitored_entity_history` is **NOT modified** (0 writes).
   - Checkpoint is **NOT advanced** (remains at Scenario 1.2 watermark).
   - Complete batch evidence is frozen and saved to `held_change_batch`.
   - Grounded GenAI explanation generated (Gemini primary -> Groq fallback -> deterministic template).

**Verification (Target History Untouched)**:
```sql
-- Electronics in target history remain ACTIVE (untouched by rogue batch):
SELECT COUNT(*) AS active_electronics
FROM monitored_entity_history
WHERE source_name = 'product_master'
  AND is_current = true
  AND attributes->>'category' = 'Electronics'
  AND attributes->>'status' = 'ACTIVE';
-- Returns: 30 (zero corrupted rows committed)

-- Check hold in containment queue:
SELECT hold_id, status, severity, records_affected, reason
FROM held_change_batch
WHERE source_name = 'product_master' AND status = 'HELD'
ORDER BY created_at DESC LIMIT 1;
```

---

### Scenario 1.4: Operator Containment & Recovery

An operator reviews the held batch and either releases, reprocesses, or discards it.

#### Option A: Via Streamlit UI
1. Navigate to **⚡ Live Guardrail Monitor (V2)** → **Containment Queue** tab.
2. Locate the held batch with severity `HIGH` and triggered rules `[HIGH_CHANGE_VOLUME, HIGH_POPULATION_IMPACT, MASS_DEACTIVATION]`.
3. Expand **🤖 AI Root-Cause Explanation** to inspect the grounded narrative.
4. Click **Release Batch** (with operator reason e.g. "Verified bulk seasonal deactivation by merchandising director").

#### Option B: Via FastAPI Headless API
```bash
curl -X POST "http://localhost:8000/api/v1/holds/<hold_id>/release" \
  -H "Content-Type: application/json" \
  -d '{"reason": "Verified bulk seasonal deactivation by merchandising director."}'
```

#### Expected Result After Release:
1. Hold status transitions to `RELEASED`.
2. The 30 deactivated records are safely committed to `monitored_entity_history` (`is_current = true`, `status = INACTIVE`).
3. Checkpoint advances to the batch watermark.

**Verification**:
```sql
SELECT COUNT(*) AS inactive_electronics
FROM monitored_entity_history
WHERE source_name = 'product_master'
  AND is_current = true
  AND attributes->>'category' = 'Electronics'
  AND attributes->>'status' = 'INACTIVE';
-- Returns: 30
```

---

## Demo 2 — Warehouse Inventory Monitor (Backward Compatibility)

Demonstrates backward compatibility with the canonical warehouse inventory source table (`inventory_source`) with composite business keys `(sku_id, warehouse_id)`.

### Scenario 2.1: Normal Inventory Adjustment

```sql
UPDATE inventory_source
SET quantity_on_hand = quantity_on_hand - 2,
    updated_at = NOW()
WHERE sku_id = 'SKU-1001' AND warehouse_id = 'WH-EAST';
```

**Expected**: NORMAL → COMMITTED → closes prior version in `inventory_history`, creates new active version.

### Scenario 2.2: Suspicious Quantity Swing

```sql
UPDATE inventory_source
SET quantity_on_hand = 5,
    updated_at = NOW()
WHERE sku_id = 'SKU-1001' AND warehouse_id = 'WH-EAST'
  AND quantity_on_hand > 100;
```

**Expected**: Guardrail flags `LARGE_QUANTITY_SWING` (>300% relative change with absolute delta >= 50) → HELD → zero target writes.

### Scenario 2.3: Operator Recovery

Operator inspects the hold in the Containment Queue tab and clicks Release. Target table `inventory_history` is updated only upon release.

---

## UI Operator Walkthrough

The operator workflow through the Streamlit interface follows this path:

```
Monitor → Change → Decision → Why → Evidence → History → Recovery
```

1. **⚙️ Source Configuration**: Shows active monitor config (`product_master`), validates connectivity and column mappings against live database.
2. **Containment Queue**: Shows held batches with severity, triggered rules, frozen evidence, AI explanation, and action buttons (**Release**, **Reprocess**, **Discard**).
3. **Processing Runs**: Shows execution history with status, durations, seen/changed/held metrics.
4. **📋 Upstream Source Records**: Real-time paginated view of raw records from `public.product_master`.
5. **SCD2 History**: Point-in-time entity lookup by business key (`product_id`) with complete chronological version audit trail.

---

## Automated Verification Commands

```bash
# Verify live Product Master end-to-end lifecycle on Supabase:
uv run python scripts/verify_product_master_live.py

# Run Product Master monitor unit tests:
uv run pytest tests/test_product_master_monitor.py -v

# Run full regression suite (excluding long-running benchmarks):
uv run pytest -m "not benchmark" -q
```
