# SCD2 Copilot — Demo Script

> **Purpose**: Reproducible demonstration sequences proving the V3 system works end-to-end as a configurable PostgreSQL data-change guardrail.

---

## Prerequisites

### Environment Variables

The following must be set before starting any service:

```bash
DATABASE_URL=postgresql://...          # Supabase PostgreSQL connection string
SUPABASE_URL=https://...               # Supabase project URL
SUPABASE_ANON_KEY=...                  # Supabase anon/public key
SUPABASE_SERVICE_ROLE_KEY=...          # Supabase service role key (server-side only)
GEMINI_API_KEY=...                     # Google Gemini API key (optional, template fallback available)
GROQ_API_KEY=...                       # Groq API key (optional, secondary fallback)
```

### Database Tables

The following tables must exist in Supabase PostgreSQL:

- `inventory_source` — Demo A source table (warehouse inventory)
- `inventory_history` — Demo A SCD2 target
- `monitored_entity_history` — Generic SCD2 target (Demo B)
- `processing_checkpoint` — Watermark state
- `processing_run` — Run audit log
- `held_change_batch` — Containment queue

### Starting Services

```bash
# Terminal 1 — FastAPI
uv run uvicorn src.scd2_copilot.api.app:app --reload --port 8000

# Terminal 2 — Streamlit
uv run streamlit run app/streamlit_app.py

# Terminal 3 — Worker (optional, for continuous monitoring)
uv run python -m src.scd2_copilot.worker
```

---

## Demo A — Inventory Monitor

The canonical warehouse inventory demo uses `inventory_source` with business keys `(sku_id, warehouse_id)` and tracked columns `(quantity_on_hand, reorder_level, status)`.

### Scenario A1: Normal Operational Change

**Setup**: Ensure `inventory_source` has baseline records.

```sql
-- Small quantity adjustment (within guardrail bounds)
UPDATE inventory_source
SET quantity_on_hand = quantity_on_hand - 2,
    updated_at = NOW()
WHERE sku_id = 'SKU-1001' AND warehouse_id = 'WH-EAST';
```

**Expected**:
1. Worker detects the change via incremental cursor
2. SCD2 engine classifies as CHANGED
3. Guardrail evaluates as **NORMAL** (small quantity delta, single record)
4. Previous version closed in `inventory_history` (`effective_to = processing_date`, `is_current = false`)
5. New current version inserted (`effective_to = NULL`, `is_current = true`)
6. Checkpoint advances to the new cursor position
7. Processing run recorded with `status = COMPLETED`

**Verification**:
```sql
-- Check SCD2 history shows two versions
SELECT * FROM inventory_history
WHERE sku_id = 'SKU-1001' AND warehouse_id = 'WH-EAST'
ORDER BY effective_from;
```

### Scenario A2: Suspicious Large Change

```sql
-- Large quantity swing (triggers LARGE_QUANTITY_SWING rule)
UPDATE inventory_source
SET quantity_on_hand = 5,
    updated_at = NOW()
WHERE sku_id = 'SKU-1001' AND warehouse_id = 'WH-EAST'
  AND quantity_on_hand > 100;
```

**Expected**:
1. Worker detects the change
2. Guardrail evaluates as **SUSPICIOUS** (`LARGE_QUANTITY_SWING` rule: >300% relative change)
3. Batch is **HELD** — `inventory_history` is NOT updated
4. Evidence persisted in `held_change_batch` with frozen source records
5. Checkpoint preserved at previous position
6. AI explanation generated (Gemini → Groq → Deterministic Template)

**Verification**:
```sql
-- Check hold exists
SELECT hold_id, status, severity, records_affected
FROM held_change_batch
WHERE source_name = 'inventory' AND status = 'HELD';
```

### Scenario A3: Operator Recovery

Using the Streamlit UI Containment Queue tab:

1. Navigate to **⚡ Live Guardrail Monitor (V2)** → **Containment Queue**
2. Find the held batch from Scenario A2
3. Inspect: evidence, triggered rules, AI explanation
4. Click **Release** (or **Reprocess** / **Discard**)
5. Verify the hold status transitions to `RELEASED`

