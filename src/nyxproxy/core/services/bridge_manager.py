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
from typing import Any, Callable, Dict, List, Optional, Tuple

import aiofiles
from rich import box
from rich.panel import Panel
from rich.table import Table

from ..config.exceptions import InsufficientProxiesError, XrayError
from ..ui.interactive import InteractiveUI
from ..models.proxy import BridgeRuntime, Outbound, TestResult
from .load_balancer import BridgeLoadBalancer


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
        self,
        outbound: Outbound,
        tag_prefix: str = "bridge",
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

                raise XrayError(
                    f"Temporary Xray port did not open in time. Error: {error_output or 'No error output.'}"
                )

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
                        raise XrayError(
                            "Could not allocate an available TCP port after multiple attempts."
                        )
                    await asyncio.sleep(0.1)
                finally:
                    sock.close()
            # This should not be reached
            raise XrayError("Could not allocate an available TCP port.")

    @staticmethod
    async def _terminate_process(
        proc: Optional[asyncio.subprocess.Process],
        *,
        wait_timeout: float = 3.0,
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
            except ProcessLookupError:  # nosec B110
                pass

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
    
    def _print_or_status(self, message: str, also_buffer: bool = True) -> None:
        """Prints message to console or adds to status buffer if interactive UI is active.
        
        Args:
            message: The message to print/buffer
            also_buffer: If True, also adds to initial buffer for later UI display
        """
        if self._interactive_ui:
            # UI is active, send directly to it
            self._interactive_ui.add_status_message(message)
        elif also_buffer and hasattr(self, '_initial_status_messages') and self.console:
            # Console mode with buffer: store for UI, DON'T print now
            import re
            clean_msg = re.sub(r'\[/?[^\]]+\]', '', message)
            self._initial_status_messages.append(clean_msg)
        elif self.console:
            # No buffer or non-interactive: print normally
            self.console.print(message)
    
    def _transfer_initial_messages_to_ui(self) -> None:
        """Transfers buffered initial messages to the interactive UI."""
        if self._interactive_ui and hasattr(self, '_initial_status_messages'):
            for msg in self._initial_status_messages:
                self._interactive_ui.add_status_message(f"[text.secondary]{msg}[/]")
            self._initial_status_messages.clear()

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
                raise InsufficientProxiesError(
                    "No proxies loaded and the cache is empty."
                )

        if not self._outbounds:
            raise InsufficientProxiesError("No valid proxies could be loaded to start.")

    async def _test_and_filter_proxies_for_start(
        self,
        threads: int,
        amounts: int,
        country_filter: Optional[str],
        find_first: Optional[int],
        skip_geo: bool,
    ) -> List[TestResult]:
        """Tests, filters, and sorts the proxies to be used for creating bridges."""
        ok_from_cache = [
            e
            for e in self._entries
            if e.status == "OK" and self.matches_country(e, country_filter)
        ]

        needed_proxies = find_first or amounts
        if len(ok_from_cache) < needed_proxies:
            self._print_or_status(
                f"[warning]Insufficient cache. Testing up to {needed_proxies} valid proxies...[/warning]"
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
        else:
            self._print_or_status(
                "[success]Sufficient proxies found in cache. Starting...[/success]"
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
            raise InsufficientProxiesError(
                f"{msg} Run the test and check the results."
            )

        if amounts > 0:
            if len(approved_entries) < amounts:
                msg = (
                    f"⚠ Only {len(approved_entries)} approved proxies "
                    f"(requested: {amounts}). Starting available ones."
                )
                self._print_or_status(msg)
            return approved_entries[:amounts]
        return approved_entries

    async def _launch_and_monitor_bridges(
        self, entries: List[TestResult]
    ) -> List[BridgeRuntime]:
        """Starts Xray processes for approved proxies and returns the runtimes."""
        bridges_runtime: List[BridgeRuntime] = []

        if entries:
            self._print_or_status(
                f"[success]Starting {len(entries)} bridges sorted by ping[/]"
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
        display_summary: bool = True,
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

        # Note: We don't print the initial summary here anymore because:
        # - For 'start' command: InteractiveUI will display it immediately
        # - For 'chains' command: _display_proxies_table() is called separately

        bridges_with_id = [
            {"id": idx, "url": bridge.url, "uri": bridge.uri, "tag": bridge.tag}
            for idx, bridge in enumerate(self._bridges)
        ]

        return bridges_with_id

    def _display_active_bridges_summary(
        self, country_filter: Optional[str], scroll_offset: int, view_height: int
    ) -> Optional[Panel]:
        """Returns a rich Panel summarizing the active bridges."""
        if not self.console:
            return None

        entry_map = {e.uri: e for e in self._entries}

        rows_table = Table(
            show_header=True,
            header_style="table.header",
            box=box.SIMPLE,
            expand=True,
            pad_edge=False,
            show_lines=False,
        )
        rows_table.add_column(
            "ID", style="table.row.id", no_wrap=True, justify="center", width=4
        )
        rows_table.add_column("PORT", style="table.row.url", no_wrap=True, justify="center", width=8)
        rows_table.add_column("Tag", style="table.row.tag", width=20)
        rows_table.add_column("Destination", style="table.row.dest", width=25)
        rows_table.add_column("Country", style="table.row.country", no_wrap=True, width=15)
        rows_table.add_column("Ping", style="table.row.ping", justify="right", no_wrap=True, width=10)

        
        visible_bridges = self._bridges[scroll_offset : scroll_offset + view_height]
        
        for idx, bridge in enumerate(visible_bridges, start=scroll_offset):
            entry = entry_map.get(bridge.uri)
            destination = "-"
            country = "-"
            ping = "-"
            tag = bridge.tag

            if entry:
                destination = self._format_destination(entry.host, entry.port)
                tag = entry.tag or tag
                if entry.exit_geo:
                    country = f"{entry.exit_geo.emoji} {entry.exit_geo.label}" if hasattr(entry.exit_geo, 'emoji') else entry.exit_geo.label
                elif entry.server_geo:
                    country = f"{entry.server_geo.emoji} {entry.server_geo.label}" if hasattr(entry.server_geo, 'emoji') else entry.server_geo.label
                if entry.ping is not None:
                    ping = f"{entry.ping:.0f}ms"

            # Truncate long strings
            tag = tag[:18] + ".." if len(tag) > 20 else tag
            destination = destination[:23] + ".." if len(destination) > 25 else destination
            
            # Extract port from bridge URL (format: http://127.0.0.1:PORT/...)
            port = bridge.url.split(':')[-1].split('/')[0] if ':' in bridge.url else "-"

            rows_table.add_row(
                f"{idx}",
                port,
                tag,
                destination,
                country,
                ping,
            )

        # Create a compact title line
        total_bridges = len(self._bridges)
        showing_range = f"{scroll_offset + 1}-{min(scroll_offset + view_height, total_bridges)}/{total_bridges}"
        title = f"[primary]━ Proxies ({showing_range})[/]"
        if country_filter:
            title += f" [text.secondary]| Filter: {country_filter}[/]"

        subtitle = f"[text.secondary]↑↓ Scroll | ESC Exit[/]"
        
        return Panel(
            rows_table,
            title=title,
            subtitle=subtitle,
            border_style="border",
            padding=(0, 1),
        )

    async def wait(self) -> None:
        """Blocks until all bridges terminate or `stop` is called."""
        if not self._running:
            raise RuntimeError("No active bridges to wait for.")
        if not self.console:
            # For non-interactive mode, just wait for the stop event
            await self._stop_event.wait()
            return

        try:
            ui = InteractiveUI(self)
            self._interactive_ui = ui  # Store reference for status messages
            
            # Transfer initial messages to UI
            self._transfer_initial_messages_to_ui()

            def get_summary_renderable(scroll_offset: int, view_height: int) -> Optional[Panel]:
                return self._display_active_bridges_summary(self.country_filter, scroll_offset, view_height)

            await ui.run(get_summary_renderable)

        except (KeyboardInterrupt, asyncio.CancelledError):
            if self.console:
                self.console.print(
                    "\n[warning]Interruption received, terminating bridges...[/warning]"
                )
        finally:
            self._interactive_ui = None  # Clear reference
            await self.stop()

    async def stop(self) -> None:
        """Terminates active Xray processes and cleans up temporary files."""
        if not self._running and not self._bridges:
            return

        self._stop_event.set()
        
        # Stop load balancer if active
        if self._load_balancer and self._load_balancer.is_active:
            try:
                await self._load_balancer.stop()
            except Exception:
                pass  # Ignore errors during cleanup

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
        self,
        port: int,
        outbound: Outbound,
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
        self,
        xray_bin: str,
        cfg: Dict[str, Any],
        name: str,
    ) -> Tuple[asyncio.subprocess.Process, Path]:
        """Initializes Xray with stdout/stderr capture for better diagnostics."""
        tmpdir = Path(tempfile.mkdtemp(prefix=f"xray_{name}_"))
        cfg_path = tmpdir / "config.json"
        async with aiofiles.open(cfg_path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(cfg, ensure_ascii=False, indent=2))

        proc = await asyncio.create_subprocess_exec(  # nosec B603
            xray_bin,
            "-config",
            str(cfg_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return proc, cfg_path

    async def rotate_proxy(self, bridge_id: int) -> bool:
        """Swaps the proxy of a running bridge with another random, functional proxy.
        
        Uses a queue to track recently used proxies and ensures rotation picks new proxies.
        If no new proxies are available:
        1. Tries to fetch from cache
        2. Tries to fetch from sources and test them
        3. Clears the used queue and restarts the cycle
        
        Uses a lock to prevent race conditions when multiple rotations run in parallel.
        """
        # Use lock to prevent race conditions during parallel rotations (e.g., "rotate all")
        async with self._rotation_lock:
            if not self._running or not (0 <= bridge_id < len(self._bridges)):
                msg = f"Invalid bridge ID: {bridge_id}. Valid IDs: 0 to {len(self._bridges) - 1}."
                self._print_or_status(f"[feedback.error]Error: {msg}[/feedback.error]")
                return False

            bridge = self._bridges[bridge_id]
            old_uri = bridge.uri
            
            # Combine currently active URIs with recently used URIs from the queue
            used_uris = {b.uri for b in self._bridges}
            used_uris.update(self._used_proxies_queue)
            
            # Also track used destinations (server:port) to avoid duplicates
            # Get destinations from active bridges
            used_destinations = set()
            entry_map = {e.uri: e for e in self._entries}
            for b in self._bridges:
                entry = entry_map.get(b.uri)
                if entry and entry.host and entry.port:
                    used_destinations.add(f"{entry.host}:{entry.port}")

            def get_candidates():
                """Helper to get candidate proxies (excluding used URIs and destinations)."""
                candidates = []
                for entry in self._entries:
                    if entry.status != "OK":
                        continue
                    if not self.matches_country(entry, self.country_filter):
                        continue
                    if entry.uri in used_uris:
                        continue
                    # Check if destination is already in use
                    destination = f"{entry.host}:{entry.port}" if entry.host and entry.port else None
                    if destination and destination in used_destinations:
                        continue
                    candidates.append(entry)
                return candidates

            candidates = get_candidates()

            # If no candidates, try multiple strategies
            if not candidates:
                self._print_or_status(
                    "[info]No new proxies available. Checking cache and sources...[/info]"
                )
                
                # Strategy 1: Try to load from cache if not already loaded
                if self.use_cache and self._cache_entries:
                    ok_cached = [
                        uri for uri, cache_entry in self._cache_entries.items()
                        if cache_entry.get('status') == 'OK'
                        and uri not in self._outbounds
                        and uri not in used_uris
                    ]
                    
                    if ok_cached:
                        self._print_or_status(
                            f"[info]Found {len(ok_cached)} unused proxies in cache. Loading...[/info]"
                        )
                        # Cache entries are already loaded in _merge_ok_cache_entries
                        # but we can check if there are entries not yet tested
                        
                # Strategy 2: Try to load more proxies from sources
                if not candidates and self._sources:
                    self._print_or_status(
                        "[info]Fetching more proxies from sources...[/info]"
                    )
                    
                    try:
                        # Load more proxies from sources
                        await self.add_sources(self._sources)
                        
                        # Test the new proxies
                        needed_proxies = len(self._bridges) + 10  # Test more than we have bridges
                        await self.test(
                            threads=10,
                            country=self.country_filter,
                            verbose=False,
                            find_first=needed_proxies,
                            force=False,
                            skip_geo=True,
                            render_summary=False,
                            progress_transient=True,
                        )
                        
                        # Check for candidates again
                        candidates = get_candidates()
                        
                        if candidates:
                            self._print_or_status(
                                f"[success]Found {len(candidates)} new proxy candidates from sources.[/success]"
                            )
                    except Exception as e:
                        self._print_or_status(
                            f"[feedback.error]Failed to load more proxies: {e}[/feedback.error]"
                        )
            
            # Strategy 3: If still no candidates, clear the queue and try again
            if not candidates:
                self._print_or_status(
                    "[warning]No new proxies available. Clearing used queue and restarting cycle...[/warning]"
                )
                
                # Clear the queue to restart the cycle
                self._used_proxies_queue.clear()
                
                # Update used_uris to only include active bridges
                used_uris = {b.uri for b in self._bridges}
                
                # Try to get candidates again
                candidates = get_candidates()
                
                if candidates:
                    self._print_or_status(
                        f"[success]Queue cleared. Found {len(candidates)} candidates.[/success]"
                    )
            
            # Final check: if still no candidates, rotation fails
            if not candidates:
                self._print_or_status(
                    f"[feedback.error]No available proxies to rotate bridge ID {bridge_id}.[/feedback.error]"
                )
                return False

            # Select a random candidate
            new_entry = random.choice(candidates)  # nosec B311
            new_outbound = self._outbounds.get(new_entry.uri)
            if not new_outbound:
                return False  # Should not happen if entries and outbounds are in sync

            # Terminate old bridge
            await self._terminate_process(bridge.process, wait_timeout=2)
            self._safe_remove_dir(bridge.workdir)

            # Launch new bridge
            try:
                xray_bin = self._which_xray()
                cfg = self._make_xray_config_http_inbound(bridge.port, new_outbound)
                new_proc, new_cfg_path = await self._launch_bridge_with_diagnostics(
                    xray_bin, cfg, new_outbound.tag
                )
                if not await self._wait_for_port(bridge.port):
                    raise XrayError(
                        f"Rotated bridge {bridge_id} port {bridge.port} did not open."
                    )
            except XrayError as e:
                self._print_or_status(
                    f"[feedback.error]Failed to restart bridge {bridge_id} on port {bridge.port}: {e}[/feedback.error]"
                )
                bridge.process = None  # Mark the bridge as inactive
                return False

            # Update the bridge
            self._bridges[bridge_id] = BridgeRuntime(
                tag=new_outbound.tag,
                port=bridge.port,
                uri=new_entry.uri,
                process=new_proc,
                workdir=new_cfg_path.parent,
            )
            
            # Add old URI to the used queue
            self._used_proxies_queue.append(old_uri)
            
            queue_size = len(self._used_proxies_queue)
            self._print_or_status(
                f"[success]✓ Rotated bridge {bridge_id} ({queue_size} proxies in history)[/success]"
            )
            return True
    
    async def adjust_bridge_amount(self, target_amount: int) -> str:
        """Adjusts the number of active bridges to the target amount.
        
        Args:
            target_amount: Desired number of bridges
            
        Returns:
            Status message with the result
        """
        if target_amount < 1:
            return "✗ Amount must be at least 1"
        
        current_amount = len(self._bridges)
        
        if target_amount == current_amount:
            return f"Already running {current_amount} bridges"
        
        if target_amount < current_amount:
            # Reduce bridges - terminate excess ones
            bridges_to_remove = current_amount - target_amount
            self._print_or_status(
                f"[info]Reducing from {current_amount} to {target_amount} bridges...[/info]"
            )
            
            # Terminate bridges from the end
            for i in range(bridges_to_remove):
                bridge_id = current_amount - 1 - i
                if bridge_id < len(self._bridges):
                    bridge = self._bridges[bridge_id]
                    if bridge.process:
                        await self._terminate_process(bridge.process, wait_timeout=2)
                    if bridge.workdir:
                        self._safe_remove_dir(bridge.workdir)
            
            # Remove from the list
            self._bridges = self._bridges[:target_amount]
            return f"✓ Reduced to {target_amount} bridges"
        
        else:
            # Increase bridges - need more proxies
            bridges_to_add = target_amount - current_amount
            self._print_or_status(
                f"[info]Increasing from {current_amount} to {target_amount} bridges...[/info]"
            )
            
            # Get approved entries not currently in use
            used_uris = {b.uri for b in self._bridges}
            available_entries = [
                e for e in self._entries
                if e.status == "OK" and e.uri not in used_uris
            ]
            
            # If not enough, try to get more from sources
            if len(available_entries) < bridges_to_add:
                if self._sources:
                    self._print_or_status(
                        "[info]Not enough proxies. Fetching from sources...[/info]"
                    )
                    try:
                        await self.add_sources(self._sources)
                        await self.test_proxies(
                            threads=50,
                            skip_geo=True,
                            stop_on_success=target_amount
                        )
                        # Refresh available entries
                        available_entries = [
                            e for e in self._entries
                            if e.status == "OK" and e.uri not in used_uris
                        ]
                    except Exception as e:
                        return f"✗ Error fetching proxies: {e}"
            
            # Sort by ping
            available_entries.sort(key=lambda e: e.ping if e.ping else float('inf'))
            
            # Take what we need
            entries_to_start = available_entries[:bridges_to_add]
            
            if not entries_to_start:
                return f"✗ No additional proxies available. Keeping {current_amount} bridges."
            
            # Launch new bridges
            new_bridges = await self._launch_and_monitor_bridges(entries_to_start)
            
            if new_bridges:
                self._bridges.extend(new_bridges)
                actual_amount = len(self._bridges)
                if actual_amount == target_amount:
                    return f"✓ Increased to {actual_amount} bridges"
                else:
                    return f"⚠ Increased to {actual_amount} bridges (requested {target_amount}, limited by available proxies)"
            else:
                return f"✗ Failed to start additional bridges. Keeping {current_amount} bridges."
    
    async def start_load_balancer(self, port: int, strategy: str = 'random') -> str:
        """Start the load balancer on specified port.
        
        Args:
            port: Port to listen on
            strategy: Selection strategy ('random', 'round-robin', 'least-conn')
            
        Returns:
            Status message
        """
        if self._load_balancer and self._load_balancer.is_active:
            return f"Load balancer already running on port {self._load_balancer.port}"
        
        if not self._bridges:
            return "✗ No bridges available. Start bridges first."
        
        # Check if port is available
        try:
            # Try to bind to the port to check availability
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('127.0.0.1', port))
            sock.close()
        except OSError:
            return f"✗ Port {port} is already in use"
        
        try:
            from .load_balancer import BridgeLoadBalancer
            self._load_balancer = BridgeLoadBalancer(
                bridges=self._bridges,
                port=port,
                strategy=strategy
            )
            await self._load_balancer.start()
            self._load_balancer_port = port
            self._load_balancer_strategy = strategy
            
            self._print_or_status(
                f"[success]✓ Load balancer started on port {port} with {len(self._bridges)} bridges[/success]"
            )
            return f"✓ Load balancer started on port {port} ({strategy} strategy)"
        
        except Exception as e:
            return f"✗ Failed to start load balancer: {e}"
    
    async def stop_load_balancer(self) -> str:
        """Stop the load balancer.
        
        Returns:
            Status message
        """
        if not self._load_balancer or not self._load_balancer.is_active:
            return "Load balancer is not running"
        
        try:
            await self._load_balancer.stop()
            port = self._load_balancer_port
            self._load_balancer = None
            self._load_balancer_port = None
            
            self._print_or_status(
                "[info]Load balancer stopped[/info]"
            )
            return f"✓ Load balancer stopped (was on port {port})"
        
        except Exception as e:
            return f"✗ Failed to stop load balancer: {e}"
    
    def get_load_balancer_stats(self) -> Optional[Dict[str, Any]]:
        """Get load balancer statistics.
        
        Returns:
            Dictionary with statistics or None if not active
        """
        if not self._load_balancer or not self._load_balancer.is_active:
            return None
        
        return {
            'port': self._load_balancer.port,
            'strategy': self._load_balancer.strategy,
            'active': self._load_balancer.is_active,
            'total_connections': self._load_balancer.total_connections,
            'active_connections': self._load_balancer.active_connections,
            'bridge_stats': self._load_balancer.get_bridge_stats()
        }