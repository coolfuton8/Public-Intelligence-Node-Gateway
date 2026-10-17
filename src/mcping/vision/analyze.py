"""Turn a captured frame into a Screen tree of windows and controls, deterministically."""

from __future__ import annotations

import logging
import re
import time
from typing import Optional

import cv2
import numpy as np
from PIL import Image

from .detect import (Rect, accent_mask, best_line, components, edge_map, edge_run, estimate_scale,
                     find_circles, find_close_glyphs, find_rects, find_taskbar, solid)
from .model import Element, Item, Screen, Window
from .ocr import Ocr, Phrase, Word, group_phrases, quality

log = logging.getLogger("mcping.vision")
_TIME = re.compile(r"^\d{1,2}:\d{2}(\s?[AP]M)?$", re.I)
_ALNUM = re.compile(r"[A-Za-z0-9]")


class Analyzer:
    def __init__(self, ocr_scale: float = 2.0):
        self.ocr = Ocr(scale=ocr_scale)

    def analyze(self, img: Image.Image, more=None) -> Screen:
        """`more(i)`: returns another capture of the same screen (or None). It is only called when
        the first capture reads badly; the captures then vote (see Ocr.words)."""
        t0 = time.perf_counter()
        ctx = _Ctx(self.ocr, img.convert("RGB"))
        ctx.more = more
        ctx.run()
        ctx.screen.seconds = round(time.perf_counter() - t0, 2)
        return ctx.screen


