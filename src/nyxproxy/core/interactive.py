import asyncio
from functools import partial

from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from pynput import keyboard

class InteractiveUI:
    def __init__(self, manager):
        self.manager = manager
        self.console = manager.console
        self.input_buffer = ""
        self.exit_flag = False

    def _get_input_panel(self):
        return Panel(f"proxy> {self.input_buffer}", style="muted", border_style="accent")

    async def _handle_key_press(self, key):
        try:
            if key.char:
                self.input_buffer += key.char
        except AttributeError:
            if key == keyboard.Key.space:
                self.input_buffer += " "
            elif key == keyboard.Key.backspace:
                self.input_buffer = self.input_buffer[:-1]
            elif key == keyboard.Key.enter:
                await self._process_command()
                self.input_buffer = ""
            elif key == keyboard.Key.esc:
                self.exit_flag = True
                return

    async def _process_command(self):
        command = self.input_buffer.strip().lower()
        if not command:
            return

        parts = command.split()
        if parts[0] == "proxy" and parts[1] == "rotate":
            if len(parts) == 3:
                target = parts[2]
                if target == "all":
                    for i in range(len(self.manager._bridges)):
                        await self.manager.rotate_proxy(i)
                else:
                    try:
                        bridge_id = int(target)
                        await self.manager.rotate_proxy(bridge_id)
                    except ValueError:
                        self.console.print(f"[danger]Invalid bridge ID: {target}[/danger]")
            else:
                self.console.print("[danger]Usage: proxy rotate <id|all>[/danger]")

    def _keyboard_listener(self, loop):
        def on_press(key):
            asyncio.run_coroutine_threadsafe(self._handle_key_press(key), loop)

        with keyboard.Listener(on_press=on_press) as listener:
            listener.join()

    async def run(self):
        loop = asyncio.get_running_loop()
        layout = Layout()

        with Live(layout, console=self.console, screen=True, redirect_stderr=False, auto_refresh=False) as live:
            listener_task = loop.run_in_executor(None, self._keyboard_listener, loop)

            while not self.exit_flag:
                summary_panel = self.manager._display_active_bridges_summary(self.manager.country_filter)
                if not summary_panel:
                    summary_panel = Panel("[warning]No active bridges.[/warning]")
                
                input_panel = self._get_input_panel()

                layout = Layout()
                layout.split(Layout(summary_panel), Layout(input_panel, size=3))
                live.update(layout)
                live.refresh()
                await asyncio.sleep(0.1)

            await listener_task