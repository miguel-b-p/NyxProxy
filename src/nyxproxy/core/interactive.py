import asyncio
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel

from .async_input import AsyncInput

class InteractiveUI:
    """Manages an interactive UI using Rich.Live and a non-blocking input reader."""
    def __init__(self, manager):
        self.manager = manager
        self.console = manager.console
        self.input_buffer = ""
        self.exit_flag = False
        self.scroll_offset = 0
        self.last_message = ""
        self.message_display_time = 0

    def _get_input_panel(self):
        """Creates the panel for user input."""
        prompt = f"proxy> {self.input_buffer}"
        if self.last_message and asyncio.get_running_loop().time() < self.message_display_time:
            return Panel(self.last_message, style="bold red", border_style="danger")
        return Panel(prompt, style="muted", border_style="accent")

    async def _process_command(self):
        """Processes the command entered by the user."""
        command = self.input_buffer.strip().lower()
        self.input_buffer = ""  # Clear buffer after processing

        if not command:
            return

        parts = command.split()
        try:
            if parts[0] == "proxy" and parts[1] == "rotate":
                if len(parts) == 3:
                    target = parts[2]
                    if target == "all":
                        tasks = [self.manager.rotate_proxy(i) for i in range(len(self.manager._bridges))]
                        await asyncio.gather(*tasks)
                    else:
                        bridge_id = int(target)
                        await self.manager.rotate_proxy(bridge_id)
                else:
                    raise ValueError("Invalid command format")
            else:
                 raise ValueError("Unknown command")
        except (ValueError, IndexError):
            self.last_message = "[danger]Usage: proxy rotate <id|all>[/danger]"
            self.message_display_time = asyncio.get_running_loop().time() + 2  # Show for 2 seconds
        except Exception as e:
            self.last_message = f"[danger]Error: {e}[/danger]"
            self.message_display_time = asyncio.get_running_loop().time() + 3

    async def run(self, main_renderable_callable):
        """Starts the interactive UI loop."""
        async_input = AsyncInput()
        async_input.start()

        layout = Layout()
        layout.split(
            Layout(name="main"),
            Layout(size=3, name="footer"),
        )

        try:
            with Live(layout, console=self.console, screen=True, transient=True, refresh_per_second=10) as live:
                while not self.exit_flag:
                    # Process keyboard input
                    char = async_input.get_input()
                    if char:
                        if char in ('\x03', '\x1b'): # Ctrl+C or ESC
                            self.exit_flag = True
                            break
                        elif char in ('\r', '\n'): # Enter
                            await self._process_command()
                        elif char in ('\x7f', '\b'): # Backspace
                            self.input_buffer = self.input_buffer[:-1]
                        elif char == '\x1b[A': # Up arrow
                            self.scroll_offset = max(0, self.scroll_offset - 1)
                        elif char == '\x1b[B': # Down arrow
                            self.scroll_offset += 1
                        elif char and char.isprintable():
                            self.input_buffer += char

                    # Update UI components
                    main_content = main_renderable_callable(self.scroll_offset, self.console.height - 4)
                    layout["main"].update(main_content)
                    layout["footer"].update(self._get_input_panel())
                    
                    # Ensure scroll offset is not out of bounds
                    if hasattr(main_content.renderable, "row_count"):
                         max_scroll = max(0, main_content.renderable.row_count - (self.console.height - 5))
                         self.scroll_offset = min(self.scroll_offset, max_scroll)


                    await asyncio.sleep(0.05)  # Yield control to allow other tasks to run
        finally:
            async_input.stop()