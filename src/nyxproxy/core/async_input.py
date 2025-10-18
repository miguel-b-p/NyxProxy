import asyncio
import queue
import sys
import threading
import time

try:
    import msvcrt
    _WINDOWS = True
except ImportError:
    import select
    import termios
    import tty
    _WINDOWS = False

class AsyncInput:
    """
    A class to read keyboard input asynchronously without blocking,
    and in a cross-platform manner, correctly handling escape sequences.
    """

    def __init__(self):
        self._input_queue = queue.Queue()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run_windows(self):
        """The main loop for Windows."""
        while not self._stop_event.is_set():
            if msvcrt.kbhit():
                char = msvcrt.getwch()
                # Handle special keys like arrows on Windows
                if char == '\xe0':
                    # Read the second byte of the sequence
                    next_char = msvcrt.getwch()
                    full_sequence = char + next_char
                    self._input_queue.put(full_sequence)
                else:
                    self._input_queue.put(char)
            # Prevent high CPU usage
            time.sleep(0.02)

    def _run_unix(self):
        """The main loop for Unix-like systems."""
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while not self._stop_event.is_set():
                # Use select to wait for input with a small timeout
                rlist, _, _ = select.select([sys.stdin], [], [], 0.05)
                if rlist:
                    char = sys.stdin.read(1)
                    # If it's an escape character, wait briefly for more characters
                    if char == '\x1b':
                        # Give a tiny moment for the rest of the sequence to arrive
                        time.sleep(0.01)
                        # Drain any other characters that are part of the sequence
                        while select.select([sys.stdin], [], [], 0)[0]:
                            char += sys.stdin.read(1)
                    
                    self._input_queue.put(char)
        finally:
            # CRITICAL: Always restore terminal settings upon exit
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    def _run(self):
        """Selects the correct runner based on the OS."""
        if _WINDOWS:
            self._run_windows()
        else:
            self._run_unix()

    def start(self):
        """Starts the input reader thread."""
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self):
        """Stops the input reader thread."""
        self._stop_event.set()
        # Give the thread a moment to finish gracefully
        self._thread.join(timeout=0.2)

    def get_input(self) -> str | None:
        """
        Retrieves a character or sequence from the queue if available.
        """
        try:
            return self._input_queue.get_nowait()
        except queue.Empty:
            return None