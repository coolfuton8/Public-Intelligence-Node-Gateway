"""MCPing MCP server: screenshot + keyboard/mouse tools for a target computer.

Coordinates in every mouse tool are pixels of the MOST RECENT screenshot image, so what
you see in the image is what you click. Call `screenshot` first.

Configuration (environment variables):
  MCPING_HID_PORT      serial port of the ESP32 (default: auto-detect by USB id)
  MCPING_VIDEO_DEVICE  capture device (default: the card found by USB id, else /dev/video0)
  MCPING_FRAME_SIZE    capture size WxH (default 1920x1080)
  MCPING_TOKEN         bearer token, required for --transport http
"""

import argparse
import functools
import hmac
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Optional

import anyio
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings
from serial.tools import list_ports

from . import __version__
from . import clients as _clients
from . import monitor as _monitor
from .capture import Capture, CaptureError, find_capture_device, to_jpeg
from .hid import HidClient, HidError, to_abs
from .monitor import monitor as mon

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="mcping: %(message)s")
log = logging.getLogger("mcping")

# Over HTTP the device is reached by name or IP from other machines, so the SDK's
# localhost-only Host-header check (DNS-rebinding protection for browsers) is off; the
# bearer token is the access control. stdio ignores this.
mcp = FastMCP(
    "mcping",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    streamable_http_path="/mcp",
    stateless_http=os.environ.get("MCPING_STATELESS", "1") != "0",
)

# USB-serial chips seen on ESP32 dev boards: WCH CH343/CH340, Silicon Labs CP210x, FTDI.
_SERIAL_IDS = {(0x1A86, 0x55D3), (0x1A86, 0x7523), (0x10C4, 0xEA60), (0x0403, 0x6001)}
_ACTION_LOG = Path(os.environ.get("MCPING_ACTION_LOG", Path.home() / ".local/state/mcping/actions.jsonl"))


@dataclass
class Shot:
    img_w: int
    img_h: int
    frame_w: int
    frame_h: int


VARIANTS = os.environ.get("MCPING_CAPTURE_VARIANTS", "contrast=60;contrast=44,brightness=70").split(";")


def _same_picture(a, b) -> bool:
    """True when two frames show the same screen, even with different contrast: compare small grey
    thumbnails by correlation (a window opening or text scrolling lowers it sharply)."""
    import numpy as np
    from PIL import ImageOps

    def thumb(im):
        return np.asarray(ImageOps.grayscale(im).resize((96, 54)), dtype=np.float32).ravel()

    if a.size != b.size:
        return False
    x, y = thumb(a), thumb(b)
    if x.std() < 1e-3 or y.std() < 1e-3:
        return bool(abs(x.mean() - y.mean()) < 40)
    return float(np.corrcoef(x, y)[0, 1]) > 0.97


class State:
    def __init__(self) -> None:
        w, h = (int(v) for v in os.environ.get("MCPING_FRAME_SIZE", "1920x1080").lower().split("x"))
        self.capture = Capture(os.environ.get("MCPING_VIDEO_DEVICE") or find_capture_device(), w, h)
        self._hid: Optional[HidClient] = None
        self.last_shot: Optional[Shot] = None
        self._analyzer = None
        self.registry = None
        self.screen = None            # last parsed Screen (element IDs refer to it)

    @property
    def analyzer(self):
        if self._analyzer is None:
            from .vision import Analyzer
            from .vision.model import IdRegistry
            self._analyzer = Analyzer()
            self.registry = IdRegistry()
        return self._analyzer

    def parse(self):
        """Capture and parse the screen; IDs stay stable for elements that did not move."""
        frame = self.capture.grab()
        screen = self.analyzer.analyze(frame, self._more_captures(frame))
        self.registry.assign(screen)
        return screen

    def _more_captures(self, frame):
        """A function that returns further captures of the same screen with other card settings
        (contrast 60, then brightness 70), or None. The parser calls it only when the first capture
        reads badly, and the captures then vote (best 2 of 3). MCPING_CAPTURE_VARIANTS=""
        turns it off; it takes ';'-separated control sets."""
        specs = [v for v in VARIANTS if v.strip()]

        def more(i: int):
            while i < len(specs):
                spec = specs[i]
                try:
                    controls = {k.strip(): int(v) for k, v in (p.split("=") for p in spec.split(","))}
                    other = self.capture.grab_variant(controls)
                except (CaptureError, ValueError, OSError) as exc:
                    log.info("variant capture %r skipped: %s", spec, exc)
                    return None
                if _same_picture(frame, other):
                    return other
                log.info("variant capture %r dropped: the screen changed between captures", spec)
                return None
            return None

        return more if specs else None

    @property
    def hid(self) -> HidClient:
        if self._hid is None:
            port = os.environ.get("MCPING_HID_PORT") or find_hid_port()
            if not port:
                raise HidError("HID board not found; set MCPING_HID_PORT or check the UART cable")
            self._hid = HidClient(port)
            log.info("HID connected on %s: %s", port, self._hid.version)
        return self._hid


