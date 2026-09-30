"""The HTTP API (stdlib only, same process as the worker). Contract: API.md.

Security model: Trimarr has no published port and no users of its own. Only Tubarr talks to it, over the internal
Docker network, through Tubarr's signed-in proxy (tubarr/webapp/addons.py). Every request except GET /health must
carry the shared secret `X-Trimarr-Token` (env TRIMARR_TOKEN, set in both containers; at least 16 characters).
Without a usable token the API refuses everything but /health (fails closed). The Host header must name an allowed
host (trimarr, localhost, 127.0.0.1, plus TRIMARR_ALLOWED_HOSTS) as a DNS-rebinding defense. No CORS: browsers
never call this API directly. Never returns tokens or secrets."""
import hmac
import json
import logging
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config, db, library, settings, trimmer, views
from .trimmer import iso

log = logging.getLogger("trimarr.api")
_SERVICE = [None]


def port():
    return config.API_PORT


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _video_or_404(vid):
    r = db.video(vid) if library.valid_video_id(vid) else None
    if not r:
        raise ApiError(404, "not_found", "Video not found.")
    return r


def _status():
    svc = _SERVICE[0]
    s = settings.load()
    kb, kn = trimmer.originals_bytes()
    nxt = db.one("SELECT MIN(backup_expires) AS t FROM videos WHERE backup_path IS NOT NULL")
    tr = db.one("SELECT COUNT(*) AS n, COALESCE(SUM(before_duration - after_duration), 0) AS sec FROM videos "
                "WHERE state='trimmed'")
    wt = db.one("SELECT COUNT(*) AS n, COALESCE(SUM(cut_seconds), 0) AS sec FROM videos WHERE state='cuttable'")
    return {
        "version": config.VERSION, "server_time": iso(time.time()),
        "enabled": s["enabled"], "paused": s["paused"],
        "worker": {"state": svc.state if svc else "not_running", "current": svc.current if svc else None,
                   "queue": svc.queued() if svc else [], "last_pass": svc.last_pass if svc else None,
                   "next_pass_at": iso(svc.next_pass_at) if svc and svc.next_pass_at else None},
        "counts": views.status_counts(),
        "seconds": {"removed": round(tr["sec"], 1), "trimmed_videos": tr["n"],
                    "would_remove": round(wt["sec"], 1), "would_trim_videos": wt["n"]},
        "originals": {"count": kn, "bytes": kb, "cap_bytes": int(s["max_originals_gb"] * 1e9),
                      "keep_days": s["keep_originals_days"], "next_expiry_at": iso(nxt["t"]) if nxt and nxt["t"] else None},
        "sponsorblock": {"last_error": svc.sponsorblock_error if svc else None},
    }


def _channels():
    s = settings.load()
    rows = db.query("SELECT channel_id, channel, folder, state, COUNT(*) AS n, COALESCE(SUM(cut_seconds),0) AS cut, "
                    "COALESCE(SUM(before_duration - after_duration),0) AS rem FROM videos WHERE state != 'gone' "
                    "GROUP BY channel_id, state")
    out = {}
    for r in rows:
        own, eff = settings.channel_setting(s, r["channel_id"])
        c = out.setdefault(r["channel_id"], {"id": r["channel_id"], "title": r["channel"], "folder": r["folder"],
                                             "trim": own, "auto_trim": eff, "videos": 0, "trimmed": 0,
                                             "removed_seconds": 0.0, "cuttable": 0, "would_remove_seconds": 0.0,
                                             "suspicious": 0})
        c["videos"] += r["n"]
        if r["state"] == "trimmed":
            c["trimmed"] += r["n"]
            c["removed_seconds"] = round(c["removed_seconds"] + r["rem"], 1)
        elif r["state"] == "cuttable":
            c["cuttable"] += r["n"]
            c["would_remove_seconds"] = round(c["would_remove_seconds"] + r["cut"], 1)
        elif r["state"] == "suspicious":
            c["suspicious"] += r["n"]
    return {"channel_default": s["channel_default"], "enabled": s["enabled"],
            "channels": sorted(out.values(), key=lambda c: (c["title"] or "").lower())}


