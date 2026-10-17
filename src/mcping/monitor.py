"""Live monitor: a web page on the device that shows what the MCP is doing.

    https://mcping.local:8765/monitor

Shows the frames the MCP grabs from the capture card (scaled down), a marker where it last
clicked, optional boxes for the elements found by describe_screen, and a text log of every
tool call with its arguments (typed text included) and result.

State lives in memory only. Nothing here is written to disk. The page sits behind the same
token as the MCP endpoint: sign in once with the token and it keeps an HTTP-only cookie.
"""

from __future__ import annotations

import collections
import asyncio
import contextvars
import hashlib
import hmac
import json
import threading
import time

import numpy as np
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from . import clients as _clients

FRAME_WIDTH = 960
FRAME_QUALITY = 60
LOG_LINES = 400
COOKIE = "mcping_monitor"


def classify_signal(luma: float, contrast: float) -> tuple[str, str]:
    """Judge a captured frame from its mean brightness and standard deviation (0..255)."""
    if luma < 8 and contrast < 4:
        return "warn", "frame is black: no HDMI signal, or the target is asleep or showing a black screen"
    if contrast < 1.5:
        return "warn", (f"frame is one flat {'dark ' if luma < 60 else ''}colour (brightness {luma:.0f}): "
                        "no HDMI signal, or the target's screen is blank or asleep")
    return "ok", f"picture present (brightness {luma:.0f}, contrast {contrast:.0f})"


current_client: contextvars.ContextVar[str] = contextvars.ContextVar("mcping_client", default="")
clients_path = _clients.DEFAULT_PATH        # where the per-computer tokens live (tests point this elsewhere)


def cookie_value(token: str, client_id: str = "master") -> str:
    """Value of the sign-in cookie: "<client id>.<signature>", derived from the master token with
    the client's id, never a token itself. Revoking the client invalidates it."""
    sig = hmac.new(token.encode(), b"mcping-monitor-v2:" + client_id.encode(), hashlib.sha256).hexdigest()
    return f"{client_id}.{sig}"


def cookie_client(value: str, token: str) -> tuple[str, str] | None:
    """(id, label) when the cookie is genuine and its computer still has access, else None."""
    cid = value.partition(".")[0]
    if not cid or not hmac.compare_digest(value.encode(), cookie_value(token, cid).encode()):
        return None
    if cid == "master":
        return ("master", "master")
    for c in _clients.list_clients(clients_path):
        if c["id"] == cid and not c.get("revoked"):
            return (cid, c["label"])
    return None


