
import asyncio
import time
from functools import partial

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from pynput import keyboard

# Attempt to import the theme from the main project
try:
    from src.nyxproxy.core.config import DEFAULT_RICH_THEME
except ImportError:
    from rich.theme import Theme
    DEFAULT_RICH_THEME = Theme({
        "accent": "#B7B098",
        "info": "#D6D3C1",
        "muted": "#4A546D",
    })

console = Console(theme=DEFAULT_RICH_THEME)
layout = Layout()

input_buffer = ""
exit_flag = False

# --- Rich Panels ---
def get_main_panel():
    return Panel(
        f"Last updated: {time.ctime()}\n\nThis is where the main content will go.",
        title="NyxProxy Status",
        border_style="accent",
        style="info"
    )

def get_input_panel():
    return Panel(f"proxy> {input_buffer}", style="muted", border_style="accent")

layout.split(
    Layout(get_main_panel(), name="main"),
    Layout(get_input_panel(), name="footer", size=3),
)

# --- Async Keyboard Handling ---

async def handle_key_press(key, live, loop):
    global input_buffer, exit_flag

    try:
        if key.char:
            input_buffer += key.char
    except AttributeError:
        if key == keyboard.Key.space:
            input_buffer += " "
        elif key == keyboard.Key.backspace:
            input_buffer = input_buffer[:-1]
        elif key == keyboard.Key.enter:
            # Process command here
            input_buffer = ""
        elif key == keyboard.Key.esc:
            exit_flag = True
            return False  # Stop listener

    layout["footer"].update(get_input_panel())
    live.refresh()

def keyboard_listener(loop, live):
    def on_press(key):
        asyncio.run_coroutine_threadsafe(handle_key_press(key, live, loop), loop)

    with keyboard.Listener(on_press=on_press) as listener:
        listener.join()

# --- Main Application Loop ---

async def main():
    global exit_flag
    loop = asyncio.get_running_loop()

    with Live(layout, console=console, screen=True, redirect_stderr=False, refresh_per_second=10) as live:
        # Start keyboard listener in a separate thread
        listener_task = loop.run_in_executor(None, keyboard_listener, loop, live)

        # Main update loop
        while not exit_flag:
            layout["main"].update(get_main_panel())
            live.refresh()
            await asyncio.sleep(1)

        await listener_task

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    print("Exiting...")