class _Ctx:
    """One analysis pass. Phrases and rectangles are marked `used` as controls claim them."""

    def __init__(self, ocr: Ocr, img: Image.Image):
        self.ocr = ocr
        self.full = img           # OCR always reads the full-resolution frame
        self.f = 1.0              # full-resolution pixels per layout pixel
        self._set_layout_image(img)
        self.screen = Screen(img.width, img.height)
        self.elements: list[Element] = []
        self.windows: list[tuple[Window, Rect]] = []

    def _set_layout_image(self, img: Image.Image) -> None:
        self.img = img
        self.rgb = np.asarray(img)
        self.gray = cv2.cvtColor(self.rgb, cv2.COLOR_RGB2GRAY)
        self.H, self.W = self.gray.shape

    # --- pixel helpers -------------------------------------------------------------------
    def crop(self, x, y, w, h) -> Image.Image:
        """Crop from the full-resolution frame; arguments are layout coordinates."""
        f = self.f
        x0, y0 = max(0, int(x * f)), max(0, int(y * f))
        x1, y1 = min(self.full.width, int((x + w) * f)), min(self.full.height, int((y + h) * f))
        return self.full.crop((x0, y0, max(x0 + 1, x1), max(y0 + 1, y1)))

    def interior(self, r: Rect, inset: int = 2) -> np.ndarray:
        return self.gray[r.y + inset:r.y2 - inset, r.x + inset:r.x2 - inset]

    def bg_level(self, r: Rect) -> float:
        a = self.interior(r)
        return float(np.median(a)) if a.size else 255.0

    def frac(self, mask: np.ndarray, x0, y0, x1, y1) -> float:
        a = mask[max(0, int(y0)):max(0, int(y1)), max(0, int(x0)):max(0, int(x1))]
        return float(a.mean()) if a.size else 0.0

    def ring_accent(self, r: Rect, t: int = 2) -> float:
        parts = [self.frac(self.accent, r.x, r.y, r.x2, r.y + t), self.frac(self.accent, r.x, r.y2 - t, r.x2, r.y2),
                 self.frac(self.accent, r.x, r.y, r.x + t, r.y2), self.frac(self.accent, r.x2 - t, r.y, r.x2, r.y2)]
        return sum(parts) / 4

    def ink_level(self, box) -> float:
        """Brightness of the darkest pixels in a box (low = black text, high = greyed out)."""
        x, y, w, h = box
        a = self.gray[max(0, y):y + h, max(0, x):x + w]
        return float(np.percentile(a, 8)) if a.size else 255.0

    def text_is_blue(self, box) -> bool:
        x, y, w, h = box
        a = self.rgb[max(0, y):y + h, max(0, x):x + w].reshape(-1, 3).astype(int)
        if a.size == 0:
            return False
        lum = a.mean(axis=1)
        ink = a[lum < np.percentile(lum, 25)]
        if len(ink) == 0:
            return False
        r, g, b = ink.mean(axis=0)
        return b > r + 50 and b > g + 25

    def free_phrases(self) -> list[Phrase]:
        return [p for p in self.phrases if not p.used]

    def phrases_in(self, r: Rect, margin: float = 0) -> list[Phrase]:
        return [p for p in self.phrases if not p.used and r.contains(p.cx, p.cy, margin)]

    # --- main ----------------------------------------------------------------------------
    def run(self) -> None:
        words = self.ocr.words(self.full, more=getattr(self, "more", None))
        self.words_full = list(words)
        self.screen.text_quality = quality(words)
        self.screen.captures = len(self.ocr.last_quality) or 1
        s_full = estimate_scale(words)
        if s_full > 1.25:
            # Enlarged picture (a capture card upscaling a smaller screen, or high-DPI
            # scaling): analyse the layout on a copy shrunk back to ~100%, which turns
            # smeared 1 px borders crisp again. OCR stays on the full-size frame.
            self.f = s_full
            small = self.full.resize((round(self.full.width / s_full), round(self.full.height / s_full)),
                                     Image.LANCZOS)
            self._set_layout_image(small)
            words = [Word(w.text, round(w.x / s_full), round(w.y / s_full), max(1, round(w.w / s_full)),
                          max(1, round(w.h / s_full)), w.conf) for w in words]
        self.s = s = estimate_scale(words)
        self.edges = edge_map(self.gray)
        self.accent = accent_mask(self.rgb)
        self.solid = solid(self.accent)
        self.rects = find_rects(self.edges, s)
        self.circles = find_circles(self.gray, s)
        words = self.drop_glyph_words(words)
        self.phrases = group_phrases(words)
        self.rects = [r for r in self.rects if not any(r.inside(Rect(*p.box), margin=1) for p in self.phrases)]

        taskbar_top = find_taskbar(self.rgb, s)
        self.find_windows(taskbar_top)
        for w, wr in self.windows:
            self.window_furniture(wr)
        self.tabs()
        self.groups()
        self.checkboxes()
        self.radios()
        self.lists()
        self.sliders()
        self.progress()
        self.field_like()
        self.labels()
        self.leftover_text()
        if taskbar_top is not None:
            self.taskbar(taskbar_top)
        self.place()
        if self.f != 1.0:
            self._to_full_resolution()
        self.terminals()

    def _to_full_resolution(self) -> None:
        f = self.f

        def sc(box):
            x, y, w, h = box
            return (round(x * f), round(y * f), round(w * f), round(h * f))

        for w in self.screen.windows:
            w.box = sc(w.box)
        for e in self.screen.elements():
            e.box = sc(e.box)
            for it in e.items:
                it.box = sc(it.box)

    def drop_glyph_words(self, words: list[Word]) -> list[Word]:
        """OCR reads a radio circle or a checkbox square as 'O', 'o', '0' or '©'; drop those."""
        s = self.s
        squares = [r for r in self.rects if 10 * s <= r.w <= 24 * s and 0.8 <= r.w / max(1, r.h) <= 1.25]
        out = []
        for w in words:
            if len(w.text) <= 2:
                on_circle = any(abs(w.cx - x) <= r + 2 and abs(w.cy - y) <= r + 2 for x, y, r in self.circles)
                on_square = any(r.contains(w.cx, w.cy) for r in squares)
                followed = any(o is not w and w.x2 <= o.x <= w.x2 + 22 * s and abs(o.cy - w.cy) < 6 * s for o in words)
                if (on_circle or on_square) and followed:
                    continue
            out.append(w)
        return out

    # --- windows -------------------------------------------------------------------------
    def find_windows(self, taskbar_top: Optional[int]) -> None:
        s, edges, W = self.s, self.edges, self.W
        bottom_limit = taskbar_top if taskbar_top is not None else self.H
        for gx, gy in find_close_glyphs(self.gray, s):
            if gy > bottom_limit or any(wr.contains(gx, gy) and gy - wr.y < 40 * s for _, wr in self.windows):
                continue
            rl = best_line(edges, False, round(gx + 12 * s), min(W - 1, round(gx + 40 * s)), round(gy), 1, round(60 * s))
            tl = best_line(edges, True, max(0, round(gy - 26 * s)), max(0, round(gy - 7 * s)), round(gx), -1, round(100 * s))
            right = rl[0] if rl else (W if gx + 40 * s >= W - 1 else None)
            top = tl[0] if tl else (0 if gy - 26 * s <= 2 else None)
            if right is None or top is None:
                continue
            bottom = rl[1] if rl else bottom_limit
            if bottom >= self.H - 2:
                bottom = self.H
            bottom = min(bottom, bottom_limit)
            left = tl[1] if tl else 0
            if right - left < 150 * s or bottom - top < 60 * s:
                continue
            wr = Rect(left, top, right - left, bottom - top)
            # caption-bar text: the title on the left, glyphs (min/max/close) on the right
            band = [p for p in self.phrases if not p.used and top <= p.cy <= gy + 12 * s and left <= p.cx <= right]
            title_ps = sorted([p for p in band if p.x2 < gx - 110 * s], key=lambda p: p.x)
            for p in band:
                p.used = True
            title = " ".join(p.text for p in title_ps)
            active = bool(title_ps) and self.ink_level(title_ps[0].box) < 100
            self.windows.append((Window(title=title or "(untitled)", box=wr.box, active=active), wr))
            for r in self.rects:
                if r.y < gy + 16 * s and wr.contains(r.cx, r.cy):     # icon and caption buttons
                    r.used = True
                if abs(r.x - wr.x) <= 4 and abs(r.y - wr.y) <= 4 and abs(r.x2 - wr.x2) <= 4:
                    r.used = True
        self.screen.windows = [w for w, _ in self.windows]

    def window_furniture(self, wr: Rect) -> None:
        s = self.s
        cap_bottom = wr.y + 31 * s
        menu = [p for p in self.phrases if not p.used and wr.contains(p.cx, p.cy)
                and cap_bottom <= p.cy <= cap_bottom + 22 * s
                and not any(not r.used and r.contains(p.cx, p.cy) and r.area < wr.area * 0.5 for r in self.rects)]
        if len(menu) >= 2:
            menu.sort(key=lambda p: p.x)
            e = Element("MENUBAR", (wr.x, round(min(p.y for p in menu) - 4 * s), wr.w,
                                    round(max(p.h for p in menu) + 8 * s)))
            for p in menu:
                p.used = True
                e.items.append(Item(p.text, _pad(p.box, 6 * s, 4 * s)))
            self.elements.append(e)
        status = [p for p in self.phrases if not p.used and wr.contains(p.cx, p.cy)
                  and wr.y2 - 24 * s <= p.cy <= wr.y2]
        if status:
            status.sort(key=lambda p: p.x)
            for p in status:
                p.used = True
            self.elements.append(Element("STATUSBAR", (wr.x, round(wr.y2 - 24 * s), wr.w, round(24 * s)),
                                         text=" | ".join(p.text for p in status)))

    # --- containers ----------------------------------------------------------------------
    def tabs(self) -> None:
        """A row of labels standing on a long horizontal line whose right end turns down
        (the top edge of the tab page)."""
        s = self.s
        for row in _rows(self.free_phrases(), 4 * s):
            if len(row) < 2 or any(b.x - a.x2 > 60 * s for a, b in zip(row, row[1:])):
                continue
            base = max(p.y2 for p in row)
            line = best_line(self.edges, True, round(base + 3 * s), round(base + 14 * s), round(row[-1].cx), 1,
                             round(150 * s))
            if not line:
                continue
            ly, right_end = line
            down = edge_run(self.edges, right_end, ly + 2, 1, horizontal=False)
            if down - ly < 60 * s:
                continue
            left = min(edge_run(self.edges, ly, round(row[-1].cx), -1, horizontal=True), round(row[0].x - 8 * s))
            top = round(min(p.y for p in row) - 6 * s)
            e = Element("TABS", (left, top, right_end - left, ly - top))
            below = self.gray[ly + 3:ly + 3 + round(8 * s), max(0, left):right_end]
            panel_bg = float(np.median(below)) if below.size else 255.0
            best = None
            for p in row:
                p.used = True
                box = _pad(p.box, 6 * s, 4 * s)
                x, y, w, h = box
                patch = self.gray[max(0, y):y + h, max(0, x):x + w]
                d = abs(float(np.median(patch)) - panel_bg) if patch.size else 999.0
                it = Item(p.text, box)
                e.items.append(it)
                if best is None or d < best[0]:
                    best = (d, it)
            best[1].selected = True
            for r in self.rects:
                if not r.used and r.y >= top - 6 * s and r.y2 <= ly + 4 * s and left - 4 <= r.x <= right_end:
                    r.used = True
            self.elements.append(e)

    def groups(self) -> None:
        s = self.s
        for r in sorted(self.rects, key=lambda r: -r.area):
            if r.used or r.w < 80 * s or r.h < 40 * s:
                continue
            cap = [p for p in self.phrases if not p.used and abs(p.cy - r.y) <= 7 * s
                   and r.x + 2 <= p.x <= r.x + 40 * s]
            if not cap:
                continue
            p = min(cap, key=lambda p: p.x)
            p.used = r.used = True
            self.elements.append(Element("GROUP", r.box, text=p.text))

    # --- small toggles -------------------------------------------------------------------
    def _right_phrase(self, x2: float, cy: float) -> Optional[Phrase]:
        s = self.s
        cands = [p for p in self.phrases if not p.used and x2 <= p.x <= x2 + 18 * s and abs(p.cy - cy) <= 7 * s]
        return min(cands, key=lambda p: p.x) if cands else None

    def checkboxes(self) -> None:
        s = self.s
        for r in self.rects:
            if r.used or not (10 * s <= r.w <= 24 * s) or not (0.8 <= r.w / r.h <= 1.25):
                continue
            p = self._right_phrase(r.x2, r.cy)
            if p is None:
                continue
            inner = self.interior(r, max(2, round(2 * s)))
            dark = float((inner < 160).mean()) if inner.size else 0.0
            r.used = p.used = True
            if self._is_circle(r):
                k = max(2, round(r.w * 0.22))
                cx, cy = round(r.cx), round(r.cy)
                core = self.gray[cy - k:cy + k + 1, cx - k:cx + k + 1]
                sel = self.frac(self.accent, cx - k, cy - k, cx + k + 1, cy + k + 1) > 0.3 or \
                    (core.size > 0 and float((core < 140).mean()) > 0.3)
                self.elements.append(Element("RADIO", (r.x, r.y, p.x2 - r.x, max(r.h, p.h)), text=p.text,
                                             state=["selected"] if sel else []))
                continue
            acc = self.frac(self.solid, r.x + 1, r.y + 1, r.x2 - 1, r.y2 - 1)
            checked = acc > 0.25 or dark > 0.25
            e = Element("CHECKBOX", (r.x, r.y, p.x2 - r.x, max(r.h, p.h)), text=p.text,
                        state=["checked" if checked else "unchecked"])
            if self.ink_level(p.box) > 120:
                e.state.append("disabled")
            self.elements.append(e)

    def _is_circle(self, r: Rect) -> bool:
        """A radio button's outline leaves its bounding box corners empty; a checkbox's does not."""
        a = self.gray[r.y:r.y2, r.x:r.x2].astype(int)
        if a.size == 0 or min(a.shape) < 6:
            return False
        if self.frac(self.solid, r.x + 1, r.y + 1, r.x2 - 1, r.y2 - 1) > 0.35:
            # filled with the accent colour (checked / selected): a radio shows a compact white
            # dot in the centre, a checkbox a sparse diagonal tick
            white = (a[2:-2, 2:-2] > 200).astype(np.uint8)
            blobs = components(white, 2)
            if not blobs:
                return False
            b = max(blobs, key=lambda b: b.w * b.h * b.fill)
            c = (white.shape[1] - 1) / 2, (white.shape[0] - 1) / 2
            return (b.fill >= 0.6 and abs(b.x + (b.w - 1) / 2 - c[0]) <= 1.5
                    and abs(b.y + (b.h - 1) / 2 - c[1]) <= 1.5)
        ring = np.concatenate([a[0, :], a[-1, :], a[1:-1, 0], a[1:-1, -1]])
        dark = ring < ring.max() - 60          # the brightest ring pixel is the background
        return 0.15 <= float(dark.mean()) <= 0.6

    def radios(self) -> None:
        s = self.s
        for (x, y, rad) in self.circles:
            # a ring inside or touching a word is a glyph such as the (R) in "Intel(R)", not a button
            if any(w.x - 2 <= x <= w.x2 + 2 and abs(w.cy - y) <= max(w.h, 2 * rad)
                   for p in self.phrases for w in p.words):
                continue
            p = self._right_phrase(x + rad, y)
            if p is None:
                continue
            k = rad * 0.45
            core = self.gray[int(y - k):int(y + k) + 1, int(x - k):int(x + k) + 1]
            acc = self.frac(self.accent, x - k, y - k, x + k + 1, y + k + 1)
            selected = acc > 0.3 or (core.size > 0 and float((core < 140).mean()) > 0.3)
            p.used = True
            bx = round(x - rad)
            self.elements.append(Element("RADIO", (bx, round(y - max(rad, p.h / 2)), p.x2 - bx, round(max(2 * rad, p.h))),
                                         text=p.text, state=["selected"] if selected else []))
            for r in self.rects:   # the circle's outline sometimes also reads as a square
                if not r.used and r.contains(x, y) and r.w < 3 * rad:
                    r.used = True

    # --- lists, sliders, progress bars -------------------------------------------------------
    def lists(self) -> None:
        s = self.s
        for r in sorted(self.rects, key=lambda r: -r.area):
            if r.used or r.w < 60 * s or r.h < 34 * s:
                continue
            # boxes spanning the full width are rows (the selection splits the outline); any
            # other box inside means this is a panel, not a list
            if any(not c.used and c is not r and c.inside(r, 1) and c.area < 0.6 * r.area and c.h > 30 * s
                   and not (abs(c.x - r.x) <= 4 and abs(c.x2 - r.x2) <= 4) for c in self.rects):
                continue
            inside = sorted(self.phrases_in(r), key=lambda p: p.cy)
            sub = self.solid[r.y + 1:r.y2 - 1, r.x + 1:r.x2 - 1]
            bands = [b for b in components(sub, 20) if b.w >= 0.6 * (r.w - 4) and 9 * s <= b.h <= 30 * s]
            if len(inside) + len(bands) < 2:
                continue
            if inside and max(p.x for p in inside) - min(p.x for p in inside) > 0.35 * r.w:
                continue   # not a column of rows
            rows: list[tuple[float, Item]] = []
            for p in inside:
                p.used = True
                rows.append((p.cy, Item(p.text, (r.x + 2, p.y - 1, r.w - 4, p.h + 2))))
            for b in bands:
                by = r.y + 1 + b.y
                hit = [it for cy, it in rows if by <= cy <= by + b.h]
                if hit:
                    for it in hit:
                        it.selected = True
                    continue
                txt, conf = self.ocr.line(self.crop(r.x + 1 + b.x, by, b.w, b.h), invert=True)
                if txt:
                    rows.append((by + b.h / 2, Item(txt, (r.x + 2, by, r.w - 4, b.h), selected=True)))
            rows.sort(key=lambda t: t[0])
            r.used = True
            for c in self.rects:      # inner outlines and the selected row's box
                if not c.used and c.inside(r, 2):
                    c.used = True
            self.elements.append(Element("LIST", r.box, items=[it for _, it in rows]))

    def sliders(self) -> None:
        s = self.s
        taken: list[Rect] = []
        for t in components(self.solid, max(12, round(30 * s * s))):
            # a slider thumb is solid, narrow and tall
            if not (5 * s <= t.w <= 18 * s and 13 * s <= t.h <= 28 * s) or t.fill < 0.5 or t.h < 1.3 * t.w:
                continue
            if any(t.x < p.x2 and p.x < t.x2 and t.y < p.y2 and p.y < t.y2 for p in self.phrases):
                continue
            if any(t.x < o.x2 and o.x < t.x2 and t.y < o.y2 and o.y < t.y2 for o in taken):
                continue
            best = None
            for dy in range(round(-4 * s), round(t.h * 0.6)):
                row = t.y + dy
                left = edge_run(self.edges, row, t.x - 2, -1, horizontal=True, gap=4)
                right = edge_run(self.edges, row, t.x2 + 2, 1, horizontal=True, gap=4)
                if right - left >= 80 * s and (best is None or right - left > best[1] - best[0]):
                    best = (left, right)
            if best is None:
                continue
            left, right = best
            pct = round(100 * (t.cx - left) / max(1, right - left))
            e = Element("SLIDER", (left, t.y, right - left, t.h), value=f"position={pct}%")
            taken.append(Rect(left, t.y, right - left, t.h))
            self.elements.append(e)
            for r in self.rects:
                if not r.used and r.y >= t.y - 8 * s and r.y2 <= t.y2 + 8 * s and r.x >= left - 4 and r.x2 <= right + 4:
                    r.used = True

    def progress(self) -> None:
        s = self.s
        for r in sorted(self.rects, key=lambda r: -r.area):     # the outer bar before its fill
            if r.used or not (8 * s <= r.h <= 30 * s) or r.w < 60 * s or self.phrases_in(r):
                continue
            inner = self.solid[r.y + 2:r.y2 - 2, r.x + 2:r.x2 - 2]
            if inner.size == 0:
                continue
            cols = inner.mean(axis=0) > 0.5
            if not cols[:3].any():
                continue
            if cols.all():
                # the fill and the empty part are separate boxes: join the empty part on the right
                nb = next((c for c in self.rects if not c.used and c is not r and abs(c.x - r.x2) <= 4 * s
                           and abs(c.y - r.y) <= 3 and abs(c.h - r.h) <= 3), None)
                if nb is not None:
                    nb.used = True
                    r = Rect(r.x, min(r.y, nb.y), nb.x2 - r.x, max(r.h, nb.h))
                    inner = self.solid[r.y + 2:r.y2 - 2, r.x + 2:r.x2 - 2]
                    cols = inner.mean(axis=0) > 0.5
            fill = int(np.argmin(cols)) if not cols.all() else len(cols)
            r.used = True
            for c in self.rects:
                if not c.used and c.inside(r, 2):
                    c.used = True
            self.elements.append(Element("PROGRESS", r.box, value=f"{round(100 * fill / len(cols))}%"))

    # --- text boxes, dropdowns, spinners, buttons ----------------------------------------
    def field_like(self) -> None:
        s = self.s
        for r in sorted(self.rects, key=lambda r: (r.y, r.x)):
            if r.used or not (16 * s <= r.h <= 44 * s) or not (28 * s <= r.w <= 520 * s):
                continue
            inside = self.phrases_in(r, margin=1)
            bg = self.bg_level(r)
            kind = None
            default = self.ring_accent(r) > 0.3       # a default button has an accent border
            child = [c for c in self.rects if c is not r and not c.used and c.inside(r, 2)
                     and abs(c.x2 - r.x2) <= 6 * s and c.w <= 22 * s and c.h >= 0.3 * r.h]
            if default:
                pass
            elif child or self._updown(r):
                kind = "SPINNER"
                for c in child:
                    c.used = True
            elif self._chevron(r):
                kind = "DROPDOWN"
            text_box = Rect(r.x, r.y, r.w - (round(20 * s) if kind else 0), r.h)
            value = " ".join(p.text for p in sorted(inside, key=lambda p: p.x)) if inside else ""
            for p in inside:
                p.used = True
            if not value:
                value = self._read_face(text_box, bg)
            if kind is None:
                centred = self._centred(text_box, inside)
                if default or (value and centred):
                    kind = "BUTTON"
                elif bg >= 236 or value:
                    kind = "TEXTBOX"
                else:
                    continue     # an unexplained box
            r.used = True
            e = Element(kind, r.box)
            if kind == "BUTTON":
                e.text = value
                if default:
                    e.state.append("default")
            else:
                if kind in ("SPINNER", "DROPDOWN"):
                    # arrow glyphs at the right edge get read as '-', '^' or 'v'
                    parts = value.split()
                    while parts and (not _ALNUM.search(parts[-1]) or parts[-1] in ("v", "V", "^")):
                        parts.pop()
                    value = " ".join(parts)
                e.value = value
                if bg < 236 and value and self.ink_level(inside[0].box if inside else r.box) > 105:
                    e.state.append("disabled")
            self.elements.append(e)

    def _read_face(self, r: Rect, bg: float) -> str:
        """Read the text on a button face or in a field that the full-screen pass missed."""
        crop = self.crop(r.x + 3, r.y + 3, r.w - 6, r.h - 6)
        txt, conf = self.ocr.line(crop, invert=bg < 110)
        txt = txt.strip("|[]{}()_ ")       # border lines read as bars or brackets
        if conf >= 55 and _ALNUM.search(txt):
            return txt
        txt2, conf2 = self.ocr.line(crop, invert=bg < 110, word=True)
        txt2 = txt2.strip("|[]{}()_ ")
        if _ALNUM.search(txt2) and (conf2 >= 55 or (len(txt2) <= 4 and conf2 >= 30)):
            return txt2
        return ""

    def _chevron(self, r: Rect) -> bool:
        s = self.s
        side = min(r.h, round(22 * s))
        x0 = r.x2 - side
        patch = self.gray[r.y + 3:r.y2 - 3, x0:r.x2 - 2]
        if patch.size == 0:
            return False
        if any(not p.used and x0 <= p.cx <= r.x2 and r.contains(p.cx, p.cy) for p in self.phrases):
            return False
        ink = patch < (np.median(patch) - 60)
        if ink.sum() < 4:
            return False
        ys, xs = np.nonzero(ink)
        w, h = xs.max() - xs.min() + 1, ys.max() - ys.min() + 1
        return 4 * s <= w <= 15 * s and 2 * s <= h <= 9 * s and w > h

    def _updown(self, r: Rect) -> bool:
        """Two small arrow glyphs stacked at the right edge (a spinner)."""
        s = self.s
        x0 = r.x2 - round(18 * s)
        patch = self.gray[r.y + 3:r.y2 - 3, x0:r.x2 - 3]     # stay clear of the border
        if patch.size == 0:
            return False
        ink = (patch < (np.median(patch) - 60)).astype(np.uint8)
        blobs = [b for b in components(ink, 3) if b.w <= 10 * s and b.h <= 7 * s]
        if len(blobs) < 2:
            return False
        blobs.sort(key=lambda b: b.y)
        return blobs[-1].y - blobs[0].y >= 0.3 * r.h and abs(blobs[-1].cx - blobs[0].cx) <= 3 * s

    def _centred(self, r: Rect, inside: list[Phrase]) -> bool:
        if inside:
            cx = (min(p.x for p in inside) + max(p.x2 for p in inside)) / 2
            return abs(cx - r.cx) <= 0.12 * r.w + 3
        a = self.interior(r, 3)
        if a.size == 0:
            return False
        ink = a < (np.median(a) - 60)
        if ink.sum() < 5:
            return False
        xs = np.nonzero(ink)[1]
        cx = (xs.min() + xs.max()) / 2 + r.x + 3
        return abs(cx - r.cx) <= 0.12 * r.w + 3

    # --- labels, links, plain text ---------------------------------------------------------
    def labels(self) -> None:
        s = self.s
        wants = [e for e in self.elements if e.kind in ("TEXTBOX", "DROPDOWN", "SPINNER", "SLIDER", "PROGRESS", "LIST")]
        for e in sorted(wants, key=lambda e: e.box[0]):
            x, y, w, h = e.box
            cy = y + h / 2
            left = [p for p in self.phrases if not p.used and p.x2 <= x + 2 and x - p.x2 <= 260 * s
                    and abs(p.cy - cy) <= max(9 * s, h / 2)]
            if left:
                p = max(left, key=lambda p: p.x2)
            else:
                above = [p for p in self.phrases if not p.used and p.y2 <= y + 2 and y - p.y2 <= 22 * s
                         and abs(p.x - x) <= 30 * s]
                p = max(above, key=lambda p: p.y2) if above else None
            if p is not None:
                p.used = True
                e.label = p.text

    def leftover_text(self) -> None:
        for p in self.phrases:
            if p.used or (p.conf < 55 and len(p.text) < 4):
                continue
            p.used = True
            kind = "LINK" if self.text_is_blue(p.box) else "TEXT"
            self.elements.append(Element(kind, _pad(p.box, 2, 2), text=p.text, sure=p.conf >= 60))

    def taskbar(self, top: int) -> None:
        bar = [e for e in self.elements if e.center[1] >= top and e.kind in ("TEXT", "LINK")]
        e = Element("TASKBAR", (0, top, self.W, self.H - top))
        for t in sorted(bar, key=lambda e: e.box[0]):
            self.elements.remove(t)
            if _TIME.match(t.text):
                e.value = f"clock={t.text}"
            else:
                e.items.append(Item(t.text, t.box))
        self.elements.append(e)

    # --- assemble ------------------------------------------------------------------------
    def place(self) -> None:
        s = self.s
        for g in [e for e in self.elements if e.kind == "GROUP"]:
            gr = Rect(*g.box)
            for e in list(self.elements):
                if e is not g and e.kind != "GROUP" and gr.contains(*e.center):
                    g.children.append(e)
                    self.elements.remove(e)
            g.children.sort(key=lambda e: (round(e.center[1] / (10 * s)), e.box[0]))
        order = sorted(self.elements, key=lambda e: (round(e.center[1] / (10 * s)), e.box[0]))
        wins = sorted(self.windows, key=lambda t: t[1].area)
        for e in order:
            home = next((w for w, wr in wins if wr.contains(*e.center)), None)
            (home.elements if home else self.screen.loose).append(e)


    # --- monospace text (terminals) -------------------------------------------------------
    def terminals(self) -> None:
        """Re-read blocks of monospace text by matching glyphs against the system's monospace fonts.
        That is much more accurate than OCR for the digits in a terminal. A block is used only when
        it is monospace AND fits a real font well; otherwise the OCR reading stays."""
        try:
            from .monospace import read_monospace
        except ImportError:
            return
        for bx, by, bw, bh, cw_hint, pitch_hint in _mono_blocks(self.words_full)[:2]:
            m = 14
            x0, y0 = max(0, bx - m), max(0, by - m)
            x1, y1 = min(self.full.width, bx + bw + m), min(self.full.height, by + bh + m)
            r = read_monospace(np.asarray(self.full.crop((x0, y0, x1, y1))), cell_w_hint=cw_hint,
                               line_pitch_hint=pitch_hint)
            if r is None or r.score > 0.10:
                log.info("monospace block at %s: not accepted (%s)", (bx, by, bw, bh),
                         "no fit" if r is None else f"score {r.score:.3f} with {r.font} {r.cell_w}x{r.cell_h}")
                continue
            nonblank = sum(len(line.replace(" ", "")) for line in r.lines)
            if nonblank < 20 or r.unsure > 0.08 * nonblank:
                log.info("monospace block at %s: too uncertain (%d characters, %d unsure)", (bx, by, bw, bh),
                         nonblank, r.unsure)
                continue
            log.info("monospace block at %s read as TERMINAL: %s %dx%d score %.3f", (bx, by, bw, bh), r.font,
                     r.cell_w, r.cell_h, r.score)
            gx, gy, gw, gh = r.box
            items = [Item(line, (x0 + gx, y0 + gy + i * r.cell_h, max(r.cell_w, len(line) * r.cell_w), r.cell_h))
                     for i, line in enumerate(r.lines) if line.strip()]
            box = (x0 + gx, y0 + gy, gw, gh)
            name = r.font.rsplit(".", 1)[0]
            el = Element("TERMINAL", box, value=f"{name} {r.cell_w}x{r.cell_h}, {len(items)} lines", items=items)
            self._replace_in(box, el)

    def _replace_in(self, box, el: Element) -> None:
        """Swap every element whose centre lies inside `box` for `el`, in the right window."""
        x, y, w, h = box

        def inside(e: Element) -> bool:
            cx, cy = e.center
            return x <= cx <= x + w and y <= cy <= y + h

        for win in self.screen.windows:
            win.elements = [e for e in win.elements if not inside(e)]
        self.screen.loose = [e for e in self.screen.loose if not inside(e)]
        cx, cy = el.center
        home = next((win for win in sorted(self.screen.windows, key=lambda v: v.box[2] * v.box[3])
                     if win.box[0] <= cx <= win.box[0] + win.box[2] and win.box[1] <= cy <= win.box[1] + win.box[3]),
                    None)
        target = home.elements if home is not None else self.screen.loose
        target.append(el)
        target.sort(key=lambda e: (round(e.center[1] / 10), e.box[0]))


