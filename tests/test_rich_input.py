import time
from threading import Thread, Lock

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
layout_lock = Lock()

input_buffer = ""
exit_flag = False

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

def on_press(key):
    global input_buffer
    with layout_lock:
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
                global exit_flag
                exit_flag = True
                return False  # Stop listener
        
        layout["footer"].update(get_input_panel())

def keyboard_listener():
    with keyboard.Listener(on_press=on_press) as listener:
        listener.join()

def main():
    listener_thread = Thread(target=keyboard_listener, daemon=True)
    listener_thread.start()

    with Live(layout, console=console, screen=True, redirect_stderr=False, refresh_per_second=10) as live:
        while not exit_flag:
            with layout_lock:
                layout["main"].update(get_main_panel())
            time.sleep(1)

if __name__ == "__main__":
    main()