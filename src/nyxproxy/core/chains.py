from __future__ import annotations

"""Routines for integration with the proxychains utility."""

import shutil
import subprocess  # nosec B404
import tempfile
import threading
from collections import deque
from pathlib import Path
from typing import Deque, List, Tuple

from rich.live import Live
from rich.panel import Panel

from .config import PROXYCHAINS_CONF_TEMPLATE
from .exceptions import InsufficientProxiesError, ProxyChainsError


class ChainsMixin:
    """Functionality to execute commands through proxychains."""

    def _which_proxychains(self) -> str:
        """Locates the proxychains4 or proxychains binary."""
        for candidate in ("proxychains4", "proxychains"):
            if found := self._shutil_which(candidate):
                return found
        raise ProxyChainsError(
            "Command 'proxychains4' or 'proxychains' not found. "
            "Ensure it is installed and in your PATH."
        )

    def run_with_chains(
        self,
        cmd_list: List[str],
        *,
        threads: int = 1,
        amounts: int = 1,
        country: str | None = None,
    ) -> int:
        """
        Starts bridges, creates a proxychains config, and executes a command.

        Returns the exit code of the executed command.
        """
        if not cmd_list:
            raise ValueError("The command to be executed cannot be empty.")

        self.start(
            threads=threads,
            amounts=amounts,
            country=country,
            wait=False,
            find_first=amounts,
        )

        if not self._bridges:
            raise InsufficientProxiesError("No proxy bridges could be started for the chain.")

        tmpdir_path: Path | None = None
        try:
            proxychains_bin = self._which_proxychains()

            proxy_lines = [f"http 127.0.0.1 {bridge.port}" for bridge in self._bridges]
            config_content = PROXYCHAINS_CONF_TEMPLATE.format(
                proxy_list="\n".join(proxy_lines)
            ).strip()

            tmpdir_path = Path(tempfile.mkdtemp(prefix="nyxproxy_chains_"))
            config_path = tmpdir_path / "proxychains.conf"
            config_path.write_text(config_content, encoding="utf-8")

            full_command = [proxychains_bin, "-f", str(config_path), *cmd_list]

            if self.console:
                self.console.print("\n[accent]Executing command via proxychains[/]")
                cmd_str = " ".join(f"'{arg}'" if " " in arg else arg for arg in full_command)
                self.console.print(f"[muted]$ {cmd_str}[/muted]\n")

            if not self.console:
                result = subprocess.run(full_command, check=False)  # nosec B603
                return result.returncode

            tail_buffer: Deque[Tuple[str, str]] = deque(maxlen=12)
            buffer_lock = threading.Lock()

            def render_tail() -> Panel:
                if not tail_buffer:
                    body = "[muted]Waiting for proxychains output...[/]"
                else:
                    formatted_lines = []
                    for stream_label, text in tail_buffer:
                        style = "success" if stream_label == "STDOUT" else "danger"
                        formatted_lines.append(f"[{style}]{stream_label.lower():>6}[/] {text}")
                    body = "\n".join(formatted_lines)
                return Panel(
                    body,
                    title="[accent]Last proxychains messages[/]",
                    border_style="accent",
                    padding=(0, 1),
                )

            process = subprocess.Popen(  # nosec B603
                full_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

            with Live(render_tail(), console=self.console, refresh_per_second=8, transient=False) as live:

                def reader(stream, label: str) -> None:
                    if not stream:
                        return
                    for raw_line in iter(stream.readline, ""):
                        line = raw_line.rstrip("\n")
                        with buffer_lock:
                            tail_buffer.append((label, line))
                        live.update(render_tail())
                    stream.close()

                threads_list = [
                    threading.Thread(target=reader, args=(process.stdout, "STDOUT"), daemon=True),
                    threading.Thread(target=reader, args=(process.stderr, "STDERR"), daemon=True),
                ]
                for thread in threads_list:
                    thread.start()

                process.wait()

                for thread in threads_list:
                    thread.join()

                live.update(render_tail())

            return process.returncode

        finally:
            if self.console:
                self.console.print("\n[warning]Terminating bridges and cleaning up...[/]")
            self.stop()
            if tmpdir_path:
                shutil.rmtree(tmpdir_path, ignore_errors=True)
