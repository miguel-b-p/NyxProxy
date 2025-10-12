from __future__ import annotations

"""Routines responsible for initiating, monitoring, and terminating HTTP bridges."""

import atexit
import json
import random
import shutil
import socket
import subprocess  # nosec B404
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .exceptions import InsufficientProxiesError, XrayError
from .models import BridgeRuntime, Outbound, TestResult


class BridgeMixin:
    """Functionality related to the lifecycle of Xray bridges."""

    def _wait_for_port(self, port: int, timeout: float = 2.0) -> bool:
        """Polls until the local port is open."""
        start_time = time.time()
        while time.time() - start_time < timeout:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    return True
            time.sleep(0.05)
        return False

    @contextmanager
    def _temporary_bridge(
        self,
        outbound: Outbound,
        *,
        tag_prefix: str = "temp",
    ):
        """Creates a temporary Xray bridge, ensuring resource cleanup."""
        port: Optional[int] = None
        proc: Optional[subprocess.Popen] = None
        cfg_dir: Optional[Path] = None

        try:
            port = self._find_available_port()
            cfg = self._make_xray_config_http_inbound(port, outbound)
            xray_bin = self._which_xray()

            proc, cfg_path = self._launch_bridge_with_diagnostics(
                xray_bin, cfg, f"{tag_prefix}_{outbound.tag}"
            )
            cfg_dir = cfg_path.parent

            if not self._wait_for_port(port):
                error_output = ""
                if proc.stderr:
                    error_output = self._decode_bytes(proc.stderr.read()).strip()
                raise XrayError(
                    "Temporary Xray port did not open in time. "
                    f"Error: {error_output or 'No error output.'}"
                )

            yield port, proc
        finally:
            self._terminate_process(proc, wait_timeout=2)
            self._safe_remove_dir(cfg_dir)
            if port is not None:
                self._release_port(port)

    def _find_available_port(self) -> int:
        """Finds an available TCP port by asking the OS to allocate one."""
        with self._port_allocation_lock:
            while True:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
                    if port not in self._allocated_ports:
                        self._allocated_ports.add(port)
                        return port
                except OSError as e:
                    raise XrayError("Could not allocate an available TCP port.") from e
                finally:
                    sock.close()

    @staticmethod
    def _terminate_process(
        proc: Optional[subprocess.Popen], *, wait_timeout: float = 3.0
    ) -> None:
        """Terminates a process silently, ignoring errors."""
        if not proc:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=wait_timeout)
        except Exception:
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass  # nosec B110

    @staticmethod
    def _safe_remove_dir(path: Optional[Path]) -> None:
        """Removes temporary directories without propagating exceptions."""
        if path and path.is_dir():
            shutil.rmtree(path, ignore_errors=True)

    def _release_port(self, port: Optional[int]) -> None:
        """Releases a port registered as in use."""
        if port is not None:
            with self._port_allocation_lock:
                self._allocated_ports.discard(port)

    def _prepare_proxies_for_start(self):
        """Validates and loads proxies to be started, using cache if necessary."""
        if self._running:
            raise RuntimeError("Bridges are already running. Call stop() first.")

        if not self._outbounds:
            if self.use_cache and self._cache_entries:
                if self.console:
                    self.console.print(
                        "[yellow]No sources provided. Using proxies from cache...[/yellow]"
                    )
                self._load_outbounds_from_cache()
            else:
                raise InsufficientProxiesError("No proxies loaded and the cache is empty.")

        if not self._outbounds:
            raise InsufficientProxiesError("No valid proxies could be loaded to start.")

    def _test_and_filter_proxies_for_start(
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
                    f"[yellow]Insufficient cache. Testing up to {needed_proxies} valid proxies...[/yellow]"
                )
            self.test(
                threads=threads,
                country=country_filter,
                verbose=False,
                find_first=needed_proxies,
                force=False,
                skip_geo=skip_geo,
            )
        elif self.console:
            self.console.print("[green]Sufficient proxies found in cache. Starting...[/green]")

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
                        f"[yellow]Warning: Only {len(approved_entries)} approved proxies "
                        f"(requested: {amounts}). Starting available ones.[/yellow]"
                    )
            return approved_entries[:amounts]
        return approved_entries

    def _launch_and_monitor_bridges(self, entries: List[TestResult]) -> List[BridgeRuntime]:
        """Starts Xray processes for approved proxies and returns the runtimes."""
        xray_bin = self._which_xray()
        bridges_runtime: List[BridgeRuntime] = []

        if self.console and entries:
            self.console.print(f"\n[green]Starting {len(entries)} bridges sorted by ping[/]")

        try:
            for entry in entries:
                outbound = self._outbounds.get(entry.uri)
                if not outbound:
                    continue

                port = self._find_available_port()
                cfg = self._make_xray_config_http_inbound(port, outbound)
                proc, cfg_path = self._launch_bridge_with_diagnostics(
                    xray_bin, cfg, outbound.tag
                )

                if not self._wait_for_port(port):
                    error_output = self._decode_bytes(proc.stderr.read()).strip() if proc.stderr else ""
                    raise XrayError(
                        f"Xray port {port} for '{outbound.tag}' did not open in time. "
                        f"Error: {error_output or 'No error output.'}"
                    )

                bridge = BridgeRuntime(
                    tag=outbound.tag,
                    port=port,
                    uri=entry.uri,
                    process=proc,
                    workdir=cfg_path.parent,
                )
                bridges_runtime.append(bridge)
        except Exception:
            for bridge in bridges_runtime:
                self._terminate_process(bridge.process)
                self._safe_remove_dir(bridge.workdir)
                self._release_port(bridge.port)
            raise
        return bridges_runtime

    def start(
        self,
        *,
        threads: int = 1,
        amounts: int = 1,
        country: Optional[str] = None,
        auto_test: bool = True,
        wait: bool = False,
        find_first: Optional[int] = None,
        skip_geo: bool = False,
    ) -> List[Dict[str, Any]]:
        """Creates local HTTP bridges for approved proxies, testing if necessary."""
        self._prepare_proxies_for_start()
        country_filter = country if country is not None else self.country_filter

        approved_entries = (
            self._test_and_filter_proxies_for_start(
                threads, amounts, country_filter, find_first, skip_geo
            )
            if auto_test
            else [e for e in self._entries if e.status == "OK"]
        )

        bridges_runtime = self._launch_and_monitor_bridges(approved_entries)
        self._bridges = bridges_runtime
        self._running = True

        if not self._atexit_registered:
            atexit.register(self.stop)
            self._atexit_registered = True

        if self.console:
            self._display_active_bridges_summary(country_filter)

        bridges_with_id = [
            {"id": idx, "url": bridge.url, "uri": bridge.uri, "tag": bridge.tag}
            for idx, bridge in enumerate(self._bridges)
        ]

        if wait:
            self.wait()
        else:
            self._start_wait_thread()

        return bridges_with_id

    def _display_active_bridges_summary(self, country_filter: Optional[str]) -> None:
        """Displays the table of active bridges in the console."""
        if not self.console:
            return

        entry_map = {e.uri: e for e in self._entries}

        self.console.print()
        title = "Active HTTP Bridges"
        if country_filter:
            title += f" - Country: {country_filter}"
        self.console.rule(f"{title} - Sorted by Ping")

        for idx, bridge in enumerate(self._bridges):
            entry = entry_map.get(bridge.uri)
            ping = entry.ping if entry else None
            ping_str = f"{ping:6.1f}ms" if isinstance(ping, (int, float)) else "      -   "
            self.console.print(
                f"[bold cyan]ID {idx:<2}[/] http://127.0.0.1:{bridge.port}  ->  [{ping_str}] ('{bridge.tag}')"
            )

        self.console.print()
        self.console.print("Press Ctrl+C to terminate all bridges.")

    def _start_wait_thread(self) -> None:
        """Starts a background thread to monitor running processes."""
        if self._wait_thread and self._wait_thread.is_alive():
            return
        self._stop_event.clear()
        thread = threading.Thread(
            target=self._wait_loop_wrapper, name="ProxyWaitThread", daemon=True
        )
        self._wait_thread = thread
        thread.start()

    def _wait_loop_wrapper(self) -> None:
        """Executes `wait` capturing exceptions for a clean thread exit."""
        try:
            self.wait()
        except RuntimeError:
            pass

    def wait(self) -> None:
        """Blocks until all bridges terminate or `stop` is called."""
        if not self._running:
            raise RuntimeError("No active bridges to wait for.")
        try:
            while not self._stop_event.is_set():
                alive = any(
                    bridge.process and bridge.process.poll() is None for bridge in self._bridges
                )
                if not alive:
                    if self.console:
                        self.console.print("\n[yellow]All xray processes have terminated.[/yellow]")
                    break
                time.sleep(0.5)
        except KeyboardInterrupt:
            if self.console:
                self.console.print("\n[yellow]Interruption received, terminating bridges...[/yellow]")
        finally:
            self.stop()

    def stop(self) -> None:
        """Terminates active Xray processes and cleans up temporary files."""
        if not self._running and not self._bridges:
            return

        self._stop_event.set()

        bridges_to_stop = list(self._bridges)
        if bridges_to_stop:
            for bridge in bridges_to_stop:
                self._terminate_process(bridge.process)
                self._safe_remove_dir(bridge.workdir)
                self._release_port(bridge.port)

        self._bridges = []
        self._running = False

        if self._wait_thread and self._wait_thread is not threading.current_thread():
            self._wait_thread.join(timeout=2.0)
        self._wait_thread = None

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

    def _launch_bridge_with_diagnostics(
        self, xray_bin: str, cfg: Dict[str, Any], name: str
    ) -> Tuple[subprocess.Popen, Path]:
        """Initializes Xray with stdout/stderr capture for better diagnostics."""
        tmpdir = Path(tempfile.mkdtemp(prefix=f"xray_{name}_"))
        cfg_path = tmpdir / "config.json"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

        proc = subprocess.Popen(  # nosec B603
            [xray_bin, "-config", str(cfg_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return proc, cfg_path

    def rotate_proxy(self, bridge_id: int) -> bool:
        """Swaps the proxy of a running bridge with another random, functional proxy."""
        if not self._running or not (0 <= bridge_id < len(self._bridges)):
            if self.console:
                msg = f"Invalid bridge ID: {bridge_id}. Valid IDs: 0 to {len(self._bridges) - 1}."
                self.console.print(f"[red]Error: {msg}[/red]")
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
                    f"[yellow]Warning: No other available proxies to rotate bridge ID {bridge_id}.[/yellow]"
                )
            return False

        new_entry = random.choice(candidates)  # nosec B311
        new_outbound = self._outbounds.get(new_entry.uri)
        if not new_outbound:
            return False  # Should not happen if entries and outbounds are in sync

        self._terminate_process(bridge.process, wait_timeout=2)
        self._safe_remove_dir(bridge.workdir)

        try:
            xray_bin = self._which_xray()
            cfg = self._make_xray_config_http_inbound(bridge.port, new_outbound)
            new_proc, new_cfg_path = self._launch_bridge_with_diagnostics(
                xray_bin, cfg, new_outbound.tag
            )
            if not self._wait_for_port(bridge.port):
                raise XrayError(f"Rotated bridge {bridge_id} port {bridge.port} did not open.")
        except XrayError as e:
            if self.console:
                self.console.print(
                    f"[red]Failed to restart bridge {bridge_id} on port {bridge.port}: {e}[/red]"
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
                f"[green]Success:[/green] Bridge [bold]ID {bridge_id}[/] (port {bridge.port}) "
                f"rotated to proxy '[bold]{new_outbound.tag}[/]'"
            )
            self._display_active_bridges_summary(self.country_filter)

        return True