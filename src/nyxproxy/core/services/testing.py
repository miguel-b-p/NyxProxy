from __future__ import annotations

"""Implementations of proxy tests and status reports."""

import asyncio
import ipaddress
import json
import os
import re
import socket
import time
from collections import deque
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

import httpx
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from ..config.exceptions import InsufficientProxiesError
from ..models.proxy import GeoInfo, Outbound, TestResult


class TestingMixin:
    """Set of routines for validating proxies and displaying results."""

    @staticmethod
    def _is_public_ip(ip: str) -> bool:
        """Returns `True` if the IP is public and routable on the Internet."""
        try:
            addr = ipaddress.ip_address(ip)
            return addr.is_global and not addr.is_private
        except ValueError:
            return False

    async def _lookup_geo_info(self, ip: Optional[str]) -> Optional[GeoInfo]:
        """Queries geolocation information for an IP, using an in-memory cache."""
        if not ip or not self._is_public_ip(ip):
            return None

        if ip in self._ip_lookup_cache:
            return self._ip_lookup_cache[ip]

        if not self.requests:
            self._ip_lookup_cache[ip] = None
            return None

        result: Optional[GeoInfo] = None

        # Primary API: findip.net
        if self._findip_token:
            try:
                resp = await self.requests.get(
                    f"https://api.findip.net/{ip}/?token={self._findip_token}", timeout=3
                )
                resp.raise_for_status()
                data = resp.json()

                if "error" in data:
                    raise ValueError(data["error"])

                country_info = data.get("country", {})
                code = (country_info.get("iso_code") or "").strip().upper() or None
                name = (country_info.get("names", {}).get("en") or "").strip() or None

                if code or name:
                    result = GeoInfo(ip=ip, country_code=code, country_name=name)

            except (httpx.RequestError, ValueError, KeyError):
                result = None

        # Fallback API: ip-api.com
        if not result:
            try:
                resp = await self.requests.get(f"http://ip-api.com/json/{ip}", timeout=3)
                resp.raise_for_status()
                data = resp.json()
                
                if data.get("status") == "success":
                    country_code = data.get("countryCode")
                    country_name = data.get("country")
                    if country_code or country_name:
                        result = GeoInfo(ip=ip, country_code=country_code, country_name=country_name)

            except (httpx.RequestError, ValueError, KeyError):
                result = None

        self._ip_lookup_cache[ip] = result
        return result

    async def _socket_screening_phase(
        self,
        entries: List[TestResult],
        threads: int = 100,
        emit_progress: Optional[Any] = None,
        tested_count: int = 0,
        total_proxies: int = 0,
    ) -> tuple[List[TestResult], int]:
        """Phase 1: Quick TCP socket screening to filter out offline proxies.
        
        This phase uses many threads (100+) since it's just TCP connections,
        which are cheap and fast. Returns only proxies that are online.
        
        Args:
            entries: List of test results to screen
            threads: Number of concurrent socket tests (default 100)
            emit_progress: Optional progress emitter
            tested_count: Starting count for progress tracking
            total_proxies: Total proxies for progress calculation
            
        Returns:
            Tuple of (online proxies list, updated tested_count)
        """
        semaphore = asyncio.Semaphore(threads)
        online_proxies = []
        current_tested = tested_count
        
        async def screen_proxy(entry: TestResult):
            nonlocal current_tested
            async with semaphore:
                is_online = await self._test_socket_connection(
                    entry.host, entry.port, timeout=1.0
                )
                
                if is_online:
                    online_proxies.append(entry)
                    # Don't increment tested_count yet - will be incremented in Phase 2
                else:
                    # Proxy is offline - increment counter and report
                    current_tested += 1
                    entry.status = "ERROR"
                    entry.error = "Connection refused (Phase 1)"
                    entry.tested_at_ts = time.time()
                    
                    if emit_progress:
                        self._emit_test_progress(
                            entry, current_tested, total_proxies, emit_progress
                        )
        
        await asyncio.gather(*[screen_proxy(e) for e in entries])
        return online_proxies, current_tested

    async def _post_load_exit_geo(self) -> None:
        """Loads geo for unique exit IPs in parallel after testing."""
        if not self._findip_token:
            return

        ip_to_results: Dict[str, List[TestResult]] = {}
        unique_ips = set()
        for result in self._entries:
            if result.status == "OK" and result.exit_geo and result.exit_geo.ip and not result.exit_geo.country_code:
                unique_ips.add(result.exit_geo.ip)
                ip_to_results.setdefault(result.exit_geo.ip, []).append(result)

        if not unique_ips:
            return

        async def lookup_ip(ip: str) -> tuple[str, Optional[GeoInfo]]:
            geo = await self._lookup_geo_info(ip)
            return ip, geo

        results = await asyncio.gather(*[lookup_ip(ip) for ip in unique_ips])
        for ip, geo in results:
            if geo:
                for res in ip_to_results.get(ip, []):
                    res.exit_geo = geo
            else:
                for res in ip_to_results.get(ip, []):
                    if res.exit_geo:
                        res.exit_geo.is_loading = False

    async def _test_socket_connection(self, host: str, port: int, timeout: float = 1.0) -> bool:
        """Tests if a socket connection can be established to the given host and port."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=timeout
            )
            writer.close()
            await writer.wait_closed()
            return True
        except (asyncio.TimeoutError, ConnectionRefusedError, OSError):
            return False

    async def _test_outbound(self, result: TestResult, timeout: float, skip_geo: bool = False) -> None:
        """Executes measurements for an outbound, updating the result object."""
        try:
            # 1. Quick socket connection test
            is_online = await self._test_socket_connection(result.host, result.port, timeout=1.0)
            if not is_online:
                result.status = "ERROR"
                result.error = "Connection refused"
                return

            # 2. Perform functional test with Xray
            func_result = await self._test_proxy_functionality(self._outbounds[result.uri], timeout=timeout)
            if func_result.get("functional"):
                result.status = "OK"
                result.ping = func_result.get("response_time")
                exit_ip = func_result.get("external_ip")
                if exit_ip and not skip_geo:
                    result.exit_geo = GeoInfo(ip=exit_ip, is_loading=True)

                # 3. Look up server geo info if not already available
                if not result.server_geo:
                    try:
                        ip_info = await asyncio.get_event_loop().getaddrinfo(result.host, None, family=socket.AF_INET)
                        ip = ip_info[0][4][0] if ip_info else None
                        if ip:
                            result.server_geo = await self._lookup_geo_info(ip)
                    except socket.gaierror:
                        pass # Ignore DNS resolution errors
            else:
                result.status = "ERROR"
                result.error = func_result.get("error", "Proxy not functional")

        except Exception as exc:
            result.status = "ERROR"
            result.error = f"Test preparation error: {exc}"
        finally:
            result.tested_at_ts = time.time()
    
    async def _test_outbound_functional_only(self, result: TestResult, timeout: float, skip_geo: bool = False) -> None:
        """Phase 2: Functional test only (socket test already done in Phase 1)."""
        try:
            # Perform functional test with Xray (socket test skipped)
            func_result = await self._test_proxy_functionality(self._outbounds[result.uri], timeout=timeout)
            if func_result.get("functional"):
                result.status = "OK"
                result.ping = func_result.get("response_time")
                exit_ip = func_result.get("external_ip")
                if exit_ip and not skip_geo:
                    result.exit_geo = GeoInfo(ip=exit_ip, is_loading=True)

                # Look up server geo info if not already available
                if not result.server_geo:
                    try:
                        ip_info = await asyncio.get_event_loop().getaddrinfo(result.host, None, family=socket.AF_INET)
                        ip = ip_info[0][4][0] if ip_info else None
                        if ip:
                            result.server_geo = await self._lookup_geo_info(ip)
                    except socket.gaierror:
                        pass # Ignore DNS resolution errors
            else:
                result.status = "ERROR"
                result.error = func_result.get("error", "Proxy not functional (Phase 2)")

        except Exception as exc:
            result.status = "ERROR"
            result.error = f"Test error (Phase 2): {exc}"
        finally:
            result.tested_at_ts = time.time()

    async def _test_proxy_functionality(
        self, outbound: Outbound, timeout: float = 5.0,
    ) -> Dict[str, Any]:
        """Tests the actual functionality of the proxy by creating a temporary bridge."""
        if not self.requests:
            return {"functional": False, "error": "Requests module not available"}

        original_http_proxy = os.environ.get('HTTP_PROXY')
        original_https_proxy = os.environ.get('HTTPS_PROXY')
        
        try:
            async with self._temporary_bridge(outbound, tag_prefix="test") as (port, _):
                proxy_url = f"http://127.0.0.1:{port}"
                os.environ['HTTP_PROXY'] = proxy_url
                os.environ['HTTPS_PROXY'] = proxy_url

                async with httpx.AsyncClient(verify=False) as client:
                    start_time = time.perf_counter()
                    response = await client.get(
                        self.test_url,
                        timeout=timeout,
                        headers={"User-Agent": self.user_agent},
                    )
                    response.raise_for_status()
                    duration_ms = (time.perf_counter() - start_time) * 1000

                    return {
                        "functional": True,
                        "response_time": duration_ms,
                        "external_ip": self._extract_external_ip(response),
                    }
        except Exception as exc:
            return {"functional": False, "error": self._format_request_error(exc, timeout)}
        finally:
            if original_http_proxy:
                os.environ['HTTP_PROXY'] = original_http_proxy
            else:
                os.environ.pop('HTTP_PROXY', None)
            
            if original_https_proxy:
                os.environ['HTTPS_PROXY'] = original_https_proxy
            else:
                os.environ.pop('HTTPS_PROXY', None)

    def _format_request_error(self, exc: Exception, timeout: float) -> str:
        """Normalizes error messages from HTTP requests via proxy."""
        if isinstance(exc, httpx.TimeoutException):
            return f"Timeout after {timeout:.1f}s"
        if isinstance(exc, httpx.ProxyError):
            return f"Proxy error: {str(exc.__cause__)[:100]}"
        if isinstance(exc, httpx.ConnectError):
            return f"Connection error: {str(exc.__cause__)[:100]}"
        if isinstance(exc, httpx.HTTPStatusError):
            return f"HTTP error {exc.response.status_code}: {exc.response.reason_phrase}"
        return f"{type(exc).__name__}: {str(exc)[:100]}"

    @staticmethod
    def _extract_external_ip(response: httpx.Response) -> Optional[str]:
        """Extracts external IP from the test service's JSON response."""
        try:
            text = response.text
            match = re.search(r"ip=([\d\.]+)", text)
            if match:
                return match.group(1).strip()
        except Exception:
            return None
        return None

    async def _perform_health_checks(
        self,
        *,
        country_filter: Optional[str] = None,
        emit_progress: Optional[Any] = None,
        force_refresh: bool = False,
        timeout: float = 5.0,
        threads: int = 1,
        stop_on_success: Optional[int] = None,
        skip_geo: bool = False,
    ) -> None:
        """Runs tests concurrently and manages the cache (2-phase testing)."""
        self._ip_lookup_cache.clear()
        to_test: List[TestResult] = []
        success_count = 0
        tested_count = 0
        total_proxies = len(self._entries)

        # Check cache first
        for result in self._entries:
            cached = self.use_cache and result.uri in self._cache_entries
            if self.use_cache and not force_refresh and cached:
                tested_count += 1
                if result.status == "OK":
                    if country_filter and not self.matches_country(result, country_filter):
                        result.status = "FILTERED"
                        result.error = f"Does not match filter '{country_filter}'"
                    else:
                        success_count += 1
                if emit_progress:
                    self._emit_test_progress(result, tested_count, total_proxies, emit_progress, cached=True)
            else:
                to_test.append(result)

        if stop_on_success and success_count >= stop_on_success:
            if self.use_cache:
                await self._save_cache()
            return

        if to_test:
            # PHASE 1: Socket Screening (fast, many threads)
            # Filter out offline proxies before expensive Xray tests
            online_proxies, tested_count = await self._socket_screening_phase(
                to_test,
                threads=100,  # Many threads for cheap TCP tests
                emit_progress=emit_progress,
                tested_count=tested_count,
                total_proxies=total_proxies,
            )
            
            # Early exit if we already have enough successes
            if stop_on_success and success_count >= stop_on_success:
                if self.use_cache:
                    await self._save_cache()
                return
            
            # PHASE 2: Functional Testing (expensive, fewer threads)
            # Only test proxies that passed socket screening
            if online_proxies:
                semaphore = asyncio.Semaphore(threads)

                async def run_functional_test(res):
                    async with semaphore:
                        # Skip socket test since we already did it in Phase 1
                        await self._test_outbound_functional_only(res, timeout, skip_geo=skip_geo)
                        nonlocal success_count, tested_count
                        
                        # Increment counter for each proxy tested in Phase 2
                        tested_count += 1

                        if res.status == "OK":
                            if country_filter and not self.matches_country(res, country_filter):
                                res.status = "FILTERED"
                                res.error = f"Does not match filter '{country_filter}'"

                        if emit_progress:
                            self._emit_test_progress(res, tested_count, total_proxies, emit_progress)

                        if res.status == "OK":
                            success_count += 1
                            if stop_on_success and success_count >= stop_on_success:
                                # This will cancel other tasks
                                for task in tasks:
                                    task.cancel()

                tasks = [asyncio.create_task(run_functional_test(res)) for res in online_proxies]
                try:
                    await asyncio.gather(*tasks)
                except asyncio.CancelledError:
                    pass

        if not skip_geo:
            await self._post_load_exit_geo()

        if self.use_cache:
            await self._save_cache()
        
        # Save geo cache (independent of proxy cache)
        await self._save_geo_cache()

    def _emit_test_progress(
        self,
        entry: TestResult,
        count: int,
        total: int,
        progress_emitter: Optional["_TestProgressDisplay"],
        cached: bool = False,
    ) -> None:
        """Updates the interactive progress display with the latest result."""
        if not progress_emitter:
            return

        progress_emitter.update(entry, count=count, total=total, cached=cached)

    def _create_progress_display(self, total: int, *, transient: bool = False) -> "_TestProgressDisplay":
        """Initializes the Rich-based progress display."""
        if not self.console:
            raise RuntimeError("Console not available for progress rendering.")
        return _TestProgressDisplay(
            console=self.console,
            total=total,
            status_styles=self.STATUS_STYLES,
            transient=transient,
        )

    async def test(
        self,
        *,
        threads: int = 1,
        country: Optional[str] = None,
        verbose: Optional[bool] = None,
        timeout: float = 3.0,  # Reduced from 5.0s to 3.0s
        force: bool = False,
        find_first: Optional[int] = None,
        skip_geo: bool = False,
        render_summary: bool = True,
        progress_transient: bool = False,
    ) -> List[TestResult]:
        """Tests the loaded proxies, displaying results and updating internal state."""
        if not self._outbounds:
            raise InsufficientProxiesError("No proxies loaded to test.")

        country_filter = country if country is not None else self.country_filter
        show_progress = self.console and (verbose is None or verbose)
        progress_display: Optional[_TestProgressDisplay] = None

        if show_progress and self._entries:
            progress_display = self._create_progress_display(
                len(self._entries),
                transient=progress_transient,
            )

        with (progress_display or nullcontext()) as emitter:
            await self._perform_health_checks(
                country_filter=country_filter,
                emit_progress=emitter,
                force_refresh=force,
                timeout=timeout,
                threads=threads,
                stop_on_success=find_first,
                skip_geo=skip_geo,
            )

        self.country_filter = country_filter

        if show_progress and render_summary:
            self._render_test_summary(self._entries, country_filter)

        return self._entries

    def _render_test_summary(self, entries: List[TestResult], country_filter: Optional[str]) -> None:
        """Displays the final test report in the console."""
        if not self.console:
            return

        ok_entries = [e for e in entries if e.status == "OK"]

        self.console.print()
        self.console.rule("Functional Proxies")
        if ok_entries:
            self.console.print(self._render_test_table(ok_entries))
        else:
            msg = (
                f"No functional proxies found for filter '{country_filter}'."
                if country_filter
                else "No functional proxies found."
            )
            self.console.print(f"[warning]{msg}[/warning]")

        counts = {
            "Total": len(entries),
            "Success": sum(1 for e in entries if e.status == "OK"),
            "Failures": sum(1 for e in entries if e.status == "ERROR"),
            "Filtered": sum(1 for e in entries if e.status == "FILTERED"),
        }
        summary = "    ".join([f"[bold]{k}:[/] {v}" for k, v in counts.items()])

        self.console.print()
        self.console.rule("Test Summary")
        self.console.print(summary)

    @classmethod
    def _render_test_table(cls, entries: List[TestResult]) -> Table:
        """Generates a Rich table with the test results."""
        entries.sort(key=lambda e: e.ping or float('inf'))
        table = Table(show_header=True, header_style="table.header", expand=True)
        table.add_column("Tag", no_wrap=True, max_width=30)
        table.add_column("Destination", overflow="fold")
        table.add_column("Exit Country", no_wrap=True)
        table.add_column("Ping", justify="right", no_wrap=True)

        for entry in entries:
            destination = cls._format_destination(entry.host, entry.port)
            ping_str = f"{entry.ping:.1f} ms" if entry.ping is not None else "-"
            country = (entry.exit_geo.label if entry.exit_geo else None) or \
                      (entry.server_geo.label if entry.server_geo else None) or "-"

            table.add_row(entry.tag or "-", destination, country, ping_str)
        return table


