"""Client for the ESP32-S3 HID firmware."""

from __future__ import annotations

import threading
import time
from urllib.parse import quote

import serial

ABS_MAX = 32767
MAX_CHUNK = 200  # characters of plain text per TYPE line (percent-encoding triples it)
_BUTTONS = {"left", "right", "middle"}


class HidError(RuntimeError):
    """The firmware rejected a command or did not answer."""


def to_abs(value: float, size: int) -> int:
    """Map a pixel coordinate in [0, size-1] onto the HID absolute range [0, 32767]."""
    if size < 2:
        raise ValueError("size must be at least 2")
    value = min(max(value, 0), size - 1)
    return round(value / (size - 1) * ABS_MAX)


def encode_text(text: str) -> str:
    """Percent-encode text for the TYPE command. Only ASCII is supported by the firmware."""
    try:
        text.encode("ascii")
    except UnicodeEncodeError as exc:
        raise HidError(f"only ASCII text can be typed (offending character: {text[exc.start]!r})") from exc
    return quote(text, safe="")


def chunk_text(text: str, size: int = MAX_CHUNK) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


class HidClient:
    def __init__(self, port: str, baud: int = 115200, timeout: float = 5.0):
        self._lock = threading.Lock()
        # DTR/RTS drive the ESP32 EN (reset) and IO0 (boot) pins through two transistors.
        # Both asserted (1,1) is the idle state; any other combination during open can reset
        # the chip, re-enumerating its USB HID device and losing the first commands.
        # So: configure the port unopened, with both lines high, and never toggle them.
        self._ser = serial.Serial()
        self._ser.port = port
        self._ser.baudrate = baud
        self._ser.timeout = timeout
        self._ser.dtr = True
        self._ser.rts = True
        self._ser.open()
        time.sleep(0.2)
        self._ser.reset_input_buffer()
        self.version = self.command("VER")

    def close(self) -> None:
        try:
            self.abort()
        except Exception:
            pass
        self._ser.close()

    def command(self, line: str, extra_timeout: float = 0.0) -> str:
        """Send one command line and return the data after OK. Raises HidError on ERR."""
        if "\n" in line or "\r" in line:
            raise HidError("command must be a single line")
        with self._lock:
            self._ser.reset_input_buffer()
            self._ser.write(line.encode("ascii") + b"\n")
            self._ser.flush()
            old = self._ser.timeout
            self._ser.timeout = (old or 5.0) + extra_timeout
            try:
                raw = self._ser.readline()
            finally:
                self._ser.timeout = old
        reply = raw.decode("ascii", errors="replace").strip()
        if not reply:
            raise HidError(f"no reply to {line.split(' ', 1)[0]}")
        if reply.startswith("OK"):
            return reply[2:].strip()
        if reply.startswith("ERR"):
            raise HidError(reply[3:].strip() or "firmware error")
        raise HidError(f"unexpected reply: {reply!r}")

    # --- actions -----------------------------------------------------------

    def ping(self) -> bool:
        return self.command("PING") == "PONG"

    def abort(self) -> None:
        self.command("ABORT")

    def type_text(self, text: str, delay_ms: int = 8) -> None:
        if not 0 <= delay_ms <= 1000:
            raise HidError("delay_ms must be 0..1000")
        self.command(f"DELAY {delay_ms}")
        for part in chunk_text(text):
            if not part:
                continue
            est = len(part) * (delay_ms + 2) / 1000.0
            self.command(f"TYPE {encode_text(part)}", extra_timeout=est + 2)

    def key_combo(self, combo: str) -> None:
        combo = combo.strip().replace(" ", "")
        if not combo:
            raise HidError("empty key combo")
        self.command(f"KEYS {combo}")

    def move_abs(self, x: int, y: int) -> None:
        self.command(f"MOVE {x} {y}")

    def click(self, button: str = "left", x: int | None = None, y: int | None = None) -> None:
        if button not in _BUTTONS:
            raise HidError(f"button must be one of {sorted(_BUTTONS)}")
        if (x is None) != (y is None):
            raise HidError("give both x and y, or neither")
        if x is None:
            self.command(f"CLICK {button}")
        else:
            self.command(f"CLICK {button} {x} {y}")

    def button(self, button: str, down: bool) -> None:
        if button not in _BUTTONS:
            raise HidError(f"button must be one of {sorted(_BUTTONS)}")
        self.command(f"{'DOWN' if down else 'UP'} {button}")

    def scroll(self, amount: int) -> None:
        amount = max(-127, min(127, int(amount)))
        self.command(f"SCROLL {amount}")
