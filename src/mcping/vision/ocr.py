"""Tesseract OCR, kept loaded in-process (tesserocr) so each read has no start-up cost."""

from __future__ import annotations

import glob
import os
import re
import threading
from dataclasses import dataclass, field

import cv2
import numpy as np
from PIL import Image, ImageOps
from tesserocr import OEM, PSM, RIL, PyTessBaseAPI, iterate_level

_ALNUM = re.compile(r"[A-Za-z0-9]")


def _tessdata() -> str:
    """Folder holding eng.traineddata. MCPING_TESSDATA points at a different model set, for
    example tessdata_best (larger and more accurate on small text, slower)."""
    override = os.environ.get("MCPING_TESSDATA")
    if override:
        if not os.path.exists(os.path.join(override, "eng.traineddata")):
            raise RuntimeError(f"MCPING_TESSDATA={override!r} has no eng.traineddata")
        return override.rstrip("/") + "/"
    found = sorted(glob.glob("/usr/share/tesseract-ocr/*/tessdata"))
    if not found:
        raise RuntimeError("Tesseract language data not found (install tesseract-ocr-eng)")
    return found[-1] + "/"


@dataclass
class Word:
    text: str
    x: int
    y: int
    w: int
    h: int
    conf: float

    @property
    def x2(self) -> int:
        return self.x + self.w

    @property
    def y2(self) -> int:
        return self.y + self.h

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


@dataclass
class Phrase:
    """Words on one line that belong together (a label, a button caption, a menu item)."""

    words: list[Word] = field(default_factory=list)
    used: bool = False

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def x(self) -> int:
        return min(w.x for w in self.words)

    @property
    def y(self) -> int:
        return min(w.y for w in self.words)

    @property
    def x2(self) -> int:
        return max(w.x2 for w in self.words)

    @property
    def y2(self) -> int:
        return max(w.y2 for w in self.words)

    @property
    def w(self) -> int:
        return self.x2 - self.x

    @property
    def h(self) -> int:
        return self.y2 - self.y

    @property
    def cx(self) -> float:
        return (self.x + self.x2) / 2

    @property
    def cy(self) -> float:
        return (self.y + self.y2) / 2

    @property
    def box(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.w, self.h)

    @property
    def conf(self) -> float:
        return sum(w.conf for w in self.words) / len(self.words)


def _gamma(g: np.ndarray) -> np.ndarray:
    """Lift dim grey text on a dark ground (disabled controls, secondary labels)."""
    return (255.0 * (g / 255.0) ** 0.6).astype(np.uint8)


def _clahe(g: np.ndarray) -> np.ndarray:
    """Local contrast: small text whose contrast with its own background is low."""
    return cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(g)


# Extra readings of the same frame. Each wins on text the others lose (measured on the Mint dark
# fixtures: one pass 49% of tokens exact, raw+gamma+clahe about 55%), so the passes are merged.
_PASSES = {"raw": None, "gamma": _gamma, "clahe": _clahe}
DEFAULT_PASSES = tuple(p for p in os.environ.get("MCPING_OCR_PASSES", "raw,gamma,clahe").split(",")
                       if p in _PASSES) or ("raw",)


# A capture whose share of confidently read words (confidence >= 80) is below this is "bad": more
# captures are asked for and the readings vote. Calibrated on 25 capture modes of one dark Mint screen
# (good captures 0.45-0.65; the poor ones 0.26-0.35).
BAD_BELOW = float(os.environ.get("MCPING_OCR_BAD_BELOW", "0.40"))
SOLO_CONF = 60.0          # a word only one capture saw must be at least this confident to stay


def _keep(w: Word, min_conf: float) -> bool:
    return w.conf >= min_conf or (len(w.text) >= 4 and w.conf >= 20)


def _norm(text: str) -> str:
    return re.sub(r"^[^\w/%]+|[^\w/%]+$", "", text.lower().replace("×", "x"))


def vote(captures: list[list[Word]]) -> list[Word]:
    """Merge several readings of the same screen by majority. Words from different captures that
    overlap are the same spot; the text most captures agree on wins (ties go to the more confident),
    and a spot is kept when at least two captures read the winning text, or one capture read it with
    confidence >= SOLO_CONF. Guesses nobody else saw are dropped, which also removes junk."""
    spots: list[list[tuple[int, Word]]] = []
    items = sorted(((i, w) for i, ws in enumerate(captures) for w in ws), key=lambda t: -t[1].conf)
    for i, w in items:
        for spot in spots:
            if _iou(w, spot[0][1]) >= 0.3:
                spot.append((i, w))
                break
        else:
            spots.append([(i, w)])
    out: list[Word] = []
    for spot in spots:
        votes: dict[str, set[int]] = {}
        best: dict[str, Word] = {}
        for i, w in spot:
            key = _norm(w.text)
            votes.setdefault(key, set()).add(i)
            if key not in best or w.conf > best[key].conf:
                best[key] = w
        key = max(votes, key=lambda k: (len(votes[k]), best[k].conf))
        w = best[key]
        if len(votes[key]) >= 2 or w.conf >= SOLO_CONF:
            out.append(w)
    return out


