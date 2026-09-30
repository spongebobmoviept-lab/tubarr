"""The optional Trimarr add-on, proxied under /api/trimarr/... (API.md "Trimarr"), plus Import preview (read-only).

Trimarr ships with Tubarr (docker-compose.yml starts both) and is reached at TRIMARR_URL, default
http://trimarr:8791; set TRIMARR_URL=off to hide it (every /api/trimarr/... call then answers 404 not_found and the
UI shows "Trimarr isn't installed"). The shared secret is created automatically (tubarr/trimlink.py: a random token
in the shared trimarr-link volume); TRIMARR_TOKEN in the environment overrides it. Trimarr is reached only over the
internal Docker network; every call carries X-Trimarr-Token, ignores proxy environment variables and never follows
redirects. The proxy translates Trimarr's own API (trimarr/trimarr/api.py) into the small shapes the UI's
web/js/trimarr.js expects.

Trimarr holds no Plex credentials. After it replaces a file (trim or undo) the trim poller below asks Plex, with
Tubarr's own scoped token, for a partial scan of that folder (and an analyze of the episode when its key is known).
"""
import csv
import io
import logging
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from urllib.parse import quote

import requests

from .. import config, plexhttp, redact, trimlink
from . import store

log = logging.getLogger("tubarr.webapp.addons")


class ProxyError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


DEFAULT_TRIMARR_URL = "http://trimarr:8791"


def _trimarr_url(env):
    """TRIMARR_URL, default http://trimarr:8791 (the compose service); empty, "off", "none" or "0" = no Trimarr."""
    v = env.get("TRIMARR_URL")
    v = DEFAULT_TRIMARR_URL if v is None else v.strip()
    return "" if v.lower() in ("", "off", "none", "0", "false", "no") else v.rstrip("/")


TRIMARR_URL = _trimarr_url(os.environ)
TRIMARR_TOKEN = (os.environ.get("TRIMARR_TOKEN") or "").strip()     # optional override; normally the link file
MIN_TOKEN_LEN = 16
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
RATING_KEY = re.compile(r"\d{1,12}")
redact.remember(TRIMARR_TOKEN)        # never in logs or error messages


def _token():
    """The token Tubarr sends: TRIMARR_TOKEN if set (it wins), else the auto-created link token (made if missing)."""
    if TRIMARR_TOKEN:
        return TRIMARR_TOKEN
    tok = trimlink.ensure_token()
    redact.remember(tok)
    return tok

_SESSION = requests.Session()
_SESSION.trust_env = False            # never route Tubarr -> Trimarr through HTTP(S)_PROXY / .netrc


def _call(method, path, timeout, **kw):
    """One request to Trimarr (token header, no proxies, no redirects) -> (status, json body)."""
    if not TRIMARR_URL:
        raise ProxyError(404, "not_found", "Trimarr isn't installed.")
    token = _token()
    if len(token) < MIN_TOKEN_LEN:
        if TRIMARR_TOKEN:
            raise ProxyError(503, "not_configured", "TRIMARR_TOKEN is too short: use at least %d characters (the same "
                             "value for Tubarr and Trimarr), or remove it to use the automatic link." % MIN_TOKEN_LEN)
        raise ProxyError(503, "not_configured", "Tubarr couldn't create the Trimarr link token: the shared trimarr-link "
                         "volume isn't mounted at %s or isn't writable. Use the docker-compose.yml from the release, "
                         "then run: docker compose up -d" % trimlink.LINK_DIR)
    try:
        r = _SESSION.request(method, TRIMARR_URL + path, timeout=timeout, allow_redirects=False,
                             headers={"X-Trimarr-Token": token}, **kw)
    except requests.RequestException:
        raise ProxyError(502, "internal", "Tubarr can't reach Trimarr.") from None
    try:
        body = r.json()
    except ValueError:
        body = {}
    if 300 <= r.status_code < 400:
        raise ProxyError(502, "internal", "Trimarr answered with a redirect; Tubarr doesn't follow it.")
    if r.status_code >= 400:
        err = ((body or {}).get("error") or {}) if isinstance(body, dict) else {}
        raise ProxyError(r.status_code, err.get("code") or "internal", err.get("message") or "Trimarr refused that.")
    return r.status_code, body


def _t(method, path, **kw):
    return _call(method, path, 10, **kw)[1]


def _status():
    s = _t("GET", "/api/status")
    try:
        ch = _t("GET", "/api/channels")
        n = sum(1 for c in ch.get("channels") or [] if c.get("auto_trim"))
    except ProxyError:
        n = None
    sec = s.get("seconds") or {}
    w = s.get("worker") or {}
    last = w.get("last_pass")
    return {"enabled": bool(s.get("enabled")), "paused": bool(s.get("paused")),
            "trimmed_videos": sec.get("trimmed_videos", 0), "removed_seconds": sec.get("removed", 0),
            "would_trim_videos": sec.get("would_trim_videos"), "would_remove_seconds": sec.get("would_remove"),
            "queue": len(w.get("queue") or []), "channels_enabled": n,
            "last_run_at": (last or {}).get("finished_at") or (last or {}).get("at") if isinstance(last, dict) else None,
            "version": s.get("version")}


