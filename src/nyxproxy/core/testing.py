from __future__ import annotations

"""Implementations of proxy tests and status reports."""

import ipaddress
import re
import socket
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from typing import Any, Deque, Dict, List, Optional, Tuple

import niquests as requests
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

from .exceptions import InsufficientProxiesError
from .models import GeoInfo, Outbound, TestResult


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

    def _lookup_geo_info(self, ip: Optional[str]) -> Optional[GeoInfo]:
        """Queries geolocation information for an IP, using an in-memory cache."""
        if not ip or not self._is_public_ip(ip):
            return None

        if ip in self._ip_lookup_cache:
            return self._ip_lookup_cache[ip]

        if not self.requests or not self._findip_token:
            self._ip_lookup_cache[ip] = None
            return None

        result: Optional[GeoInfo] = None
        try:
            resp = self.requests.get(
                f"https://api.findip.net/{ip}/?token={self._findip_token}", timeout=5
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

        except (requests.exceptions.RequestException, ValueError, KeyError):
            result = None

        self._ip_lookup_cache[ip] = result
        return result

    def _pre_load_server_geo(self) -> None:
        """Pre-resolves hosts and loads geo for unique server IPs in parallel."""
        if not self._findip_token:
            return

        host_to_results: Dict[str, List[TestResult]] = {}
        unique_hosts = set()
        for result in self._entries:
            if result.server_geo is None:
                unique_hosts.add(result.host)
                host_to_results.setdefault(result.host, []).append(result)

        if not unique_hosts:
            return

        def resolve_and_lookup(host: str) -> tuple[str, Optional[str], Optional[GeoInfo]]:
            try:
                ip_info = socket.getaddrinfo(host, None, socket.AF_INET)
                ip = ip_info[0][4][0] if ip_info else None
                if ip:
                    geo = self._lookup_geo_info(ip)
                    return host, ip, geo
                return host, None, None
            except socket.gaierror:
                return host, None, None

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(resolve_and_lookup, host) for host in unique_hosts]
            for future in as_completed(futures):
                host, ip, geo = future.result()
                if geo:
                    for res in host_to_results.get(host, []):
                        res.server_geo = geo

    def _post_load_exit_geo(self) -> None:
        """Loads geo for unique exit IPs in parallel after testing."""
        if not self._findip_token:
            return

        ip_to_results: Dict[str, List[TestResult]] = {}
        unique_ips = set()
        for result in self._entries:
            if result.status == "OK" and result.exit_geo is None and result.exit_geo:
                # Wait, exit_geo is set from exit_ip
                # Note: This assumes _test_proxy_functionality sets exit_ip in GeoInfo.ip, but actually in _test_outbound, it's set if exit_ip
                # Since exit_geo = self._lookup_geo_info(exit_ip)
                # To batch, collect unique exit_ips from functional tests
                # But since it's after, and exit_ip is in geo.ip if set
                if result.exit_geo and result.exit_geo.ip:
                    continue  # Already set
                # Assume in _test_outbound, set exit_geo = GeoInfo(ip=exit_ip) first, then lookup
                # But to batch, change to collect exit_ips, then lookup
                # Let's modify _test_outbound to set temp exit_ip, then batch lookup here

                # For now, assume it's set in thread, but to batch, change
                # To optimize, move lookup from _test_outbound to here

                # Modify _test_outbound to set result.exit_geo = GeoInfo(ip=exit_ip) if functional, without lookup
                # Then here, collect unique ips where geo.ip and no country_code, lookup, set

                if result.exit_geo and result.exit_geo.ip and not result.exit_geo.country_code:
                    unique_ips.add(result.exit_geo.ip)
                    ip_to_results.setdefault(result.exit_geo.ip, []).append(result)

        if not unique_ips:
            return

        def lookup_ip(ip: str) -> tuple[str, Optional[GeoInfo]]:
            geo = self._lookup_geo_info(ip)
            return ip, geo

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(lookup_ip, ip) for ip in unique_ips]
            for future in as_completed(futures):
                ip, geo = future.result()
                if geo:
                    for res in ip_to_results.get(ip, []):
                        res.exit_geo = geo

    def _test_outbound(self, result: TestResult, timeout: float) -> None:
        """Executes measurements for an outbound, updating the result object."""
        try:
            # Server geo is pre-loaded

            # Perform functional test
            func_result = self._test_proxy_functionality(self._outbounds[result.uri], timeout=timeout)
            if func_result.get("functional"):
                result.status = "OK"
                result.ping = func_result.get("response_time")
                exit_ip = func_result.get("external_ip")
                if exit_ip:
                    result.exit_geo = GeoInfo(ip=exit_ip)  # Lookup later in batch
            else:
                result.status = "ERROR"
                result.error = func_result.get("error", "Proxy not functional")

        except Exception as exc:
            result.status = "ERROR"
            result.error = f"Test preparation error: {exc}"
        finally:
            result.tested_at_ts = time.time()

    def _test_proxy_functionality(
        self, outbound: Outbound, timeout: float = 5.0,
    ) -> Dict[str, Any]:
        """Tests the actual functionality of the proxy by creating a temporary bridge."""
        if not self.requests:
            return {"functional": False, "error": "Requests module not available"}

        try:
            with self._temporary_bridge(outbound, tag_prefix="test") as (port, _):
                proxy_url = f"http://127.0.0.1:{port}"
                proxies = {"http": proxy_url, "https": proxy_url}

                start_time = time.perf_counter()
                response = self.requests.get(
                    self.test_url,
                    proxies=proxies,
                    timeout=timeout,
                    verify=False,
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

    def _format_request_error(self, exc: Exception, timeout: float) -> str:
        """Normalizes error messages from HTTP requests via proxy."""
        if isinstance(exc, requests.exceptions.Timeout):
            return f"Timeout after {timeout:.1f}s"
        if isinstance(exc, requests.exceptions.ProxyError):
            return f"Proxy error: {str(exc.__cause__)[:100]}"
        if isinstance(exc, requests.exceptions.ConnectionError):
            return f"Connection error: {str(exc.__cause__)[:100]}"
        if isinstance(exc, requests.exceptions.HTTPError):
            return f"HTTP error {exc.response.status_code}: {exc.response.reason}"
        return f"{type(exc).__name__}: {str(exc)[:100]}"

    @staticmethod
    def _extract_external_ip(response: requests.Response) -> Optional[str]:
        """Extracts external IP from the test service's JSON response."""
        try:
            text = response.text
            match = re.search(r"ip=([\d\.]+)", text)
            if match:
                return match.group(1).strip()
        except Exception:
            return None
        return None

    def _perform_health_checks(
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
        """Runs tests concurrently and manages the cache."""
        self._ip_lookup_cache.clear()
        to_test: List[TestResult] = []
        success_count = 0
        tested_count = 0
        total_proxies = len(self._entries)

        if not skip_geo:
            self._pre_load_server_geo()

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
                self._save_cache()
            return

        if to_test:
            with ThreadPoolExecutor(max_workers=threads) as executor:
                futures = {executor.submit(self._test_outbound, res, timeout): res for res in to_test}
                for future in as_completed(futures):
                    future.result()  # Propagate exceptions
                    result_entry = futures[future]
                    tested_count += 1

                    if country_filter and result_entry.status == "OK":
                        if not self.matches_country(result_entry, country_filter):
                            result_entry.status = "FILTERED"
                            result_entry.error = f"Does not match filter '{country_filter}'"

                    if emit_progress:
                        self._emit_test_progress(result_entry, tested_count, total_proxies, emit_progress)

                    if result_entry.status == "OK":
                        success_count += 1
                        if stop_on_success and success_count >= stop_on_success:
                            for f in futures:
                                f.cancel()
                            break

        if not skip_geo:
            self._post_load_exit_geo()

        if self.use_cache:
            self._save_cache()

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

    def test(
        self,
        *,
        threads: int = 1,
        country: Optional[str] = None,
        verbose: Optional[bool] = None,
        timeout: float = 5.0,
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
            self._perform_health_checks(
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
        table = Table(show_header=True, header_style="bold cyan", expand=True)
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
            header_style="accent",
            box=box.ROUNDED,
            expand=True,
            pad_edge=False,
        )
        table.add_column("Status", style="info", no_wrap=True)
        table.add_column("Proxy", style="info", overflow="fold")
        table.add_column("Exit IP", style="info", no_wrap=True)
        table.add_column("Country", style="info", no_wrap=True)
        table.add_column("Ping", style="info", justify="right", no_wrap=True)

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
                        f"[muted]Reason: {self._trim(entry.error, 70)}[/]",
                        "",
                        "",
                        "",
                    )

        panel = Panel(
            table,
            title="[accent]Latest results[/]",
            border_style="accent",
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
        source = "[muted]cache[/]" if cached else "[success]live[/]"
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