def quality(words: list[Word]) -> int:
    """How well the text of a screen was read, 0-100: the share of words read with high confidence
    (area-weighted, so a long caption counts more than a stray mark)."""
    area = sum(max(1, w.w * w.h) for w in words)
    if not area:
        return 100
    good = sum(max(1, w.w * w.h) for w in words if w.conf >= 80)
    return round(100 * good / area)


_t = os.environ.get("MCPING_OCR_TILE", "960x540").lower()
TILE = tuple(int(v) for v in _t.split("x")) if re.fullmatch(r"\d+x\d+", _t) else None   # "0" = whole frame
TILE_OVERLAP = 64
# A whole-frame read with fewer than this share of confident words is repeated in tiles and merged.
# Seen: a clean Linux Mint "Welcome" window read 6% confident as a whole frame and 53% in tiles, while
# the Display / terminal / menu fixtures read 25-45% whole and get worse when tiled.
TILE_BELOW = float(os.environ.get("MCPING_OCR_TILE_BELOW", "0.25"))


def _tiles(w: int, h: int) -> list[tuple[int, int, int, int]]:
    """Overlapping tiles covering a w x h frame (one tile when the frame is not larger than a tile)."""
    tw, th = TILE
    out = []
    y = 0
    while True:
        x = 0
        while True:
            out.append((x, y, min(w, x + tw), min(h, y + th)))
            if x + tw >= w:
                break
            x += tw - TILE_OVERLAP
        if y + th >= h:
            break
        y += th - TILE_OVERLAP
    return out


def _dedupe(words: list[Word]) -> list[Word]:
    """Words found twice in a tile overlap, and fragments cut by a tile edge, are reduced to the most
    confident reading: drop a word that overlaps a kept one (IoU >= 0.3) or lies mostly inside it."""
    kept: list[Word] = []
    for w in sorted(words, key=lambda w: -w.conf):
        dup = False
        for k in kept:
            ix = max(0, min(w.x2, k.x2) - max(w.x, k.x))
            iy = max(0, min(w.y2, k.y2) - max(w.y, k.y))
            inter = ix * iy
            if inter and (inter / (w.w * w.h) >= 0.5 or _iou(w, k) >= 0.3):
                dup = True
                break
        if not dup:
            kept.append(w)
    return kept


def _iou(a: Word, b: Word) -> float:
    ix = max(0, min(a.x2, b.x2) - max(a.x, b.x))
    iy = max(0, min(a.y2, b.y2) - max(a.y, b.y))
    inter = ix * iy
    union = a.w * a.h + b.w * b.h - inter
    return inter / union if union > 0 else 0.0


