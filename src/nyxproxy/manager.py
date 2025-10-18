#!/usr//-bin/env python3
# -*- coding: utf-8 -*-
"""Tool and library to test and create HTTP bridges for V2Ray/Xray proxies."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

import httpx
import urllib3
from dotenv import load_dotenv
from rich.console import Console

from .core import (
    BridgeMixin,
    CacheMixin,
    ChainsMixin,
    ConfigDeduplicator,
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
from .core.config.settings import (
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
        self.requests = requests_session or httpx.AsyncClient()
        self.console = Console(theme=DEFAULT_RICH_THEME) if use_console else None

        self.test_url = DEFAULT_TEST_URL
        self.user_agent = DEFAULT_USER_AGENT

        self._outbounds: Dict[str, Proxy.Outbound] = {}
        self._entries: List[Proxy.TestResult] = []
        self._bridges: List[Proxy.BridgeRuntime] = []
        self._parse_errors: List[str] = []
        self._running = False
        self._sources: List[str] = []  # Store proxy sources for reloading

        self._port_allocation_lock = asyncio.Lock()
        self._allocated_ports: set[int] = set()
        self._cache_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()

        default_cache_path = Path.home() / ".nyxproxy" / DEFAULT_CACHE_FILENAME
        self.cache_path = Path(cache_path) if cache_path is not None else default_cache_path
        self._cache_entries: Dict[str, Dict[str, Any]] = {}
        self._cache_available = False
        self._ip_lookup_cache: Dict[str, Optional[Proxy.GeoInfo]] = {}

    async def load_resources(
        self,
        proxies: Optional[Iterable[str]] = None,
        sources: Optional[Iterable[str]] = None,
    ) -> None:
        # 1. Load cache from disk first to make it available for sources.
        if self.use_cache:
            await self._load_cache()

        # 2. Store sources for potential reloading when rotating proxies
        if sources:
            self._sources = list(sources)

        # 3. Load proxies from provided sources, applying cached data if available.
        if proxies:
            self.add_proxies(proxies)
        if sources:
            await self.add_sources(sources)

        # 4. Deduplicate proxies
        if self._outbounds:
            outbounds_list = []
            for ob in self._outbounds.values():
                flat_config = {}
                flat_config['type'] = ob.protocol
                flat_config['remarks'] = ob.tag
                flat_config['server'] = ob.host
                flat_config['port'] = ob.port
                
                xray_config = ob.config
                settings = xray_config.get('settings', {})
                stream_settings = xray_config.get('streamSettings', {})

                if ob.protocol in ['vless', 'vmess']:
                    vnext = settings.get('vnext', [{}])[0]
                    users = vnext.get('users', [{}])[0]
                    flat_config['uuid'] = users.get('id', '')
                elif ob.protocol == 'trojan':
                    servers = settings.get('servers', [{}])[0]
                    flat_config['password'] = servers.get('password', '')
                elif ob.protocol == 'shadowsocks':
                    servers = settings.get('servers', [{}])[0]
                    flat_config['password'] = servers.get('password', '')
                    flat_config['method'] = servers.get('method', '')

                flat_config['network'] = stream_settings.get('network', '')
                if flat_config['network'] == 'ws':
                    ws_settings = stream_settings.get('wsSettings', {})
                    flat_config['path'] = ws_settings.get('path', '')
                    flat_config['host'] = ws_settings.get('headers', {}).get('Host', '')
                elif flat_config['network'] == 'grpc':
                    grpc_settings = stream_settings.get('grpcSettings', {})
                    flat_config['serviceName'] = grpc_settings.get('serviceName', '')

                flat_config['tls'] = stream_settings.get('security', '')
                if flat_config['tls'] in ('tls', 'reality'):
                    tls_settings = stream_settings.get(f'{flat_config["tls"]}Settings', {})
                    flat_config['sni'] = tls_settings.get('serverName', '')
                    if 'alpn' in tls_settings:
                        flat_config['alpn'] = ','.join(tls_settings.get('alpn', []))

                outbounds_list.append(flat_config)

            deduplicator = ConfigDeduplicator(outbounds_list, console=self.console)
            unique_configs = deduplicator.process()
            
            if unique_configs:
                self._outbounds.clear()
                self._entries.clear()
                
                uris_to_add = []
                for config in unique_configs:
                    uri = deduplicator.reconstruct_config_url(config)
                    if uri:
                        uris_to_add.append(uri)
                    elif self.console:
                        self.console.print(f"[warning]Could not reconstruct URI for config: {config.get('remarks', 'N/A')}[/warning]")
                
                self.add_proxies(uris_to_add)

        # 5. Merge functional proxies from cache that were not in the sources.
        self._merge_ok_cache_entries()

    @property
    def entries(self) -> List[Proxy.TestResult]:
        """Returns the records loaded or resulting from the latest tests."""
        return self._entries

    @property
    def parse_errors(self) -> List[str]:
        """List of parsing errors encountered while loading proxies."""
        return self._parse_errors
