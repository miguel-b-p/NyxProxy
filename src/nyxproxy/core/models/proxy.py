from __future__ import annotations

"""Data models for shared objects within the proxy manager."""

import asyncio
import subprocess  # nosec B404
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class Outbound:
    """Represents a parsed outbound configuration for Xray."""

    tag: str
    config: Dict[str, Any]
    protocol: str
    host: str
    port: int


@dataclass
class BridgeRuntime:
    """Represents an active HTTP bridge and its associated resources."""

    tag: str
    port: int
    uri: str
    process: Optional[asyncio.subprocess.Process]
    workdir: Optional[Path]

    @property
    def url(self) -> str:
        """Returns the full local HTTP URL of the bridge."""
        return f"http://127.0.0.1:{self.port}"


@dataclass
class GeoInfo:
    """Holds geolocation information for an IP address."""

    ip: str
    country_code: Optional[str] = None
    country_name: Optional[str] = None
    is_loading: bool = False

    @property
    def label(self) -> str:
        """A user-friendly label for the location."""
        if self.country_name:
            return self.country_name
        if self.country_code:
            return self.country_code
        if self.is_loading:
            return "Loading..."
        return "Unknown"


@dataclass
class TestResult:
    """Contains the detailed results of a single proxy health check."""

    uri: str
    tag: str
    protocol: str
    host: str
    port: int
    status: str = "PENDING"
    ping: Optional[float] = None
    error: Optional[str] = None
    server_geo: Optional[GeoInfo] = None
    exit_geo: Optional[GeoInfo] = None
    tested_at_ts: Optional[float] = None