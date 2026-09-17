"""API route modules."""

from .health import router as health_router
from .history import router as history_router
from .holds import router as holds_router
from .inventory import router as inventory_router
from .metrics import router as metrics_router
from .monitors import router as monitors_router
from .runs import router as runs_router

__all__ = [
    "health_router",
    "history_router",
    "holds_router",
    "inventory_router",
    "metrics_router",
    "monitors_router",
    "runs_router",
]