def _flat(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


def summarise(result) -> tuple[str, int]:
    """One-line summary of a tool result and its number of lines."""
    if isinstance(result, str):
        text = result
    elif isinstance(result, (list, tuple)):
        strings = [r for r in result if isinstance(r, str)]
        text = " ".join(strings) + ("" if all(isinstance(r, str) for r in result) else " [image]")
    elif isinstance(result, dict):
        text = json.dumps(result, default=str)
    else:
        text = str(result)
    return _flat(text), text.count("\n") + 1


class Monitor:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: collections.deque = collections.deque(maxlen=LOG_LINES)
        self._seq = 0
        self._ver = 0
        self.current = ""                       # tool being run, for frame attribution
        self._frame: bytes | None = None
        self._frame_meta = {"seq": 0, "t": 0.0, "w": 0, "h": 0, "by": "", "luma": None, "contrast": None}
        self._marker: dict | None = None
        self._elements: list = []
        self._elements_ver = 0
        self._thumb: np.ndarray | None = None            # 64x36 grayscale of the current frame
        self._elements_thumb: np.ndarray | None = None   # ... of the frame the boxes came from
        self._viewer_t = 0.0                    # last time a browser asked for anything

    def touch(self) -> None:
        """A browser is watching (called by the web handlers)."""
        self._viewer_t = time.time()

    def idle_refresh_due(self, interval: float, viewer_window: float = 30.0, now: float | None = None) -> bool:
        """True when someone is watching and the last frame is at least `interval` seconds old."""
        now = time.time() if now is None else now
        with self._lock:
            watching = now - self._viewer_t <= viewer_window
            age = now - self._frame_meta["t"] if self._frame_meta["seq"] else float("inf")
        return watching and age >= interval

    # --- written by the server -----------------------------------------------------------
    def begin(self, tool: str, args: dict, client: str | None = None) -> dict:
        if client is None:
            client = current_client.get()
        with self._lock:
            self._seq += 1
            self._ver += 1
            ev = {"seq": self._seq, "ver": self._ver, "t": time.time(), "tool": tool, "args": args,
                  "status": "running", "ms": None, "result": "", "lines": 0, "client": client}
            self._events.append(ev)
            self.current = tool
            return ev

    def end(self, ev: dict, result=None, error: str | None = None) -> None:
        with self._lock:
            self._ver += 1
            ev["ver"] = self._ver
            ev["ms"] = round((time.time() - ev["t"]) * 1000)
            if error is not None:
                ev["status"], ev["result"], ev["lines"] = "error", _flat(error), 1
            else:
                ev["status"] = "ok"
                ev["result"], ev["lines"] = summarise(result)
            self.current = ""

    def publish_frame(self, img) -> None:
        """Called with every PIL image the server grabs from the capture card."""
        from PIL import ImageStat

        from .capture import to_jpeg
        try:
            data, (w, h) = to_jpeg(img, FRAME_WIDTH, FRAME_QUALITY)
            small = img.convert("L").resize((64, 36))
            stat = ImageStat.Stat(small)
            luma, contrast = float(stat.mean[0]), float(stat.stddev[0])
            thumb = np.asarray(small, dtype=np.int16)
        except Exception:  # noqa: BLE001 - the monitor must never break a tool call
            return
        with self._lock:
            self._frame = data
            self._thumb = thumb
            m = self._frame_meta
            self._frame_meta = {"seq": m["seq"] + 1, "t": time.time(), "w": w, "h": h, "by": self.current,
                                "luma": round(luma, 1), "contrast": round(contrast, 1)}

    def frame_info(self) -> dict:
        with self._lock:
            return dict(self._frame_meta)

    def set_marker(self, marker: dict | None) -> None:
        if marker is None:
            return
        clamp = lambda v: max(0.0, min(1.0, float(v)))  # noqa: E731
        m = {k: (clamp(v) if k in ("x", "y", "x0", "y0") else v) for k, v in marker.items()}
        with self._lock:
            self._marker = {**m, "t": time.time()}

    def set_elements(self, screen) -> None:
        out = []
        try:
            sw, sh = float(screen.width), float(screen.height)
            for w in screen.windows:
                x, y, bw, bh = w.box
                out.append({"id": w.id, "kind": "WINDOW", "text": w.title, "x": x / sw, "y": y / sh,
                            "w": bw / sw, "h": bh / sh})
            for e in screen.elements():
                x, y, bw, bh = e.box
                out.append({"id": e.id, "kind": e.kind, "text": e.text or e.label or "", "x": x / sw,
                            "y": y / sh, "w": bw / sw, "h": bh / sh})
        except Exception:  # noqa: BLE001
            return
        with self._lock:
            self._elements, self._elements_ver = out, self._elements_ver + 1
            # remember what the screen looked like when these boxes were found
            self._elements_thumb = None if self._thumb is None else self._thumb.copy()

    def elements_fresh(self) -> bool:
        """True while the picture still matches the one the element boxes were detected on.
        A clock tick or a blinking cursor changes a few thumbnail pixels; a window opening or
        closing changes many."""
        if self._thumb is None or self._elements_thumb is None:
            return False
        d = np.abs(self._thumb - self._elements_thumb)
        return bool(d.mean() < 3.0 and (d > 25).mean() < 0.015)

    # --- read by the web handlers --------------------------------------------------------
    def state(self, since: int, elements_since: int) -> dict:
        with self._lock:
            out = {"now": time.time(), "ver": self._ver, "frame": dict(self._frame_meta),
                   "marker": dict(self._marker) if self._marker else None,
                   "events": [dict(e) for e in self._events if e["ver"] > since],
                   "elements_fresh": self.elements_fresh()}
            if self._elements_ver != elements_since:
                out["elements"] = {"ver": self._elements_ver, "list": list(self._elements)}
            return out

    def frame(self) -> bytes | None:
        with self._lock:
            return self._frame


monitor = Monitor()
_token = ""


def set_token(token: str) -> None:
    global _token
    _token = token


# --- web handlers (registered with FastMCP.custom_route) ---------------------------------------
async def page(request: Request) -> Response:
    return HTMLResponse(PAGE, headers={"Cache-Control": "no-store"})


async def login(request: Request) -> Response:
    try:
        body = await request.json()
        supplied = str(body.get("token", ""))
    except Exception:  # noqa: BLE001
        supplied = ""
    who = None
    if _token and hmac.compare_digest(supplied.encode(), _token.encode()):
        who = "master"
    else:
        c = _clients.verify(supplied, clients_path)
        who = c["id"] if c else None
    if not _token or who is None:
        await asyncio.sleep(0.5)                  # slows guessing without blocking the server
        return JSONResponse({"ok": False}, status_code=401)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, cookie_value(_token, who), max_age=30 * 86400, path="/monitor", httponly=True,
                    samesite="strict", secure=request.url.scheme == "https")
    return resp


