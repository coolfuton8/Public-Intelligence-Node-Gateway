"""Read monospace (terminal) text by matching each character cell against rendered glyphs.

Tesseract has to guess at 8 px anti-aliased text. A terminal, though, draws every character in an
identical, evenly spaced cell with a known font, so each cell can be compared against that font's
own glyphs. That is deterministic and far more accurate for the things that matter in a terminal:
IP addresses, versions, paths, sizes.

    result = read_monospace(gray_array)         # gray crop of the terminal text area

Steps: the line pitch comes from where the ink is absent between lines; every installed monospace
font and size whose line height matches that pitch gives a candidate character width; the grid
shift and glyph placement that best explain a sample of cells are searched (blurred, mean-centred
correlation, which tolerates hinting and anti-aliasing differences); the best candidate then
classifies every cell by nearest template.
"""

from __future__ import annotations

import glob
import subprocess
from dataclasses import dataclass
from functools import lru_cache

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

CHARS = [chr(c) for c in range(33, 127)]          # printable ASCII except space (blank cells are spaces)
BLUR = 0.8
_DEFAULT_BONUS = 0.04                             # the system's default monospace wins close calls
_GOOD_ENOUGH = 0.07                              # a fit this close needs no further font search


def _pad_for(cell_h: int) -> int:
    return max(8, cell_h // 2 + 4)


@dataclass
class MonoResult:
    lines: list[str]
    font: str
    size: float
    cell_w: int
    cell_h: int
    score: float                    # mean (1 - correlation) over text cells; lower is better
    box: tuple[int, int, int, int]  # x, y, w, h covered by the grid, in crop coordinates
    unsure: int = 0                 # cells that matched poorly (reported as the nearest glyph anyway)


@lru_cache(maxsize=1)
def mono_font_files() -> tuple[str, ...]:
    """Regular-weight monospace fonts installed on this machine (the matcher renders them itself)."""
    try:
        out = subprocess.run(["fc-list", ":spacing=mono", "file", "style"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    files = []
    for line in out.splitlines():
        path, _, style = line.partition(":")
        path = path.strip()
        if not path.lower().endswith((".ttf", ".otf")):
            continue
        low = (path + style).lower()
        if any(b in low for b in ("bold", "italic", "oblique", "mathjax", "vector")):
            continue
        files.append(path)
    if not files:
        files = glob.glob("/usr/share/fonts/**/DejaVuSansMono.ttf", recursive=True)
    return tuple(dict.fromkeys(files))


@lru_cache(maxsize=1)
def default_mono_file() -> str:
    try:
        out = subprocess.run(["fc-match", "-f", "%{file}", "monospace"], capture_output=True, text=True,
                             timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    return out


def _ink(img: np.ndarray) -> np.ndarray:
    """How far each pixel is from the background colour (0..255). Colour text on a dark
    background (a red label, a green prompt) counts as much as white text."""
    a = img.astype(np.float32)
    if a.ndim == 2:
        return np.abs(a - float(np.median(a)))
    bg = np.median(a.reshape(-1, a.shape[2]), axis=0)
    return np.sqrt(((a - bg) ** 2).sum(axis=2) / a.shape[2])


def _line_pitch(profile: np.ndarray, lo: int = 9, hi: int = 70, floor: float = 0.55) -> int:
    """Smallest period whose gaps are clearly emptier than the rest (the rows between text lines)."""
    mean = float(profile.mean())
    if mean <= 0 or len(profile) < 3 * lo:
        return 0
    scores = {}
    for lag in range(lo, min(hi, len(profile) // 3) + 1):
        scores[lag] = 1.0 - min(profile[o::lag].mean() for o in range(lag)) / mean
    if not scores:
        return 0
    best = max(scores.values())
    if best < floor:
        return 0
    near = [lag for lag in sorted(scores) if scores[lag] >= 0.93 * best]
    if not near:
        return 0
    plateau = [lag for lag in near if lag <= near[0] * 1.15 + 2]
    if len(plateau) == 1:
        return plateau[0]
    # short blocks leave several lags with perfectly empty gaps; the true pitch repeats best
    p = profile.astype(np.float64) - profile.mean()
    ac = {lag: float((p[:-lag] * p[lag:]).sum()) / max(1, len(p) - lag) for lag in plateau}
    return max(ac, key=ac.get)


def _unit(v: np.ndarray) -> np.ndarray:
    """Mean-centred, unit-length rows (so a dot product is a correlation)."""
    v = v - v.mean(axis=1, keepdims=True)
    n = np.linalg.norm(v, axis=1, keepdims=True)
    return v / np.maximum(n, 1e-6)


_SS = 4                                           # templates are rendered 4x and averaged down (unhinted)


def _blur3(a: np.ndarray) -> np.ndarray:
    """[1 2 1] x [1 2 1] / 16 smoothing over the last two axes, edges replicated."""
    p = np.pad(a, [(0, 0)] * (a.ndim - 2) + [(1, 1), (1, 1)], mode="edge")
    h = (p[..., :, :-2] + 2.0 * p[..., :, 1:-1] + p[..., :, 2:]) / 4.0
    return (h[..., :-2, :] + 2.0 * h[..., 1:-1, :] + h[..., 2:, :]) / 4.0


def _glyph_stack(path: str, size: float, cell_w: int, cell_h: int) -> np.ndarray | None:
    """Every printable glyph at 4x resolution, in a padded canvas: (N, (H+2pad)*4, (W+2pad)*4).
    Averaging this down gives unhinted anti-aliased glyphs with quarter-pixel placement."""
    try:
        font = ImageFont.truetype(path, size * _SS)
    except (OSError, ValueError):
        return None
    pad = _pad_for(cell_h)
    w, h = (cell_w + 2 * pad) * _SS, (cell_h + 2 * pad) * _SS
    stack = np.zeros((len(CHARS), h, w), dtype=np.float32)
    for i, ch in enumerate(CHARS):
        im = Image.new("L", (w, h), 0)
        ImageDraw.Draw(im).text((pad * _SS, pad * _SS), ch, font=font, fill=255)
        stack[i] = np.asarray(im, dtype=np.float32) / 255.0
    return stack


def _bank(stack: np.ndarray, cell_w: int, cell_h: int, dy_q: int, dx_q: int = 0) -> np.ndarray:
    """Unit templates with the glyph placed dy_q / dx_q quarter pixels down / right in the cell."""
    pad = _pad_for(cell_h) * _SS
    y0, x0 = pad - dy_q, pad - dx_q
    sub = stack[:, y0:y0 + cell_h * _SS, x0:x0 + cell_w * _SS]
    small = sub.reshape(len(CHARS), cell_h, _SS, cell_w, _SS).mean(axis=(2, 4))
    return _unit(_blur3(small).reshape(len(CHARS), -1))

def _cells(img: np.ndarray, x0: int, y0: int, cw: int, ch: int) -> tuple[np.ndarray, int, int]:
    nr, nc = (img.shape[0] - y0) // ch, (img.shape[1] - x0) // cw
    if nr < 1 or nc < 1:
        return np.empty((0, cw * ch), dtype=np.float32), 0, 0
    block = img[y0:y0 + nr * ch, x0:x0 + nc * cw]
    return np.ascontiguousarray(block.reshape(nr, ch, nc, cw).transpose(0, 2, 1, 3).reshape(nr * nc, ch * cw)), nr, nc


def _candidates(cell_h: int) -> list[tuple[str, float, int]]:
    """(font file, size, character width) whose line height fits the measured line pitch."""
    found = []
    for path in mono_font_files():
        try:
            base = ImageFont.truetype(path, 40)
        except (OSError, ValueError):
            continue
        adv40, (asc40, desc40) = base.getlength("M"), base.getmetrics()
        for size in np.arange(6.0, 40.0, 0.25):
            k = size / 40.0
            if abs((asc40 + desc40) * k - cell_h) > 2.0:
                continue
            cw = int(round(adv40 * k))
            if 4 <= cw <= 40:
                found.append((path, float(size), cw))
    return found


def read_monospace(gray: np.ndarray, min_cells: int = 12, cell_w_hint: float | None = None,
                   line_pitch_hint: float | None = None) -> MonoResult | None:
    """Recognise a monospace text block in a crop (gray or RGB). None if it does not look like one.`n`n    The hints (character width and line pitch in pixels, for example from OCR word boxes) narrow the`n    search a lot and keep short blocks from locking onto a wrong pitch."""
    ink = _ink(gray)
    pos = ink[ink > 15]
    if pos.size < 50 or float(np.percentile(pos, 90)) < 40:
        return None
    rows_p = (ink > 0.35 * float(np.percentile(pos, 90))).sum(axis=1).astype(np.float32)
    if line_pitch_hint:
        h = int(round(line_pitch_hint))
        ch = _line_pitch(rows_p, max(9, h - 2), h + 2, floor=0.35)
    else:
        ch = _line_pitch(rows_p)
    if not ch:
        return None
    yo = int(np.argmin([rows_p[o::ch].sum() for o in range(ch)]))
    cands = _candidates(ch)
    if cell_w_hint:
        cands = [c for c in cands if abs(c[2] - cell_w_hint) <= 2.0]
    if not cands:
        return None
    blurred = _blur3(ink)
    default = default_mono_file()
    rng = np.random.default_rng(0)

    def fit(path: str, size: float, cw: int, stack: np.ndarray, n_sample: int, refine: bool = False):
        """Best (score, x0, dy_q, dx_q) for this candidate over grid shifts and quarter-pixel placements."""
        best = None
        banks = {dq: _bank(stack, cw, ch, dq) for dq in range(-12, (ch // 2) * _SS, 2)}
        for x0 in range(cw):
            raw, nr, nc = _cells(ink, x0, yo, cw, ch)
            if nr < 1 or nc < 6:
                continue
            peak = raw.max(axis=1)
            if not (peak > 0).any():
                continue
            ref = float(np.percentile(peak[peak > 0], 85))
            if ref < 40:
                continue
            idx = np.nonzero(peak >= 0.28 * ref)[0]
            if len(idx) < min_cells:
                continue
            sample = idx if len(idx) <= n_sample else rng.choice(idx, n_sample, replace=False)
            cells_b, _, _ = _cells(blurred, x0, yo, cw, ch)
            xs = _unit(cells_b[sample])
            for dq, bank in banks.items():
                score = float((1.0 - (xs @ bank.T).max(axis=1)).mean())
                if best is None or score < best[0]:
                    best = (score, x0, dq, 0, xs)
        if best is None:
            return None
        if refine:                                   # quarter-pixel polish around the winner
            score, x0, dq, _, xs = best
            for ddq in (-1, 0, 1):
                for dxq in (-2, 0, 2):
                    s = float((1.0 - (xs @ _bank(stack, cw, ch, dq + ddq, dxq).T).max(axis=1)).mean())
                    if s < score:
                        best = (s, x0, dq + ddq, dxq, xs)
        return best[:4]

    # Stage 1: score candidates on a small sample, the system's default monospace font first; if it
    # already fits well there is no need to try the others. Stage 2: the front-runners on a larger
    # sample, with a quarter-pixel polish.
    stacks: dict[tuple[str, float, int], np.ndarray] = {}

    def coarse_for(group):
        out = []
        for path, size, cw in group:
            stack = _glyph_stack(path, size, cw, ch)
            if stack is None or stack.max() == 0:
                continue
            stacks[(path, size, cw)] = stack
            b = fit(path, size, cw, stack, 24)
            if b is not None:
                out.append((b[0] - (_DEFAULT_BONUS if path == default else 0.0), path, size, cw))
        return sorted(out)

    first = [c for c in cands if c[0] == default]
    coarse = coarse_for(first) if first else []
    if not coarse or coarse[0][0] > _GOOD_ENOUGH:
        coarse = sorted(coarse + coarse_for([c for c in cands if c[0] != default]))
    if not coarse:
        return None
    best = None
    for _, path, size, cw in coarse[:4]:
        b = fit(path, size, cw, stacks[(path, size, cw)], 90, refine=True)
        if b is None:
            continue
        s = b[0] - (_DEFAULT_BONUS if path == default else 0.0)
        if best is None or s < best[0]:
            best = (s, b[0], path, size, cw, b[1], b[2], b[3])
    if best is None:
        return None
    _, score, path, size, cw, x0, dq, dxq = best

    raw, nr, nc = _cells(ink, x0, yo, cw, ch)
    cells_b, _, _ = _cells(blurred, x0, yo, cw, ch)
    peak = raw.max(axis=1)
    ref = float(np.percentile(peak[peak > 0], 85))
    blank = peak < 0.28 * ref
    # ink that only touches the cell's outer quarters is a neighbour bleeding over, not a character
    cols = raw.reshape(-1, ch, cw).sum(axis=1)                    # (cells, cw)
    q = max(1, cw // 4)
    centre = cols[:, q:cw - q].sum(axis=1)
    blank |= centre < 0.30 * np.maximum(cols.sum(axis=1), 1e-6)
    # a solid block is the text cursor, not a character
    solid_frac = (raw >= 0.6 * np.maximum(peak, 1e-6)[:, None]).mean(axis=1)
    blank |= (solid_frac > 0.72) & (peak > 0.5 * ref)
    # a thin vertical line down one of the outer columns is a scrollbar or window border
    blank2d = blank.reshape(nr, nc)
    cells3 = raw.reshape(-1, ch, cw)
    half = 0.5 * np.maximum(peak, 1e-6)
    ink_cols = cells3.max(axis=1) >= half[:, None]                       # (cells, cw)
    widths = ink_cols.sum(axis=1).reshape(nr, nc)
    # a line that runs the full height of the cell continues into the cells above and below it
    full_height = ((cells3[:, 0, :].max(axis=1) >= half) & (cells3[:, -1, :].max(axis=1) >= half)).reshape(nr, nc)
    for c in (0, 1, nc - 2, nc - 1):
        if not (0 <= c < nc) or nr < 3:
            continue
        rows_on = np.nonzero(~blank2d[:, c])[0]
        if len(rows_on) < max(3, 0.6 * nr):
            continue
        thin = float(np.median(widths[rows_on, c])) <= max(3, cw // 4)
        continuous = float(full_height[rows_on, c].mean()) >= 0.8
        if thin or continuous:
            blank2d[:, c] = True
    blank = blank2d.reshape(-1)
    # each cell may sit up to a pixel off the grid: take the best of several placements
    units = _unit(cells_b)
    stack = stacks[(path, size, cw)]
    sim = np.maximum.reduce([units @ _bank(stack, cw, ch, dq, dxq + off).T for off in (-4, -2, 0, 2, 4)])
    pick, top = sim.argmax(axis=1), sim.max(axis=1)
    # an outer column that reads as brackets/pipes in almost every row is a window border or scrollbar
    pick2d, blank2 = pick.reshape(nr, nc), blank.reshape(nr, nc)
    sure2 = (~blank2) & (top.reshape(nr, nc) >= 0.50)             # outermost columns by well-matched cells
    used_cols = np.nonzero(sure2.any(axis=0))[0]
    if len(used_cols) and nc >= 4 and nr >= 3:
        for c, right in ((int(used_cols.max()), True), (int(used_cols.min()), False)):
            rows_on = np.nonzero(sure2[:, c])[0]
            if len(rows_on) < max(3, 0.6 * nr):
                continue
            glyphs = [CHARS[pick2d[r, c]] for r in rows_on]
            if sum(g in "[]|{}()!:;'`.,_-" for g in glyphs) >= 0.8 * len(glyphs):
                if right:                                          # right border: everything beyond is outside
                    blank2[:, c:] = True
                else:                                              # left border: everything before it too
                    blank2[:, :c + 1] = True
    blank = blank2.reshape(-1)
    # a faint cell that matches no glyph is noise on a space; a strong one that matches nothing is
    # unknown and shown as "?"
    weak = peak < 0.5 * ref
    blank = blank | ((~blank) & (top < 0.50) & weak)
    poor = (~blank) & (top < 0.50)
    lines = []
    for r in range(nr):
        row = [" " if blank[r * nc + c] else ("?" if poor[r * nc + c] else CHARS[pick[r * nc + c]])
               for c in range(nc)]
        lines.append("".join(row).rstrip())
    indent = min((len(l) - len(l.lstrip(" ")) for l in lines if l.strip()), default=0)
    if indent:
        lines = [l[indent:] for l in lines]
    return MonoResult(lines, path.rsplit("/", 1)[-1], size, cw, ch, round(score, 4),
                      (x0, yo, nc * cw, nr * ch), unsure=int(poor.sum()))
