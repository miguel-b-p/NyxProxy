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

from ..config.settings import PROXYCHAINS_CONF_TEMPLATE
from ..config.exceptions import InsufficientProxiesError, ProxyChainsError


class ChainsMixin:
    """Functionality to execute commands through proxychains."""

    def _display_proxies_table(self) -> None:
        """Exibe uma tabela organizada dos proxies ativos."""
        if not self.console or not self._bridges:
            return

        entry_map = {e.uri: e for e in self._entries}

        table = Table(
            show_header=True,
            header_style="table.header",
            box=box.ROUNDED,
            expand=True,
            pad_edge=False,
            show_lines=False,
        )
        table.add_column(
            "ID", style="table.row.id", no_wrap=True, justify="center", width=4
        )
        table.add_column("URL", style="table.row.url", no_wrap=True, width=22)
        table.add_column("Tag", style="table.row.tag", width=20)
        table.add_column("Destination", style="table.row.dest", width=25)
        table.add_column("Country", style="table.row.country", no_wrap=True, width=15)
        table.add_column(
            "Ping", style="table.row.ping", justify="right", no_wrap=True, width=10
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
        title = f"[primary]Proxies Ativos[/] [highlight]({total_bridges})[/]"

        panel = Panel(
            table,
            title=title,
            border_style="border",
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
        skip_geo: bool = True,
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
            skip_geo=skip_geo,
        )

        if not self._bridges:
            raise InsufficientProxiesError(
                "No proxy bridges could be started for the chain."
            )

        # Don't display table here - it will be shown in the interactive interface
        # self._display_proxies_table()

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

            # Add execution message to initial status buffer
            if hasattr(self, '_initial_status_messages'):
                cmd_display = ' '.join(cmd_list[:3])  # Show first 3 args
                if len(cmd_list) > 3:
                    cmd_display += '...'
                self._initial_status_messages.append(f"Executing: {cmd_display}")

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
            scroll_offset = 0  # For scrolling through proxies list

            tail_buffer: Deque[Tuple[str, str]] = deque(maxlen=5)
            status_buffer: Deque[str] = deque(maxlen=5)  # Buffer for status messages
            
            # Create a simple object to hold status messages for _print_or_status
            class StatusHolder:
                def __init__(self):
                    self.messages = status_buffer
                def add_status_message(self, msg):
                    self.messages.append(msg)
            
            self._interactive_ui = StatusHolder()  # Set reference for status messages
            
            # Transfer initial messages to status buffer
            if hasattr(self, '_initial_status_messages'):
                for msg in self._initial_status_messages:
                    status_buffer.append(f"[text.secondary]{msg}[/]")
                self._initial_status_messages.clear()

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
                nonlocal input_buffer, exit_flag, last_message, message_time, scroll_offset
                
                escape_sequence = ""
                
                while not exit_flag:
                    try:
                        char = await asyncio.wait_for(input_queue.get(), timeout=0.1)
                    except asyncio.TimeoutError:
                        escape_sequence = ""  # Reset escape sequence on timeout
                        continue
                    
                    # Handle escape sequences (arrow keys)
                    if escape_sequence:
                        escape_sequence += char
                        if escape_sequence == "[A":  # Up arrow
                            scroll_offset = max(0, scroll_offset - 1)
                            escape_sequence = ""
                        elif escape_sequence == "[B":  # Down arrow
                            scroll_offset += 1
                            escape_sequence = ""
                        elif len(escape_sequence) >= 2:  # Unknown sequence, reset
                            escape_sequence = ""
                        continue

                    if char == '\x1b':  # ESC - start of escape sequence or exit
                        # Wait a moment to see if it's an escape sequence
                        try:
                            next_char = await asyncio.wait_for(input_queue.get(), timeout=0.05)
                            if next_char == '[':  # Start of arrow key sequence
                                escape_sequence = '['
                            else:
                                # Not an escape sequence, treat as ESC key
                                exit_flag = True
                                if next_char:  # Put back the character
                                    input_queue.put_nowait(next_char)
                        except asyncio.TimeoutError:
                            # Just ESC key press
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
                        icon = "[feedback.success]▶[/]"
                    else:
                        icon = "[feedback.error]⚠[/]"
                    # Truncate very long lines
                    if len(text) > 100:
                        truncated = text[:100] + "..."
                    else:
                        truncated = text
                    lines.append(f"{icon} [text.secondary]{truncated}[/]")
                return "\n".join(lines)

            def get_input_display() -> str:
                """Creates the input line."""
                current_time = asyncio.get_running_loop().time()
                
                if last_message and current_time < message_time:
                    return last_message
                
                cursor = "[input.cursor]▊[/]" if int(current_time * 2) % 2 == 0 else " "
                return f"[input.prompt]❯[/] {input_buffer}{cursor}"

            def get_header() -> str:
                """Creates a beautiful header."""
                proxy_count = len(self._bridges)
                return f"[primary]╭─[/] [text.primary]Proxychains[/] [primary]─[/] [highlight]{proxy_count}[/] proxies [primary]─[/] [text.secondary]ESC para sair[/]"
            
            def get_status_panel():
                """Creates the panel for status messages."""
                if not status_buffer:
                    return Panel(
                        "[text.secondary]Ready[/]",
                        title="[primary]│[/] [text.primary]Status[/]",
                        title_align="left",
                        border_style="border.bright",
                        padding=(0, 1),
                        height=7
                    )
                
                messages_text = "\n".join(list(status_buffer))
                return Panel(
                    messages_text,
                    title="[primary]│[/] [text.primary]Status[/]",
                    title_align="left",
                    border_style="border.bright",
                    padding=(0, 1),
                    height=7
                )

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
                        
                        # Calculate scroll limits
                        view_height = 3
                        total_proxies = len(self._bridges)
                        max_scroll = max(0, total_proxies - view_height)
                        scroll_offset = min(scroll_offset, max_scroll)
                        
                        # Add proxies table (compact version for chains)
                        proxies_panel = self._display_active_bridges_summary(self.country_filter, scroll_offset, view_height)
                        
                        output_panel = Panel(
                            render_output(),
                            title="[primary]│[/] [text.primary]Saída[/]",
                            title_align="left",
                            border_style="border",
                            padding=(0, 1),
                            height=7,
                        )
                        
                        status_panel = get_status_panel()
                        
                        input_panel = Panel(
                            get_input_display(),
                            title="[primary]│[/] [text.primary]Command[/]",
                            title_align="left",
                            subtitle="[text.secondary]proxy rotate <id|all>[/]",
                            border_style="border.bright",
                            padding=(0, 1),
                        )
                        
                        display = Group(header, proxies_panel, output_panel, status_panel, input_panel)
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
            self._interactive_ui = None  # Clear reference
            if self.console:
                self.console.print(
                    "\n[warning]Terminating bridges and cleaning up...[/]"
                )
            await self.stop()
            if tmpdir_path:
                shutil.rmtree(tmpdir_path, ignore_errors=True)