async def api_state(request: Request) -> Response:
    q = request.query_params
    try:
        since, ev = int(q.get("since", 0)), int(q.get("ev", -1))
    except ValueError:
        since, ev = 0, -1
    monitor.touch()
    return JSONResponse(monitor.state(since, ev), headers={"Cache-Control": "no-store"})


async def api_frame(request: Request) -> Response:
    monitor.touch()
    data = monitor.frame()
    if data is None:
        return Response(status_code=204)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


_health_fn = None     # () -> list of checks; cheap, never grabs a frame
_test_fn = None       # () -> list of checks; the full "Test connectivity" run


async def _run_checks(fn) -> Response:
    import anyio
    if fn is None:
        return JSONResponse({"checks": []})
    try:
        checks = await anyio.to_thread.run_sync(fn)
    except Exception as exc:  # noqa: BLE001
        checks = [{"id": "monitor", "label": "Diagnostics", "status": "fail", "detail": f"{type(exc).__name__}: {exc}"}]
    return JSONResponse({"checks": checks, "t": time.time()}, headers={"Cache-Control": "no-store"})


async def api_health(request: Request) -> Response:
    monitor.touch()
    return await _run_checks(_health_fn)


async def api_test(request: Request) -> Response:
    return await _run_checks(_test_fn)


