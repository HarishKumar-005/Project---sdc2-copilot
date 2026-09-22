"""Live verification script for Product Master end-to-end lifecycle on Supabase PostgreSQL.

Executes:
1. Baseline bootstrap ingestion (240 products -> monitored_entity_history).
2. Normal single-record update -> COMMITTED, NORMAL, SCD2 version created.
3. Suspicious bulk deactivation (30 products) -> HELD, SUSPICIOUS, target untouched.
4. Operator recovery release -> RELEASED, history updated only upon release, checkpoint advanced.
"""

from __future__ import annotations

import logging
from pathlib import Path
import sys
from uuid import UUID

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.scd2_copilot.config import get_settings
from src.scd2_copilot.containment.service import ContainmentService
from src.scd2_copilot.db.connection import DatabaseManager
from src.scd2_copilot.guardrail.models import RuleId
from src.scd2_copilot.worker.worker import IngestionWorker

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("scd2_copilot.verify_live")


def run_live_verification() -> None:
    settings = get_settings()
    db = DatabaseManager(settings=settings)
    if not db.is_configured:
        logger.error("DATABASE_URL is not configured.")
        sys.exit(1)

    # Ensure database is freshly seeded at baseline
    from scripts.seed_product_master import main as seed_main
    logger.info("Seeding product_master baseline...")
    assert seed_main() == 0, "Failed to seed product_master"

    # Reset any previous product_master history/checkpoints for a pristine bootstrap run
    logger.info("Resetting product_master checkpoints and history for clean bootstrap verification...")
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM monitored_entity_history WHERE source_name = 'product_master';")
            cur.execute("DELETE FROM processing_checkpoint WHERE source_name = 'product_master';")
            cur.execute("DELETE FROM held_change_batch WHERE source_name = 'product_master';")
            cur.execute("DELETE FROM processing_run WHERE source_name = 'product_master';")
        conn.commit()

    logger.info("Initializing IngestionWorker with active default monitor (product_master)...")
    worker = IngestionWorker(db_manager=db, batch_size=300)
    assert worker.source_name == "product_master"
    assert worker.business_key == ["product_id"]

    # ── Phase 1: Baseline Bootstrap ─────────────────────────────
    logger.info("Phase 1: Running bootstrap cycle for product_master...")
    result1 = worker.run_once(raise_on_error=True)
    logger.info(
        "Phase 1 result: status=%s, seen=%d, changed=%d",
        result1.status,
        result1.records_seen,
        result1.records_changed,
    )
    assert result1.status in ("COMMITTED", "EMPTY"), f"Unexpected status: {result1.status}"

    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS active_count FROM monitored_entity_history "
                "WHERE source_name = 'product_master' AND is_current = true;"
            )
            res = cur.fetchone()
            active_count = res["active_count"] if isinstance(res, dict) else res[0]
            logger.info("Active SCD2 rows in monitored_entity_history: %d", active_count)
            assert active_count == 240, f"Expected 240 active rows, found {active_count}"

    # ── Phase 2: Normal Single-Record Price Update ──────────────
    logger.info("Phase 2: Updating price for PRD-0031...")
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE public.product_master SET price = price + 15.00 WHERE product_id = 'PRD-0031';"
            )
        conn.commit()

    logger.info("Phase 2: Running worker cycle for normal update...")
    result2 = worker.run_once(raise_on_error=True)
    logger.info(
        "Phase 2 result: status=%s, seen=%d, changed=%d",
        result2.status,
        result2.records_seen,
        result2.records_changed,
    )
    assert result2.status == "COMMITTED"
    assert result2.guardrail_decision is not None
    assert result2.guardrail_decision.is_normal, "Expected NORMAL decision for single price update"

    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT history_id, is_current, effective_from, effective_to, attributes "
                "FROM monitored_entity_history "
                "WHERE source_name = 'product_master' AND entity_key->>'product_id' = 'PRD-0031' "
                "ORDER BY effective_from ASC;"
            )
            prd_rows = cur.fetchall()
            logger.info("PRD-0031 history versions: %d version(s)", len(prd_rows))
            assert len(prd_rows) >= 2, f"Expected at least 2 versions for PRD-0031, got {len(prd_rows)}"
            v_old = prd_rows[-2]
            v_new = prd_rows[-1]
            assert v_old["is_current"] is False
            assert v_old["effective_to"] is not None
            assert v_new["is_current"] is True
            assert v_new["effective_to"] is None

    # ── Phase 3: Suspicious Bulk Deactivation (30 records) ──────
    logger.info("Phase 3: Deactivating 30 Electronics products...")
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE public.product_master SET status = 'INACTIVE' "
                "WHERE category = 'Electronics' AND status = 'ACTIVE';"
            )
        conn.commit()

    logger.info("Phase 3: Running worker cycle for suspicious bulk update...")
    result3 = worker.run_once(raise_on_error=True)
    logger.info(
        "Phase 3 result: status=%s, seen=%d, held=%d",
        result3.status,
        result3.records_seen,
        result3.records_held,
    )
    assert result3.status == "HELD"
    assert result3.guardrail_decision is not None
    assert result3.guardrail_decision.is_suspicious, "Expected SUSPICIOUS decision for mass deactivation"
    triggered_rules = [r.rule_id for r in result3.guardrail_decision.triggered_rules]
    logger.info("Phase 3 triggered rules: %s", triggered_rules)
    assert RuleId.MASS_DEACTIVATION.value in triggered_rules or RuleId.HIGH_CHANGE_VOLUME.value in triggered_rules

    # Verify monitored_entity_history was NOT modified
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS active_electronics FROM monitored_entity_history "
                "WHERE source_name = 'product_master' AND is_current = true "
                "AND attributes->>'category' = 'Electronics' "
                "AND attributes->>'status' = 'ACTIVE';"
            )
            res = cur.fetchone()
            active_elec = res["active_electronics"] if isinstance(res, dict) else res[0]
            logger.info("Active Electronics in history target: %d (must still be 30, untouched)", active_elec)
            assert active_elec == 30, "Target history was incorrectly modified during HELD cycle!"

    # ── Phase 4: Operator Containment & Recovery Release ─────────
    logger.info("Phase 4: Locating held batch and performing operator release...")
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT hold_id, run_id, status, reason "
                "FROM held_change_batch "
                "WHERE source_name = 'product_master' AND status = 'HELD' "
                "ORDER BY created_at DESC LIMIT 1;"
            )
            hold_record = cur.fetchone()
            assert hold_record is not None, "No active HELD batch found in held_change_batch!"
            hold_id = UUID(str(hold_record["hold_id"]))
            logger.info("Found held batch: %s, reason=%s", hold_id, hold_record["reason"])

    containment_svc = worker.containment
    logger.info("Releasing held batch %s...", hold_id)
    release_res = containment_svc.release_held_batch(
        hold_id=hold_id,
        operator_reason="Verified bulk seasonal deactivation by merchandising director.",
    )
    logger.info(
        "Release result: status=%s, records_affected=%d",
        release_res.status,
        release_res.records_affected,
    )
    assert release_res.status == "RELEASED"
    assert release_res.records_affected == 30

    # Verify history updated AFTER release
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS inactive_electronics FROM monitored_entity_history "
                "WHERE source_name = 'product_master' AND is_current = true "
                "AND attributes->>'category' = 'Electronics' "
                "AND attributes->>'status' = 'INACTIVE';"
            )
            res = cur.fetchone()
            inactive_elec = res["inactive_electronics"] if isinstance(res, dict) else res[0]
            logger.info("Inactive Electronics in target history after release: %d", inactive_elec)
            assert inactive_elec == 30, f"Expected 30 released inactive records, got {inactive_elec}"

    logger.info("=================================================================")
    logger.info("ALL LIVE PRODUCT MASTER DEMO PHASES VERIFIED SUCCESSFULLY ON SUPABASE!")
    logger.info("=================================================================")


if __name__ == "__main__":
    run_live_verification()
