"""Frame grabbing from a UVC HDMI capture card via ffmpeg."""

from __future__ import annotations

import io
import os
import subprocess
import threading
import time

from PIL import Image


class CaptureError(RuntimeError):
    pass


def find_capture_device() -> str:
    """The capture card's video node, found by its USB identity so a laptop's built-in webcam
    (often /dev/video0) is never picked. Falls back to /dev/video0."""
    import glob
    for pattern in ("/dev/v4l/by-id/*MACROSILICON*index0", "/dev/v4l/by-id/*USB3*Video*index0",
                    "/dev/v4l/by-id/*HDMI*index0", "/dev/v4l/by-id/*Capture*index0"):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[0]
    return "/dev/video0"


class Capture:
    def __init__(self, device: str = "/dev/video0", width: int = 1920, height: int = 1080,
                 warmup_frames: int = 6, timeout: float = 15.0,
                 controls: dict[str, int] | None = None):
        self.device = device
        self.width = width
        self.height = height
        self.warmup_frames = warmup_frames
        self.timeout = timeout
        # The MS2130 card treats a full-range HDMI picture as limited range by default and
        # stretches it: every grey above ~232 clips to white, which erases most Windows
        # control borders. Contrast 44 / brightness 53 measured as a ~1:1 mapping
        # Override with MCPING_CAPTURE_CONTRAST / _BRIGHTNESS.
        self.controls = controls if controls is not None else {
            "contrast": int(os.environ.get("MCPING_CAPTURE_CONTRAST", "44")),
            "brightness": int(os.environ.get("MCPING_CAPTURE_BRIGHTNESS", "53")),
        }
        self._controls_applied = 0.0
        # YUYV is uncompressed (the card sends 1080p at 10 fps in it, plenty for single grabs). Set
        # MCPING_CAPTURE_FORMAT=mjpeg for the card's compressed stream (faster, lower quality).
        self.input_format = os.environ.get("MCPING_CAPTURE_FORMAT", "yuyv422")
        self._lock = threading.Lock()  # one process may open the device at a time

    def _apply_controls(self) -> None:
        """Set the card's picture controls (they reset when the card is re-plugged)."""
        if not self.controls or time.time() - self._controls_applied < 60:
            return
        arg = ",".join(f"{k}={v}" for k, v in self.controls.items())
        try:
            subprocess.run(["v4l2-ctl", "-d", self.device, "--set-ctrl", arg],
                           capture_output=True, timeout=5)
            self._controls_applied = time.time()
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _run(self, input_format: str, timeout: float | None) -> subprocess.CompletedProcess:
        """One ffmpeg grab. The frame leaves ffmpeg as PNG (lossless): JPEG compression, applied
        twice before, smears small text and costs OCR accuracy."""
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "v4l2", "-input_format", input_format,
            "-video_size", f"{self.width}x{self.height}",
            "-i", self.device,
            "-vf", f"select=gte(n\\,{self.warmup_frames})",
            "-vsync", "0", "-frames:v", "1",
            "-f", "image2pipe", "-c:v", "png", "pipe:1",
        ]
        proc = None
        for attempt in range(5):
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=timeout or self.timeout)
            except subprocess.TimeoutExpired as exc:
                raise CaptureError("capture timed out (is the HDMI source active?)") from exc
            except FileNotFoundError as exc:
                raise CaptureError("ffmpeg is not installed") from exc
            # another process (the CLI, a test script) may hold the card for a moment
            if not proc.stdout and b"busy" in proc.stderr.lower() and attempt < 4:
                time.sleep(0.4)
                continue
            break
        return proc

    def grab_variant(self, controls: dict[str, int], timeout: float | None = None) -> Image.Image:
        """Another frame of the same screen with different picture controls (for example a higher
        contrast), used only to give the text reader a second opinion. The card's usual controls are
        restored afterwards; the frame is not published or returned to clients."""
        def put(values: dict[str, int]) -> None:
            arg = ",".join(f"{k}={v}" for k, v in values.items())
            try:
                subprocess.run(["v4l2-ctl", "-d", self.device, "--set-ctrl", arg],
                               capture_output=True, timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass

        self._apply_controls()
        with self._lock:
            put({**self.controls, **controls})
            try:
                proc = self._run(self.input_format, timeout)
                if not proc.stdout and self.input_format != "mjpeg":
                    proc = self._run("mjpeg", timeout)
            finally:
                put(self.controls)
                self._controls_applied = time.time()
        if not proc.stdout:
            raise CaptureError("no variant frame captured")
        img = Image.open(io.BytesIO(proc.stdout))
        img.load()
        return img.convert("RGB")

    def grab(self, timeout: float | None = None) -> Image.Image:
        """Capture one clean frame at the configured size.

        A fresh stream's first frames are often truncated, so the first few are skipped.
        `timeout` overrides the default for this grab (background refreshes use a short one).
        """
        self._apply_controls()
        with self._lock:
            proc = self._run(self.input_format, timeout)
            if not proc.stdout and self.input_format != "mjpeg" and b"busy" not in proc.stderr.lower():
                # uncompressed video may not fit the USB link or the card's mode list: use MJPEG
                proc = self._run("mjpeg", timeout)
        if not proc.stdout:
            tail = proc.stderr.decode(errors="replace").strip().splitlines()[-1:] or ["no output"]
            raise CaptureError(f"no frame captured: {tail[0]}")
        try:
            img = Image.open(io.BytesIO(proc.stdout))
            img.load()
        except Exception as exc:  # noqa: BLE001
            raise CaptureError(f"could not decode captured frame: {exc}") from exc
        return img.convert("RGB")


def to_jpeg(img: Image.Image, max_width: int | None = None, quality: int = 75) -> tuple[bytes, tuple[int, int]]:
    """Downscale (never upscale) to max_width and encode as JPEG. Returns (bytes, (w, h))."""
    if max_width and img.width > max_width:
        h = round(img.height * max_width / img.width)
        img = img.resize((max_width, h), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue(), img.size
