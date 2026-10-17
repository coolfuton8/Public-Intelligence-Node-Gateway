"""Command-line access to the same capture and HID code, for testing without an MCP client."""

import argparse
import os
import sys

from .capture import Capture, find_capture_device, to_jpeg
from .hid import HidClient, to_abs
from .server import find_hid_port


def _hid() -> HidClient:
    port = os.environ.get("MCPING_HID_PORT") or find_hid_port()
    if not port:
        sys.exit("HID board not found (set MCPING_HID_PORT)")
    return HidClient(port)


def main() -> None:
    ap = argparse.ArgumentParser(prog="mcping-cli")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("shot", help="save a screenshot")
    p.add_argument("out")
    p.add_argument("--max-width", type=int, default=1280)
    p.add_argument("--device", default=os.environ.get("MCPING_VIDEO_DEVICE") or find_capture_device())

    sub.add_parser("ping")
    sub.add_parser("abort")
    t = sub.add_parser("type")
    t.add_argument("text")
    k = sub.add_parser("keys")
    k.add_argument("combo")
    c = sub.add_parser("click", help="click at x y of a 1920x1080 screen")
    c.add_argument("x", type=int)
    c.add_argument("y", type=int)
    c.add_argument("--button", default="left")
    c.add_argument("--size", default="1920x1080")

    a = ap.parse_args()
    if a.cmd == "shot":
        frame = Capture(a.device).grab()
        data, size = to_jpeg(frame, a.max_width)
        with open(a.out, "wb") as f:
            f.write(data)
        print(f"saved {a.out} {size[0]}x{size[1]} (frame {frame.width}x{frame.height})")
        return
    hid = _hid()
    try:
        if a.cmd == "ping":
            print(hid.version, "pong" if hid.ping() else "no pong")
        elif a.cmd == "abort":
            hid.abort()
            print("released")
        elif a.cmd == "type":
            hid.type_text(a.text)
            print("typed")
        elif a.cmd == "keys":
            hid.key_combo(a.combo)
            print("pressed")
        elif a.cmd == "click":
            w, h = (int(v) for v in a.size.split("x"))
            hid.click(a.button, to_abs(a.x, w), to_abs(a.y, h))
            print("clicked")
    finally:
        hid.close()


if __name__ == "__main__":
    main()