def find_hid_port() -> Optional[str]:
    for p in list_ports.comports():
        if (p.vid, p.pid) in _SERIAL_IDS:
            return p.device
    return None


state = State()

# tesserocr installs signal handlers when first imported, which Python only allows on the main
# thread. Tools run in worker threads, so import the parser here, at start-up.
try:
    from . import vision as _vision  # noqa: F401
except ImportError:
    log.info("screen parser not available (vision dependencies not installed)")

# Every frame the server grabs is also shown on the /monitor page (scaled down).
_grab = state.capture.grab


def _grab_and_publish(*args, **kwargs):
    frame = _grab(*args, **kwargs)
    mon.publish_frame(frame)
    return frame


state.capture.grab = _grab_and_publish

# One tool at a time (they share the capture card and the serial port). `abort` and `status`
# skip the queue so a stuck action can always be cancelled.
_tool_lock = threading.Lock()
_NO_QUEUE = {"abort", "status"}


def _marker_for(name: str, kw: dict) -> Optional[dict]:
    """Where on the screen a pointer tool is about to act, as fractions of the screen."""
    try:
        if name in ("mouse_move", "mouse_click") and kw.get("x") is not None:
            iw, ih = _shot_dims()
            return {"kind": name, "x": kw["x"] / iw, "y": kw["y"] / ih}
        if name == "mouse_drag":
            iw, ih = _shot_dims()
            return {"kind": name, "x0": kw["x1"] / iw, "y0": kw["y1"] / ih, "x": kw["x2"] / iw, "y": kw["y2"] / ih}
        if name in ("click_element", "type_into") and state.screen is not None:
            target = state.screen.find(kw["id"])
            if target is not None:
                x, y, w, h = target.box
                return {"kind": name, "x": (x + w / 2) / state.screen.width,
                        "y": (y + h / 2) / state.screen.height}
    except Exception:  # noqa: BLE001 - the monitor must never break a tool call
        pass
    return None


def tool():
    """Like @mcp.tool(), but logs the call for the /monitor page and runs it in a worker
    thread so the web page stays responsive during slow tools (OCR, waits)."""
    def deco(fn):
        name = fn.__name__

        @functools.wraps(fn)
        async def wrapper(**kwargs):
            ev = mon.begin(name, kwargs)
            marker: dict = {}

            def run():
                if name in _NO_QUEUE:
                    return fn(**kwargs)
                with _tool_lock:
                    marker["m"] = _marker_for(name, kwargs)
                    return fn(**kwargs)

            try:
                result = await anyio.to_thread.run_sync(run)
            except Exception as exc:  # noqa: BLE001
                mon.end(ev, error=f"{type(exc).__name__}: {exc}")
                raise
            mon.end(ev, result=result)
            mon.set_marker(marker.get("m"))
            if name in ("describe_screen", "screen_changes") and state.screen is not None:
                mon.set_elements(state.screen)
            return result

        return mcp.tool()(wrapper)
    return deco


def _check(id: str, label: str, status: str, detail: str) -> dict:
    return {"id": id, "label": label, "status": status, "detail": detail}


def _esp32_checks(rounds: int) -> list[dict]:
    """ESP32 serial link and, when the firmware reports it, the target's USB link. Sends only
    PING / LINK: nothing is typed or clicked."""
    port = os.environ.get("MCPING_HID_PORT") or find_hid_port()
    if not port or not os.path.exists(port):
        return [_check("esp32", "ESP32 serial link", "fail",
                       "serial device not found: check the board's UART USB-C cable and that it is plugged in"),
                _check("link", "Target USB link", "unknown", "not checked: the board is not reachable")]
    try:
        hid = state.hid
        times = []
        for _ in range(rounds):
            t = time.perf_counter()
            if not hid.ping():
                raise HidError("PING was not answered with PONG")
            times.append((time.perf_counter() - t) * 1000)
    except Exception as exc:  # noqa: BLE001
        try:                                   # drop the connection so the next call reconnects cleanly
            if state._hid is not None:
                state._hid._ser.close()
        except Exception:  # noqa: BLE001
            pass
        state._hid = None
        return [_check("esp32", "ESP32 serial link", "fail", f"{port} present but no reply: {exc}"),
                _check("link", "Target USB link", "unknown", "not checked: the board is not answering")]
    out = [_check("esp32", "ESP32 serial link", "ok",
                  f"{hid.version} answers on {port} ({min(times):.0f}-{max(times):.0f} ms)")]
    try:
        link = hid.command("LINK").lower()
    except HidError:
        out.append(_check("link", "Target USB link", "unknown",
                          "this firmware cannot report it: look at the board's LED (green = target connected, red = not)"))
        return out
    if link.startswith("linked"):
        out.append(_check("link", "Target USB link", "ok", "the target computer sees the keyboard and mouse"))
    elif link.startswith("suspended"):
        out.append(_check("link", "Target USB link", "warn", "the target has suspended its USB port (asleep?)"))
    else:
        out.append(_check("link", "Target USB link", "fail",
                          "the board's USB port is not connected to a powered target: plug its other USB-C port into the target"))
    return out


