from __future__ import annotations

"""Routines for integration with the proxychains utility."""

import shutil
import subprocess  # nosec B404
import tempfile
import asyncio
from collections import deque
from pathlib import Path
from typing import Deque, List, Tuple

import aiofiles
from rich.live import Live
from rich.panel import Panel
from rich.layout import Layout
from pynput import keyboard

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

    async def run_with_chains(
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

        await self.start(
            threads=threads,
            amounts=amounts,
            country=country,
            find_first=amounts,
        )

        if not self._bridges:
            raise InsufficientProxiesError(
                "No proxy bridges could be started for the chain."
            )

        tmpdir_path: Path | None = None
        try:
            proxychains_bin = self._which_proxychains()

            proxy_lines = [f"http 127.0.0.1 {bridge.port}" for bridge in self._bridges]
            config_content = PROXYCHAINS_CONF_TEMPLATE.format(
                proxy_list="\n".join(proxy_lines)
            ).strip()

            tmpdir_path = Path(tempfile.mkdtemp(prefix="nyxproxy_chains_"))
            config_path = tmpdir_path / "proxychains.conf"
            async with aiofiles.open(config_path, "w", encoding="utf-8") as f:
                await f.write(config_content)

            full_command = [proxychains_bin, "-f", str(config_path), *cmd_list]

            if self.console:
                self.console.print("\n[accent]Executing command via proxychains[/]")
                cmd_str = " ".join(f"'{arg}'" if " " in arg else arg for arg in full_command)
                self.console.print(f"[muted]$ {cmd_str}[/muted]\n")

            if not self.console:
                process = await asyncio.create_subprocess_exec(*full_command)
                await process.wait()
                return process.returncode

            input_buffer = ""
            exit_flag = False

            async def _handle_key_press(key):
                nonlocal input_buffer, exit_flag
                try:
                    if key.char:
                        input_buffer += key.char
                except AttributeError:
                    if key == keyboard.Key.space:
                        input_buffer += " "
                    elif key == keyboard.Key.backspace:
                        input_buffer = input_buffer[:-1]
                    elif key == keyboard.Key.enter:
                        await _process_command()
                        input_buffer = ""
                    elif key == keyboard.Key.esc:
                        exit_flag = True
                        return

            async def _process_command():
                nonlocal input_buffer
                command = input_buffer.strip().lower()
                if not command:
                    return

                parts = command.split()
                if parts[0] == "proxy" and parts[1] == "rotate":
                    if len(parts) == 3:
                        target = parts[2]
                        if target == "all":
                            for i in range(len(self._bridges)):
                                await self.rotate_proxy(i)
                        else:
                            try:
                                bridge_id = int(target)
                                await self.rotate_proxy(bridge_id)
                            except ValueError:
                                self.console.print(f"[danger]Invalid bridge ID: {target}[/danger]")
                    else:
                        self.console.print("[danger]Usage: proxy rotate <id|all>[/danger]")

            def _keyboard_listener(loop):
                def on_press(key):
                    asyncio.run_coroutine_threadsafe(_handle_key_press(key), loop)

                with keyboard.Listener(on_press=on_press) as listener:
                    listener.join()

            tail_buffer: Deque[Tuple[str, str]] = deque(maxlen=12)

            def render_tail() -> Panel:
                if not tail_buffer:
                    body = "[muted]Waiting for proxychains output...[/]"
                else:
                    formatted_lines = []
                    for stream_label, text in tail_buffer:
                        style = "success" if stream_label == "STDOUT" else "danger"
                        formatted_lines.append(
                            f"[{style}]{stream_label.lower():>6}[/] {text}"
                        )
                    body = "\n".join(formatted_lines)
                return Panel(
                    body,
                    title="[accent]Last proxychains messages[/]",
                    border_style="accent",
                    padding=(0, 1),
                )

            def get_input_panel():
                return Panel(f"proxy> {input_buffer}", style="muted", border_style="accent")

            layout = Layout()
            process = await asyncio.create_subprocess_exec(
                *full_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            async def read_stream(stream, label):
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    tail_buffer.append((label, line.decode().rstrip()))

            loop = asyncio.get_running_loop()
            with Live(layout, console=self.console, screen=True, redirect_stderr=False, auto_refresh=False) as live:
                listener_task = loop.run_in_executor(None, _keyboard_listener, loop)
                stdout_task = asyncio.create_task(read_stream(process.stdout, "STDOUT"))
                stderr_task = asyncio.create_task(read_stream(process.stderr, "STDERR"))

                while not stdout_task.done() or not stderr_task.done():
                    layout.split(render_tail(), Layout(get_input_panel(), size=3))
                    live.update(layout)
                    live.refresh()
                    await asyncio.sleep(0.1)

                await asyncio.gather(stdout_task, stderr_task, listener_task)

            await process.wait()
            return process.returncode

        finally:
            if self.console:
                self.console.print(
                    "\n[warning]Terminating bridges and cleaning up...[/]"
                )
            await self.stop()
            if tmpdir_path:
                shutil.rmtree(tmpdir_path, ignore_errors=True)