# The Trim ads page talks to Trimarr's own API through /api/trimarr/raw/<path> (same origin); only these routes pass.
RAW = [("GET", r"status|settings|channels|videos|videos/[A-Za-z0-9_-]{11}|report|history"),
       ("PATCH", r"settings|channels/[\w:%-]+"),
       ("POST", r"videos/[A-Za-z0-9_-]{11}/(trim|undo|approve|check)|originals/approve|pass|pause|resume")]


def raw(method, path, body, query):
    path = path.strip("/")
    if not any(m == method and re.fullmatch(pat, path) for m, pat in RAW):
        raise ProxyError(404, "not_found", "Trimarr doesn't have that.")
    kw = {"params": {k: v for k, v in (query or {}).items()}} if method == "GET" else {"json": body or {}}
    status, out = _call(method, "/api/" + path, 15, **kw)
    if method != "GET":
        TRIM_INDEX.poke()
    return status, out


# ------------------------------------------------------------------ Plex refresh after a Trimarr swap
_PLEX_SECTION = {}


def _plex(method, path, timeout=20, **kw):
    """One Plex request with Tubarr's own token (from the vault); never follows redirects."""
    r = plexhttp.request(method, config.PLEX_URL + path, timeout=timeout,     # no redirects, no env proxies
                         headers={"X-Plex-Token": config.PLEX_TOKEN, "Accept": "application/json"}, **kw)
    if r.status_code >= 300:
        raise RuntimeError("Plex HTTP %d on %s" % (r.status_code, path))
    return r


def _plex_section_key():
    want = (config.PLEX_URL, config.PLEX_SECTION)
    if _PLEX_SECTION.get("for") != want:                    # the setup page can change these live
        dirs = _plex("GET", "/library/sections").json()["MediaContainer"].get("Directory") or []
        key = next((d["key"] for d in dirs if d.get("title") == config.PLEX_SECTION), None)
        if key is None:
            raise RuntimeError("Plex has no library called %r" % config.PLEX_SECTION)
        _PLEX_SECTION.clear()
        _PLEX_SECTION.update({"for": want, "key": str(key)})
    return _PLEX_SECTION["key"]


def _tubarr_video(vid):
    """(library path, Plex ratingKey) from Tubarr's own database (never from Trimarr), or (None, None)."""
    r = store.conn().execute("SELECT path, plex_rating_key FROM videos WHERE id=?", (vid,)).fetchone()
    if not r or not r["path"] or not r["path"].startswith(config.ROOT + "/") or "/../" in r["path"]:
        return None, None
    return r["path"], r["plex_rating_key"]


