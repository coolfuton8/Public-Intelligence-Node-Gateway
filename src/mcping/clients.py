"""Per-computer access tokens.

Each computer that is invited gets its own token. The device keeps only a hash of it, plus a label, the
time it was created and when it was last seen. A token can be revoked on its own, without touching the
others or the master token in /etc/mcping/mcping.env (which keeps working as the administrator's key).

    cid, token = create("Ana's laptop")     # token is returned once and never stored
    client = verify(token)                  # {"id": ..., "label": ...} or None
    revoke(cid)

    mcping-clients list | revoke <id-or-label>
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import time
from contextlib import contextmanager
from pathlib import Path

DEFAULT_PATH = Path(os.environ.get("MCPING_CLIENTS", "/var/lib/mcping/clients.json"))
TOUCH_EVERY = 60.0                      # seconds between "last seen" writes for one client

_cache: dict[Path, tuple[int, dict]] = {}
_last_touch: dict[str, float] = {}


def _hash(token: str) -> str:
    return hashlib.sha256(("mcping-client-v1:" + token).encode()).hexdigest()


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            data = {"clients": []}
            if path.exists():
                try:
                    data = json.loads(path.read_text())
                except (OSError, ValueError):
                    pass
            data.setdefault("clients", [])
            yield data
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1))
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _read(path: Path) -> dict:
    """The store, re-read only when the file changed (every request calls verify)."""
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return {"clients": []}
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {"clients": []}
    _cache[path] = (mtime, data)
    return data


def create(label: str = "", path: Path | None = None, now: float | None = None) -> tuple[str, str]:
    """A new client: returns (id, token). The token is shown once; only its hash is kept."""
    path = path or DEFAULT_PATH
    now = time.time() if now is None else now
    cid = secrets.token_hex(4)
    token = secrets.token_urlsafe(32)
    label = " ".join(label.split())[:60] or f"computer-{cid}"
    with _locked(path) as data:
        data["clients"].append({"id": cid, "label": label, "hash": _hash(token), "created": now,
                                "last_seen": 0.0, "last_ip": "", "revoked": False})
    return cid, token


def verify(token: str, path: Path | None = None) -> dict | None:
    """The (non-revoked) client this token belongs to, or None."""
    path = path or DEFAULT_PATH
    if not token or len(token) > 200:
        return None
    h = _hash(token)
    found = None
    for c in _read(path).get("clients", []):          # no early exit: same work for any token
        if hmac.compare_digest(c["hash"], h) and not c.get("revoked"):
            found = c
    return found


def is_active(cid: str, path: Path | None = None) -> bool:
    path = path or DEFAULT_PATH
    return any(c["id"] == cid and not c.get("revoked") for c in _read(path).get("clients", []))


def list_clients(path: Path | None = None) -> list[dict]:
    path = path or DEFAULT_PATH
    return [dict(c) for c in _read(path).get("clients", [])]


def revoke(ident: str, path: Path | None = None, now: float | None = None) -> int:
    """Revoke by id or exact label. Returns how many clients were revoked."""
    path = path or DEFAULT_PATH
    now = time.time() if now is None else now
    n = 0
    with _locked(path) as data:
        for c in data["clients"]:
            if not c.get("revoked") and ident in (c["id"], c["label"]):
                c["revoked"], c["revoked_at"] = True, now
                n += 1
    _cache.pop(path, None)
    return n


def touch(cid: str, ip: str = "", path: Path | None = None, now: float | None = None) -> None:
    """Record that a client was just seen (rate limited, so ordinary traffic does not hit the disk)."""
    path = path or DEFAULT_PATH
    now = time.time() if now is None else now
    if now - _last_touch.get(cid, 0.0) < TOUCH_EVERY:
        return
    _last_touch[cid] = now
    try:
        with _locked(path) as data:
            for c in data["clients"]:
                if c["id"] == cid:
                    c["last_seen"], c["last_ip"] = now, ip[:64]
    except OSError:
        pass


def ago(t: float, now: float | None = None) -> str:
    if not t:
        return "never"
    s = int((time.time() if now is None else now) - t)
    if s < 90:
        return "just now"
    if s < 5400:
        return f"{s // 60} min ago"
    if s < 172800:
        return f"{s // 3600} h ago"
    return f"{s // 86400} days ago"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mcping-clients", description="Computers that have their own access token")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list")
    r = sub.add_parser("revoke")
    r.add_argument("ident", help="client id or label")
    a = ap.parse_args(argv)
    if a.cmd == "revoke":
        n = revoke(a.ident)
        print(f"revoked {n} client(s)" if n else f"no active client {a.ident!r}")
        return 0 if n else 1
    rows = list_clients()
    if not rows:
        print("no per-computer tokens yet (use Create invite in MCPing Connect)")
    for c in rows:
        state = "REVOKED" if c.get("revoked") else "active"
        print(f"{c['id']}  {c['label']:<28} {state:<8} created {ago(c['created'])}, last seen {ago(c['last_seen'])}"
              + (f" from {c['last_ip']}" if c.get("last_ip") else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
