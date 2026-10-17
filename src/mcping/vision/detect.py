"""Low-level detectors: rectangles, accent-coloured regions, circles, caption buttons, taskbar.

All sizes are written for 100% Windows scaling at native resolution and multiplied by `s`,
the measured UI scale (from OCR text height), so they adapt to 125%/150% scaling and to a
capture card that resizes the picture.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .ocr import Word


@dataclass
class Rect:
    x: int
    y: int
    w: int
    h: int
    used: bool = False
    fill: float = 1.0

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

    @property
    def area(self) -> int:
        return self.w * self.h

    @property
    def box(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.w, self.h)

    def contains(self, px: float, py: float, margin: float = 0) -> bool:
        return self.x - margin <= px <= self.x2 + margin and self.y - margin <= py <= self.y2 + margin

    def inside(self, other: "Rect", margin: float = 0) -> bool:
        return (self.x >= other.x - margin and self.y >= other.y - margin and
                self.x2 <= other.x2 + margin and self.y2 <= other.y2 + margin)


def estimate_scale(words: list[Word]) -> float:
    """UI scale from the median OCR word height. Tesseract's word boxes hug the ink, and
    Segoe UI 9 pt at 100% Windows scaling measures about 9 px."""
    hs = sorted(w.h for w in words if w.conf >= 60 and len(w.text) >= 2)
    if len(hs) < 3:
        return 1.0
    return float(min(3.0, max(0.6, hs[len(hs) // 2] / 9.0)))


def edge_map(gray: np.ndarray, low: int = 8, high: int = 24) -> np.ndarray:
    # Very low thresholds: a Windows 11 text box border is grey 236 on white 255.
    return cv2.Canny(gray, low, high)


def _rect_ok(edges: np.ndarray, x: int, y: int, w: int, h: int, min_cov: float) -> bool:
    """All four sides well covered, or strong top and bottom with somewhat weaker sides (a text
    cursor or the first letter often breaks a text box's left border)."""
    t, b, l, r = _sides(edges, x, y, w, h)
    return min(t, b, l, r) >= min_cov or (min(t, b) >= 0.9 and min(l, r) >= 0.5)


def _sides(edges: np.ndarray, x: int, y: int, w: int, h: int) -> tuple[float, float, float, float]:
    H, W = edges.shape
    ix, iw = x + max(1, w // 10), max(1, w - 2 * max(1, w // 10))
    iy, ih = y + max(1, h // 10), max(1, h - 2 * max(1, h // 10))

    def band_h(row: int) -> float:
        r0, r1 = max(0, row - 1), min(H, row + 2)
        seg = edges[r0:r1, ix:ix + iw]
        return float(seg.any(axis=0).mean()) if seg.size else 0.0

    def band_v(col: int) -> float:
        c0, c1 = max(0, col - 1), min(W, col + 2)
        seg = edges[iy:iy + ih, c0:c1]
        return float(seg.any(axis=1).mean()) if seg.size else 0.0

    return band_h(y), band_h(y + h - 1), band_v(x), band_v(x + w - 1)


def _side_coverage(edges: np.ndarray, x: int, y: int, w: int, h: int) -> float:
    H, W = edges.shape
    ix, iw = x + max(1, w // 10), max(1, w - 2 * max(1, w // 10))   # skip rounded corners
    iy, ih = y + max(1, h // 10), max(1, h - 2 * max(1, h // 10))

    def band_h(row: int) -> float:
        r0, r1 = max(0, row - 1), min(H, row + 2)
        seg = edges[r0:r1, ix:ix + iw]
        return float(seg.any(axis=0).mean()) if seg.size else 0.0

    def band_v(col: int) -> float:
        c0, c1 = max(0, col - 1), min(W, col + 2)
        seg = edges[iy:iy + ih, c0:c1]
        return float(seg.any(axis=1).mean()) if seg.size else 0.0

    return min(band_h(y), band_h(y + h - 1), band_v(x), band_v(x + w - 1))


def _fit_sides(edges: np.ndarray, x: int, y: int, w: int, h: int, slack: int = 3) -> tuple[int, int, int, int]:
    """Snap each side of a box to the strongest edge line within +-slack px. Windows draws
    borders in layers (a light outline plus a darker bottom line), so a traced outline often
    stops a pixel or two short of the real border."""
    H, W = edges.shape
    x2, y2 = x + w - 1, y + h - 1
    ix0, ix1 = x + max(1, w // 10), x2 - max(1, w // 10)
    iy0, iy1 = y + max(1, h // 10), y2 - max(1, h // 10)

    def row_cov(r: int) -> float:
        return float((edges[r, ix0:ix1 + 1] > 0).mean()) if 0 <= r < H and ix1 > ix0 else 0.0

    def col_cov(c: int) -> float:
        return float((edges[iy0:iy1 + 1, c] > 0).mean()) if 0 <= c < W and iy1 > iy0 else 0.0

    top = max(range(y - slack, y + slack + 1), key=row_cov)
    bot = max(range(y2 - slack, y2 + slack + 1), key=row_cov)
    lef = max(range(x - slack, x + slack + 1), key=col_cov)
    rig = max(range(x2 - slack, x2 + slack + 1), key=col_cov)
    return lef, top, rig - lef + 1, bot - top + 1


def find_rects(edges: np.ndarray, s: float, min_cov: float = 0.72) -> list[Rect]:
    H, W = edges.shape
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    found: list[Rect] = []
    min_side = max(8, round(9 * s))
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if w < min_side or h < min_side:
            continue
        if w >= W - 2 and h >= H - 2:
            continue
        if not _rect_ok(edges, x, y, w, h, min_cov):
            x, y, w, h = _fit_sides(edges, x, y, w, h)
            if w < min_side or h < min_side or not _rect_ok(edges, x, y, w, h, min_cov):
                continue
        found.append(Rect(x, y, w, h))
    # merge near-duplicates (the two sides of a 1-2 px border give two outlines); keep outer
    found.sort(key=lambda r: -r.area)
    out: list[Rect] = []
    tol = max(3, round(3 * s))
    for r in found:
        if any(abs(r.x - o.x) <= tol and abs(r.y - o.y) <= tol and
               abs(r.x2 - o.x2) <= tol and abs(r.y2 - o.y2) <= tol for o in out):
            continue
        out.append(r)
    return out


def accent_mask(rgb: np.ndarray) -> np.ndarray:
    """Saturated colour: selections, checked boxes, default-button borders, fills, thumbs.
    Includes ClearType colour fringes on text, so use `solid()` for regions."""
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    return ((hsv[..., 1] > 110) & (hsv[..., 2] > 70)).astype(np.uint8)


def solid(mask: np.ndarray) -> np.ndarray:
    """Remove features thinner than 3 px (ClearType fringes, 1 px borders)."""
    return cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))


def components(mask: np.ndarray, min_area: int) -> list[Rect]:
    """Connected regions; each Rect also carries `fill` = pixels / box area."""
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out = []
    for i in range(1, n):
        x, y, w, h, a = stats[i]
        if a >= min_area:
            r = Rect(int(x), int(y), int(w), int(h))
            r.fill = float(a) / max(1, w * h)
            out.append(r)
    return out


def best_line(edges: np.ndarray, horizontal: bool, lo: int, hi: int, start: int, step: int,
              min_len: int) -> tuple[int, int] | None:
    """Among rows (or columns) lo..hi, the one whose edge line running from `start` in
    direction `step` is longest. Returns (position, end) or None if none reaches min_len."""
    best = None
    for pos in range(max(0, lo), hi + 1):
        end = edge_run(edges, pos, start, step, horizontal=horizontal)
        length = abs(end - start)
        if length >= min_len and (best is None or length > best[2]):
            best = (pos, end, length)
    return (best[0], best[1]) if best else None


def find_circles(gray: np.ndarray, s: float) -> list[tuple[float, float, float]]:
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    c = cv2.HoughCircles(blur, cv2.HOUGH_GRADIENT, dp=1, minDist=max(8, 10 * s), param1=60, param2=11,
                         minRadius=max(4, round(5 * s)), maxRadius=max(6, round(10 * s)))
    if c is None:
        return []
    return [(float(x), float(y), float(r)) for x, y, r in c[0]]


def _x_template(n: int, dark_on_light: bool) -> np.ndarray:
    bg, fg = (240, 30) if dark_on_light else (30, 230)
    t = np.full((n + 6, n + 6), bg, np.uint8)
    cv2.line(t, (3, 3), (n + 2, n + 2), fg, 1, cv2.LINE_AA)
    cv2.line(t, (n + 2, 3), (3, n + 2), fg, 1, cv2.LINE_AA)
    return t


def find_close_glyphs(gray: np.ndarray, s: float, thresh: float = 0.62) -> list[tuple[int, int]]:
    """Centres of window close buttons (the thin 'X' glyph in a title bar)."""
    hits: list[tuple[int, int, float]] = []
    for n in sorted({max(7, round(10 * s)), max(7, round(9 * s)), max(7, round(11 * s))}):
        for dark in (True, False):
            t = _x_template(n, dark)
            if gray.shape[0] < t.shape[0] or gray.shape[1] < t.shape[1]:
                continue
            res = cv2.matchTemplate(gray, t, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.where(res >= thresh)
            for y, x in zip(ys, xs):
                hits.append((int(x + t.shape[1] / 2), int(y + t.shape[0] / 2), float(res[y, x])))
    hits.sort(key=lambda h: -h[2])
    out: list[tuple[int, int]] = []
    for x, y, _ in hits:
        if all(abs(x - ox) > 12 * s or abs(y - oy) > 12 * s for ox, oy in out):
            out.append((x, y))
    return out


def edge_run(edges: np.ndarray, fixed: int, start: int, step: int, horizontal: bool, gap: int = 3) -> int:
    """Follow an edge line from `start`; return where it ends. `fixed` is the row (horizontal)
    or column (vertical); a +-2 px band is searched."""
    H, W = edges.shape
    limit = W if horizontal else H
    pos, miss, last_hit = start, 0, start
    while 0 <= pos < limit:
        if horizontal:
            hit = edges[max(0, fixed - 2):min(H, fixed + 3), pos].any()
        else:
            hit = edges[pos, max(0, fixed - 2):min(W, fixed + 3)].any()
        if hit:
            last_hit, miss = pos, 0
        else:
            miss += 1
            if miss > gap:
                break
        pos += step
    if not (0 <= pos < limit):
        return 0 if step < 0 else limit - 1
    return last_hit


def find_taskbar(rgb: np.ndarray, s: float) -> int | None:
    """Top y of a full-width band at the bottom of the screen (the Windows taskbar)."""
    H, W = rgb.shape[:2]
    if H < 200:
        return None
    med = np.median(rgb[:, :: max(1, W // 400)], axis=1)   # per-row median colour
    base = med[H - 3]
    y = H - 3
    while y > H - 80 * s and np.abs(med[y] - base).sum() < 24:
        y -= 1
    band = H - 1 - y
    if 34 * s <= band <= 66 * s:      # Windows 10 taskbar 40 px, Windows 11 48 px at 100%
        return y + 1
    return None
