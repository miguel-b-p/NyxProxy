from __future__ import annotations

"""Routines responsible for initiating, monitoring, and terminating HTTP bridges."""

import asyncio
import json
import random
import shutil
import socket
import subprocess  # nosec B404
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiofiles
from rich import box
from rich.panel import Panel
from rich.table import Table

from .exceptions import InsufficientProxiesError, XrayError
from .models import BridgeRuntime, Outbound, TestResult


class BridgeMixin:
    """Functionality related to the lifecycle of Xray bridges."""

    @staticmethod
    def _decode_bytes(data: Optional[bytes]) -> str:
        """Decodes bytes to a string, ignoring errors."""
        if not data:
            return ""
        return data.decode(errors="ignore")

    async def _wait_for_port(self, port: int, timeout: float = 2.0) -> bool:
        """Polls until the local port is open."""
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                return True
            except OSError:
                await asyncio.sleep(0.05)
        return False

    async def _launch_single_bridge_with_retry(
        self, outbound: Outbound, tag_prefix: str = "bridge"
    ) -> Tuple[int, asyncio.subprocess.Process, Path]:
        """Launches a single Xray bridge with retry logic."""
        max_retries = 5
        last_error: Optional[Exception] = None

        for attempt in range(max_retries):
            port: Optional[int] = None
            proc: Optional[asyncio.subprocess.Process] = None
            cfg_dir: Optional[Path] = None
            try:
                port = await self._find_available_port()
                cfg = self._make_xray_config_http_inbound(port, outbound)
                xray_bin = self._which_xray()

                proc, cfg_path = await self._launch_bridge_with_diagnostics(
                    xray_bin, cfg, f"{tag_prefix}_{outbound.tag}"
                )
                cfg_dir = cfg_path.parent

                if await self._wait_for_port(port, timeout=2.0):
                    return port, proc, cfg_dir

                # Capture stderr for better error reporting
                error_output = ""
                if proc.stderr:
                    error_output = self._decode_bytes(await proc.stderr.read()).strip()
                
                raise XrayError(f"Temporary Xray port did not open in time. Error: {error_output or 'No error output.'}")

            except Exception as e:
                last_error = e
                await self._terminate_process(proc, wait_timeout=2)
                self._safe_remove_dir(cfg_dir)
                if port is not None:
                    await self._release_port(port)
                
                if attempt + 1 >= max_retries:
                    raise XrayError(
                        f"Failed to create a bridge for '{outbound.tag}' after {max_retries} attempts. "
                        f"Last error: {last_error}"
                    ) from last_error
                
                await asyncio.sleep(0.1)
        
        # This part should not be reachable
        raise XrayError("Failed to create a bridge due to an unknown error.")

    @asynccontextmanager
    async def _temporary_bridge(
        self,
        outbound: Outbound,
        *,
        tag_prefix: str = "temp",
    ):
        """Creates a temporary Xray bridge, ensuring resource cleanup."""
        port, proc, cfg_dir = await self._launch_single_bridge_with_retry(
            outbound, tag_prefix
        )

        try:
            yield port, proc
        finally:
            await self._terminate_process(proc, wait_timeout=2)
            self._safe_remove_dir(cfg_dir)
            if port is not None:
                await self._release_port(port)

    async def _find_available_port(self) -> int:
        """Finds an available TCP port by asking the OS to allocate one."""
        async with self._port_allocation_lock:
            max_retries = 10
            for attempt in range(max_retries):
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
                    if port not in self._allocated_ports:
                        self._allocated_ports.add(port)
                        return port
                except OSError:
                    if attempt + 1 >= max_retries:
                        raise XrayError("Could not allocate an available TCP port after multiple attempts.")
                    await asyncio.sleep(0.1)
                finally:
                    sock.close()
            # This should not be reached
            raise XrayError("Could not allocate an available TCP port.")

    @staticmethod
    async def _terminate_process(
        proc: Optional[asyncio.subprocess.Process], *, wait_timeout: float = 3.0
    ) -> None:
        """Terminates a process silently, ignoring errors."""
        if not proc:
            return
        try:
            if proc.returncode is None:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=wait_timeout)
        except (ProcessLookupError, asyncio.TimeoutError):
            try:
                if proc.returncode is None:
                    proc.kill()
            except ProcessLookupError:
                pass  # nosec B110

    @staticmethod
    def _safe_remove_dir(path: Optional[Path]) -> None:
        """Removes temporary directories without propagating exceptions."""
        if path and path.is_dir():
            shutil.rmtree(path, ignore_errors=True)

    async def _release_port(self, port: Optional[int]) -> None:
        """Releases a port registered as in use."""
        if port is not None:
            async with self._port_allocation_lock:
                self._allocated_ports.discard(port)

    def _prepare_proxies_for_start(self):
        """Validates and loads proxies to be started, using cache if necessary."""
        if self._running:
            raise RuntimeError("Bridges are already running. Call stop() first.")

        if not self._outbounds:
            if self.use_cache and self._cache_entries:
                if self.console:
                    self.console.print(
                        Panel.fit(
                            "[warning]No sources provided. Using proxies from cache...[/warning]",
                            border_style="warning",
                            padding=(0, 1),
                        )
                    )
                self._load_outbounds_from_cache()
            else:
                raise InsufficientProxiesError("No proxies loaded and the cache is empty.")

        if not self._outbounds:
            raise InsufficientProxiesError("No valid proxies could be loaded to start.")

    async def _test_and_filter_proxies_for_start(
        self, threads: int, amounts: int, country_filter: Optional[str], find_first: Optional[int], skip_geo: bool
    ) -> List[TestResult]:
        """Tests, filters, and sorts the proxies to be used for creating bridges."""
        ok_from_cache = [
            e
            for e in self._entries
            if e.status == "OK" and self.matches_country(e, country_filter)
        ]

        needed_proxies = find_first or amounts
        if len(ok_from_cache) < needed_proxies:
            if self.console:
                self.console.print(
                    Panel.fit(
                        f"[warning]Insufficient cache. Testing up to {needed_proxies} valid proxies...[/warning]",
                        border_style="warning",
                        padding=(0, 1),
                    )
                )
            await self.test(
                threads=threads,
                country=country_filter,
                verbose=True,
                find_first=needed_proxies,
                force=False,
                skip_geo=skip_geo,
                render_summary=False,
                progress_transient=True,
            )
        elif self.console:
            self.console.print(
                Panel.fit(
                    "[success]Sufficient proxies found in cache. Starting...[/success]",
                    border_style="success",
                    padding=(0, 1),
                )
            )

        approved_entries = [
            entry
            for entry in self._entries
            if entry.status == "OK" and self.matches_country(entry, country_filter)
        ]

        approved_entries.sort(key=lambda e: e.ping or float("inf"))

        if not approved_entries:
            msg = (
                f"No approved proxies for country '{country_filter}'."
                if country_filter
                else "No approved proxies to start."
            )
            raise InsufficientProxiesError(f"{msg} Run the test and check the results.")

        if amounts > 0:
            if len(approved_entries) < amounts:
                if self.console:
                    self.console.print(
                        Panel.fit(
                            f"[warning]Only {len(approved_entries)} approved proxies "
                            f"(requested: {amounts}). Starting available ones.[/warning]",
                            border_style="warning",
                            padding=(0, 1),
                        )
                    )
            return approved_entries[:amounts]
        return approved_entries

    async def _launch_and_monitor_bridges(self, entries: List[TestResult]) -> List[BridgeRuntime]:
        """Starts Xray processes for approved proxies and returns the runtimes."""
        bridges_runtime: List[BridgeRuntime] = []

        if self.console and entries:
            self.console.print(
                Panel.fit(
                    f"[success]Starting {len(entries)} bridges sorted by ping[/]",
                    border_style="success",
                    padding=(0, 1),
                )
            )

        try:
            for entry in entries:
                outbound = self._outbounds.get(entry.uri)
                if not outbound:
                    continue

                port, proc, cfg_dir = await self._launch_single_bridge_with_retry(
                    outbound, "bridge"
                )

                bridge = BridgeRuntime(
                    tag=outbound.tag,
                    port=port,
                    uri=entry.uri,
                    process=proc,
                    workdir=cfg_dir,
                )
                bridges_runtime.append(bridge)
        except Exception:
            for bridge in bridges_runtime:
                await self._terminate_process(bridge.process)
                self._safe_remove_dir(bridge.workdir)
                await self._release_port(bridge.port)
            raise
        return bridges_runtime

    async def start(
        self,
        *,
        threads: int = 1,
        amounts: int = 1,
        country: Optional[str] = None,
        auto_test: bool = True,
        find_first: Optional[int] = None,
        skip_geo: bool = False,
    ) -> List[Dict[str, Any]]:
        """Creates local HTTP bridges for approved proxies, testing if necessary."""
        self._prepare_proxies_for_start()
        country_filter = country if country is not None else self.country_filter

        approved_entries = (
            await self._test_and_filter_proxies_for_start(
                threads, amounts, country_filter, find_first, skip_geo
            )
            if auto_test
            else [e for e in self._entries if e.status == "OK"]
        )

        bridges_runtime = await self._launch_and_monitor_bridges(approved_entries)
        self._bridges = bridges_runtime
        self._running = True

        if self.console:
            self._display_active_bridges_summary(country_filter)

        bridges_with_id = [
            {"id": idx, "url": bridge.url, "uri": bridge.uri, "tag": bridge.tag}
            for idx, bridge in enumerate(self._bridges)
        ]

        return bridges_with_id

    def _display_active_bridges_summary(self, country_filter: Optional[str]) -> None:
        """Displays the table of active bridges in the console."""
        if not self.console:
            return

        entry_map = {e.uri: e for e in self._entries}

        rows_table = Table(
            show_header=True,
            header_style="accent",
            box=box.ROUNDED,
            expand=True,
            pad_edge=False,
        )
        rows_table.add_column("ID", style="accent.secondary", no_wrap=True, justify="center")
        rows_table.add_column("Local URL", style="accent", no_wrap=True)
        rows_table.add_column("Tag", style="info")
        rows_table.add_column("Destination", style="muted")
        rows_table.add_column("Country", style="info", no_wrap=True)
        rows_table.add_column("Ping", style="success", justify="right", no_wrap=True)

        for idx, bridge in enumerate(self._bridges):
            entry = entry_map.get(bridge.uri)
            destination = "-"
            country = "-"
            ping = "-"
            tag = bridge.tag

            if entry:
                destination = self._format_destination(entry.host, entry.port)
                tag = entry.tag or tag
                if entry.exit_geo:
                    country = entry.exit_geo.label
                elif entry.server_geo:
                    country = entry.server_geo.label
                if entry.ping is not None:
                    ping = f"{entry.ping:.1f} ms"

            rows_table.add_row(
                f"{idx}",
                bridge.url,
                tag,
                destination,
                country,
                ping,
            )

        title = "[accent]Active HTTP Bridges[/]"
        if country_filter:
            title += f" [muted](Country: {country_filter})[/]"

        self.console.print(
            Panel(
                rows_table,
                title=title,
                subtitle="[muted]Sorted by ping - Press Ctrl+C to terminate all bridges[/]",
                border_style="accent",
                padding=(0, 1),
            )
        )

    async def wait(self) -> None:
        """Blocks until all bridges terminate or `stop` is called."""
        if not self._running:
            raise RuntimeError("No active bridges to wait for.")
        try:
            while not self._stop_event.is_set():
                alive = any(
                    bridge.process and bridge.process.returncode is None
                    for bridge in self._bridges
                )
                if not alive:
                    if self.console:
                        self.console.print(
                            "\n[warning]All xray processes have terminated.[/warning]"
                        )
                    break
                await asyncio.sleep(0.5)
        except (KeyboardInterrupt, asyncio.CancelledError):
            if self.console:
                self.console.print(
                    "\n[warning]Interruption received, terminating bridges...[/warning]"
                )
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Terminates active Xray processes and cleans up temporary files."""
        if not self._running and not self._bridges:
            return

        self._stop_event.set()

        bridges_to_stop = list(self._bridges)
        if bridges_to_stop:
            for bridge in bridges_to_stop:
                await self._terminate_process(bridge.process)
                self._safe_remove_dir(bridge.workdir)
                await self._release_port(bridge.port)

        self._bridges = []
        self._running = False

    def get_http_proxy(self) -> List[Dict[str, Any]]:
        """Returns ID, local URL, and URI of each running bridge."""
        if not self._running:
            return []
        return [
            {"id": idx, "url": bridge.url, "uri": bridge.uri, "tag": bridge.tag}
            for idx, bridge in enumerate(self._bridges)
        ]

    def _make_xray_config_http_inbound(
        self, port: int, outbound: Outbound
    ) -> Dict[str, Any]:
        """Assembles the Xray configuration file for a local HTTP bridge."""
        cfg = {
            "log": {"loglevel": "warning"},
            "inbounds": [
                {
                    "tag": "http-in",
                    "listen": "127.0.0.1",
                    "port": port,
                    "protocol": "http",
                    "settings": {},
                }
            ],
            "outbounds": [
                outbound.config,
                {"tag": "direct", "protocol": "freedom", "settings": {}},
                {"tag": "block", "protocol": "blackhole", "settings": {}},
            ],
            "routing": {
                "domainStrategy": "AsIs",
                "rules": [
                    {
                        "type": "field",
                        "outboundTag": outbound.tag,
                        "network": "tcp,udp",
                    }
                ],
            },
        }
        if "tag" not in cfg["outbounds"][0]:
            cfg["outbounds"][0]["tag"] = outbound.tag
        return cfg

    async def _launch_bridge_with_diagnostics(
        self, xray_bin: str, cfg: Dict[str, Any], name: str
    ) -> Tuple[asyncio.subprocess.Process, Path]:
        """Initializes Xray with stdout/stderr capture for better diagnostics."""
        tmpdir = Path(tempfile.mkdtemp(prefix=f"xray_{name}_"))
        cfg_path = tmpdir / "config.json"
        async with aiofiles.open(
            cfg_path, "w", encoding="utf-8"
        ) as f:
            await f.write(json.dumps(cfg, ensure_ascii=False, indent=2))

        proc = await asyncio.create_subprocess_exec(  # nosec B603
            xray_bin, "-config", str(cfg_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return proc, cfg_path

    async def rotate_proxy(self, bridge_id: int) -> bool:
        """Swaps the proxy of a running bridge with another random, functional proxy."""
        if not self._running or not (0 <= bridge_id < len(self._bridges)):
            if self.console:
                msg = f"Invalid bridge ID: {bridge_id}. Valid IDs: 0 to {len(self._bridges) - 1}."
                self.console.print(f"[danger]Error: {msg}[/danger]")
            return False

        bridge = self._bridges[bridge_id]
        used_uris = {b.uri for b in self._bridges}

        candidates = [
            entry
            for entry in self._entries
            if entry.status == "OK"
            and self.matches_country(entry, self.country_filter)
            and entry.uri not in used_uris
        ]

        if not candidates:
            if self.console:
                self.console.print(
                    f"[warning]No other available proxies to rotate bridge ID {bridge_id}.[/warning]"
                )
            return False

        new_entry = random.choice(candidates)  # nosec B311
        new_outbound = self._outbounds.get(new_entry.uri)
        if not new_outbound:
            return False  # Should not happen if entries and outbounds are in sync

        await self._terminate_process(bridge.process, wait_timeout=2)
        self._safe_remove_dir(bridge.workdir)

        try:
            xray_bin = self._which_xray()
            cfg = self._make_xray_config_http_inbound(bridge.port, new_outbound)
            new_proc, new_cfg_path = await self._launch_bridge_with_diagnostics(
                xray_bin, cfg, new_outbound.tag
            )
            if not await self._wait_for_port(bridge.port):
                raise XrayError(f"Rotated bridge {bridge_id} port {bridge.port} did not open.")
        except XrayError as e:
            if self.console:
                self.console.print(
                    f"[danger]Failed to restart bridge {bridge_id} on port {bridge.port}: {e}[/danger]"
                )
            bridge.process = None  # Mark the bridge as inactive
            return False

        self._bridges[bridge_id] = BridgeRuntime(
            tag=new_outbound.tag,
            port=bridge.port,
            uri=new_entry.uri,
            process=new_proc,
            workdir=new_cfg_path.parent,
        )

        if self.console:
            self.console.print(
                Panel.fit(
                    f"[success]Bridge [bold]ID {bridge_id}[/] (port {bridge.port}) "
                    f"rotated to proxy '[bold]{new_outbound.tag}[/]'[/success]",
                    border_style="success",
                    padding=(0, 1),
                )
            )
            self._display_active_bridges_summary(self.country_filter)

        return True