def _capture_checks(grab: bool) -> list[dict]:
    """Capture card: device node and, with grab=True, a fresh frame judged for a video signal."""
    dev = state.capture.device
    if not os.path.exists(dev):
        return [_check("capture", "Capture card", "fail",
                       f"{dev} not found: check the capture card's USB cable (use a USB 3 port)")]
    info = None
    if grab:
        mon.current = "test connectivity"
        t = time.perf_counter()
        try:
            frame = state.capture.grab()           # also published to the picture
        except Exception as exc:  # noqa: BLE001
            return [_check("capture", "Capture card", "fail", f"device present but no frame: {exc}")]
        finally:
            mon.current = ""
        ms = (time.perf_counter() - t) * 1000
        info = mon.frame_info()
        detail_head = f"{frame.width}x{frame.height} frame in {ms:.0f} ms; "
    else:
        info = mon.frame_info()
        if not info.get("seq"):
            return [_check("capture", "Capture card", "unknown",
                           "device present; no frame grabbed yet (press Test connectivity)")]
        detail_head = f"last frame {max(0, round(time.time() - info['t']))} s ago ({info['by'] or 'tool'}); "
    status, why = _monitor.classify_signal(info["luma"], info["contrast"])
    return [_check("capture", "Capture card", "ok", detail_head + "device responding"),
            _check("signal", "HDMI picture", status, why)]


def health_checks() -> list[dict]:
    """Cheap, repeated by the monitor page: no frame grab, one serial ping."""
    return ([_check("service", "MCPing service", "ok", f"running, version {__version__}")]
            + _esp32_checks(1) + _capture_checks(False))


def test_connectivity() -> list[dict]:
    """The Test connectivity button: 3 serial pings, the link query and a fresh frame grab."""
    ev = mon.begin("test_connectivity", {})
    with _tool_lock:                               # never in the middle of another action
        checks = (_esp32_checks(3) + _capture_checks(True))
    checks.insert(0, _check("service", "MCPing service", "ok", f"running, version {__version__}"))
    worst = "fail" if any(c["status"] == "fail" for c in checks) else (
        "warn" if any(c["status"] == "warn" for c in checks) else "ok")
    mon.end(ev, result=f"{worst}: " + "; ".join(f"{c['label']} {c['status']}" for c in checks))
    return checks


_monitor.register(mcp, health=health_checks, test=test_connectivity)

# While a browser has the monitor page open, grab a frame every IDLE_REFRESH_S seconds when no
# tool is running and the last frame is older than that, so the picture and the capture/HDMI
# indicators stay current without any MCP activity. 0 turns it off. Nobody watching: no grabs.
IDLE_REFRESH_S = float(os.environ.get("MCPING_IDLE_REFRESH", "10"))


def _idle_refresh_loop() -> None:
    while True:
        time.sleep(1)
        try:
            if _tool_lock.locked() or not mon.idle_refresh_due(IDLE_REFRESH_S):
                continue
            mon.current = "idle refresh"
            try:
                state.capture.grab(timeout=6)
            finally:
                if mon.current == "idle refresh":
                    mon.current = ""
        except Exception:  # noqa: BLE001 - card unplugged or no frame: try again later
            time.sleep(IDLE_REFRESH_S)


def _record(action: str, **fields) -> None:
    try:
        _ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _ACTION_LOG.open("a") as f:
            f.write(json.dumps({"t": round(time.time(), 3), "action": action, **fields}) + "\n")
    except OSError:
        pass


def _shot_dims() -> tuple[int, int]:
    s = state.last_shot
    return (s.img_w, s.img_h) if s else (state.capture.width, state.capture.height)