def _videos(q):
    s = settings.load()
    sql, args = "SELECT * FROM videos WHERE 1=1", []
    if q.get("state"):
        states = q["state"][0].split(",")
        sql += " AND state IN (%s)" % ",".join("?" * len(states))
        args += states
    else:
        sql += " AND state != 'gone'"
    if q.get("channel"):
        sql += " AND channel_id=?"
        args.append(q["channel"][0])
    if q.get("swapped_since"):                    # files trimmed/restored since (epoch s): Tubarr refreshes Plex for these
        try:
            sql += " AND swapped_at >= ?"
            args.append(float(q["swapped_since"][0]))
        except ValueError:
            raise ApiError(400, "bad_request", "swapped_since must be a number.") from None
    if q.get("q"):
        sql += " AND (title LIKE ? OR channel LIKE ?)"
        args += ["%" + q["q"][0] + "%"] * 2
    rows = db.query(sql + " ORDER BY upload_date DESC, channel", args)
    try:
        limit = max(1, min(500, int(q.get("limit", ["100"])[0])))
        offset = max(0, int(q.get("offset", ["0"])[0]))
    except ValueError:
        raise ApiError(400, "bad_request", "limit and offset must be numbers.") from None
    return {"videos": [views.video(r, s) for r in rows[offset:offset + limit]], "total": len(rows)}


def _history(q):
    try:
        limit = max(1, min(500, int(q.get("limit", ["50"])[0])))
    except ValueError:
        limit = 50
    rows = db.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
    return {"history": [{"id": r["id"], "at": iso(r["at"]), "level": r["level"], "video_id": r["video_id"],
                         "message": r["message"]} for r in rows]}


def _need_service():
    if not _SERVICE[0]:
        raise ApiError(409, "conflict", "The Trimarr service isn't running.")
    return _SERVICE[0]


ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


@route("GET", r"/health")
def _health(m, q, body):
    return 200, {"ok": True, "version": config.VERSION}


@route("GET", r"/api/status")
def _get_status(m, q, body):
    return 200, _status()


@route("GET", r"/api/settings")
def _get_settings(m, q, body):
    return 200, settings.load()


@route("PATCH", r"/api/settings")
def _patch_settings(m, q, body):
    try:
        new = settings.update(body or {})
    except settings.SettingsError as e:
        raise ApiError(400, "bad_request", str(e)) from None
    if _SERVICE[0]:
        _SERVICE[0].wake.set()
    return 200, new


@route("GET", r"/api/channels")
def _get_channels(m, q, body):
    return 200, _channels()


@route("PATCH", r"/api/channels/([^/]+)")
def _patch_channel(m, q, body):
    if "trim" not in (body or {}) or body["trim"] not in (True, False, None):
        raise ApiError(400, "bad_request", "Send {\"trim\": true, false or null}.")
    settings.set_channel(m.group(1), body["trim"])
    ch = next((c for c in _channels()["channels"] if c["id"] == m.group(1)), None)
    return 200, ch or {"id": m.group(1), "trim": body["trim"]}


@route("GET", r"/api/videos")
def _get_videos(m, q, body):
    return 200, _videos(q)


@route("GET", r"/api/videos/([^/]+)")
def _get_video(m, q, body):
    return 200, views.video(_video_or_404(m.group(1)), settings.load(), detail=True)


@route("POST", r"/api/videos/([^/]+)/trim")
def _post_trim(m, q, body):
    r = _video_or_404(m.group(1))
    if r["state"] == "trimmed":
        raise ApiError(409, "conflict", "This video is already trimmed.")
    _need_service().submit("trim", r["video_id"], force=bool((body or {}).get("force")))
    return 202, {"ok": True, "message": "Trimming queued: %s." % r["title"]}


@route("POST", r"/api/videos/([^/]+)/undo")
def _post_undo(m, q, body):
    r = _video_or_404(m.group(1))
    if r["state"] != "trimmed":
        raise ApiError(409, "conflict", "This video isn't trimmed.")
    if not r["backup_path"] or not os.path.exists(r["backup_path"]):
        raise ApiError(409, "conflict", "The original is no longer kept, so this trim can't be undone.")
    _need_service().submit("undo", r["video_id"])
    return 202, {"ok": True, "message": "Restoring the original: %s." % r["title"]}


@route("POST", r"/api/videos/([^/]+)/approve")
def _post_approve(m, q, body):
    r = _video_or_404(m.group(1))
    try:
        trimmer.approve(r["video_id"], block=False)
    except trimmer.Skip as e:
        raise ApiError(409, "conflict", str(e)[:1].upper() + str(e)[1:] + ".") from None
    return 200, views.video(db.video(r["video_id"]), settings.load())


