import asyncio
import os
import sys
from collections import deque

from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel

# Cross-platform terminal raw mode handling
try:
    import msvcrt
    _WINDOWS = True
except ImportError:
    import termios
    import tty
    _WINDOWS = False

class InteractiveUI:
    """Manages an interactive UI using Rich.Live and asyncio's event loop for input."""
    def __init__(self, manager):
        self.manager = manager
        self.console = manager.console
        self.input_buffer = ""
        self.exit_flag = False
        self.scroll_offset = 0
        self.last_message = ""
        self.message_display_time = 0
        self.input_queue = asyncio.Queue()

    def _get_input_panel(self):
        """Creates the panel for user input."""
        current_time = asyncio.get_running_loop().time()
        
        if self.last_message and current_time < self.message_display_time:
            return self.last_message
        
        cursor = "[bold cyan]▊[/]" if int(current_time * 2) % 2 == 0 else " "
        return f"[bold cyan]❯[/] {self.input_buffer}{cursor}"

    async def _process_command(self):
        """Processes the command entered by the user."""
        command = self.input_buffer.strip().lower()
        self.input_buffer = ""

        if not command:
            return

        parts = command.split()
        try:
            if len(parts) >= 3 and parts[0] == "proxy" and parts[1] == "rotate":
                target = parts[2]
                if target == "all":
                    tasks = [self.manager.rotate_proxy(i) for i in range(len(self.manager._bridges))]
                    await asyncio.gather(*tasks)
                    self.last_message = "[green]✓[/] Rotated all proxies"
                else:
                    bridge_id = int(target)
                    await self.manager.rotate_proxy(bridge_id)
                    self.last_message = f"[green]✓[/] Rotated proxy {bridge_id}"
                self.message_display_time = asyncio.get_running_loop().time() + 2
            else:
                self.last_message = "[yellow]?[/] Usage: proxy rotate <id|all>"
                self.message_display_time = asyncio.get_running_loop().time() + 2
        except (ValueError, IndexError) as e:
            self.last_message = f"[red]✗[/] Error: {e}"
            self.message_display_time = asyncio.get_running_loop().time() + 2
        except Exception as e:
            self.last_message = f"[red]✗[/] Error: {e}"
            self.message_display_time = asyncio.get_running_loop().time() + 3
            
    def _handle_stdin(self):
        """Callback for asyncio's reader, reads from stdin and puts to queue."""
        # Read up to 1024 bytes to get whole escape sequences at once.
        # This is non-blocking because add_reader only calls it when data is ready.
        try:
            data = os.read(sys.stdin.fileno(), 1024)
            for char in data.decode(errors='ignore'):
                self.input_queue.put_nowait(char)
        except (BlockingIOError, InterruptedError):
            pass  # Should not happen with add_reader, but good practice.

    async def _process_input_queue(self):
        """Processes characters and sequences from the input queue."""
        char_buffer = deque()

        while not self.exit_flag:
            if not char_buffer:
                # Wait for the first character
                char = await self.input_queue.get()
            else:
                # Use what's left in the buffer
                char = char_buffer.popleft()

            # Handle escape sequences
            if char == '\x1b':
                sequence = char
                # Greedily read subsequent chars if they arrive quickly
                try:
                    while True:
                        sequence += await asyncio.wait_for(self.input_queue.get(), timeout=0.01)
                except asyncio.TimeoutError:
                    pass # End of sequence
                
                if sequence == '\x1b': # Lone ESC
                    self.exit_flag = True
                elif sequence == '\x1b[A': # Up Arrow
                    self.scroll_offset = max(0, self.scroll_offset - 1)
                elif sequence == '\x1b[B': # Down Arrow
                    self.scroll_offset += 1
                # Add other sequences here if needed (e.g., \xe0H for Windows)

            # Handle Windows arrow keys (2-byte sequences)
            elif _WINDOWS and char == '\xe0':
                try:
                    next_char = await asyncio.wait_for(self.input_queue.get(), timeout=0.01)
                    if next_char == 'H': # Up
                        self.scroll_offset = max(0, self.scroll_offset - 1)
                    elif next_char == 'P': # Down
                        self.scroll_offset += 1
                except asyncio.TimeoutError:
                    pass

            # Handle regular characters
            elif char in ('\r', '\n'):
                await self._process_command()
            elif char in ('\x7f', '\b'): # Backspace
                self.input_buffer = self.input_buffer[:-1]
            elif char == '\x03': # Ctrl+C
                self.exit_flag = True
            elif char.isprintable():
                self.input_buffer += char


    async def run(self, main_renderable_callable):
        """Starts the interactive UI loop with a compact, fixed-height interface."""
        loop = asyncio.get_running_loop()
        
        # Setup terminal for raw input
        if _WINDOWS:
            # No special setup needed for Windows msvcrt
            pass
        else:
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            tty.setcbreak(fd)
            loop.add_reader(fd, self._handle_stdin)

        input_task = asyncio.create_task(self._process_input_queue())

        try:
            from rich.console import Group
            from rich.text import Text
            
            with Live(
                "",
                console=self.console,
                transient=False,
                refresh_per_second=15
            ) as live:
                while not self.exit_flag:
                    # Fixed height for proxy list
                    view_height = 10
                    main_content = main_renderable_callable(self.scroll_offset, view_height)
                    
                    # Calculate scroll limits
                    if hasattr(main_content, 'renderable'):
                        total_rows = len(self.manager._bridges)
                        max_scroll = max(0, total_rows - view_height)
                        self.scroll_offset = min(self.scroll_offset, max_scroll)

                    # Create beautiful compact display with fixed height
                    input_panel = Panel(
                        self._get_input_panel(),
                        title="[bold cyan]│[/] [bold white]Comando[/]",
                        title_align="left",
                        subtitle="[dim]proxy rotate <id|all>[/]",
                        border_style="bright_cyan",
                        padding=(0, 1)
                    )
                    
                    display = Group(main_content, input_panel)
                    live.update(display)
                    await asyncio.sleep(0.066)  # ~15 FPS
        finally:
            # Stop the input processing task
            input_task.cancel()
            
            # Clean up terminal state
            if not _WINDOWS:
                loop.remove_reader(fd)
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)