def _abs_xy(x: int, y: int) -> tuple[int, int]:
    iw, ih = _shot_dims()
    return to_abs(x, iw), to_abs(y, ih)


@tool()
def screenshot(max_width: int = 1280, quality: int = 70):
    """Capture the target computer's screen.

    Returns the image. Mouse coordinates in other tools refer to pixels of this image,
    so call this before clicking. max_width downsizes the image (default 1280); use a
    larger value or the `zoom` tool when text is hard to read.
    """
    max_width = max(320, min(int(max_width), 3840))
    frame = state.capture.grab()
    data, (w, h) = to_jpeg(frame, max_width, max(30, min(int(quality), 95)))
    state.last_shot = Shot(w, h, frame.width, frame.height)
    _record("screenshot", size=[w, h])
    return [f"Screenshot {w}x{h} (target frame {frame.width}x{frame.height}). "
            f"Mouse coordinates are pixels of this image, origin top-left.",
            Image(data=data, format="jpeg")]


@tool()
def zoom(x: int, y: int, width: int, height: int, max_width: int = 1280):
    """Capture a fresh frame and return just one region, at full detail.

    x, y, width, height are in the coordinates of the most recent `screenshot` image.
    Coordinates for clicking are unchanged by zoom; keep using the screenshot's.
    """
    iw, ih = _shot_dims()
    frame = state.capture.grab()
    sx, sy = frame.width / iw, frame.height / ih
    left, top = max(0, round(x * sx)), max(0, round(y * sy))
    right = min(frame.width, round((x + width) * sx))
    bottom = min(frame.height, round((y + height) * sy))
    if right - left < 2 or bottom - top < 2:
        raise ValueError("zoom region is empty or outside the screen")
    data, (w, h) = to_jpeg(frame.crop((left, top, right, bottom)), max(320, min(int(max_width), 3840)), 85)
    _record("zoom", region=[x, y, width, height])
    return [f"Zoom of region x={x}, y={y}, w={width}, h={height} ({w}x{h} image).",
            Image(data=data, format="jpeg")]


@tool()
def type_text(text: str, delay_ms: int = 8) -> str:
    """Type text on the target's keyboard (ASCII, US layout). Newlines press Enter.

    The text goes to whatever has keyboard focus; click the field first. delay_ms is the
    gap between characters (raise it for slow machines, BIOS screens or remote sessions).
    """
    if len(text) > 5000:
        raise ValueError("text too long (limit 5000 characters per call)")
    state.hid.type_text(text, delay_ms)
    _record("type_text", chars=len(text))  # note: the live monitor's call list shows the arguments, text included
    return f"typed {len(text)} characters"


@tool()
def key_combo(keys: str) -> str:
    """Press a key or chord, e.g. "enter", "ctrl+c", "alt+f4", "ctrl+alt+del", "win+r", "f5".

    Names: ctrl shift alt win enter esc tab backspace delete insert home end pageup
    pagedown up down left right capslock space f1-f12, or a single character.
    """
    state.hid.key_combo(keys)
    _record("key_combo", keys=keys)
    return f"pressed {keys}"


@tool()
def mouse_move(x: int, y: int) -> str:
    """Move the pointer to (x, y) in the most recent screenshot's coordinates."""
    ax, ay = _abs_xy(x, y)
    state.hid.move_abs(ax, ay)
    _record("mouse_move", x=x, y=y)
    return f"moved to ({x}, {y})"


@tool()
def mouse_click(x: Optional[int] = None, y: Optional[int] = None,
                button: str = "left", double: bool = False) -> str:
    """Click at (x, y) in screenshot coordinates, or at the current position if omitted.

    button is left, right or middle. double=True double-clicks.
    """
    if (x is None) != (y is None):
        raise ValueError("give both x and y, or neither")
    if x is not None:
        ax, ay = _abs_xy(x, y)
        state.hid.click(button, ax, ay)
    else:
        state.hid.click(button)
    if double:
        state.hid.click(button)
    _record("mouse_click", x=x, y=y, button=button, double=double)
    where = f"({x}, {y})" if x is not None else "current position"
    return f"{'double-' if double else ''}clicked {button} at {where}"


@tool()
def mouse_drag(x1: int, y1: int, x2: int, y2: int, button: str = "left", steps: int = 10) -> str:
    """Press at (x1, y1), drag to (x2, y2) and release, in screenshot coordinates."""
    steps = max(2, min(int(steps), 50))
    sx, sy = _abs_xy(x1, y1)
    ex, ey = _abs_xy(x2, y2)
    hid = state.hid
    hid.move_abs(sx, sy)
    hid.button(button, True)
    try:
        for i in range(1, steps + 1):
            hid.move_abs(round(sx + (ex - sx) * i / steps), round(sy + (ey - sy) * i / steps))
            time.sleep(0.02)
    finally:
        hid.button(button, False)
    _record("mouse_drag", frm=[x1, y1], to=[x2, y2], button=button)
    return f"dragged ({x1}, {y1}) -> ({x2}, {y2})"