class _TestProgressDisplay:
    """Rich-based layout used to render proxy testing progress."""

    def __init__(
        self,
        *,
        console: Console,
        total: int,
        status_styles: Dict[str, str],
        transient: bool = False,
    ) -> None:
        self.console = console
        self.total = max(total, 1)
        self.status_styles = status_styles
        self._records: Deque[Tuple[TestResult, bool]] = deque(maxlen=6)
        self._last_count = 0
        self._last_total = self.total
        self._completed = False
        self._live_is_running = False
        self._transient = transient

        self.progress = Progress(
            SpinnerColumn(style="accent.secondary"),
            TextColumn("[progress.description]{task.description}", style="progress.description"),
            BarColumn(
                bar_width=None,
                style="accent",
                complete_style="success",
                finished_style="success",
                pulse_style="accent.secondary",
            ),
            TextColumn("{task.completed}/{task.total}", style="progress.percentage"),
            TimeElapsedColumn(),
            TimeRemainingColumn(compact=True, elapsed_when_finished=True),
            console=console,
            expand=True,
        )
        self._live = Live(console=console, refresh_per_second=12, transient=transient)
        self._task_id: Optional[int] = None

    def __enter__(self) -> "_TestProgressDisplay":
        self._task_id = self.progress.add_task("Testing proxies", total=self.total)
        self._live.start()
        self._live_is_running = True
        self._live.update(self._render())
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.complete()

    def update(self, entry: TestResult, *, count: int, total: int, cached: bool) -> None:
        """Refreshes the progress bar and the detail table with the latest entry."""
        self._last_count = count
        self._last_total = max(total, 1)

        if self._task_id is not None:
            self.progress.update(
                self._task_id,
                completed=count,
                total=max(total, 1),
                description=self._format_description(entry, cached),
            )

        self._records.appendleft((entry, cached))
        self._live.update(self._render())

    def complete(self) -> None:
        """Stops the live display while keeping the last frame on screen."""
        if self._completed:
            return

        self._completed = True

        if self._task_id is not None:
            self.progress.update(
                self._task_id,
                completed=self._last_count,
                total=max(self._last_total, 1),
            )

        self._live.update(self._render())
        if self._live_is_running:
            self._live.stop()
            self._live_is_running = False
        if not self._transient:
            self.console.print()

    def _render(self) -> Group:
        """Builds the grouped renderable containing the bar and latest results."""
        table = Table(
            show_header=True,
            header_style="table.header",
            box=box.ROUNDED,
            expand=True,
            pad_edge=False,
        )
        table.add_column("Status", style="text.primary", no_wrap=True)
        table.add_column("Proxy", style="text.primary", overflow="fold")
        table.add_column("Exit IP", style="text.primary", no_wrap=True)
        table.add_column("Country", style="text.primary", no_wrap=True)
        table.add_column("Ping", style="text.primary", justify="right", no_wrap=True)

        if not self._records:
            table.add_row("-", "-", "-", "-", "-")
        else:
            for entry, cached in self._records:
                status_style = self.status_styles.get(entry.status, "info")
                status_text = f"[{status_style}]{entry.status}[/{status_style}]"
                if cached:
                    status_text += " [muted](cache)[/]"

                proxy_label = self._compose_proxy_label(entry)
                exit_ip = entry.exit_geo.ip if entry.exit_geo and entry.exit_geo.ip else entry.host
                ping_label = f"{entry.ping:.1f} ms" if entry.ping is not None else "-"
                country = (entry.exit_geo.label if entry.exit_geo else None) or \
                          (entry.server_geo.label if entry.server_geo else None) or "-"

                table.add_row(
                    status_text,
                    proxy_label,
                    exit_ip or "-",
                    country,
                    ping_label,
                )

                if entry.error:
                    table.add_row(
                        "",
                        f"[text.secondary]Reason: {self._trim(entry.error, 200)}[/]",
                        "",
                        "",
                        "",
                    )

        panel = Panel(
            table,
            title="[primary]Latest results[/]",
            border_style="border",
            padding=(0, 1),
        )
        return Group(self.progress, panel)

    def _format_description(self, entry: TestResult, cached: bool) -> str:
        """Creates the text shown alongside the progress bar."""
        status_style = self.status_styles.get(entry.status, "info")
        status_text = f"[{status_style}]{entry.status}[/{status_style}]"
        identifier = (
            entry.tag
            or (entry.protocol.upper() if entry.protocol else None)
            or entry.host
            or "-"
        )
        identifier = self._trim(identifier, 24)
        source = "[text.secondary]cache[/]" if cached else "[success]live[/]"
        return f"{status_text} - {identifier} - {source}"

    @staticmethod
    def _trim(value: Optional[str], max_length: int) -> str:
        """Shortens text to the supplied maximum length with an ellipsis."""
        if not value:
            return "-"
        if len(value) <= max_length:
            return value
        if max_length <= 3:
            return value[:max_length]
        return value[: max_length - 3] + "..."

    def _compose_proxy_label(self, entry: TestResult) -> str:
        """Formats the proxy identification for the summary rows."""
        protocol = entry.protocol.upper() if entry.protocol else None
        tag = entry.tag or protocol
        host = f"{entry.host}:{entry.port}" if entry.port else entry.host
        label = f"{tag} - {host}" if tag else host
        return self._trim(label, 48)