def _mono_blocks(words: list[Word]) -> list[tuple[int, int, int, int, float, float]]:
    """Boxes of dense blocks of text whose words all have the same width per character."""
    lines: list[list[Word]] = []
    for w in sorted(words, key=lambda w: w.cy):
        for ln in lines:
            cy = sum(v.cy for v in ln) / len(ln)
            if abs(w.cy - cy) < 0.4 * max(max(v.h for v in ln), w.h):
                ln.append(w)
                break
        else:
            lines.append([w])
    boxes = []
    for ln in lines:
        ln.sort(key=lambda w: w.x)
        boxes.append([min(w.x for w in ln), min(w.y for w in ln), max(w.x2 for w in ln),
                      max(w.y2 for w in ln), list(ln)])
    boxes.sort(key=lambda b: b[1])
    blocks: list[list] = []                       # x, y, x2, y2, words, n_lines
    for x, y, x2, y2, ln in boxes:
        h = y2 - y
        for b in blocks:
            if y - b[3] <= 2.6 * max(h, 12) and x < b[2] and x2 > b[0] and abs(x - b[0]) <= 120:
                b[0], b[1], b[2], b[3] = min(b[0], x), min(b[1], y), max(b[2], x2), max(b[3], y2)
                b[4].extend(ln)
                b[5] += 1
                break
        else:
            blocks.append([x, y, x2, y2, list(ln), 1])
    out = []
    for x, y, x2, y2, ws, n_lines in blocks:
        if n_lines < 4 or len(ws) < 10:
            continue
        ratios = [w.w / len(w.text) for w in ws if len(w.text) >= 4]
        if len(ratios) < 8:
            continue
        med = float(np.median(ratios))
        if sum(1 for r in ratios if abs(r - med) <= 0.12 * med) < 0.8 * len(ratios):
            continue                              # proportional text: character widths vary
        centres = sorted({round(w.cy) for w in ws})            # one value per text line (approximately)
        merged = []
        for c in centres:
            if not merged or c - merged[-1] > 0.5 * float(np.median([w.h for w in ws])):
                merged.append(c)
        pitch = float(np.median(np.diff(merged))) if len(merged) >= 3 else 0.0
        out.append((x, y, x2 - x, y2 - y, med * 1.02, pitch))
    return sorted(out, key=lambda b: -b[2] * b[3])

def _rows(phrases: list[Phrase], tol: float) -> list[list[Phrase]]:
    rows: list[list[Phrase]] = []
    for p in sorted(phrases, key=lambda p: p.cy):
        for row in rows:
            if abs(row[0].cy - p.cy) <= tol:
                row.append(p)
                break
        else:
            rows.append([p])
    for row in rows:
        row.sort(key=lambda p: p.x)
    return rows


def _pad(box, px: float, py: float) -> tuple[int, int, int, int]:
    x, y, w, h = box
    return (round(x - px), round(y - py), round(w + 2 * px), round(h + 2 * py))
