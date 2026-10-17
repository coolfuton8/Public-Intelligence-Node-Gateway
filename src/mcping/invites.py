"""One-time invite codes: a short code, easy to type, that hands a new computer its own access token
once, within a minute, without the token ever being typed or pasted.

    code = create()            # "K7QX2", shown on the device: https://mcping.local/join/K7QX2
    redeem("k7qx2")            # True once, then never again

A 5-character code is only about 25 bits, so its safety comes from the limits around it, not from its
length: it expires after 60 seconds, works once, and MAX_FAILURES wrong codes within FAIL_WINDOW seconds
cancel every pending invite and lock redemption for LOCK_SECONDS. That leaves an attacker at most two
guesses against one live code (about 1 in 14 million). Creating a new invite (which needs the device's
desktop or a shell on it) cancels every earlier code and clears a lock-out. Only a hash of each code is stored.
MCPING_INVITE_LEN and MCPING_INVITE_TTL raise the length or the lifetime if you want more margin.

    mcping-invite [--ttl SECONDS]     create an invite from a shell on the device
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

ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"       # 31 symbols: no 0, 1, I, L or O to misread
CODE_LEN = int(os.environ.get("MCPING_INVITE_LEN", "5"))      # 5 x log2(31) = about 25 bits
DEFAULT_TTL = int(os.environ.get("MCPING_INVITE_TTL", "60"))  # seconds
MAX_FAILURES = 2                                    # two wrong tries, then everything is cancelled
FAIL_WINDOW = 600
LOCK_SECONDS = 300

DEFAULT_PATH = Path(os.environ.get("MCPING_INVITES", "/var/lib/mcping/invites.json"))


def normalise(code: str) -> str:
    """Accept lower case, dashes and spaces; nothing else is changed."""
    return "".join(ch for ch in code.upper() if ch.isalnum())


def _hash(code: str) -> str:
    return hashlib.sha256(("mcping-invite-v1:" + normalise(code)).encode()).hexdigest()


def display(code: str) -> str:
    """As shown to people: short codes as they are, longer ones in groups of four."""
    if len(code) <= 6:
        return code
    return "-".join(code[i:i + 4] for i in range(0, len(code), 4))


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(".lock")
    with open(lock, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            state = {"invites": [], "failures": [], "locked_until": 0.0}
            if path.exists():
                try:
                    state.update(json.loads(path.read_text()))
                except (OSError, ValueError):
                    pass
            yield state
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state))
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _prune(state: dict, now: float) -> None:
    state["invites"] = [i for i in state["invites"] if i["expires"] > now and not i.get("used")]
    state["failures"] = [t for t in state["failures"] if now - t < FAIL_WINDOW]


def create(ttl: int = DEFAULT_TTL, path: Path | None = None, now: float | None = None, *,
           label: str = "") -> str:
    """A new invite code (uppercase, no separators). Only its hash is stored. `label` names the
    computer the invite is for; it becomes the label of that computer's own token."""
    path = path or DEFAULT_PATH
    now = time.time() if now is None else now
    code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))
    with _locked(path) as st:
        # Whoever is at the device can always ask for a fresh code: it replaces every earlier one and
        # clears any lock-out and failure count.
        st["invites"] = [{"hash": _hash(code), "expires": now + ttl, "used": False,
                          "label": " ".join(label.split())[:60]}]
        st["failures"] = []
        st["locked_until"] = 0.0
    return code


def pending(path: Path | None = None, now: float | None = None) -> int:
    path = path or DEFAULT_PATH
    now = time.time() if now is None else now
    with _locked(path) as st:
        _prune(st, now)
        return len(st["invites"])


def redeem(code: str, path: Path | None = None, now: float | None = None) -> bool:
    """True exactly once for a valid, unexpired invite. Wrong codes are counted and can lock out."""
    return redeem_info(code, path, now) is not None


def redeem_info(code: str, path: Path | None = None, now: float | None = None) -> dict | None:
    """Like redeem(), but returns {"label": ...} for the invite that was used, or None."""
    path = path or DEFAULT_PATH
    now = time.time() if now is None else now
    with _locked(path) as st:
        _prune(st, now)
        if now < float(st.get("locked_until", 0.0)):
            return None
        h = _hash(code)
        for inv in st["invites"]:
            if hmac.compare_digest(inv["hash"], h):
                inv["used"] = True                    # marked before the caller learns of the success
                st["invites"] = [i for i in st["invites"] if not i.get("used")]
                return {"label": inv.get("label", "")}
        st["failures"].append(now)
        if len(st["failures"]) >= MAX_FAILURES:
            st["invites"] = []                        # cancel everything pending
            st["failures"] = []
            st["locked_until"] = now + LOCK_SECONDS
        return None


def locked(path: Path | None = None, now: float | None = None) -> bool:
    path = path or DEFAULT_PATH
    now = time.time() if now is None else now
    with _locked(path) as st:
        return now < float(st.get("locked_until", 0.0))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mcping-invite", description=__doc__.splitlines()[0])
    ap.add_argument("--ttl", type=int, default=DEFAULT_TTL, help="seconds the invite stays valid")
    ap.add_argument("--address", default="mcping.local")
    ap.add_argument("--name", default="", help="name of the computer this invite is for")
    a = ap.parse_args(argv)
    code = create(a.ttl, label=a.name)
    when = f"{a.ttl // 60} minutes" if a.ttl >= 120 and a.ttl % 60 == 0 else f"{a.ttl} seconds"
    print(f"https://{a.address}/join/{display(code)}   (valid {when}, works once)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
