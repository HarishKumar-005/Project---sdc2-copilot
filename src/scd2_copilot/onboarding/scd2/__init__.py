"""SCD2 Integration package for Customer Data Onboarding."""

from __future__ import annotations

from .adapter import CustomerSCD2Adapter
from .models import (
    CustomerPointInTimeState,
    CustomerSCD2Config,
    CustomerSCD2ExecutionResult,
)
from .service import CustomerSCD2Service

__all__ = [
    "CustomerPointInTimeState",
    "CustomerSCD2Adapter",
    "CustomerSCD2Config",
    "CustomerSCD2ExecutionResult",
    "CustomerSCD2Service",
]