class Ocr:
    def __init__(self, scale: float = 2.0, passes: tuple[str, ...] | None = None):
        self.scale = scale
        self.passes = tuple(passes) if passes else DEFAULT_PASSES
        self._lock = threading.Lock()
        self.last_quality: list[float] = []     # per capture, from the latest words() call
        path = _tessdata()
        self._sparse = PyTessBaseAPI(path=path, psm=PSM.SPARSE_TEXT, oem=OEM.LSTM_ONLY)
        self._line = PyTessBaseAPI(path=path, psm=PSM.SINGLE_LINE, oem=OEM.LSTM_ONLY)
        self._word = PyTessBaseAPI(path=path, psm=PSM.SINGLE_WORD, oem=OEM.LSTM_ONLY)

    def words(self, img: Image.Image, min_conf: float = 35.0,
              extra: list[Image.Image] | None = None, more=None, max_more: int = 2,
              bad_below: float = BAD_BELOW) -> list[Word]:
        """All words in the image, boxes in the image's own pixel coordinates.

        Within one capture the readings of _PASSES are merged (the more confident wins where they
        overlap). If the capture reads badly (see `quality`) and `more` is given, up to `max_more` more
        captures of the same screen are fetched by calling `more(i)` (an Image, or None to stop) and
        the captures vote: a word stays when two captures agree on it, or one read it confidently.
        `extra` are captures supplied up front, which join the vote without being asked for."""
        captures = [self._read_capture(img, min_conf)]
        for frame in extra or []:
            captures.append(self._read_capture(frame, min_conf))
        if more is not None and captures[0][1] < bad_below:
            for i in range(max_more):
                frame = more(i)
                if frame is None:
                    break
                captures.append(self._read_capture(frame, min_conf))
        self.last_quality = [round(q, 2) for _, q in captures]
        if len(captures) == 1:
            return captures[0][0]
        return vote([ws for ws, _ in captures])

    def _read_capture(self, frame: Image.Image, min_conf: float) -> tuple[list[Word], float]:
        """One capture, all passes merged, and its quality (the share of confidently read words)."""
        runs: list[list[Word]] = []
        quality = 1.0
        gray = None
        for name in self.passes:
            fn = _PASSES[name]
            if fn is None:
                allw = self._read(frame)
                if allw:
                    quality = sum(1 for w in allw if w.conf >= 80) / len(allw)
                runs.append([w for w in allw if _keep(w, min_conf)])
                continue
            if gray is None:
                gray = np.asarray(ImageOps.grayscale(frame))
            runs.append([w for w in self._read(Image.fromarray(fn(gray))) if _keep(w, min_conf)])
        if len(runs) == 1:
            return runs[0], quality
        merged: list[Word] = []
        # stable sort: the raw pass wins ties, and confident readings are placed first
        for w in sorted((w for run in runs for w in run), key=lambda w: -w.conf):
            if all(_iou(w, k) < 0.3 for k in merged):
                merged.append(w)
        return merged, quality

    def _read(self, img: Image.Image) -> list[Word]:
        """Every word Tesseract finds, as (text, box, confidence). The frame is read in overlapping
        tiles: on a whole 1080p frame the page layout and the binarisation are thrown off by the
        wallpaper and by big empty areas, and a clean window reads 6% confident words instead of 50%+."""
        whole = self._read_one(img, 0, 0)
        tiles = _tiles(img.width, img.height) if TILE else []
        if len(tiles) <= 1:
            return whole
        good = sum(1 for w in whole if w.conf >= 80) / len(whole) if whole else 0.0
        if good >= TILE_BELOW:                 # the whole-frame read is fine: tiling can only hurt
            return whole
        out = list(whole)
        for x0, y0, x1, y1 in tiles:
            out.extend(self._read_one(img.crop((x0, y0, x1, y1)), x0, y0))
        return _dedupe(out)

    def _read_one(self, img: Image.Image, ox: int, oy: int) -> list[Word]:
        s = self.scale
        big = img.resize((round(img.width * s), round(img.height * s)), Image.BICUBIC) if s != 1 else img
        out: list[Word] = []
        with self._lock:
            self._sparse.SetImage(big)
            try:
                self._sparse.Recognize()
                for r in iterate_level(self._sparse.GetIterator(), RIL.WORD):
                    text = (r.GetUTF8Text(RIL.WORD) or "").strip()
                    if not text:
                        continue
                    conf = r.Confidence(RIL.WORD)
                    if not _ALNUM.search(text) and text not in {"...", "…", "&", "/", "-", "+", "|", ":"}:
                        continue
                    x1, y1, x2, y2 = r.BoundingBox(RIL.WORD)
                    out.append(Word(text, ox + round(x1 / s), oy + round(y1 / s),
                                    max(1, round((x2 - x1) / s)), max(1, round((y2 - y1) / s)), conf))
            except RuntimeError:          # "No text returned": a blank or text-free frame
                pass
        return out

    def line(self, img: Image.Image, scale: float = 3.0, invert: bool = False,
             word: bool = False) -> tuple[str, float]:
        """Read one line of text from a small crop (a button face, a selected row, a field).
        word=True reads a single short word (OK, Yes, No), which line mode often misses."""
        if img.width < 2 or img.height < 2:
            return "", 0.0
        gray = ImageOps.grayscale(img)
        if invert:
            gray = ImageOps.invert(gray)
        # The engine ignores text that touches the border: pad with the edge colour.
        arr = np.asarray(gray)
        edge = int(np.median(np.concatenate([arr[0], arr[-1], arr[:, 0], arr[:, -1]])))
        pad = max(6, round(gray.height * 0.5))
        gray = ImageOps.expand(gray, border=pad, fill=edge)
        gray = gray.resize((round(gray.width * scale), round(gray.height * scale)), Image.BICUBIC)
        api = self._word if word else self._line
        with self._lock:
            api.SetImage(gray)
            text = (api.GetUTF8Text() or "").strip()
            conf = float(api.MeanTextConf())
        return " ".join(text.split()), conf


def group_phrases(words: list[Word], gap_factor: float = 0.9) -> list[Phrase]:
    """Join words into phrases: same line and separated by a normal word space."""
    # 1) cluster into text lines by vertical centre
    lines: list[list[Word]] = []
    for w in sorted(words, key=lambda w: w.cy):
        for ln in lines:
            cy = sum(v.cy for v in ln) / len(ln)
            h = max(max(v.h for v in ln), w.h)
            if abs(w.cy - cy) < 0.4 * h:
                ln.append(w)
                break
        else:
            lines.append([w])
    # 2) within a line, split where the gap is wider than a word space
    phrases: list[Phrase] = []
    for ln in lines:
        ln.sort(key=lambda w: w.x)
        cur = Phrase([ln[0]])
        for w in ln[1:]:
            last = cur.words[-1]
            gap = w.x - last.x2
            if gap <= gap_factor * max(last.h, w.h):
                cur.words.append(w)
            else:
                phrases.append(cur)
                cur = Phrase([w])
        phrases.append(cur)
    return phrases
