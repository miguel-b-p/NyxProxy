"""
Data models for NyxProxy.

This module contains all data model definitions including:
- Proxy-related models
- Test result models
- Bridge runtime models
"""

from .proxy import (
    Outbound,
    TestResult,
    BridgeRuntime,
    GeoInfo,
)

__all__ = [
    "Outbound",
    "TestResult",
    "BridgeRuntime",
    "GeoInfo",
]
