from __future__ import annotations

"""Routines for integration with the proxychains utility."""

import shutil
import subprocess  # nosec B404
import tempfile
from pathlib import Path
from typing import List

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
                self.console.print("\n[bold magenta]Executing command via ProxyChains...[/]")
                cmd_str = ' '.join(f"'{arg}'" if ' ' in arg else arg for arg in full_command)
                self.console.print(f"[dim]$ {cmd_str}[/dim]\n")

            result = subprocess.run(full_command, check=False)  # nosec B603
            return result.returncode

        finally:
            if self.console:
                self.console.print("\n[bold yellow]Terminating bridges and cleaning up...[/]")
            self.stop()
            if tmpdir_path:
                shutil.rmtree(tmpdir_path, ignore_errors=True)