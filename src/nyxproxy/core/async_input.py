import asyncio
import queue
import sys
import threading

try:
    import msvcrt
    _WINDOWS = True
except ImportError:
    import termios
    import tty
    _WINDOWS = False

class AsyncInput:
    """
    A class to read keyboard input asynchronously without blocking,
    and in a cross-platform manner.
    """

    def __init__(self):
        self._input_queue = queue.Queue()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        """The main loop for the input reader thread."""
        while not self._stop_event.is_set():
            try:
                char = self._get_char()
                if char:
                    self._input_queue.put(char)
            except (IOError, OSError):
                # This can happen when the program is exiting
                break
            except Exception:
                # Ignore other potential errors in the input thread
                break

    def _get_char(self):
        """Reads a single character from stdin."""
        if _WINDOWS:
            # For Windows, msvcrt.getwch() is non-blocking and reads one char.
            if msvcrt.kbhit():
                return msvcrt.getwch()
            # Add a small sleep to prevent pegging the CPU
            asyncio.sleep(0.01)
            return None
        else:
            # For Unix-like systems, we need to set the terminal to cbreak mode.
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                # Use a select call or similar to avoid blocking indefinitely.
                # Here, we'll just read one character. A more robust solution
                # might use select.select, but this is simpler for this context.
                return sys.stdin.read(1)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    def start(self):
        """Starts the input reader thread."""
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self):
        """Stops the input reader thread."""
        self._stop_event.set()

    def get_input(self) -> str | None:
        """
        Retrieves a character from the queue if available, otherwise returns None.
        """
        try:
            return self._input_queue.get_nowait()
        except queue.Empty:
            return None