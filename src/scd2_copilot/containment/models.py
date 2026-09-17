"""Typed data models for containment operations and resolution outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from ..db.models import HoldStatus, _ensure_utc


@dataclass(frozen=True)
class HoldResolutionResult:
    """Represents the structured outcome of a hold resolution operation.

    Tracks whether an action (RELEASE, REPROCESS, DISCARD) succeeded,
    its effective status, whether the checkpoint advanced, and whether
    the call was an idempotent replay of an already resolved hold.
    """

    hold_id: UUID
    status: str
    success: bool
    message: str
    resolved_at: Optional[datetime] = None
    records_affected: int = 0
    checkpoint_advanced_to: Optional[datetime] = None
    checkpoint_cursor_keys: Optional[dict[str, Any]] = None
    is_idempotent: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "hold_id": str(self.hold_id),
            "status": self.status,
            "success": self.success,
            "message": self.message,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "records_affected": self.records_affected,
            "checkpoint_advanced_to": (
                self.checkpoint_advanced_to.isoformat()
                if self.checkpoint_advanced_to
                else None
            ),
            "checkpoint_cursor_keys": self.checkpoint_cursor_keys,
            "is_idempotent": self.is_idempotent,
        }
