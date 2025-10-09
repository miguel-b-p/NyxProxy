from __future__ import annotations

"""Implementations of proxy tests and status reports."""

import ipaddress
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

import requests
from rich.console import Console
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

    def _test_outbound(self, result: TestResult, timeout: float) -> None:
        """Executes measurements for an outbound, updating the result object."""
        try:
            # Resolve server IP and get geo-info
            try:
                ip_info = socket.getaddrinfo(result.host, None, socket.AF_INET)
                server_ip = ip_info[0][4][0] if ip_info else None
                if server_ip:
                    result.server_geo = self._lookup_geo_info(server_ip)
            except socket.gaierror:
                pass  # DNS resolution failure

            # Perform functional test
            func_result = self._test_proxy_functionality(self._outbounds[result.uri], timeout=timeout)
            if func_result.get("functional"):
                result.status = "OK"
                result.ping = func_result.get("response_time")
                if exit_ip := func_result.get("external_ip"):
                    result.exit_geo = self._lookup_geo_info(exit_ip)
            else:
                result.status = "ERRO"
                result.error = func_result.get("error", "Proxy not functional")

        except Exception as exc:
            result.status = "ERRO"
            result.error = f"Test preparation error: {exc}"
        finally:
            result.tested_at_ts = time.time()

    def _test_proxy_functionality(
        self, outbound: Outbound, timeout: float = 10.0,
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
        timeout: float = 10.0,
        threads: int = 1,
        stop_on_success: Optional[int] = None,
    ) -> None:
        """Runs tests concurrently and manages the cache."""
        self._ip_lookup_cache.clear()
        to_test: List[TestResult] = []
        success_count = 0
        tested_count = 0
        total_proxies = len(self._entries)

        for result in self._entries:
            use_cache = self.use_cache and not force_refresh and result.uri in self._cache_entries
            if use_cache:
                is_ok = result.status == "OK" and self.matches_country(result, country_filter)
                if is_ok:
                    success_count += 1
                    tested_count += 1
                    if emit_progress:
                        self._emit_test_progress(result, tested_count, total_proxies, emit_progress, cached=True)
                else:
                    to_test.append(result)
            else:
                to_test.append(result)

        if stop_on_success and success_count >= stop_on_success:
            return

        if to_test:
            with ThreadPoolExecutor(max_workers=threads) as executor:
                futures = {executor.submit(self._test_outbound, res, timeout): res for res in to_test}
                for future in as_completed(futures):
                    future.result() # Propagate exceptions
                    result_entry = futures[future]
                    tested_count += 1

                    if country_filter and result_entry.status == "OK":
                        if not self.matches_country(result_entry, country_filter):
                            result_entry.status = "FILTRADO"
                            result_entry.error = f"Does not match filter '{country_filter}'"

                    if emit_progress:
                        self._emit_test_progress(result_entry, tested_count, total_proxies, emit_progress)

                    if result_entry.status == "OK":
                        success_count += 1
                        if stop_on_success and success_count >= stop_on_success:
                            for f in futures: f.cancel()
                            break
        if self.use_cache:
            self._save_cache()


    def _emit_test_progress(self, entry: TestResult, count: int, total: int, progress_emitter: Any, cached: bool = False) -> None:
        """Formats and displays a line of test progress."""
        status_style = self.STATUS_STYLES.get(entry.status, "white")
        status_fmt = f"[{status_style}]{entry.status}[/{status_style}]"

        ping_fmt = f"{entry.ping:.1f} ms" if entry.ping is not None else "-"
        country = (entry.exit_geo.label if entry.exit_geo else None) or \
                  (entry.server_geo.label if entry.server_geo else None) or "-"
        cache_note = " [dim](cache)[/]" if cached else ""

        progress_emitter.print(
            f"[{count}/{total}] {status_fmt}{cache_note} [bold]{entry.tag}[/] -> "
            f"Country: {country} | Ping: {ping_fmt}"
        )

        if entry.error:
            progress_emitter.print(f"  [dim]Reason: {entry.error}[/]")

    def test(
        self,
        *,
        threads: int = 1,
        country: Optional[str] = None,
        verbose: Optional[bool] = None,
        timeout: float = 10.0,
        force: bool = False,
        find_first: Optional[int] = None,
    ) -> List[TestResult]:
        """Tests the loaded proxies, displaying results and updating internal state."""
        if not self._outbounds:
            raise InsufficientProxiesError("No proxies loaded to test.")

        country_filter = country if country is not None else self.country_filter
        show_progress = self.console and (verbose is None or verbose)

        self._perform_health_checks(
            country_filter=country_filter,
            emit_progress=self.console if show_progress else None,
            force_refresh=force,
            timeout=timeout,
            threads=threads,
            stop_on_success=find_first,
        )

        self.country_filter = country_filter

        if show_progress:
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
            msg = f"No functional proxies found for filter '{country_filter}'." if country_filter else "No functional proxies found."
            self.console.print(f"[yellow]{msg}[/yellow]")

        counts = {
            "Total": len(entries),
            "Success": sum(1 for e in entries if e.status == "OK"),
            "Failures": sum(1 for e in entries if e.status == "ERRO"),
            "Filtered": sum(1 for e in entries if e.status == "FILTRADO"),
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