@tool()
def mouse_scroll(amount: int) -> str:
    """Scroll the wheel at the current pointer position. Positive scrolls up, negative down."""
    state.hid.scroll(amount)
    _record("mouse_scroll", amount=amount)
    return f"scrolled {amount}"


@tool()
def wait(seconds: float = 1.0) -> str:
    """Pause (up to 30 s), e.g. while a page loads, before taking the next screenshot."""
    seconds = max(0.0, min(float(seconds), 30.0))
    time.sleep(seconds)
    return f"waited {seconds:g}s"


@tool()
def abort() -> str:
    """Immediately release every held key and mouse button. Use if something looks wrong."""
    state.hid.abort()
    _record("abort")
    return "all keys and buttons released"


# --- element tools: for models that read text instead of images -------------------------

_LEGEND = ("Each line is one control: KIND id \"caption\" label=... value=... state. Act with "
           "click_element(id) / type_into(id, text). Items in [brackets] have their own ids "
           "(e3.2). '?' after a kind means unsure. Coordinates are never needed.")


def _require_screen():
    if state.screen is None:
        raise ValueError("call describe_screen first")
    return state.screen


def _click_point(ident: str) -> tuple[int, int]:
    screen = _require_screen()
    target = screen.find(ident)
    if target is None:
        raise ValueError(f"no element {ident!r} in the last description; call describe_screen again")
    x, y, w, h = target.box
    return to_abs(x + w / 2, screen.width), to_abs(y + h / 2, screen.height)


@tool()
def describe_screen(coords: bool = False) -> str:
    """Read the target screen as text: windows, controls, their captions, values and states.

    Use this instead of screenshot if you cannot see images. Every control gets an id
    (e7) and clickable sub-items (menu entries, tabs, list rows) get ids like e7.3.
    coords=True adds pixel centres (@x,y), only useful for debugging.

    Small text is read less reliably than large text: digits in IP addresses, sizes and versions
    are the usual casualties. If a number matters, call text_zoom first (terminals, browsers),
    or use zoom/read_element on that spot, and check it before relying on it. A terminal whose
    text is large enough is read by matching its font's glyphs and appears as a TERMINAL block
    with numbered lines; that reading is far more reliable than OCR for digits.
    """
    from .vision.model import render_text
    state.screen = state.parse()
    s = state.screen
    _record("describe_screen", elements=sum(1 for _ in s.elements()), text_quality=s.text_quality)
    head, _, rest = render_text(s, coords=coords).partition("\n")
    note = f"TEXT QUALITY {s.text_quality}%" + (f" (best of {s.captures} captures)" if s.captures > 1 else "")
    if s.text_quality < 60:
        note += (": LOW. Some captions and numbers below are probably misread (marked ? or garbled). "
                 "Check anything that matters with screenshot, zoom or read_element before acting on it.")
    elif s.text_quality < 80:
        note += ": fair. Verify numbers and small labels with zoom before relying on them."
    return f"{head}\n{note}\n{rest}\n({s.seconds}s) " + _LEGEND


@tool()
def text_zoom(direction: str = "in", steps: int = 3) -> str:
    """Make the text in the focused window bigger (or back to normal) so describe_screen,
    read_element and wait_for_text read it accurately.

    Small text is the main cause of misread numbers (IP addresses, sizes, versions). On a real
    terminal, enlarging the font three steps raised exact word accuracy from 38% to 90%. Works in
    terminals and browsers via Ctrl+Shift+= / Ctrl+- / Ctrl+0; click the window first so it has
    focus. direction is "in", "out" or "reset" (back to the default size). Layout shifts, so call
    describe_screen again afterwards.
    """
    direction = direction.strip().lower()
    if direction not in ("in", "out", "reset"):
        raise ValueError('direction must be "in", "out" or "reset"')
    steps = max(1, min(int(steps), 8))
    hid = state.hid
    if direction == "reset":
        hid.key_combo("ctrl+0")
    else:
        combo = "ctrl+shift+=" if direction == "in" else "ctrl+-"
        for _ in range(steps):
            hid.key_combo(combo)
            time.sleep(0.15)
    time.sleep(0.6)
    _record("text_zoom", direction=direction, steps=steps)
    done = "text size reset" if direction == "reset" else f"text zoomed {direction} {steps} step(s)"
    return done + "; call describe_screen again"


