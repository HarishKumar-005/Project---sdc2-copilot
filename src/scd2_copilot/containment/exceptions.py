"""Domain exceptions for suspicious batch containment and recovery."""

from __future__ import annotations

from typing import Optional
from uuid import UUID


class ContainmentError(Exception):
    """Base exception for all containment subsystem errors."""

    def __init__(self, message: str, hold_id: Optional[UUID] = None) -> None:
        super().__init__(message)
        self.message = message
        self.hold_id = hold_id


class HoldNotFoundError(ContainmentError):
    """Raised when a specified hold_id does not exist in held_change_batch."""


class InvalidHoldTransitionError(ContainmentError):
    """Raised when attempting an illegal state transition on a held batch."""

    def __init__(
        self,
        message: str,
        hold_id: Optional[UUID] = None,
        current_status: Optional[str] = None,
        target_status: Optional[str] = None,
    ) -> None:
        super().__init__(message, hold_id=hold_id)
        self.current_status = current_status
        self.target_status = target_status


class DuplicateHoldError(ContainmentError):
    """Raised when attempting to create a duplicate active hold for an already held slice."""


class HoldReplayError(ContainmentError):
    """Raised when replaying or reconstructing frozen records from a hold fails."""