class _TrimIndex:
    """Trimarr's trimmed videos, refreshed every minute (and soon after a trim/undo), for the Trimmed badges. The
    same loop tells Plex about files Trimarr replaced (see the module docstring)."""

    PLEX_WINDOW = 6 * 3600          # swaps this recent are (re)announced to Plex, also after a Tubarr restart
    ANALYZE_AFTER = 90              # seconds between the folder scan and the analyze of the episode
    MAX_TRIES = 10

    def __init__(self):
        self.at = 0.0
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.started = False
        self.plex_done = {}         # {video_id: swapped_ts} already announced
        self.plex_tries = {}        # {(video_id, swapped_ts): failed attempts}
        self.plex_analyze = {}      # {rating_key: due_at}

    def refresh(self):
        from . import mapping
        try:
            vids = _t("GET", "/api/videos", params={"state": "trimmed", "limit": 500}).get("videos") or []
        except ProxyError:
            return
        out = {}
        for v in vids:
            t = v.get("trimmed") or {}
            out[v["id"]] = {"state": "trimmed", "removed_seconds": round(t.get("removed_seconds") or v.get("cut_seconds") or 0, 1),
                            "undo_available": bool(t.get("undo_available"))}
        mapping.TRIMS.clear()
        mapping.TRIMS.update(out)
        self.at = time.time()
        try:
            self.plex_pass()
        except Exception as e:                              # never let Plex trouble stop the badge refresh
            log.warning("Plex refresh after Trimarr swaps: %s", e)

    def plex_pass(self):
        if not config.plex_enabled():
            return
        now = time.time()
        since = now - self.PLEX_WINDOW
        try:
            vids = _t("GET", "/api/videos", params={"state": "trimmed,undone", "swapped_since": since,
                                                    "limit": 500}).get("videos") or []
        except ProxyError:
            return
        folders = {}
        for v in vids:
            vid, ts = v.get("id"), v.get("swapped_ts")
            if not isinstance(vid, str) or not VIDEO_ID.fullmatch(vid) or not isinstance(ts, (int, float)):
                continue
            if self.plex_done.get(vid) == ts or self.plex_tries.get((vid, ts), 0) >= self.MAX_TRIES:
                continue
            path, rk = _tubarr_video(vid)
            if not path:
                self.plex_done[vid] = ts                    # not one of Tubarr's videos: nothing to refresh
                continue
            folders.setdefault(os.path.dirname(path), []).append((vid, ts, rk))
        for folder, items in folders.items():
            try:
                _plex("GET", "/library/sections/%s/refresh" % _plex_section_key(),
                      params={"path": config.PLEX_ROOT + folder[len(config.ROOT):]})
            except Exception as e:
                log.warning("Plex refresh after a Trimarr swap failed: %s", e)
                for vid, ts, _ in items:
                    self.plex_tries[(vid, ts)] = self.plex_tries.get((vid, ts), 0) + 1
                continue
            for vid, ts, rk in items:
                self.plex_done[vid] = ts
                self.plex_tries.pop((vid, ts), None)
                if rk and RATING_KEY.fullmatch(str(rk)):
                    self.plex_analyze[str(rk)] = now + self.ANALYZE_AFTER
        for rk, due in list(self.plex_analyze.items()):
            if now >= due:
                self.plex_analyze.pop(rk, None)
                try:
                    _plex("PUT", "/library/metadata/%s/analyze" % rk)
                except Exception as e:
                    log.warning("Plex analyze after a Trimarr swap failed: %s", e)
        for vid in [k for k, t in self.plex_done.items() if t < since]:
            del self.plex_done[vid]
        for k in [k for k in self.plex_tries if k[1] < since]:
            del self.plex_tries[k]

    def loop(self):
        while True:
            self.refresh()
            self.wake.wait(60)
            self.wake.clear()
            time.sleep(1)

    def start(self):
        if TRIMARR_URL and not self.started:
            self.started = True
            if TRIMARR_TOKEN and len(TRIMARR_TOKEN) < MIN_TOKEN_LEN:
                log.error("TRIMARR_TOKEN is shorter than %d characters: Trimarr will refuse Tubarr's requests. Use a "
                          "longer one (the same for both containers), or remove it to use the automatic link.",
                          MIN_TOKEN_LEN)
            elif not TRIMARR_TOKEN and not _token():
                log.info("No Trimarr link folder at %s (running without the compose file's trimarr-link volume): "
                         "the Trim ads page stays unavailable.", trimlink.LINK_DIR)
            threading.Thread(target=self.loop, name="trim-index", daemon=True).start()

    def poke(self):
        self.wake.set()


TRIM_INDEX = _TrimIndex()


def trimarr(method, path, body, query):
    parts = [p for p in path.split("/") if p]
    if parts and parts[0] == "raw":
        return raw(method, "/".join(parts[1:]), body, query)
    if parts == ["status"] and method == "GET":
        return 200, _status()
    if parts == ["settings"] and method == "PATCH":
        _t("PATCH", "/api/settings", json={"enabled": bool((body or {}).get("enabled"))})
        return 200, _status()
    if len(parts) == 2 and parts[0] == "channels":
        cid = parts[1]
        if cid in (".", "..") or len(cid) > 200:
            raise ProxyError(404, "not_found", "Trimarr doesn't have that.")
        if method == "GET":
            ch = _t("GET", "/api/channels")
            c = next((x for x in ch.get("channels") or [] if x.get("id") == cid), None)
            if c is None:
                on = bool(ch.get("enabled")) and bool(ch.get("channel_default"))
                return 200, {"channel_id": cid, "enabled": on, "own": None}
            return 200, {"channel_id": cid, "enabled": bool(c.get("auto_trim")), "own": c.get("trim"),
                         "trimmed": c.get("trimmed"), "removed_seconds": c.get("removed_seconds")}
        if method == "PATCH":
            c = _t("PATCH", "/api/channels/%s" % quote(cid, safe=":"), json={"trim": bool((body or {}).get("enabled"))})
            return 200, {"channel_id": cid, "enabled": bool(c.get("auto_trim", c.get("trim"))), "own": c.get("trim")}
    if len(parts) == 3 and parts[0] == "videos" and parts[2] in ("trim", "undo") and method == "POST":
        if not VIDEO_ID.fullmatch(parts[1]):
            raise ProxyError(404, "not_found", "Not a YouTube video ID.")
        r = _t("POST", "/api/videos/%s/%s" % (parts[1], parts[2]), json={})
        TRIM_INDEX.poke()
        return 202, {"video_id": parts[1], "state": "queued", "removed_seconds": None,
                     "ok": True, "message": r.get("message")}
    if parts == ["report"] and method == "GET":
        r = _t("GET", "/api/report", params={"channel": query.get("channel")} if query.get("channel") else None)
        tot = r.get("totals") or {}
        w = tot.get("would_trim") or {}
        return 200, {"videos": w.get("videos", 0), "seconds": w.get("seconds", 0), "bytes_saved": None,
                     "channels": sum(1 for c in r.get("by_channel") or [] if c.get("would_trim_videos")),
                     "dry_run": True, "checked": tot.get("checked"), "not_checked": tot.get("not_checked")}
    raise ProxyError(404, "not_found", "Trimarr doesn't have that.")