def register(mcp, health=None, test=None) -> None:
    global _health_fn, _test_fn
    _health_fn, _test_fn = health, test
    mcp.custom_route("/monitor", methods=["GET"])(page)
    mcp.custom_route("/monitor/login", methods=["POST"])(login)
    mcp.custom_route("/monitor/api/state", methods=["GET"])(api_state)
    mcp.custom_route("/monitor/api/frame.jpg", methods=["GET"])(api_frame)
    mcp.custom_route("/monitor/api/health", methods=["GET"])(api_health)
    mcp.custom_route("/monitor/api/test", methods=["POST"])(api_test)


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MCP Intelligence Node Gateway (MCPing) Monitor</title>
<style>
:root { --bg:#f5f6f8; --panel:#fff; --ink:#1b1f24; --mute:#667085; --line:#d0d5dd; --accent:#1f6feb;
        --ok:#1a7f37; --bad:#c62828; --mark:#e5322d; --box:#1f6feb; }
@media (prefers-color-scheme: dark) { :root { --bg:#12151a; --panel:#1b2027; --ink:#e6e9ee; --mute:#98a2b3;
        --line:#323a45; --accent:#58a6ff; --ok:#3fb950; --bad:#ff6b6b; --mark:#ff5c57; --box:#58a6ff; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.4 system-ui,"Segoe UI",sans-serif; }
header { display:flex; flex-wrap:wrap; gap:6px 18px; align-items:baseline; padding:10px 16px;
         border-bottom:1px solid var(--line); background:var(--panel); }
h1 { font-size:16px; margin:0; }
#conn { font-weight:600; } #conn.ok { color:var(--ok); } #conn.bad { color:var(--bad); }
.mute { color:var(--mute); }
main { display:grid; grid-template-columns:minmax(0,1fr) minmax(320px,440px); gap:12px; padding:12px 16px; }
@media (max-width:860px) { main { grid-template-columns:1fr; } }
section { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:10px; min-width:0; }
.bar { display:flex; flex-wrap:wrap; gap:6px 14px; align-items:center; margin-bottom:8px; }
.stage { position:relative; display:block; line-height:0; background:#000; border-radius:4px; overflow:hidden; }
.stage img { width:100%; height:auto; display:block; }
.stage canvas { position:absolute; inset:0; width:100%; height:100%; pointer-events:none; }
.empty { padding:60px 10px; text-align:center; color:var(--mute); line-height:1.4; }
textarea { width:100%; height:calc(100vh - 150px); min-height:300px; resize:vertical; padding:8px;
           background:var(--bg); color:var(--ink); border:1px solid var(--line); border-radius:4px;
           font:12.5px/1.45 ui-monospace,Consolas,monospace; }
#login { display:none; max-width:360px; margin:60px auto; }
#login input { width:100%; padding:8px; margin:8px 0; border:1px solid var(--line); border-radius:4px;
               background:var(--bg); color:var(--ink); }
button { padding:7px 14px; border:0; border-radius:4px; background:var(--accent); color:#fff; font-weight:600;
         cursor:pointer; }
button:disabled { opacity:.6; cursor:wait; }
button.plain { background:none; color:var(--ink); padding:3px 10px; font-weight:400;
               border:1px solid var(--line); border-radius:6px; }
button.plain:hover, button.ghost:hover { border-color:var(--accent); }
button.plain b { font-size:inherit; }
button.ghost { background:none; color:var(--ink); border:1px solid var(--line); padding:3px 10px; font-weight:500; }
.diaghead { display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; font-size:13px; }
.hdiag { display:inline-flex; flex-wrap:wrap; gap:6px 14px; align-items:center; }
body.login .hdiag { display:none; }
body.dc #diag, body.dc #testinfo { display:none; }          /* collapsed: only the header row remains */
body:not(.dc) .mini { display:none; }                          /* expanded: the cards show the same */
.mini { display:inline-flex; flex-wrap:nowrap; gap:4px 10px; font-size:12.5px; color:var(--mute); white-space:nowrap; }
.mini em { font-style:normal; }
@media (max-width:1100px) { h1 { font-size:14px; } .mini em { display:none; } .mini i { margin-right:0; width:11px; height:11px; } }
.mini i { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:5px; background:var(--mute); }
.mini i.ok { background:var(--ok); } .mini i.warn { background:#d99100; } .mini i.fail { background:var(--bad); }
.checks { display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:8px; }
.chk { display:flex; gap:9px; align-items:flex-start; border:1px solid var(--line); border-radius:6px; padding:8px 10px; }
.dot { flex:none; width:12px; height:12px; border-radius:50%; margin-top:4px; background:var(--mute); }
.dot.ok { background:var(--ok); } .dot.warn { background:#d99100; } .dot.fail { background:var(--bad); }
.chk b { display:block; font-size:13.5px; } .chk span { color:var(--mute); font-size:12.5px; line-height:1.35; display:block; }
.pill { padding:2px 10px; border-radius:999px; font-size:12.5px; font-weight:600; background:var(--line); }
.pill.ok { background:var(--ok); color:#fff; } .pill.warn { background:#d99100; color:#fff; }
.pill.fail { background:var(--bad); color:#fff; }
</style></head><body>
<header>
  <h1>MCP Intelligence Node Gateway (MCPing) Monitor</h1>
  <span id="conn" class="bad">connecting...</span>
  <span class="hdiag">
    <button id="difftoggle" class="plain" aria-expanded="false" aria-controls="checks" title="Show or hide the checks">
      <span id="chev">&#9656;</span> <b>Device checks</b></button>
    <span id="overall" class="pill">checking...</span>
    <span id="mini" class="mini"></span>
    <button id="testbtn">Test connectivity</button>
    <span class="mute" id="testinfo"></span>
  </span>
  <span id="frameinfo" class="mute"></span>
  <span id="callinfo" class="mute"></span>
</header>
<div id="login"><section>
  <b>Sign in</b>
  <div class="mute">Enter the device token (the one in /etc/mcping/mcping.env).</div>
  <input id="token" type="password" autocomplete="off" placeholder="token">
  <button id="signin">Sign in</button> <span id="loginmsg" class="mute"></span>
</section></div>
<main id="app">
  <section id="diag" style="grid-column:1 / -1">
    <div class="diaghead">
      <span class="mute">Details of the last check</span>
      <button id="hidebtn" class="ghost" title="Collapse the device checks">&#9652; Hide details</button>
    </div>
    <div id="checks" class="checks"></div>
  </section>
  <section>
    <div class="bar">
      <b>Target screen</b>
      <label><input type="checkbox" id="showel"> Show detected elements</label>
      <span id="elnote" class="mute"></span>
      <label><input type="checkbox" id="showmark" checked> Mark clicks</label>
    </div>
    <div class="stage" id="stage">
      <div class="empty" id="empty">No frame yet.<br>A frame appears each time the MCP grabs the screen
        (screenshot, describe_screen and similar).</div>
      <img id="shot" alt="" hidden>
      <canvas id="over"></canvas>
    </div>
  </section>
  <section>
    <div class="bar"><b>MCP calls</b><span class="mute" id="count"></span></div>
    <textarea id="log" readonly spellcheck="false"></textarea>
  </section>
</main>
<script>
const $ = id => document.getElementById(id);
let since = 0, evSince = -1, frameSeq = -1, serverOff = 0, marker = null, elements = [], lastOk = 0;
let elementsFresh = false;   // the picture still matches the one the boxes were detected on
const events = new Map();

function fmtArgs(a) {
  return Object.entries(a || {}).map(([k, v]) => k + "=" + JSON.stringify(v)).join(" ");
}
function fmtTime(t) {
  const d = new Date(t * 1000), p = n => String(n).padStart(2, "0");
  return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
}
function renderLog() {
  const list = [...events.values()].sort((a, b) => a.seq - b.seq);
  const lines = list.map(e => {
    const who = e.client ? "  [" + e.client + "]" : "";
    const head = fmtTime(e.t) + who + "  " + e.tool + (Object.keys(e.args || {}).length ? "  " + fmtArgs(e.args) : "");
    if (e.status === "running") return head + "\n    ...running";
    const dur = e.ms >= 1000 ? (e.ms / 1000).toFixed(1) + " s" : e.ms + " ms";
    const more = e.lines > 1 ? " (" + e.lines + " lines)" : "";
    return head + "\n    " + (e.status === "error" ? "ERROR: " : "-> ") + e.result + more + "  [" + dur + "]";
  });
  const box = $("log"), stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
  box.value = lines.join("\n");
  if (stick) box.scrollTop = box.scrollHeight;
  $("count").textContent = list.length + " calls";
}
function draw() {
  const cv = $("over"), img = $("shot");
  const W = cv.clientWidth, H = cv.clientHeight;
  if (cv.width !== W || cv.height !== H) { cv.width = W; cv.height = H; }
  const g = cv.getContext("2d");
  g.clearRect(0, 0, W, H);
  if (img.hidden) return;
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  if ($("showel").checked && elementsFresh) {
    g.lineWidth = 1.5; g.font = "11px ui-monospace,Consolas,monospace"; g.strokeStyle = css("--box");
    for (const e of elements) {
      const x = e.x * W, y = e.y * H, w = e.w * W, h = e.h * H;
      g.strokeRect(x, y, w, h);
      const label = e.id;
      const tw = g.measureText(label).width + 6;
      g.fillStyle = css("--box"); g.fillRect(x, Math.max(0, y - 14), tw, 14);
      g.fillStyle = "#fff"; g.fillText(label, x + 3, Math.max(11, y - 3));
    }
  }
  if ($("showmark").checked && marker) {
    const age = Date.now() / 1000 + serverOff - marker.t;
    if (age >= 0 && age < 5) {
      g.globalAlpha = 1 - age / 5; g.strokeStyle = css("--mark"); g.fillStyle = css("--mark"); g.lineWidth = 3;
      const x = marker.x * W, y = marker.y * H;
      if (marker.x0 !== undefined) {
        g.beginPath(); g.moveTo(marker.x0 * W, marker.y0 * H); g.lineTo(x, y); g.stroke();
      }
      g.beginPath(); g.arc(x, y, 14 + age * 4, 0, 6.2832); g.stroke();
      g.beginPath(); g.arc(x, y, 3.5, 0, 6.2832); g.fill();
      g.globalAlpha = 1;
    }
  }
}
const RANK = {fail: 3, warn: 2, unknown: 1, ok: 0};
function renderChecks(list, tested) {
  const box = $("checks");
  box.innerHTML = "";
  let worst = "ok";
  for (const c of list) {
    if (RANK[c.status] > RANK[worst]) worst = c.status;
    const d = document.createElement("div"); d.className = "chk";
    const dot = document.createElement("i"); dot.className = "dot " + c.status;
    const t = document.createElement("div");
    const b = document.createElement("b"); b.textContent = c.label;
    const s = document.createElement("span"); s.textContent = c.detail;
    t.append(b, s); d.append(dot, t); box.append(d);
  }
  const mini = $("mini");
  mini.innerHTML = "";
  const SHORT = {service: "Service", esp32: "ESP32", link: "Target", capture: "Capture", signal: "HDMI"};
  for (const c of list) {
    const s = document.createElement("span"); s.title = c.label + ": " + c.detail;
    const i = document.createElement("i"); i.className = c.status;
    const t = document.createElement("em"); t.textContent = SHORT[c.id] || c.label;
    s.append(i, t);
    mini.append(s);
  }
  const p = $("overall");
  const words = {ok: "all OK", warn: "check needed", fail: "problem", unknown: "partly unknown"};
  p.textContent = list.length ? words[worst] : "no data";
  p.className = "pill " + (worst === "unknown" ? "" : worst);
  $("testinfo").textContent = (tested ? "Tested " : "Checked ") + new Date().toLocaleTimeString();
}
async function loadHealth() {
  try {
    const r = await fetch("/monitor/api/health", {cache: "no-store"});
    if (r.ok) renderChecks((await r.json()).checks, false);
  } catch (err) { /* the main poll shows the connection state */ }
}
async function runTest() {
  const btn = $("testbtn");
  btn.disabled = true; btn.textContent = "Testing...";
  try {
    const r = await fetch("/monitor/api/test", {method: "POST"});
    if (r.ok) { renderChecks((await r.json()).checks, true); setCollapsed(false, false); }  // show the details of a test
    else $("testinfo").textContent = "Test failed (HTTP " + r.status + ")";
  } catch (err) {
    $("testinfo").textContent = "Test failed: device unreachable";
  }
  btn.disabled = false; btn.textContent = "Test connectivity";
}
$("testbtn").addEventListener("click", runTest);
function setCollapsed(on, remember) {
  document.body.classList.toggle("dc", on);
  $("difftoggle").setAttribute("aria-expanded", String(!on));
  $("chev").innerHTML = on ? "&#9656;" : "&#9662;";
  if (remember) { try { localStorage.setItem("mcping.diag.collapsed", on ? "1" : "0"); } catch (e) { /* private mode */ } }
  setTimeout(draw, 0);
}
let startCollapsed = true;          // collapsed by default: the picture gets the room
try { startCollapsed = localStorage.getItem("mcping.diag.collapsed") !== "0"; } catch (e) { /* ignore */ }
setCollapsed(startCollapsed, false);
$("difftoggle").addEventListener("click", () => setCollapsed(!document.body.classList.contains("dc"), true));
$("hidebtn").addEventListener("click", () => setCollapsed(true, true));
loadHealth();
setInterval(loadHealth, 10000);

function showLogin(on) {
  document.body.classList.toggle("login", on);
  $("login").style.display = on ? "block" : "none";
  $("app").style.display = on ? "none" : "grid";
}
async function poll() {
  let wait = 700;
  try {
    const r = await fetch("/monitor/api/state?since=" + since + "&ev=" + evSince, {cache: "no-store"});
    if (r.status === 401) { showLogin(true); wait = 1500; $("conn").textContent = "sign in required"; $("conn").className = "bad"; }
    else {
      showLogin(false);
      const s = await r.json();
      serverOff = s.now - Date.now() / 1000;
      since = s.ver; lastOk = Date.now();
      for (const e of s.events) events.set(e.seq, e);
      if (s.elements) { elements = s.elements.list; evSince = s.elements.ver; }
      elementsFresh = s.elements_fresh === true;
      $("elnote").textContent = (elements.length && !elementsFresh)
        ? "boxes hidden: the screen changed since describe_screen" : "";
      marker = s.marker;
      if (s.frame.seq && s.frame.seq !== frameSeq) {
        frameSeq = s.frame.seq;
        $("shot").src = "/monitor/api/frame.jpg?seq=" + frameSeq;
      }
      const f = s.frame;
      $("frameinfo").textContent = f.seq ? "frame " + f.w + "x" + f.h + ", " +
        Math.max(0, Math.round(Date.now() / 1000 + serverOff - f.t)) + " s ago" + (f.by ? " by " + f.by : "") : "";
      $("conn").textContent = "connected"; $("conn").className = "ok";
      renderLog();
    }
  } catch (err) {
    $("conn").textContent = "device unreachable"; $("conn").className = "bad";
  }
  setTimeout(poll, wait);
}
$("shot").addEventListener("load", () => { $("shot").hidden = false; $("empty").hidden = true; draw(); });
$("showel").addEventListener("change", draw);
$("showmark").addEventListener("change", draw);
window.addEventListener("resize", draw);
setInterval(draw, 120);
async function signin() {
  $("loginmsg").textContent = "";
  const r = await fetch("/monitor/login", {method: "POST", headers: {"Content-Type": "application/json"},
                                           body: JSON.stringify({token: $("token").value})});
  if (r.ok) { $("token").value = ""; since = 0; evSince = -1; showLogin(false); loadHealth(); }
  else $("loginmsg").textContent = "Wrong token.";
}
$("signin").addEventListener("click", signin);
$("token").addEventListener("keydown", e => { if (e.key === "Enter") signin(); });
poll();
</script></body></html>
"""
