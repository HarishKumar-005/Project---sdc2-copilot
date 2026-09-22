"""Neutral in-memory runtime registry for active MonitorConfig instances.

NOTE: In Phase 4, monitor definitions registered here are kept in-memory
for the lifetime of the process and are NOT yet persisted across application/API restarts.
"""

from __future__ import annotations

import threading
from typing import Optional

from .inventory import get_default_inventory_monitor_config
from .product_master import get_default_product_master_monitor_config
from .models import MonitorConfig


class MonitorRegistry:
    """Thread-safe in-memory registry of active MonitorConfig definitions."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._monitors: dict[str, MonitorConfig] = {}

    def register(self, config: MonitorConfig) -> None:
        """Register or deterministically replace a monitor configuration."""
        with self._lock:
            self._monitors[config.name] = config

    def get(self, name: str) -> Optional[MonitorConfig]:
        """Retrieve a monitor by name.

        - 'active' or 'product_master': returns the active demonstration monitor (product_master).
        - 'default', 'warehouse_inventory', or 'inventory': returns the canonical inventory default.
        """
        with self._lock:
            if name in self._monitors:
                return self._monitors[name]

        active_demo = get_default_product_master_monitor_config()
        if name in (active_demo.name, "active"):
            return active_demo

        inventory_default = get_default_inventory_monitor_config()
        if name in (inventory_default.name, "default", "inventory"):
            return inventory_default

        return None

    def list_all(self) -> list[MonitorConfig]:
        """Return all registered monitors. Always ensures active demo is first and canonical default is present."""
        active_demo = get_default_product_master_monitor_config()
        inventory_default = get_default_inventory_monitor_config()
        with self._lock:
            custom_configs = [
                c for c in self._monitors.values()
                if c.name not in (active_demo.name, inventory_default.name)
            ]
            pm = self._monitors.get(active_demo.name, active_demo)
            inv = self._monitors.get(inventory_default.name, inventory_default)
        return [pm] + custom_configs + [inv]

    def clear(self) -> None:
        """Clear custom registered monitors (useful for test isolation)."""
        with self._lock:
            self._monitors.clear()


# Global singleton instance
_registry = MonitorRegistry()


def get_monitor_registry() -> MonitorRegistry:
    """Return the global in-memory MonitorRegistry singleton."""
    return _registry