@tool()
def screen_changes() -> str:
    """Re-read the screen and report only what changed since the last read
    (+ new, - gone, ~ changed). Much shorter than describe_screen after a small action."""
    from .vision.model import diff
    old = state.screen
    state.screen = state.parse()
    _record("screen_changes")
    return diff(old, state.screen)


@tool()
def click_element(id: str, button: str = "left", double: bool = False) -> str:
    """Click a control or item by its id from the last describe_screen (e.g. "e12" or "e4.2")."""
    ax, ay = _click_point(id)
    state.hid.click(button, ax, ay)
    if double:
        state.hid.click(button)
    _record("click_element", id=id, button=button, double=double)
    return f"{'double-' if double else ''}clicked {id}"


@tool()
def type_into(id: str, text: str, clear: bool = True, enter: bool = False, delay_ms: int = 8) -> str:
    """Click a text field by id, optionally replace its contents (clear=True selects all
    first), type text, and optionally press Enter."""
    if len(text) > 5000:
        raise ValueError("text too long (limit 5000 characters per call)")
    ax, ay = _click_point(id)
    hid = state.hid
    hid.click("left", ax, ay)
    time.sleep(0.15)
    if clear:
        hid.key_combo("ctrl+a")
    hid.type_text(text, delay_ms)
    if enter:
        hid.key_combo("enter")
    _record("type_into", id=id, chars=len(text), enter=enter)
    return f"typed {len(text)} characters into {id}" + (" and pressed Enter" if enter else "")


@tool()
def read_element(id: str) -> str:
    """Re-read one control's text closely (fresh capture, enlarged OCR). Use when a value
    in describe_screen looks garbled or is long."""
    screen = _require_screen()
    target = screen.find(id)
    if target is None:
        raise ValueError(f"no element {id!r}; call describe_screen again")
    x, y, w, h = target.box
    frame = state.capture.grab()
    sx, sy = frame.width / screen.width, frame.height / screen.height
    crop = frame.crop((round(x * sx), round(y * sy), round((x + w) * sx), round((y + h) * sy)))
    text, conf = state.analyzer.ocr.line(crop, scale=3.0)
    _record("read_element", id=id)
    return f"{id}: {text!r} (confidence {conf:.0f})"


@tool()
def find_elements(text: str, kind: str = "") -> str:
    """Search the last description for controls whose caption, label, value or items contain
    `text` (case-insensitive), optionally only of one kind (BUTTON, TEXTBOX, ...)."""
    from .vision.model import _line
    screen = _require_screen()
    q = text.lower()
    hits = []
    for e in screen.elements():
        if kind and e.kind != kind.upper():
            continue
        hay = " ".join([e.text or "", e.label or "", e.value or ""] + [i.text for i in e.items]).lower()
        if q in hay:
            hits.append(_line(e, coords=False))
    return "\n".join(hits) if hits else f"nothing matching {text!r}"


@tool()
def wait_for_text(text: str, timeout: float = 10.0) -> str:
    """Wait until `text` appears anywhere on screen (e.g. a dialog title or "Saved"),
    checking about once a second. Returns as soon as it is seen, or after timeout (max 60 s)."""
    timeout = max(1.0, min(float(timeout), 60.0))
    end = time.time() + timeout
    q = text.lower()
    while True:
        words = state.analyzer.ocr.words(state.capture.grab())
        joined = " ".join(w.text for w in words).lower()
        if q in joined:
            return f"seen {text!r}"
        if time.time() >= end:
            return f"not seen after {timeout:g}s"
        time.sleep(0.5)


@tool()
def status() -> dict:
    """Report device health: firmware, capture settings, last screenshot size."""
    out: dict = {"mcping": __version__, "video_device": state.capture.device,
                 "frame_size": f"{state.capture.width}x{state.capture.height}"}
    try:
        out["hid"] = state.hid.version
        out["hid_ok"] = state.hid.ping()
    except HidError as exc:
        out["hid"] = f"unavailable: {exc}"
        out["hid_ok"] = False
    s = state.last_shot
    out["last_screenshot"] = f"{s.img_w}x{s.img_h}" if s else None
    return out


# Off by default: the /monitor pages need the token even on the device itself, because any program or
# user on that computer could otherwise read the live screen and the typed text. MCPING_MONITOR_LOCAL_OPEN=1
# opens the pages without a sign-in to a browser on the device itself (the request comes from the device's
# own address). Everything else, and every other computer, always needs the token.
MONITOR_LOCAL_OPEN = os.environ.get("MCPING_MONITOR_LOCAL_OPEN", "0") == "1"
_local_cache: dict = {"at": 0.0, "ips": set()}


