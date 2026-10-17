"""Screen tree: elements, windows, stable IDs, the compact text format and change diffs."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Optional

Box = tuple[int, int, int, int]  # x, y, w, h in frame pixels

# Element kinds (rendered in upper case)
KINDS = ("WINDOW", "MENUBAR", "TABS", "TEXTBOX", "DROPDOWN", "SPINNER", "CHECKBOX", "RADIO",
         "BUTTON", "LIST", "SLIDER", "PROGRESS", "LINK", "GROUP", "STATUSBAR", "TEXT", "TASKBAR", "TERMINAL")


@dataclass
class Item:
    """A clickable sub-part: a menu entry, tab, list row or taskbar entry."""

    text: str
    box: Box
    selected: bool = False
    id: str = ""


@dataclass
class Element:
    kind: str
    box: Box
    text: str = ""                 # caption shown on/next to it (button, checkbox, link, text)
    label: Optional[str] = None    # field label to its left or above
    value: Optional[str] = None    # contents (text box, dropdown, spinner) or position
    state: list[str] = field(default_factory=list)   # checked, selected, disabled, default
    items: list[Item] = field(default_factory=list)
    children: list["Element"] = field(default_factory=list)  # GROUP contents
    sure: bool = True              # False: rendered with "?" after the kind
    id: str = ""

    @property
    def center(self) -> tuple[float, float]:
        x, y, w, h = self.box
        return (x + w / 2, y + h / 2)

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


@dataclass
class Window:
    title: str
    box: Box
    active: bool = False
    elements: list[Element] = field(default_factory=list)
    id: str = ""


@dataclass
class Screen:
    width: int
    height: int
    windows: list[Window] = field(default_factory=list)
    loose: list[Element] = field(default_factory=list)   # outside any window (desktop, taskbar)
    seconds: float = 0.0
    text_quality: int = 100      # 0-100: share of the text read with high confidence
    captures: int = 1            # how many captures of the screen were read and voted on

    def elements(self):
        for w in self.windows:
            for e in w.elements:
                yield from e.walk()
        for e in self.loose:
            yield from e.walk()

    def find(self, ident: str):
        """Look up an element ("e7"), an item ("e7.3") or a window ("w2")."""
        if ident.startswith("w"):
            return next((w for w in self.windows if w.id == ident), None)
        base, _, sub = ident.partition(".")
        for e in self.elements():
            if e.id == base:
                if not sub:
                    return e
                return next((i for i in e.items if i.id == ident), None)
        return None


# --- stable IDs ----------------------------------------------------------------------------


def _key_text(e) -> str:
    return (getattr(e, "text", "") or getattr(e, "label", "") or getattr(e, "title", "") or "").lower()


class IdRegistry:
    """Keeps element IDs stable from one analysis to the next (same kind, text and place)."""

    def __init__(self) -> None:
        self._next_e = 1
        self._next_w = 1
        self._prev: list[tuple[str, str, str, float, float]] = []   # id, kind, text, cx, cy
        self._prev_w: list[tuple[str, str, Box]] = []

    def assign(self, screen: Screen) -> None:
        tol = max(12.0, screen.width / 120)
        new_prev: list[tuple[str, str, str, float, float]] = []
        taken: set[str] = set()

        def match(kind: str, text: str, cx: float, cy: float) -> Optional[str]:
            best, best_d = None, None
            for pid, pk, pt, px, py in self._prev:
                if pid in taken or pk != kind:
                    continue
                d = abs(px - cx) + abs(py - cy)
                if pt == text and d < 8 * tol or d < tol:
                    if best_d is None or d < best_d:
                        best, best_d = pid, d
            return best

        for w in screen.windows:
            wid = next((pid for pid, t, b in self._prev_w if t == w.title.lower() and pid not in taken), None)
            if wid is None:
                wid = f"w{self._next_w}"
                self._next_w += 1
            taken.add(wid)
            w.id = wid
        self._prev_w = [(w.id, w.title.lower(), w.box) for w in screen.windows]

        for e in screen.elements():
            cx, cy = e.center
            eid = match(e.kind, _key_text(e), cx, cy)
            if eid is None:
                eid = f"e{self._next_e}"
                self._next_e += 1
            taken.add(eid)
            e.id = eid
            for n, it in enumerate(e.items, 1):
                it.id = f"{eid}.{n}"
            new_prev.append((eid, e.kind, _key_text(e), cx, cy))
        self._prev = new_prev


# --- text rendering ------------------------------------------------------------------------


def _q(s: str) -> str:
    return '"' + s.replace('"', "'") + '"'


def _items(e: Element) -> str:
    return " ".join(f"[{i.id} {i.text}{'*' if i.selected else ''}]" for i in e.items)


def _line(e: Element, coords: bool) -> str:
    kind = e.kind + ("?" if not e.sure else "")
    parts = [kind, e.id]
    if e.kind == "TERMINAL":
        if e.value:
            parts.append(e.value)          # e.g. "DejaVuSansMono 8x17, 12 lines"
    elif e.kind in ("MENUBAR", "TABS", "LIST", "TASKBAR"):
        if e.label:
            parts.append(f"label={_q(e.label)}")
        parts.append(_items(e))
        if e.value:
            parts.append(e.value)
    else:
        if e.text:
            parts.append(_q(e.text))
        if e.label:
            parts.append(f"label={_q(e.label)}")
        if e.value is not None:
            parts.append(f"value={_q(e.value)}" if e.kind in ("TEXTBOX", "DROPDOWN", "SPINNER") else e.value)
    parts.extend(e.state)
    if coords:
        cx, cy = e.center
        parts.append(f"@{round(cx)},{round(cy)}")
    return " ".join(p for p in parts if p)


def render_text(screen: Screen, coords: bool = False) -> str:
    out = [f"SCREEN {screen.width}x{screen.height}"]

    def emit(elements: list[Element], indent: str) -> None:
        for e in elements:
            out.append(indent + _line(e, coords))
            if e.kind == "TERMINAL":                       # monospace text, one numbered line each
                for it in e.items:
                    out.append(f"{indent}  {it.id} | {it.text}")
            if e.children:
                emit(e.children, indent + "  ")

    for w in screen.windows:
        head = f"WINDOW {w.id} {_q(w.title)}" + (" active" if w.active else "")
        if coords:
            x, y, ww, hh = w.box
            head += f" box={x},{y},{ww},{hh}"
        out.append(head)
        emit(w.elements, "  ")
    emit(screen.loose, "")
    return "\n".join(out)


def render_json(screen: Screen) -> str:
    return json.dumps(asdict(screen), separators=(",", ":"))


# --- change diff ---------------------------------------------------------------------------


def _snapshot(screen: Screen) -> dict[str, str]:
    snap = {}
    for w in screen.windows:
        snap[w.id] = f"WINDOW {_q(w.title)}" + (" active" if w.active else "")
    for e in screen.elements():
        snap[e.id] = _line(e, coords=False)
        if e.kind == "TERMINAL":                           # report which lines changed
            for it in e.items:
                snap[it.id] = f"LINE {it.id} | {it.text}"
    return snap


def diff(old: Optional[Screen], new: Screen) -> str:
    if old is None:
        return "no previous description; showing everything\n" + render_text(new)
    a, b = _snapshot(old), _snapshot(new)
    lines = []
    for k in b:
        if k not in a:
            lines.append(f"+ {b[k]}")
        elif a[k] != b[k]:
            lines.append(f"~ {b[k]}   (was: {a[k]})")
    for k in a:
        if k not in b:
            lines.append(f"- {a[k]}")
    return "\n".join(lines) if lines else "no changes"