**Verification after RELEASE**:
```sql
-- History should now be updated
SELECT * FROM inventory_history
WHERE sku_id = 'SKU-1001' AND warehouse_id = 'WH-EAST'
ORDER BY effective_from;

-- Hold should be RELEASED
SELECT hold_id, status, resolved_by, resolved_at
FROM held_change_batch
WHERE source_name = 'inventory'
ORDER BY created_at DESC LIMIT 1;
```

---

## Demo B — Generic Product-Price Monitor

Demonstrates the engine is configuration-driven, not inventory-hard-coded. Uses a separate source table with different business keys and tracked columns.

### Setup: Create Source Table

```sql
CREATE TABLE IF NOT EXISTS test_product_source (
    product_id TEXT NOT NULL,
    price NUMERIC,
    tier TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (product_id)
);

-- Seed baseline data
INSERT INTO test_product_source (product_id, price, tier, updated_at) VALUES
    ('PROD-001', 29.99, 'standard', NOW()),
    ('PROD-002', 99.99, 'premium', NOW()),
    ('PROD-003', 14.99, 'basic', NOW())
ON CONFLICT (product_id) DO UPDATE SET
    price = EXCLUDED.price,
    tier = EXCLUDED.tier,
    updated_at = NOW();
```

### Monitor Configuration

The monitor is configured programmatically (in tests or via API):

```python
from src.scd2_copilot.source import MonitorConfig, PostgresSourceDefinition, ChangeTimestampDefinition

config = MonitorConfig(
    name="product_price_monitor",
    source=PostgresSourceDefinition(table_name="test_product_source"),
    keys=["product_id"],
    change_timestamp=ChangeTimestampDefinition(column="updated_at"),
    tracked_columns=["price", "tier"],
    batch_size=500,
)
```

### Scenario B1: Normal Price Change

```sql
UPDATE test_product_source
SET price = 31.99, updated_at = NOW()
WHERE product_id = 'PROD-001';
```

**Expected**: NORMAL → COMMIT → history in `monitored_entity_history` → checkpoint advances

### Scenario B2: Suspicious Mass Change

```sql
-- Update all products with large price swings
UPDATE test_product_source
SET price = price * 0.1,
    tier = 'clearance',
    updated_at = NOW();
```

**Expected**: SUSPICIOUS (HIGH_CHANGE_VOLUME, LARGE_QUANTITY_SWING, HIGH_POPULATION_IMPACT) → HOLD → evidence preserved

### Scenario B3: Generic History Lookup

Via API:
```bash
curl "http://localhost:8000/api/v1/history/product_price_monitor/entity?key=%7B%22product_id%22%3A%22PROD-001%22%7D" \
  -H "Authorization: Bearer <token>"
```

Via Streamlit UI:
1. Navigate to **SCD2 History** tab
2. Select **product_price_monitor** from dropdown
3. Enter `product_id` = `PROD-001`
4. Click **Search History**
5. View chronological versions with `price` and `tier` attributes

### Scenario B4: Generic Recovery

1. Navigate to **Containment Queue** tab
2. Find the held batch from Scenario B2
3. Inspect evidence — should show per-column change counts for `price` and `tier`
4. Release or Reprocess
5. Verify `monitored_entity_history` is updated

---

## UI Walkthrough

The operator workflow through the Streamlit interface follows this path:

```
Monitor → Change → Decision → Why → Evidence → History → Recovery
```

1. **⚙️ Source Configuration**: Shows active monitor config, validates against live database
2. **Containment Queue**: Shows held batches with severity, triggered rules, evidence, AI explanation, and action buttons
3. **Processing Runs**: Shows run history with status, durations, and batch metrics
4. **SCD2 History**: Point-in-time entity lookup with monitor selector and dynamic key inputs
5. **Source Inventory**: Raw upstream source table view (inventory-specific)

---

## Automated Test Verification

```bash
# Demo A verification (inventory)
uv run pytest tests/test_live_monitoring_pipeline.py -v

# Demo B verification (generic product-price)
uv run pytest tests/test_live_generic_monitor.py -v

# Full regression suite
uv run pytest -m "not benchmark" -q
```

---

## Known Limitations

See [limitations.md](./limitations.md) for the complete verified limitations list.
