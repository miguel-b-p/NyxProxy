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
        self.status_messages = deque(maxlen=5)  # Keep last 5 status messages
    
    def add_status_message(self, message: str):
        """Adds a status message to the buffer."""
        self.status_messages.append(message)

    def _get_status_panel(self):
        """Creates the panel for status messages."""
        if not self.status_messages:
            return Panel(
                "[text.secondary]Ready[/]",
                title="[primary]│[/] [text.primary]Status[/]",
                title_align="left",
                border_style="border.bright",
                padding=(0, 1),
                height=7
            )
        
        # Show last messages, most recent at bottom
        messages_text = "\n".join(list(self.status_messages))
        return Panel(
            messages_text,
            title="[primary]│[/] [text.primary]Status[/]",
            title_align="left",
            border_style="border.bright",
            padding=(0, 1),
            height=7
        )
    
    def _get_input_panel(self) -> str:
        """Creates the panel for user input."""
        current_time = asyncio.get_running_loop().time()
        
        if self.last_message and current_time < self.message_display_time:
            return self.last_message
        
        cursor = "[input.cursor]▊[/]" if int(current_time * 2) % 2 == 0 else " "
        
        if not self.input_buffer:
            # Show placeholder when input is empty
            return f"[input.prompt]❯[/] [text.secondary]Write help[/] {cursor}"
        
        return f"[input.prompt]❯[/] {self.input_buffer}{cursor}"

    async def _process_command(self):
        """Processes the command entered by the user."""
        command = self.input_buffer.strip().lower()
        self.input_buffer = ""

        if not command:
            return

        parts = command.split()
        try:
            if parts[0] == "help":
                # Show available commands
                help_text = (
                    "[primary]Available commands:[/]\n"
                    "  [accent]proxy rotate <id|all>[/] - Rotate a specific proxy or all proxies\n"
                    "  [accent]proxy amount <number>[/] - Adjust the number of active proxies\n"
                    "  [accent]bridge on <port>[/]      - Start load balancer on specified port\n"
                    "  [accent]bridge off[/]            - Stop the load balancer\n"
                    "  [accent]bridge stats[/]          - Show load balancer statistics\n"
                    "  [accent]source add <url>[/]      - Add a new proxy source\n"
                    "  [accent]source rem <id>[/]       - Remove a source by ID\n"
                    "  [accent]source list[/]           - List all configured sources\n"
                    "  [accent]help[/]                  - Show this help message\n"
                    "  [accent]ESC[/]                   - Exit the interface"
                )
                self.last_message = help_text
                self.message_display_time = asyncio.get_running_loop().time() + 8
            elif len(parts) >= 2 and parts[0] == "source":
                if parts[1] == "list":
                    self.last_message = self.manager.list_sources()
                    self.message_display_time = asyncio.get_running_loop().time() + 5
                elif parts[1] == "add" and len(parts) >= 3:
                    source_url = " ".join(parts[2:])  # Join in case URL has spaces
                    self.last_message = f"[feedback.success]{self.manager.add_source(source_url)}[/]"
                    self.message_display_time = asyncio.get_running_loop().time() + 3
                elif parts[1] == "rem" and len(parts) >= 3:
                    try:
                        source_id = int(parts[2])
                        result = self.manager.remove_source(source_id)
                        if "✓" in result:
                            self.last_message = f"[feedback.success]{result}[/]"
                        else:
                            self.last_message = f"[feedback.error]{result}[/]"
                        self.message_display_time = asyncio.get_running_loop().time() + 3
                    except ValueError:
                        self.last_message = "[feedback.error]✗ Invalid source ID[/]"
                        self.message_display_time = asyncio.get_running_loop().time() + 2
                else:
                    self.last_message = "[warning]? Usage: source [list|add <url>|rem <id>][/]"
                    self.message_display_time = asyncio.get_running_loop().time() + 2
            elif len(parts) >= 2 and parts[0] == "proxy":
                if parts[1] == "rotate" and len(parts) >= 3:
                    target = parts[2]
                    if target == "all":
                        tasks = [self.manager.rotate_proxy(i) for i in range(len(self.manager._bridges))]
                        await asyncio.gather(*tasks)
                        self.last_message = "[feedback.success]✓[/] Rotated all proxies"
                    else:
                        bridge_id = int(target)
                        await self.manager.rotate_proxy(bridge_id)
                        self.last_message = f"[feedback.success]✓[/] Rotated proxy {bridge_id}"
                    self.message_display_time = asyncio.get_running_loop().time() + 2
                elif parts[1] == "amount" and len(parts) >= 3:
                    try:
                        target_amount = int(parts[2])
                        result = await self.manager.adjust_bridge_amount(target_amount)
                        if "✓" in result:
                            self.last_message = f"[feedback.success]{result}[/]"
                        elif "⚠" in result:
                            self.last_message = f"[warning]{result}[/]"
                        else:
                            self.last_message = f"[feedback.error]{result}[/]"
                        self.message_display_time = asyncio.get_running_loop().time() + 3
                    except ValueError:
                        self.last_message = "[feedback.error]✗ Invalid amount (must be a number)[/]"
                        self.message_display_time = asyncio.get_running_loop().time() + 2
                else:
                    self.last_message = "[warning]? Usage: proxy [rotate <id|all>|amount <number>][/]"
                    self.message_display_time = asyncio.get_running_loop().time() + 2
            elif len(parts) >= 2 and parts[0] == "bridge":
                if parts[1] == "on" and len(parts) >= 3:
                    try:
                        port = int(parts[2])
                        result = await self.manager.start_load_balancer(port)
                        if "✓" in result:
                            self.last_message = f"[feedback.success]{result}[/]"
                        else:
                            self.last_message = f"[feedback.error]{result}[/]"
                        self.message_display_time = asyncio.get_running_loop().time() + 3
                    except ValueError:
                        self.last_message = "[feedback.error]✗ Invalid port (must be a number)[/]"
                        self.message_display_time = asyncio.get_running_loop().time() + 2
                elif parts[1] == "off":
                    result = await self.manager.stop_load_balancer()
                    if "✓" in result:
                        self.last_message = f"[feedback.success]{result}[/]"
                    else:
                        self.last_message = f"[warning]{result}[/]"
                    self.message_display_time = asyncio.get_running_loop().time() + 3
                elif parts[1] == "stats":
                    stats = self.manager.get_load_balancer_stats()
                    if stats:
                        stats_text = (
                            f"[primary]Load Balancer Stats:[/]\n"
                            f"  Port: {stats['port']}\n"
                            f"  Strategy: {stats['strategy']}\n"
                            f"  Total connections: {stats['total_connections']}\n"
                            f"  Active connections: {stats['active_connections']}"
                        )
                        self.last_message = stats_text
                    else:
                        self.last_message = "[warning]Load balancer is not running[/]"
                    self.message_display_time = asyncio.get_running_loop().time() + 5
                else:
                    self.last_message = "[warning]? Usage: bridge [on <port>|off|stats][/]"
                    self.message_display_time = asyncio.get_running_loop().time() + 2
            else:
                self.last_message = "[warning]?[/] Unknown command. Type 'help' for available commands."
                self.message_display_time = asyncio.get_running_loop().time() + 2
        except (ValueError, IndexError) as e:
            self.last_message = f"[feedback.error]✗[/] Error: {e}"
            self.message_display_time = asyncio.get_running_loop().time() + 2
        except Exception as e:
            self.last_message = f"[feedback.error]✗[/] Error: {e}"
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
                    status_panel = self._get_status_panel()
                    
                    input_panel = Panel(
                        self._get_input_panel(),
                        title="[primary]│[/] [text.primary]Command[/]",
                        title_align="left",
                        border_style="border.bright",
                        padding=(0, 1)
                    )
                    
                    display = Group(main_content, status_panel, input_panel)
                    live.update(display)
                    await asyncio.sleep(0.066)  # ~15 FPS
        finally:
            # Stop the input processing task
            input_task.cancel()
            
            # Clean up terminal state
            if not _WINDOWS:
                loop.remove_reader(fd)
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)