def _is_local(scope) -> bool:
    """True when the TCP peer is this very machine: loopback or one of its own addresses."""
    peer = (scope.get("client") or ("",))[0]
    if peer in ("127.0.0.1", "::1") or peer.startswith("127."):
        return True
    now = time.time()
    if now - _local_cache["at"] > 30:
        ips: set = set()
        try:                                   # every address of this machine, IPv4 and IPv6
            out = subprocess.run(["ip", "-o", "addr", "show"], capture_output=True, text=True,
                                 timeout=5).stdout
            ips = set(re.findall(r"inet6? ([0-9a-fA-F:.]+)(?:/\d+)?", out))
        except (OSError, subprocess.TimeoutExpired):
            pass
        _local_cache["ips"], _local_cache["at"] = ips, now
    peer = peer.split("%", 1)[0].removeprefix("::ffff:")
    return peer in _local_cache["ips"]


SINGLE_CLIENT = os.environ.get("MCPING_SINGLE_CLIENT", "1") != "0"
SESSION_IDLE_S = float(os.environ.get("MCPING_SESSION_IDLE", "300"))


class _Lease:
    """Lets exactly one client drive the device at a time.

    The server runs stateless (no MCP session ids), so the holder is the authenticated computer plus the
    address it connects from. Every /mcp request renews the lease, and it is released when the holder
    sends DELETE /mcp, or has been idle (no request in flight) for `idle` seconds. Anyone else gets
    HTTP 409 until then. A holder that reconnects (same computer, same address) simply continues."""

    def __init__(self, idle: float):
        self.idle = idle
        self._lock = threading.Lock()
        self.owner = None            # (client id, peer address)
        self.label = ""
        self.last = 0.0
        self.inflight = 0

    def acquire(self, key, label: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            if self.owner is not None and self.owner != key and (
                    self.inflight > 0 or now - self.last <= self.idle):
                return False
            if self.owner != key:
                log.info("connection taken by %s (%s)", label, key[1])
                self.inflight = 0
            self.owner, self.label, self.last = key, label, now
            self.inflight += 1
            return True

    def done(self, key, now: float | None = None) -> None:
        with self._lock:
            if self.owner == key:
                self.inflight = max(0, self.inflight - 1)
                self.last = time.time() if now is None else now

    def release(self, key) -> bool:
        with self._lock:
            if self.owner != key:
                return False
            log.info("connection released by %s", self.label)
            self.owner, self.label, self.inflight = None, "", 0
            return True

    def holder(self) -> str:
        with self._lock:
            return self.label


def _release_input() -> None:
    """Nothing may stay held when the client changes: send ABORT to the keyboard and mouse."""
    try:
        if state._hid is not None:
            state._hid.abort()
    except Exception:  # noqa: BLE001
        pass


class _BearerAuth:
    """ASGI middleware: every request needs `Authorization: Bearer <token>`. A tiny
    unauthenticated /health lets a service manager or you check that it is up."""

    def __init__(self, app, token: str, clients_path=None, single_client: bool | None = None,
                 idle: float | None = None):
        self.app = app
        self.master = token
        self.clients_path = clients_path or _clients.DEFAULT_PATH
        self.single = SINGLE_CLIENT if single_client is None else single_client
        self.lease = _Lease(SESSION_IDLE_S if idle is None else idle)

    def _identify(self, supplied: bytes):
        """(client id, label) for a valid `Authorization: Bearer ...`: the master token or the
        token of a computer that has not been revoked. None otherwise."""
        value = supplied.decode("latin-1")
        if not value.startswith("Bearer "):
            return None
        tok = value[len("Bearer "):]
        if hmac.compare_digest(tok.encode(), self.master.encode()):
            return ("master", "master")
        c = _clients.verify(tok, self.clients_path)
        return (c["id"], c["label"]) if c else None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope["path"] == "/health":
            return await self._reply(send, 200, b'{"ok":true,"service":"mcping"}')
        headers = dict(scope["headers"])
        path = scope["path"]
        if path == "/monitor" or path == "/monitor/login":
            return await self.app(scope, receive, send)      # page shell and sign-in carry no data
        who = None
        if path.startswith("/monitor/"):
            who = self._cookie_client(headers.get(b"cookie", b""))
            if who is None and MONITOR_LOCAL_OPEN and _is_local(scope):
                who = ("local", "this device")               # someone at the device itself: no sign-in
        if who is None:
            who = self._identify(headers.get(b"authorization", b""))
        if who is None:
            log.warning("rejected request to %s from %s", scope["path"], (scope.get("client") or ("?",))[0])
            return await self._reply(send, 401, b'{"error":"unauthorized"}', [(b"www-authenticate", b"Bearer")])
        if who[0] != "master":
            _clients.touch(who[0], (scope.get("client") or ("",))[0], self.clients_path)
        key = None
        if self.single and (path == "/mcp" or path.startswith("/mcp/")):
            key = (who[0], (scope.get("client") or ("",))[0])
            if scope.get("method") == "DELETE":                # the client is closing its connection
                if self.lease.release(key):
                    _release_input()
                return await self._reply(send, 200, b'{"ok":true}')
            before = self.lease.holder()
            if not self.lease.acquire(key, who[1]):
                log.warning("refused %s from %s: device in use by %s", who[1], key[1], self.lease.holder())
                return await self._reply(
                    send, 409, b'{"error":"busy","message":"another client is connected to this device; '
                               b'try again when it has disconnected or has been idle for a while"}',
                    [(b"retry-after", str(int(self.lease.idle)).encode())])
            if before and before != who[1]:
                _release_input()                               # the previous holder timed out
        reset = _monitor.current_client.set(who[1])          # the monitor log shows who made each call
        try:
            return await self.app(scope, receive, send)
        finally:
            _monitor.current_client.reset(reset)
            if key is not None:
                self.lease.done(key)

    def _cookie_client(self, raw: bytes):
        try:
            jar = SimpleCookie(raw.decode("latin-1"))
            got = jar[_monitor.COOKIE].value if _monitor.COOKIE in jar else ""
        except Exception:  # noqa: BLE001
            return None
        return _monitor.cookie_client(got, self.master) if got else None

    @staticmethod
    async def _reply(send, status: int, body: bytes, extra=()):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode()), *extra]})
        await send({"type": "http.response.body", "body": body})


