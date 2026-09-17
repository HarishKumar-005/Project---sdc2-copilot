"""Command-line operational entry point for the incremental ingestion worker."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from typing import Optional

from ..config import get_settings
from ..db.connection import DatabaseManager
from .worker import IngestionWorker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("scd2_copilot.worker.cli")


def main(argv: Optional[list[str]] = None) -> int:
    """Parse CLI options and execute the ingestion worker."""
    parser = argparse.ArgumentParser(
        description="SCD2 Copilot Incremental Ingestion Worker",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Execute exactly one polling cycle and exit",
    )
    parser.add_argument(
        "--source-name",
        type=str,
        default=None,
        help="Stream source name (default: from configuration)",
    )
    parser.add_argument(
        "--table-name",
        type=str,
        default=None,
        help="Operational source table name (default: inventory_source)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="Maximum records per micro-batch",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="Polling interval in seconds for continuous mode",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        help="Maximum cycles to execute before exiting continuous mode",
    )

    args = parser.parse_args(argv)
    settings = get_settings()

    db_mgr = DatabaseManager(settings=settings)
    if not db_mgr.is_configured:
        logger.error("DATABASE_URL is not configured. Worker cannot start.")
        return 1

    worker = IngestionWorker(
        db_manager=db_mgr,
        source_name=args.source_name,
        table_name=args.table_name,
        batch_size=args.batch_size,
        poll_interval_seconds=args.interval,
        settings=settings,
    )

    # Register OS signal handlers for graceful shutdown
    def _signal_handler(signum, frame):
        logger.info("Interrupt signal (%d) received. Requesting clean worker shutdown...", signum)
        worker.stop()

    try:
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)
    except (ValueError, AttributeError):
        pass  # On non-main threads or some Windows configurations

    try:
        if args.once:
            logger.info("Executing single worker cycle (--once mode)...")
            result = worker.run_once(raise_on_error=True)
            logger.info(
                "Cycle finished: status=%s, seen=%d, changed=%d",
                result.status,
                result.records_seen,
                result.records_changed,
            )
            return 0 if result.status in ("EMPTY", "COMMITTED") else 1
        else:
            logger.info("Starting continuous worker mode. Press Ctrl+C to terminate.")
            worker.run_forever(max_cycles=args.max_cycles)
            return 0
    except Exception as exc:
        logger.error("Worker encountered fatal error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
