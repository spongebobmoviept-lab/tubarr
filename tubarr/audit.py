"""Security/audit log: who changed what, when (never secrets). /data/audit.log, one JSON object per line, mode 600.

Written for: sign-ins (ok / failed / locked out), sign-outs, password changes, API key changes, the first-run admin
account, Plex connect/disconnect, and every network/proxy change (names and modes only, never URLs or credentials),
plus automatic line switches made by the worker. Shown in Settings -> Account -> Security log.

Noisy entries (failed sign-ins, lockouts, rate limits) are aggregated: at most one line per action per NOISY_WINDOW,
carrying the count of the ones folded into it, so a flood can't push the real history out. The log rotates at
MAX_BYTES and keeps GENERATIONS old files (audit.log.1 ... .N).
"""
import json
import os
import threading
import time

from . import config, redact

PATH = os.path.join(config.DATA, "audit.log")
MAX_BYTES = 2_000_000
GENERATIONS = 5
NOISY = ("auth.login_failed", "auth.locked_out", "auth.password_failed")
NOISY_WINDOW = 60
_LOCK = threading.Lock()
_NOISY = {}                      # action -> [last written (monotonic), folded since]


def _rotate():
    try:
        if os.path.getsize(PATH) <= MAX_BYTES:
            return
    except OSError:
        return
    for i in range(GENERATIONS - 1, 0, -1):
        try:
            os.replace("%s.%d" % (PATH, i), "%s.%d" % (PATH, i + 1))
        except OSError:
            pass
    try:
        os.replace(PATH, PATH + ".1")
    except OSError:
        pass


def _fold(action, detail):
    """-> the detail to write, or None when this noisy action was already logged in the current window."""
    if action not in NOISY:
        return detail
    now = time.monotonic()
    last = _NOISY.get(action)
    if last and now - last[0] < NOISY_WINDOW:
        last[1] += 1
        return None
    folded = last[1] if last else 0
    _NOISY[action] = [now, 0]
    return detail + (" (+%d similar before this)" % folded if folded else "")


def write(who, action, detail=""):
    action = str(action)[:64]
    with _LOCK:
        detail = _fold(action, str(detail or ""))
    if detail is None:
        return
    rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "who": str(who or "system")[:64], "action": action,
           "detail": redact.text(detail)[:500]}
    line = (json.dumps(rec, ensure_ascii=False) + "\n").encode()
    with _LOCK:
        os.makedirs(config.DATA, exist_ok=True)
        _rotate()
        fd = os.open(PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)


def read(limit=100):
    limit = max(1, min(int(limit or 100), 500))
    try:
        with open(PATH, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 256 * 1024))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
        if len(out) >= limit:
            break
    return out
