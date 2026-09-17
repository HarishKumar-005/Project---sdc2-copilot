"""Neutral in-memory runtime registry for active MonitorConfig instances.

NOTE: In Phase 4, monitor definitions registered here are kept in-memory
for the lifetime of the process and are NOT yet persisted across application/API restarts.
"""

from __future__ import annotations

import threading
from typing import Optional

from .inventory import get_default_inventory_monitor_config
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

        If name is 'default', 'active', or matches the canonical default inventory name,
        and is not explicitly overridden in the registry, returns the canonical inventory default.
        """
        with self._lock:
            if name in self._monitors:
                return self._monitors[name]
        default = get_default_inventory_monitor_config()
        if name in (default.name, "default", "active"):
            return default
        return None

    def list_all(self) -> list[MonitorConfig]:
        """Return all registered monitors. Always ensures canonical default is present."""
        default = get_default_inventory_monitor_config()
        with self._lock:
            configs = list(self._monitors.values())
        if not any(c.name == default.name for c in configs):
            configs.insert(0, default)
        return configs

    def clear(self) -> None:
        """Clear custom registered monitors (useful for test isolation)."""
        with self._lock:
            self._monitors.clear()


# Global singleton instance
_registry = MonitorRegistry()


def get_monitor_registry() -> MonitorRegistry:
    """Return the global in-memory MonitorRegistry singleton."""
    return _registry
