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
from rich.table import Table
from rich import box

from .config import PROXYCHAINS_CONF_TEMPLATE
from .exceptions import InsufficientProxiesError, ProxyChainsError


class ChainsMixin:
    """Functionality to execute commands through proxychains."""

    def _display_proxies_table(self) -> None:
        """Exibe uma tabela organizada dos proxies ativos."""
        if not self.console or not self._bridges:
            return

        entry_map = {e.uri: e for e in self._entries}

        table = Table(
            show_header=True,
            header_style="bold cyan",
            box=box.ROUNDED,
            expand=True,
            pad_edge=False,
            show_lines=False,
        )
        table.add_column(
            "ID", style="bold yellow", no_wrap=True, justify="center", width=4
        )
        table.add_column("URL", style="cyan", no_wrap=True, width=22)
        table.add_column("Tag", style="green", width=20)
        table.add_column("Destination", style="dim", width=25)
        table.add_column("Country", style="magenta", no_wrap=True, width=15)
        table.add_column(
            "Ping", style="green", justify="right", no_wrap=True, width=10
        )

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
                    country = (
                        f"{entry.exit_geo.emoji} {entry.exit_geo.label}"
                        if hasattr(entry.exit_geo, "emoji")
                        else entry.exit_geo.label
                    )
                elif entry.server_geo:
                    country = (
                        f"{entry.server_geo.emoji} {entry.server_geo.label}"
                        if hasattr(entry.server_geo, "emoji")
                        else entry.server_geo.label
                    )
                if entry.ping is not None:
                    ping = f"{entry.ping:.0f}ms"

            # Truncate long strings
            tag = tag[:18] + ".." if len(tag) > 20 else tag
            destination = destination[:23] + ".." if len(destination) > 25 else destination

            table.add_row(
                f"{idx}",
                bridge.url,
                tag,
                destination,
                country,
                ping,
            )

        total_bridges = len(self._bridges)
        title = f"[bold cyan]Proxies Ativos[/] [yellow]({total_bridges})[/]"

        panel = Panel(
            table,
            title=title,
            border_style="cyan",
            padding=(0, 1),
        )
        self.console.print(panel)

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
            display_summary=False,
        )

        if not self._bridges:
            raise InsufficientProxiesError(
                "No proxy bridges could be started for the chain."
            )

        # Exibir tabela de proxies ativos
        self._display_proxies_table()

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

            # Use asyncio-based input handling
            import sys
            import os
            try:
                import termios
                import tty
                _UNIX = True
            except ImportError:
                _UNIX = False

            input_buffer = ""
            exit_flag = False
            last_message = ""
            message_time = 0
            input_queue = asyncio.Queue()

            tail_buffer: Deque[Tuple[str, str]] = deque(maxlen=5)

            def _handle_stdin():
                """Callback for stdin reader."""
                try:
                    data = os.read(sys.stdin.fileno(), 1024)
                    for char in data.decode(errors='ignore'):
                        input_queue.put_nowait(char)
                except (BlockingIOError, InterruptedError):
                    pass

            async def _process_input_queue():
                """Process input from queue."""
                nonlocal input_buffer, exit_flag, last_message, message_time
                
                while not exit_flag:
                    try:
                        char = await asyncio.wait_for(input_queue.get(), timeout=0.1)
                    except asyncio.TimeoutError:
                        continue

                    if char == '\x1b':  # ESC
                        exit_flag = True
                    elif char in ('\r', '\n'):  # Enter
                        command = input_buffer.strip().lower()
                        input_buffer = ""
                        
                        if command:
                            parts = command.split()
                            try:
                                if len(parts) >= 3 and parts[0] == "proxy" and parts[1] == "rotate":
                                    target = parts[2]
                                    if target == "all":
                                        tasks = [self.rotate_proxy(i) for i in range(len(self._bridges))]
                                        await asyncio.gather(*tasks)
                                        last_message = "[green]✓[/] Rotated all proxies"
                                    else:
                                        bridge_id = int(target)
                                        await self.rotate_proxy(bridge_id)
                                        last_message = f"[green]✓[/] Rotated proxy {bridge_id}"
                                    message_time = asyncio.get_running_loop().time() + 2
                                else:
                                    last_message = "[yellow]?[/] Usage: proxy rotate <id|all>"
                                    message_time = asyncio.get_running_loop().time() + 2
                            except (ValueError, IndexError) as e:
                                last_message = f"[red]✗[/] Error: {e}"
                                message_time = asyncio.get_running_loop().time() + 2
                    elif char in ('\x7f', '\b'):  # Backspace
                        input_buffer = input_buffer[:-1]
                    elif char == '\x03':  # Ctrl+C
                        exit_flag = True
                    elif char.isprintable():
                        input_buffer += char

            def render_output() -> str:
                """Renders the last output messages in a compact format."""
                if not tail_buffer:
                    return "[dim italic]Aguardando saída do processo...[/]"
                
                lines = []
                for stream_label, text in tail_buffer:
                    if stream_label == "STDOUT":
                        icon = "[green]▶[/]"
                    else:
                        icon = "[red]⚠[/]"
                    # Truncate very long lines
                    if len(text) > 100:
                        truncated = text[:100] + "..."
                    else:
                        truncated = text
                    lines.append(f"{icon} [dim]{truncated}[/]")
                return "\n".join(lines)

            def get_input_display() -> str:
                """Creates the input line."""
                current_time = asyncio.get_running_loop().time()
                
                if last_message and current_time < message_time:
                    return last_message
                
                cursor = "[bold cyan]▊[/]" if int(current_time * 2) % 2 == 0 else " "
                return f"[bold cyan]❯[/] {input_buffer}{cursor}"

            def get_header() -> str:
                """Creates a beautiful header."""
                proxy_count = len(self._bridges)
                return f"[bold cyan]╭─[/] [bold white]Proxychains[/] [bold cyan]─[/] [yellow]{proxy_count}[/] proxies [cyan]─[/] [dim]ESC para sair[/]"

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
            
            # Setup terminal for raw input
            old_settings = None
            if _UNIX:
                fd = sys.stdin.fileno()
                old_settings = termios.tcgetattr(fd)
                tty.setcbreak(fd)
                loop.add_reader(fd, _handle_stdin)
            
            from rich.console import Group
            from rich.text import Text
            
            try:
                with Live(
                    "", 
                    console=self.console, 
                    refresh_per_second=15,
                    transient=False
                ) as live:
                    input_task = asyncio.create_task(_process_input_queue())
                    stdout_task = asyncio.create_task(read_stream(process.stdout, "STDOUT"))
                    stderr_task = asyncio.create_task(read_stream(process.stderr, "STDERR"))

                    while not exit_flag and (not stdout_task.done() or not stderr_task.done()):
                        # Create beautiful compact display
                        header = Text.from_markup(get_header())
                        
                        output_panel = Panel(
                            render_output(),
                            title="[bold cyan]│[/] [bold white]Saída[/]",
                            title_align="left",
                            border_style="cyan",
                            padding=(0, 1),
                            height=7,
                        )
                        
                        input_panel = Panel(
                            get_input_display(),
                            title="[bold cyan]│[/] [bold white]Comando[/]",
                            title_align="left",
                            subtitle="[dim]proxy rotate <id|all>[/]",
                            border_style="bright_cyan",
                            padding=(0, 1),
                        )
                        
                        display = Group(header, output_panel, input_panel)
                        live.update(display)
                        await asyncio.sleep(0.066)  # ~15 FPS

                    # Cancel input task and wait for stream tasks
                    input_task.cancel()
                    
                    # Wait for stream tasks to complete
                    try:
                        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
                    except Exception:
                        pass
                    
                    # Try to cancel input task if still running
                    try:
                        await input_task
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        pass
            finally:
                # Restore terminal
                if _UNIX and old_settings:
                    loop.remove_reader(fd)
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

            return_code = await process.wait()
            return return_code

        finally:
            if self.console:
                self.console.print(
                    "\n[warning]Terminating bridges and cleaning up...[/]"
                )
            await self.stop()
            if tmpdir_path:
                shutil.rmtree(tmpdir_path, ignore_errors=True)