def _run_http(host: str, port: int) -> None:
    token = os.environ.get("MCPING_TOKEN", "")
    if len(token) < 24:
        sys.exit("MCPING_TOKEN (24+ characters) is required for the http transport")
    import signal
    import threading

    import uvicorn

    _monitor.set_token(token)
    if IDLE_REFRESH_S > 0:
        threading.Thread(target=_idle_refresh_loop, daemon=True, name="idle-refresh").start()
    app = mcp.streamable_http_app()
    guarded = _BearerAuth(app, token)       # wraps the app, so its lifespan still runs
    kwargs = {}
    scheme = "http"
    tls_dir = os.environ.get("MCPING_TLS_DIR")
    if tls_dir:
        from pathlib import Path as _P
        from . import tls
        d = _P(tls_dir)
        log.info("certificate: %s", tls.ensure(d))     # issue, or renew if due / addresses changed
        p = tls.paths(d)
        kwargs = dict(ssl_certfile=str(p["crt"]), ssl_keyfile=str(p["key"]))
        scheme = "https"

        def watch() -> None:
            """Restart (systemd brings the service back) when the certificate file changes,
            so renewing needs no privileges and no manual restart."""
            import time as _t
            seen = p["crt"].stat().st_mtime
            last_check = _t.time()
            while True:
                _t.sleep(5)
                try:
                    if p["crt"].stat().st_mtime != seen:
                        log.info("certificate changed on disk; restarting to load it")
                        os.kill(os.getpid(), signal.SIGTERM)
                        return
                    if _t.time() - last_check > 6 * 3600:      # twice a day is plenty
                        last_check = _t.time()
                        why = tls.renewal_reason(d)
                        if why:
                            log.info("renewing certificate: %s", why)
                            tls.issue_server(d)
                except Exception as exc:  # noqa: BLE001
                    log.warning("certificate watcher: %s", exc)

        threading.Thread(target=watch, daemon=True, name="cert-watch").start()
    cfg = uvicorn.Config(guarded, host=host, port=port, log_level="warning", lifespan="on", **kwargs)
    log.info("MCP endpoint on %s://%s:%d/mcp (bearer token required)", scheme, host, port)
    uvicorn.Server(cfg).run()


def main() -> None:
    ap = argparse.ArgumentParser(prog="mcping-server")
    ap.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    ap.add_argument("--host", default=os.environ.get("MCPING_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MCPING_PORT", "8765")))
    args = ap.parse_args()
    try:
        if args.transport == "http":
            _run_http(args.host, args.port)
        else:
            mcp.run(transport="stdio")
    finally:
        try:
            if state._hid is not None:
                state._hid.close()  # sends ABORT so nothing stays held
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