# ------------------------------------------------------------------ Import (paste) preview, read-only
_PREVIEWS = OrderedDict()           # import_id -> (created_at, preview), oldest first
_PREVIEW_TTL = 3600
_PREVIEW_MAX = 20
_PLOCK = threading.Lock()
HANDLE = re.compile(r"(?<![\w.])@([A-Za-z0-9][A-Za-z0-9._\-·]{1,40})")
CHANNEL_ID = re.compile(r"\b(UC[\w-]{22})\b")
LEGACY = re.compile(r"youtube\.com/(c|user)/([A-Za-z0-9._\-]{1,100})")
IGNORE = re.compile(r"^@(gmail|youtube|google|googlemail|outlook|yahoo|hotmail)\b", re.I)


def _parse(text, html):
    """-> {key: {"handle", "channel_id", "title", "subscribers"}} from a pasted page, Takeout CSV or Tubarr's CSV."""
    found = {}
    t = (text or "").lstrip("﻿")
    first = t.splitlines()[0].lower() if t.strip() else ""
    if "channel id" in first or "handle" in first and "," in first:
        for row in csv.DictReader(io.StringIO(t)):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            cid = row.get("channel id") or None
            h = row.get("handle") or None
            url = row.get("channel url") or ""
            if not h and "/@" in url:
                h = "@" + url.split("/@", 1)[1].split("/")[0]
            if cid or h:
                found[(cid or h).lower()] = {"handle": h, "channel_id": cid, "title": row.get("channel title")
                                             or row.get("title") or None, "subscribers": None}
        return found
    for src in (html or "", t):
        for m in HANDLE.finditer(src):
            h = "@" + m.group(1).rstrip(".-·")
            if IGNORE.match(h):
                continue
            found.setdefault(h.lower(), {"handle": h, "channel_id": None, "title": None, "subscribers": None})
        for m in CHANNEL_ID.finditer(src):
            found.setdefault(m.group(1).lower(), {"handle": None, "channel_id": m.group(1), "title": None,
                                                  "subscribers": None})
        for m in LEGACY.finditer(src):                    # old-style youtube.com/c/Name and /user/Name links
            url = "https://www.youtube.com/%s/%s" % (m.group(1), m.group(2))
            found.setdefault(url.lower(), {"handle": url, "channel_id": None, "title": None, "subscribers": None})
    return found


def import_preview(snap, text, html):
    found = _parse(text, html)
    by_handle = {(ch["handle"] or "").lower(): cid for cid, ch in snap.channels.items() if ch["handle"]}
    by_handle.update({h.lower(): h for h in snap.pseudo})
    known = set(snap.channels)
    new, matched = [], set()
    for f in found.values():
        cid = f["channel_id"] if f["channel_id"] in known else by_handle.get((f["handle"] or "").lower())
        if cid:
            matched.add(cid)
        else:
            new.append(f)
    missing = []
    for cid, ch in snap.channels.items():
        if cid in matched or ch["pending_removal_at"] or ch["gone_at"] or ch["source"] == "manual":
            continue
        missing.append({"channel_id": cid, "title": ch["title"], "handle": ch["handle"]})
    lines = [x for x in (text or "").splitlines() if x.strip()]
    iid = "imp_" + secrets.token_hex(4)
    with _PLOCK:
        now = time.time()
        for k in [k for k, (t, _) in _PREVIEWS.items() if now - t > _PREVIEW_TTL]:
            del _PREVIEWS[k]
        while len(_PREVIEWS) >= _PREVIEW_MAX:
            _PREVIEWS.popitem(last=False)                  # evict the oldest
        _PREVIEWS[iid] = (now, {"new": new, "missing": missing})
    return {"import_id": iid, "found": len(found), "new": new, "missing": missing if found else [],
            "unchanged": len(matched), "unrecognized_lines": 0 if found else len(lines)}


def import_known(iid):
    with _PLOCK:
        x = _PREVIEWS.get(iid)
        return x is not None and time.time() - x[0] <= _PREVIEW_TTL
