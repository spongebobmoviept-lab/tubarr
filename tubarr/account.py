"""YouTube push-back health, kept in /data/account.json so the worker and the web app see the same thing.

States:
  ok          downloads work normally.
  flagged     YouTube said "Sign in to confirm you're not a bot" / UNPLAYABLE / 429. The worker slows right down
              on its own (FLAGGED_GAP_H between downloads, one at a time) and goes back to the normal pace after
              OK_TO_CLEAR successful downloads in a row. Nobody needs to do anything.
  signed_out  kept for compatibility (signed-in downloads with account cookies aren't part of this build).
"""
import json
import logging
import os
import re
import threading
import time

from . import config

log = logging.getLogger("tubarr.account")

PATH = os.path.join(config.DATA, "account.json")
COOKIES = os.path.join(config.DATA, "cookies.txt")
FLAGGED_GAP_H = (0.5, 1.5)          # 30-90 min between downloads while flagged
OK_TO_CLEAR = 3                     # successful downloads in a row that end "flagged"
SLOW_STATES = ("flagged", "signed_out")   # the worker keeps the slow, one-at-a-time pace in both

FLAGGED = re.compile(r"not a bot|confirm you.?re not a bot|UNPLAYABLE|HTTP Error 429|Too Many Requests|rate.?limit",
                     re.I)
SIGNED_OUT = re.compile(r"cookies are no longer valid|cookies have (likely )?been rotated|LOGIN_INFO|"
                        r"account cookies are no longer|you (have been|were) signed out", re.I)

_LOCK = threading.Lock()
DEFAULT = {"state": "ok", "since": None, "detail": None, "ok_streak": 0, "alerted_at": None, "last_ok_at": None}


def _read():
    try:
        with open(PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(d):
    tmp = PATH + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, PATH)


def load():
    out = dict(DEFAULT)
    out.update(_read())
    # fresh cookies put in place after the sign-out (new export, or a file copied in by hand) end it
    if out["state"] == "signed_out" and out["since"]:
        try:
            if os.path.getmtime(COOKIES) > out["since"] + 5:
                out.update(state="ok", since=time.time(), detail="new cookies are in place", ok_streak=0,
                           alerted_at=None)
                _write(out)
        except OSError:
            pass
    return out


def _set(state, detail):
    """Change state; returns True when it actually changed."""
    with _LOCK:
        d = load()
        if d["state"] == state:
            if state != "ok":
                d["ok_streak"] = 0
                _write(d)
            return False
        if d["state"] == "signed_out" and state == "flagged":
            return False                               # signed out is the stronger, human-needed state
        d.update(state=state, since=time.time(), detail=(detail or "")[:300], ok_streak=0)
        if state != "signed_out":
            d["alerted_at"] = None
        _write(d)
    log.warning("DOWNLOAD ACCOUNT -> %s: %s", state, (detail or "")[:200])
    if state == "signed_out":
        _alert_once()
    return True


def classify(msg):
    m = str(msg or "")
    if SIGNED_OUT.search(m):
        return "signed_out"
    if FLAGGED.search(m):
        return "flagged"
    return None


def note_failure(msg):
    """A download / check error. Returns the state it moved to, or None if the error says nothing about the
    account (network hiccups, members-only, ...)."""
    st = classify(msg)
    if st:
        _set(st, str(msg))
    return st


def note_warning(msg):
    """yt-dlp warning lines: only the cookie-rejected ones matter."""
    if SIGNED_OUT.search(str(msg or "")):
        _set("signed_out", str(msg))


def note_success():
    with _LOCK:
        d = load()
        d["last_ok_at"] = time.time()
        if d["state"] == "flagged":
            d["ok_streak"] = int(d.get("ok_streak") or 0) + 1
            if d["ok_streak"] >= OK_TO_CLEAR:
                d.update(state="ok", since=time.time(), detail="%d downloads in a row worked" % d["ok_streak"],
                         ok_streak=0)
                log.warning("DOWNLOAD ACCOUNT -> ok after %d good downloads in a row", OK_TO_CLEAR)
        _write(d)


def mark_signed_in(detail="the download account was signed in again"):
    with _LOCK:
        d = load()
        if d["state"] != "signed_out":               # a flagged account stays on the slow pace until it recovers
            return
        d.update(state="ok", since=time.time(), detail=detail, ok_streak=0, alerted_at=None)
        _write(d)


def _alert_once():
    """One Discord message per sign-out (the notifications webhook from Settings, event 'signin_needed')."""
    with _LOCK:
        d = _read()
        if d.get("alerted_at") or d.get("alerting"):
            return
    try:
        from . import notify
        sent = notify.send("signin_needed",
                    "Tubarr: YouTube rejected the download account's cookies, so downloads may fail.")
    except Exception as e:
        log.warning("sign-out alert not sent: %s", e)
        return
    if sent:                                         # only a message that really went out counts: no repeats
        with _LOCK:
            d = _read()
            d["alerted_at"] = time.time()
            _write(d)


def ensure_alert():
    """Called every few seconds by the worker: a sign-out whose message couldn't go out yet (no webhook set, Discord
    down) is sent once as soon as it can be."""
    d = _read()
    if d.get("state") == "signed_out" and not d.get("alerted_at"):
        _alert_once()


def public():
    """For status.json and the API."""
    d = load()
    return {"state": d["state"], "since": d["since"], "detail": d["detail"], "ok_streak": d["ok_streak"],
            "ok_needed": OK_TO_CLEAR, "alerted": bool(d.get("alerted_at")), "last_ok_at": d.get("last_ok_at"),
            "slow_gap_minutes": [int(FLAGGED_GAP_H[0] * 60), int(FLAGGED_GAP_H[1] * 60)]}
