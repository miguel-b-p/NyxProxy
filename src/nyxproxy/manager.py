#!/usr//-bin/env python3
# -*- coding: utf-8 -*-
"""Tool and library to test and create HTTP bridges for V2Ray/Xray proxies."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

import niquests as requests
import urllib3
from dotenv import load_dotenv
from rich.console import Console

from .core import (
    BridgeMixin,
    CacheMixin,
    ChainsMixin,
    LoadingMixin,
    NyxProxyError,
    ParsingMixin,
    ProxyUtilityMixin,
    TestingMixin,
    BridgeRuntime as CoreBridgeRuntime,
    GeoInfo as CoreGeoInfo,
    Outbound as CoreOutbound,
    TestResult as CoreTestResult,
)
from .core.config import (
    CACHE_VERSION,
    DEFAULT_CACHE_FILENAME,
    DEFAULT_RICH_THEME,
    DEFAULT_TEST_URL,
    DEFAULT_USER_AGENT,
    STATUS_STYLES,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
load_dotenv()

__all__ = ["Proxy"]


class Proxy(
    ProxyUtilityMixin,
    CacheMixin,
    LoadingMixin,
    ParsingMixin,
    TestingMixin,
    BridgeMixin,
    ChainsMixin,
):
    """Manages a collection of proxies, with support for testing and creating HTTP bridges."""

    CACHE_VERSION = CACHE_VERSION
    STATUS_STYLES = STATUS_STYLES

    Outbound = CoreOutbound
    BridgeRuntime = CoreBridgeRuntime
    TestResult = CoreTestResult
    GeoInfo = CoreGeoInfo

    def __init__(
        self,
        proxies: Optional[Iterable[str]] = None,
        sources: Optional[Iterable[str]] = None,
        *,
        country: Optional[str] = None,
        max_count: int = 0,
        use_console: bool = False,
        use_cache: bool = True,
        cache_path: Optional[Union[str, os.PathLike]] = None,
        requests_session: Optional[Any] = None,
    ) -> None:
        """Initializes the manager by loading proxies, sources, and cache."""
        self._findip_token = os.getenv("FINDIP_TOKEN")
        if not self._findip_token:
            raise NyxProxyError(
                "The findip.net API token has not been set. "
                "Define the FINDIP_TOKEN environment variable in a .env file."
            )

        self.country_filter = country
        self.max_count = max_count
        self.use_cache = use_cache
        self.requests = requests_session or requests.Session()
        self.console = Console(theme=DEFAULT_RICH_THEME) if use_console else None

        self.test_url = DEFAULT_TEST_URL
        self.user_agent = DEFAULT_USER_AGENT

        self._outbounds: Dict[str, Proxy.Outbound] = {}
        self._entries: List[Proxy.TestResult] = []
        self._bridges: List[Proxy.BridgeRuntime] = []
        self._parse_errors: List[str] = []
        self._running = False
        self._atexit_registered = False

        self._port_allocation_lock = threading.Lock()
        self._allocated_ports: set[int] = set()
        self._cache_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wait_thread: Optional[threading.Thread] = None

        default_cache_path = Path.home() / ".nyxproxy" / DEFAULT_CACHE_FILENAME
        self.cache_path = Path(cache_path) if cache_path is not None else default_cache_path
        self._cache_entries: Dict[str, Dict[str, Any]] = {}
        self._cache_available = False
        self._ip_lookup_cache: Dict[str, Optional[Proxy.GeoInfo]] = {}

        # 1. Load cache from disk first to make it available for sources.
        if self.use_cache:
            self._load_cache()

        # 2. Load proxies from provided sources, applying cached data if available.
        if proxies:
            self.add_proxies(proxies)
        if sources:
            self.add_sources(sources)

        # 3. Merge functional proxies from cache that were not in the sources.
        self._merge_ok_cache_entries()

    @property
    def entries(self) -> List[Proxy.TestResult]:
        """Returns the records loaded or resulting from the latest tests."""
        return self._entries

    @property
    def parse_errors(self) -> List[str]:
        """List of parsing errors encountered while loading proxies."""
        return self._parse_errors
