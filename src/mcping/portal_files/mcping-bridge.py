#!/usr/bin/env python3
"""MCPing stdio bridge: lets an app that only starts local (stdio) MCP servers, such as the Claude
desktop app's connectors list, use the MCPing device over HTTPS.

The app starts this script; it reads MCP messages from stdin, POSTs each to the device's /mcp endpoint
with the access token and writes the replies to stdout. Standard library only.

Settings (environment variables, set by the setup script in the app's config):
  MCPING_URL      https://mcping.local:8765/mcp
  MCPING_TOKEN    this computer's access token
  MCPING_CA       path to the device CA certificate (mcping-ca.crt)
"""
import json
import os
import ssl
import sys
import threading
import urllib.error
import urllib.request

URL = os.environ.get("MCPING_URL", "https://mcping.local:8765/mcp")
TOKEN = os.environ.get("MCPING_TOKEN", "")
CA = os.environ.get("MCPING_CA") or os.environ.get("NODE_EXTRA_CA_CERTS") or ""
TIMEOUT = float(os.environ.get("MCPING_TIMEOUT", "300"))

_out = threading.Lock()
_session = {"id": None}


def _send(obj) -> None:
    with _out:
        sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def _context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if CA and os.path.exists(CA):
        ctx.load_verify_locations(CA)
    return ctx


def _error(msg_id, text: str) -> None:
    if msg_id is not None:
        _send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32000, "message": text}})
    else:
        sys.stderr.write(f"mcping-bridge: {text}\n")


def _handle(raw: str, ctx: ssl.SSLContext) -> None:
    try:
        msg = json.loads(raw)
    except ValueError:
        return
    msg_id = msg.get("id") if isinstance(msg, dict) else None
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "Authorization": "Bearer " + TOKEN}
    if _session["id"]:
        headers["Mcp-Session-Id"] = _session["id"]
    req = urllib.request.Request(URL, data=raw.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                _session["id"] = sid
            ctype = resp.headers.get("Content-Type", "")
            if "text/event-stream" in ctype:
                data = []
                for line in resp:
                    line = line.decode("utf-8", "replace").rstrip("\r\n")
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip())
                    elif line == "" and data:
                        _emit("\n".join(data))
                        data = []
                if data:
                    _emit("\n".join(data))
            else:
                body = resp.read().decode("utf-8", "replace").strip()
                if body:
                    _emit(body)
    except urllib.error.HTTPError as exc:
        _error(msg_id, f"device answered HTTP {exc.code}" + (" (access token refused)" if exc.code == 401 else ""))
    except Exception as exc:  # network, TLS, timeout
        _error(msg_id, f"cannot reach the device at {URL}: {exc}")


def _emit(text: str) -> None:
    try:
        obj = json.loads(text)
    except ValueError:
        return
    for item in (obj if isinstance(obj, list) else [obj]):
        _send(item)


def main() -> int:
    ctx = _context()
    workers = []
    for line in sys.stdin:
        line = line.strip()
        if line:
            t = threading.Thread(target=_handle, args=(line, ctx), daemon=True)
            t.start()
            workers.append(t)
            workers = [w for w in workers if w.is_alive()]
    for t in workers:                     # stdin closed: let answers still in flight arrive
        t.join(TIMEOUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
