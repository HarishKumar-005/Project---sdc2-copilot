"""Onboarding source adapters."""

from .base import SourceAdapter
from .csv_adapter import CSVSourceAdapter
from .mock_rest_adapter import MockRESTSourceAdapter

__all__ = [
    "SourceAdapter",
    "CSVSourceAdapter",
    "MockRESTSourceAdapter",
]