@route("POST", r"/api/videos/([^/]+)/check")
def _post_check(m, q, body):
    r = _video_or_404(m.group(1))
    _need_service().submit("check", r["video_id"])
    return 202, {"ok": True, "message": "Checking SponsorBlock: %s." % r["title"]}


@route("POST", r"/api/originals/approve")
def _post_approve_all(m, q, body):
    n, busy = 0, 0
    for r in db.query("SELECT video_id FROM videos WHERE backup_path IS NOT NULL"):
        try:
            n += bool(trimmer.approve(r["video_id"], block=False))
        except trimmer.Skip:
            busy += 1
    return 200, {"ok": True, "deleted": n, "not_deleted": busy}


@route("GET", r"/api/report")
def _get_report(m, q, body):
    return 200, views.report(channel=(q.get("channel") or [None])[0])


@route("POST", r"/api/pass")
def _post_pass(m, q, body):
    _need_service().request_pass()
    return 202, {"ok": True, "message": "A pass starts now (scan, SponsorBlock check, trims if enabled)."}


@route("POST", r"/api/pause")
def _post_pause(m, q, body):
    settings.update({"paused": True})
    return 200, _status()


@route("POST", r"/api/resume")
def _post_resume(m, q, body):
    settings.update({"paused": False})
    if _SERVICE[0]:
        _SERVICE[0].request_pass()
    return 200, _status()


@route("GET", r"/api/history")
def _get_history(m, q, body):
    return 200, _history(q)


MAX_BODY = 1_000_000


def token_ok():
    """True when TRIMARR_TOKEN is set and long enough to be served with."""
    return len(config.api_token()) >= config.MIN_TOKEN_LEN


def host_allowed(host):
    """The Host header's hostname (port and IPv6 brackets stripped) must be on the allowlist."""
    host = (host or "").strip().lower()
    if not host:
        return False
    if host.startswith("["):
        name = host[1:].split("]", 1)[0]
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return name.rstrip(".") in config.allowed_hosts()


def auth_ok(provided):
    token = config.api_token()
    if len(token) < config.MIN_TOKEN_LEN or not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), token.encode("utf-8"))


class Handler(BaseHTTPRequestHandler):
    server_version = "Trimarr/" + config.VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def _send(self, status, obj, close=False):
        data = json.dumps(obj, indent=None, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if close:                                          # the request body (if any) was not read
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(data)

    def _refuse(self, status, code, message):
        self._send(status, {"error": {"code": code, "message": message}}, close=True)

    def _handle(self, method):
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        if not host_allowed(self.headers.get("Host")):
            return self._refuse(421, "bad_host", "Trimarr doesn't answer to that host name.")
        if not (method == "GET" and path == "/health"):
            if not token_ok():
                return self._refuse(503, "not_configured", "Trimarr's API is off: TRIMARR_TOKEN isn't set "
                                    "(it must be at least %d characters, the same in Tubarr and Trimarr)."
                                    % config.MIN_TOKEN_LEN)
            if not auth_ok(self.headers.get("X-Trimarr-Token")):
                return self._refuse(401, "unauthorized", "Missing or wrong X-Trimarr-Token.")
        try:
            body = None
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._refuse(400, "bad_request", "Bad Content-Length.")
            if n < 0 or n > MAX_BODY:
                return self._refuse(413, "bad_request", "The body is too large.")
            if n:
                try:
                    body = json.loads(self.rfile.read(n).decode("utf-8"))
                except ValueError:
                    raise ApiError(400, "bad_request", "The body isn't valid JSON.") from None
            for meth, rx, fn in ROUTES:
                mm = rx.match(path)
                if mm and meth == method:
                    status, obj = fn(mm, parse_qs(u.query), body)
                    return self._send(status, obj)
            raise ApiError(404, "not_found", "No such endpoint.")
        except ApiError as e:
            self._send(e.status, {"error": {"code": e.code, "message": e.message}})
        except Exception as e:
            log.exception("API %s %s", method, u.path)
            self._send(500, {"error": {"code": "internal", "message": "Trimarr hit an error: %s" % type(e).__name__}})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PATCH(self):
        self._handle("PATCH")


def start(service):
    _SERVICE[0] = service
    if not token_ok():
        log.error("TRIMARR_TOKEN is not set or shorter than %d characters: the API refuses every request except "
                  "/health until it is set (the same value in Tubarr's and Trimarr's environment).",
                  config.MIN_TOKEN_LEN)
    srv = ThreadingHTTPServer((config.API_HOST, config.API_PORT), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="api", daemon=True).start()
    return srv
