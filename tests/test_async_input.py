import asyncio
import time

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style

# Attempt to import the theme from the main project
# This assumes the script is run from the project root
try:
    from src.nyxproxy.core.config import DEFAULT_RICH_THEME
except ImportError:
    # Fallback theme if the import fails
    from rich.theme import Theme
    DEFAULT_RICH_THEME = Theme({
        "accent": "#B7B098",
        "info": "#D6D3C1",
        "muted": "#4A546D",
    })

console = Console(theme=DEFAULT_RICH_THEME)

# --- Rich Content ---

def get_rich_content():
    """Returns the rich content to be displayed."""
    return Panel(
        f"Last updated: {time.ctime()}\n\nThis is where the main content will go.",
        title="NyxProxy Status",
        border_style="accent",
        style="info"
    )

# --- Prompt Toolkit UI ---

# The prompt_toolkit control for displaying rich content
rich_control = FormattedTextControl(text="")

# The buffer for the input field
input_buffer = Buffer()

# The main layout
root_container = HSplit([
    Window(content=rich_control, height=10),
    Window(height=1, char="-", style="class:line"),
    Window(content=BufferControl(buffer=input_buffer, input_processors=[]), height=1),
])

layout = Layout(root_container)

# Key bindings
kb = KeyBindings()

@kb.add("c-c")
@kb.add("c-q")
def _(event):
    """ Pressing Ctrl-Q or Ctrl-C will exit the user interface. """
    event.app.exit()

def rgb_to_hex(rgb):
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"

# Styling for prompt_toolkit
prompt_style = Style.from_dict({
    '': f"fg:{rgb_to_hex(DEFAULT_RICH_THEME.styles['info'].color.triplet)}",
    'line': f"fg:{rgb_to_hex(DEFAULT_RICH_THEME.styles['muted'].color.triplet)}",
})

# The application
app = Application(layout=layout, key_bindings=kb, style=prompt_style, full_screen=True)

def update_rich_content():
    """Update the rich content in the top window."""
    rich_content = get_rich_content()
    with console.capture() as capture:
        console.print(rich_content)
    rich_control.text = capture.get()
    app.invalidate()

async def main():
    """Run the application."""
    async def update_loop():
        while True:
            update_rich_content()
            await asyncio.sleep(1)

    update_task = asyncio.create_task(update_loop())
    await app.run_async()
    update_task.cancel()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    print("Exiting...")
