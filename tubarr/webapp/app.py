"""FastAPI app: the UI (web/) + the API in docs/API.md, on the worker's real data.

Security (webapp/security.py): every /api/ route except sign-in needs the admin session (+ CSRF token for changes)
or the read-only API key. Reads the worker's DB read-only; everything that changes state calls tubarr/actions.py
(or auth.py / net.py / setupflow.py). Unimplemented actions answer 501 "coming soon".
"""
import asyncio
import contextlib
import csv
import logging
import os
import re
import sqlite3
import threading
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import actions, audit, auth, config, db, net, redact
from . import VERSION, addons, events, mapping, plexinfo, security, setupflow, store, views
from .store import DATA, IN_PLEX

log = logging.getLogger("tubarr.webapp")
PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.environ.get("TUBARR_WEB_DIR") or next(
    (p for p in (os.path.join(os.path.dirname(PKG), "web"), "/app/web") if os.path.isfile(os.path.join(p, "index.html"))),
    os.path.join(os.path.dirname(PKG), "web"))


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def err(status, code, message):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status,
                        headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------ app + lifecycle
POLLER = events.Poller(DATA)


@contextlib.asynccontextmanager
async def lifespan(app):
    redact.install()
    auth.seed_from_env()
    auth.announce_setup()
    POLLER.start()
    threading.Thread(target=plexinfo.warm, name="plex-warm", daemon=True).start()
    addons.TRIM_INDEX.start()
    log.info("Tubarr web app %s: web %s, data %s", VERSION, WEB_DIR, config.DATA)
    try:
        yield
    finally:
        await POLLER.stop()


app = FastAPI(title="Tubarr", version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(ApiError)
async def _api_error(request, e):
    return err(e.status, e.code, e.message)


@app.exception_handler(actions.ActionError)
async def _action_error(request, e):
    return err(e.status, e.code, e.message)


@app.exception_handler(addons.ProxyError)
async def _proxy_error(request, e):
    return err(e.status, e.code, e.message)


@app.exception_handler(auth.AuthError)
async def _auth_error(request, e):
    extra = {"retry_after": e.retry_after} if e.retry_after else {}
    body = {"error": dict({"code": e.code, "message": e.message}, **extra)}
    return JSONResponse(body, status_code=e.status, headers={"Cache-Control": "no-store"})


@app.exception_handler(setupflow.SetupError)
async def _setup_error(request, e):
    return err(e.status, e.code, e.message)


@app.exception_handler(net.NetworkPaused)
async def _net_paused(request, e):
    return err(503, "network_paused", "YouTube traffic is paused: %s" % e)


@app.exception_handler(NotImplementedError)
async def _not_implemented(request, e):
    what = str(e) or "This"
    return err(501, "not_implemented", "Coming soon: %s." % what.rstrip("."))


@app.exception_handler(StarletteHTTPException)
async def _http_error(request, e):
    code = {400: "bad_request", 404: "not_found", 405: "bad_request", 409: "conflict", 413: "too_large"}.get(
        e.status_code, "internal")
    msg = {404: "Not found.", 405: "That method isn't supported here."}.get(e.status_code, str(e.detail))
    return err(e.status_code, code, msg)


@app.exception_handler(csv.Error)
@app.exception_handler(ValueError)
@app.exception_handler(UnicodeError)
async def _bad_input(request, e):
    return err(400, "bad_request", "The request couldn't be read.")


@app.exception_handler(RequestValidationError)
async def _validation(request, e):
    return err(400, "bad_request", "The request wasn't in the expected shape.")


@app.exception_handler(FileNotFoundError)
async def _no_db(request, e):
    return err(503, "internal", "Tubarr's database isn't there yet (the worker creates it on its first start).")


@app.exception_handler(sqlite3.Error)
async def _db_error(request, e):
    log.warning("DB error: %s", e)
    return err(503, "internal", "Tubarr couldn't read its database just now. Try again in a moment.")


@app.middleware("http")
async def _cache_headers(request, call_next):
    resp = await call_next(request)
    p = request.url.path
    if p.startswith("/api/"):
        if "Cache-Control" not in resp.headers:
            resp.headers["Cache-Control"] = "no-store"
    elif p == "/" or p.endswith((".html", ".js", ".css")):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


app.middleware("http")(security.guard)          # registered late = runs early: sign-in, CSRF, security headers
app.add_middleware(security.BodyLimit)          # registered last = outermost: request body cap (413)


# ------------------------------------------------------------------ helpers
def snap():
    try:
        return DATA.snapshot()
    except FileNotFoundError:
        return EMPTY.get()


class _Empty:
    """A snapshot of an empty DB (same schema), so the UI works before the worker's first start."""
    _s = None

    def get(self):
        if self._s is None:
            c = sqlite3.connect(":memory:", check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.executescript(db.SCHEMA)
            for table, cols in db.MIGRATIONS.items():
                for col, typ in cols.items():
                    c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, typ))
            self._s = store.Snapshot(c, None)
        return self._s


EMPTY = _Empty()


class _Data:
    """DATA, but falling back to the empty snapshot when there's no DB yet."""

    def snapshot(self):
        return snap()

    def status(self):
        return DATA.status()

    def status_age(self):
        return DATA.status_age()


D = _Data()


def ctx():
    return mapping.Ctx(DATA.status())


def need_channel(s, cid):
    if cid not in s.channels and cid not in s.pseudo:
        raise ApiError(404, "not_found", "Channel not found.")
    return cid


def need_video(s, vid):
    if vid not in s.videos:
        raise ApiError(404, "not_found", "Video not found.")
    return s.videos[vid]


async def body(request):
    if request.headers.get("content-type", "").startswith("multipart/"):
        return {"_multipart": True}
    try:
        b = await request.json()
        return b if isinstance(b, dict) else {}
    except Exception:
        return {}


def accepted(message):
    return JSONResponse({"ok": True, "message": message}, status_code=202)


def jobs_now(s):
    return views.jobs_by_video(views.active_jobs(DATA.status(), s))


# ------------------------------------------------------------------ health + sign-in
@app.get("/health")
def health():
    return {"ok": True}


def _auth_state(request, via=None, sess=None, csrf=None):
    via = via if via is not None else getattr(request.state, "via", None)
    sess = sess if sess is not None else getattr(request.state, "session", None)
    return {"setup_required": not auth.has_admin(), "setup_code_required": not auth.has_admin(),
            "authenticated": via is not None, "user": (sess or {}).get("user") if via == "session" else None,
            "csrf": csrf or ((sess or {}).get("csrf") if via == "session" else None), "via": via}


def _signed_in(request, user, action):
    cookie, csrf = auth.new_session()
    out = _auth_state(request, "session", {"user": user, "csrf": csrf}, csrf)
    resp = JSONResponse(out, headers={"Cache-Control": "no-store"})
    security.set_session_cookies(request, resp, cookie, csrf)
    audit.write(user, action, "from %s" % security.client_addr(request))
    return resp


@app.get("/api/auth/state")
def api_auth_state(request: Request):
    return _auth_state(request)


@app.post("/api/auth/setup")
async def api_auth_setup(request: Request):
    b = await body(request)
    user = auth.create_admin(b.get("username"), b.get("password"), b.get("setup_code"))
    return _signed_in(request, user, "auth.admin_created")


@app.post("/api/auth/login")
async def api_auth_login(request: Request):
    b = await body(request)
    try:
        user = await asyncio.to_thread(auth.verify_login, b.get("username"), b.get("password"),
                                       security.client_addr(request))
    except auth.AuthError as e:
        audit.write(str(b.get("username") or "?")[:64], "auth.login_failed" if e.status == 401 else "auth.locked_out",
                    "from %s" % security.client_addr(request))   # noisy entries are folded (audit.NOISY)
        await asyncio.sleep(0.4)
        raise
    return _signed_in(request, user, "auth.login")


@app.post("/api/auth/logout")
def api_auth_logout(request: Request):
    sess = request.state.session
    if sess:
        auth.end_session(sess["sid"])
        audit.write(sess.get("user"), "auth.logout", "")
    resp = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
    security.clear_session_cookies(resp)
    return resp


@app.post("/api/auth/password")
async def api_auth_password(request: Request):
    b = await body(request)
    try:
        await asyncio.to_thread(auth.change_password, b.get("current_password"), b.get("new_password"),
                                security.client_addr(request))
    except auth.AuthError as e:
        if e.status in (403, 429):
            audit.write(security.who(request), "auth.password_failed", "from %s" % security.client_addr(request))
            await asyncio.sleep(0.4)
        raise
    user = request.state.session.get("user")
    resp = _signed_in(request, user, "auth.password_changed")
    return resp


@app.get("/api/auth/apikey")
def api_apikey():
    return auth.api_key_info()


@app.post("/api/auth/apikey")
def api_apikey_new(request: Request):
    out = auth.new_api_key()
    audit.write(security.who(request), "auth.apikey_created", "key %s" % out["hint"])
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


@app.delete("/api/auth/apikey")
def api_apikey_revoke(request: Request):
    auth.revoke_api_key()
    audit.write(security.who(request), "auth.apikey_revoked", "")
    return {"ok": True}


@app.get("/api/audit")
def api_audit(limit: int = 100):
    return {"entries": audit.read(limit)}


# ------------------------------------------------------------------ first-run setup (Plex first)
def _pending_subs():
    try:
        c = sqlite3.connect("file:%s?mode=ro" % os.path.join(config.DATA, "tubarr.db"), uri=True, timeout=5)
        try:
            return c.execute("SELECT COUNT(*) FROM subs WHERE state='new'").fetchone()[0]
        finally:
            c.close()
    except sqlite3.Error:
        return 0


@app.get("/api/setup")
def api_setup():
    s = snap()
    return setupflow.state(len(s.channels), _pending_subs())


@app.post("/api/setup/plex/pin")
def api_setup_pin():
    return setupflow.start_pin()


@app.get("/api/setup/plex/pin/{pin_id}")
def api_setup_pin_poll(pin_id: int):
    return setupflow.poll_pin(pin_id)


@app.post("/api/setup/plex")
async def api_setup_plex(request: Request):
    b = await body(request)
    tok = b.get("token")
    if tok is not None and (not isinstance(tok, str) or len(tok) > 200 or any(ch.isspace() for ch in tok.strip())):
        raise ApiError(400, "bad_request", "That doesn't look like a Plex token.")
    out = await asyncio.to_thread(setupflow.save_plex, b.get("url"), (tok or "").strip() or None)
    audit.write(security.who(request), "plex.connected", "server %s%s" % (out.get("server_name") or "?",
                                                                         ", token replaced" if tok else ""))
    return out


@app.get("/api/setup/plex/libraries")
def api_setup_libraries():
    return {"libraries": setupflow.libraries()}


@app.post("/api/setup/plex/library")
async def api_setup_library(request: Request):
    b = await body(request)
    out = setupflow.save_library(b.get("title"), b.get("plex_root"))
    audit.write(security.who(request), "plex.library", "library '%s'" % str(b.get("title"))[:100])
    return out


@app.post("/api/setup/plex/clear")
def api_setup_plex_clear(request: Request):
    out = setupflow.clear_plex()
    audit.write(security.who(request), "plex.disconnected", "")
    return out


@app.post("/api/setup/complete")
def api_setup_complete(request: Request):
    audit.write(security.who(request), "setup.complete", "")
    return setupflow.complete()


# ------------------------------------------------------------------ status + live
@app.get("/api/status")
def api_status():
    return views.status_obj(D)


@app.get("/api/events")
async def api_events(request: Request):
    return StreamingResponse(events.stream(request, D), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/timeline")
def api_timeline(request: Request):
    q = dict(request.query_params)
    q["channel"] = request.query_params.getlist("channel")
    return views.timeline(D, q)


# ------------------------------------------------------------------ channels
@app.get("/api/channels")
def api_channels():
    s, c = snap(), ctx()
    ids = sorted((cid for cid in s.channels if not mapping.being_deleted(s.channels[cid])),
                 key=lambda cid: (s.channels[cid]["csv_order"] is None, s.channels[cid]["csv_order"] or 0))
    return {"channels": [mapping.channel_summary(s, cid, c) for cid in ids] +
                        [mapping.channel_summary(s, h, c) for h in s.pseudo]}


@app.get("/api/channels/{cid}")
def api_channel(cid: str):
    s = snap()
    return mapping.channel_detail(s, need_channel(s, cid), ctx())


@app.get("/api/channels/{cid}/videos")
def api_channel_videos(cid: str):
    s = snap()
    need_channel(s, cid)
    jobs = jobs_now(s)
    vids = s.by_channel.get(cid, [])
    return {"videos": [mapping.video(s, v, jobs.get(v)) for v in vids], "total": len(vids)}


@app.get("/api/channels/{cid}/posters")
def api_posters(cid: str):
    s = snap()
    need_channel(s, cid)
    url = mapping.channel_summary(s, cid)["poster_url"]
    return {"current": "current", "variants": [{"id": "current", "label": "Current poster", "url": url}] if url else [],
            "read_only": True}


@app.post("/api/channels/{cid}/poster")
async def api_set_poster(cid: str, request: Request):
    b = await body(request)
    need_channel(snap(), cid)
    actions.set_channel_poster(cid, variant=b.get("variant"))


@app.post("/api/channels/refresh")
def api_refresh_all():
    actions.refresh_all_channels()
    return accepted("Checking every channel now.")


@app.post("/api/channels/{cid}/refresh")
def api_refresh(cid: str):
    need_channel(snap(), cid)
    actions.refresh_channel(cid)
    return accepted("Checking for new uploads now.")


@app.post("/api/channels/{cid}/repolish")
def api_repolish(cid: str):
    need_channel(snap(), cid)
    actions.repolish_channel(cid)
    return accepted("Re-polishing this channel.")


@app.delete("/api/channels/{cid}")
async def api_remove_channel(cid: str, request: Request):
    b = await body(request)
    need_channel(snap(), cid)
    if b.get("immediate"):
        actions.delete_channel_now(cid, manual=True)       # confirmed in the dialog: everything goes
        return {"ok": True, "deleted": True}
    actions.flag_channel_removed(cid, reason="manual")
    return mapping.channel_detail(DATA.snapshot(max_age=0), cid, ctx())


@app.post("/api/channels/{cid}/restore")
def api_restore(cid: str):
    need_channel(snap(), cid)
    actions.restore_channel(cid)
    return mapping.channel_detail(DATA.snapshot(max_age=0), cid, ctx())


@app.post("/api/channels")
async def api_add_channel(request: Request):
    b = await body(request)
    if not (b.get("q") or "").strip():
        raise ApiError(400, "bad_request", "Paste a channel link or @handle.")
    cid = actions.add_channel(b["q"].strip(), settings=b.get("settings"), source="manual")
    return JSONResponse(mapping.channel_detail(DATA.snapshot(max_age=0), cid, ctx()), status_code=201)


@app.patch("/api/channels/{cid}")
async def api_patch_channel(cid: str, request: Request):
    b = await body(request)
    s = snap()
    need_channel(s, cid)
    meta = b.get("meta") or {}
    if "topic" in meta and meta["topic"] is not None and meta["topic"] not in mapping.TOPIC_LABEL:
        raise ApiError(400, "bad_request", "Unknown topic.")
    if b.get("settings"):
        actions.update_channel_settings(cid, b["settings"])
    if meta:
        actions.update_channel_meta(cid, meta)
    return mapping.channel_detail(DATA.snapshot(max_age=0), cid, ctx())


@app.get("/api/channels/{cid}/series")
def api_series(cid: str):
    need_channel(snap(), cid)
    return {"series": [], "ignored": [], "available": False,
            "message": "Series from playlists are coming soon. Until then every video is in its year's season."}


@app.patch("/api/channels/{cid}/series/{pid}")
async def api_patch_series(cid: str, pid: str, request: Request):
    b = await body(request)
    return actions.set_series(cid, pid, enabled=b.get("enabled"), order=b.get("order"))


_LOOKUPS = []                    # timestamps of live YouTube lookups (all sessions together)
_LOOKUP_PER_MIN = 6


def _lookup_rate_ok():
    now = time.monotonic()
    with _LOOKUP_LOCK:
        _LOOKUPS[:] = [t for t in _LOOKUPS if now - t < 60]
        if len(_LOOKUPS) >= _LOOKUP_PER_MIN:
            return False
        _LOOKUPS.append(now)
        return True


_LOOKUP_LOCK = threading.Lock()


@app.post("/api/lookup")
async def api_lookup(request: Request):
    """Preview a channel before adding it. Signed-in session only (not the API key: it makes a live YouTube request),
    POST (CSRF-checked), and at most _LOOKUP_PER_MIN live lookups a minute."""
    b = await body(request)
    q = b.get("q")
    if not isinstance(q, str) or len(q) > 300:
        raise ApiError(400, "bad_request", "Paste a channel link or @handle.")
    return await asyncio.to_thread(_lookup, q)


def _lookup(q):
    s = snap()
    ref = q.strip()
    if not ref:
        raise ApiError(400, "bad_request", "Paste a channel link or @handle.")
    m = re.search(r"(UC[\w-]{22})", ref)
    h = re.search(r"@([\w.\-·]+)", ref)
    cid = None
    if m and m.group(1) in s.channels:
        cid = m.group(1)
    elif h:
        want = ("@" + h.group(1)).lower()
        cid = next((c for c, ch in s.channels.items() if (ch["handle"] or "").lower() == want), None)
    if cid:
        ch = s.channels[cid]
        d = mapping.channel_detail(s, cid)
        return {"channel": {"id": cid, "title": d["title"], "handle": ch["handle"], "url": d["url"],
                            "subscribers": ch["subscribers"], "about": ch["description"], "avatar_url": None,
                            "banner_url": d["banner_url"], "youtube_video_count": None}, "already_added": True}
    # A NEW channel: preview it live with the same ytdl.channel_tab() call add_channel() uses, so the preview card
    # and the eventual add always agree on which channel was found.
    from .. import ytdl
    if not _lookup_rate_ok():
        raise ApiError(429, "rate_limited", "Too many lookups. Wait a minute.")
    resolved = actions._resolve_ref(ref)
    try:
        info = ytdl.channel_tab(resolved, limit=1)
    except ytdl.BotCheck as e:
        raise ApiError(503, "youtube_busy", "YouTube asked Tubarr to slow down. Try again in a few minutes.") from e
    except net.NetworkPaused:
        raise
    except Exception as e:
        raise ApiError(404, "not_found", "YouTube doesn't know that channel (%s)." % redact.text(str(e))[:120]) from None
    new_cid = info.get("channel_id") or info.get("id")
    if not new_cid:
        raise ApiError(404, "not_found", "That link isn't a YouTube channel.")
    thumbs = info.get("thumbnails") or []
    avatar = next((t.get("url") for t in thumbs if (t.get("id") or "") == "avatar_uncropped"), None)
    banner = next((t.get("url") for t in thumbs if (t.get("id") or "") == "banner_uncropped"), None)
    handle = info.get("uploader_id") or info.get("channel_handle") or ("@" + h.group(1) if h else None)
    return {"channel": {"id": new_cid, "title": info.get("channel") or info.get("title") or new_cid,
                        "handle": handle, "url": info.get("channel_url") or info.get("webpage_url"),
                        "subscribers": info.get("channel_follower_count"), "about": info.get("description"),
                        "avatar_url": avatar, "banner_url": banner,
                        "youtube_video_count": info.get("playlist_count")}, "already_added": False}


# ------------------------------------------------------------------ videos
@app.get("/api/videos/{vid}")
def api_video(vid: str):
    s = snap()
    need_video(s, vid)
    return mapping.video_detail(s, vid, jobs_now(s).get(vid))


@app.patch("/api/videos/{vid}")
async def api_patch_video(vid: str, request: Request):
    b = await body(request)
    s = snap()
    need_video(s, vid)
    if "keep" in b:
        actions.set_keep_forever(vid, bool(b["keep"]))
    if "title" in b or "summary" in b:
        actions.update_video_meta(vid, title=b.get("title", actions.UNSET), summary=b.get("summary", actions.UNSET))
    return mapping.video_detail(DATA.snapshot(max_age=0), vid)


@app.get("/api/videos/{vid}/thumbnails")
def api_thumbs(vid: str):
    s = snap()
    need_video(s, vid)
    url = mapping.video(s, vid)["thumbnail_url"]
    return {"current": "current", "variants": [{"id": "current", "label": "Current thumbnail", "url": url}] if url else [],
            "read_only": True}


@app.post("/api/videos/{vid}/thumbnail")
async def api_set_thumb(vid: str, request: Request):
    b = await body(request)
    need_video(snap(), vid)
    actions.set_video_thumbnail(vid, variant=b.get("variant"))


def _download_one(s, vid, front, now=False):
    r = need_video(s, vid)
    a = s.meta[vid].astate
    if a == "skipped_short":
        raise actions.ActionError(409, "conflict", "Shorts are always skipped.")
    if a == "skipped_members":
        raise actions.ActionError(409, "conflict", "Members-only videos can't be downloaded signed out.")
    if a in ("downloaded", "downloading"):
        raise actions.ActionError(409, "conflict", "That video is already in Plex.")
    if a == "upcoming":
        raise actions.ActionError(409, "conflict", "That premiere hasn't started yet. Tubarr queues it automatically.")
    if r["state"] == "failed":
        actions.retry_video(vid, front=front, now=now)
    else:
        actions.download_video(vid, front=front, now=now)


@app.post("/api/videos/{vid}/download")
async def api_download(vid: str, request: Request):
    b = await body(request)
    s = snap()
    _download_one(s, vid, bool(b.get("next")), bool(b.get("now")))
    return mapping.video(DATA.snapshot(max_age=0), vid)


@app.delete("/api/videos/{vid}")
def api_delete_video(vid: str):
    s = snap()
    need_video(s, vid)
    actions.delete_video(vid)
    return mapping.video(DATA.snapshot(max_age=0), vid)


@app.post("/api/videos/bulk")
async def api_bulk(request: Request):
    b = await body(request)
    ids, action = b.get("ids") or [], b.get("action")
    if action not in ("keep", "unkeep", "delete", "download") or not isinstance(ids, list):
        raise ApiError(400, "bad_request", "Send ids and an action (keep, unkeep, delete or download).")
    s = snap()
    done, skipped = 0, []
    for vid in ids:
        try:
            if vid not in s.videos:
                raise actions.ActionError(404, "not_found", "Video not found.")
            if action in ("keep", "unkeep"):
                if s.meta[vid].astate != "downloaded":
                    raise actions.ActionError(409, "conflict", "Only videos in Plex can be kept forever.")
                actions.set_keep_forever(vid, action == "keep")
            elif action == "delete":
                actions.delete_video(vid)
            else:
                _download_one(s, vid, False)
            done += 1
        except actions.ActionError as e:
            skipped.append({"id": vid, "reason": e.message})
    return {"done": done, "skipped": skipped}


# ------------------------------------------------------------------ estimate, activity, storage
@app.post("/api/estimate")
async def api_estimate(request: Request):
    b = await body(request)
    out = views.estimate(D, b)
    if out is None:
        raise ApiError(404, "not_found", "Channel not found.")
    return out


@app.get("/api/activity")
def api_activity():
    return views.activity(D)


@app.get("/api/history")
def api_history(before: str = None, limit: int = 50):
    return views.history(D, before, limit)


@app.post("/api/downloader/pause")
def api_pause():
    actions.pause_downloads()


@app.post("/api/downloader/resume")
def api_resume():
    actions.resume_downloads()


@app.post("/api/downloader/retry-failed")
def api_retry_failed():
    return {"queued": actions.retry_failed()}


@app.get("/api/storage")
def api_storage():
    return views.storage(D)


@app.get("/api/topics")
def api_topics():
    return views.topics(D)


# ------------------------------------------------------------------ YouTube connection
# ------------------------------------------------------------------ import, Plex, settings, notifications
@app.post("/api/import/preview")
async def api_import_preview(request: Request):
    b = await body(request)
    if not (b.get("text") or b.get("html")):
        raise ApiError(400, "bad_request", "Paste your subscriptions page (or a subscriptions .csv) first.")
    return addons.import_preview(snap(), b.get("text"), b.get("html"))


@app.post("/api/import/apply")
async def api_import_apply(request: Request):
    b = await body(request)
    if not addons.import_known(b.get("import_id")):
        raise ApiError(404, "not_found", "That preview expired. Paste the list again.")
    return actions.import_apply(b.get("add") or [], b.get("remove") or [])


@app.get("/api/plex")
def api_plex():
    return plexinfo.status()


@app.post("/api/plex/test")
def api_plex_test():
    return plexinfo.status(fresh=True)


@app.post("/api/plex/scan")
def api_plex_scan():
    actions.plex_scan()
    return accepted("Plex is scanning the YouTube library.")


@app.post("/api/plex/repolish")
def api_plex_repolish():
    actions.repolish_all()
    return accepted("Re-polishing every channel.")


@app.get("/api/settings")
def api_settings(request: Request):
    return views.settings(D, full=request.state.via == "session")


@app.patch("/api/settings")
async def api_patch_settings(request: Request):
    b = await body(request)
    actions.update_settings(b)
    return views.settings(D)


@app.post("/api/notifications/test")
async def api_notif_test(request: Request):
    b = await body(request)
    await asyncio.to_thread(actions.test_notification, b.get("discord_webhook_url"))
    return {"ok": True}


# ------------------------------------------------------------------ Trimarr (optional add-on)
@app.api_route("/api/trimarr/{path:path}", methods=["GET", "POST", "PATCH"])
async def api_trimarr(path: str, request: Request):
    b = await body(request) if request.method != "GET" else None
    status, out = await asyncio.to_thread(addons.trimarr, request.method, path, b, dict(request.query_params))
    return JSONResponse(out, status_code=status)


# ------------------------------------------------------------------ art files from the library
def _inside_root(path):
    root = os.path.realpath(config.ROOT)
    p = os.path.realpath(path)
    return p if p.startswith(root + os.sep) and os.path.isfile(p) else None


def _image(path):
    p = _inside_root(path) if path else None
    if not p:
        raise ApiError(404, "not_found", "No image yet.")
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800"})


IMG_CACHE = {"Cache-Control": "public, max-age=86400"}


@app.get("/api/img/video/{vid}")
def api_img_video(vid: str):
    """The episode's thumbnail from the library (the .jpg beside the video); anything not in the library yet ->
    YouTube's own thumbnail (redirect)."""
    r = snap().videos.get(vid)
    if r is None:
        if not re.fullmatch(r"[\w-]{11}", vid):
            raise ApiError(404, "not_found", "Video not found.")
        return RedirectResponse(mapping.youtube_thumb(vid), status_code=302, headers=IMG_CACHE)
    if r["path"] and r["state"] in IN_PLEX:
        p = _inside_root(os.path.splitext(r["path"])[0] + ".jpg")
        if p:
            return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800"})
    return RedirectResponse(mapping.youtube_thumb(vid), status_code=302, headers=IMG_CACHE)


@app.get("/api/img/channel/{cid}/{kind}")
def api_img_channel(cid: str, kind: str):
    """poster (poster.jpg), fanart / banner (fanart.jpg) from the show folder; avatar -> YouTube's avatar image."""
    ch = snap().channels.get(cid)
    if ch is None:
        raise ApiError(404, "not_found", "Channel not found.")
    if kind == "avatar":
        url = ch["avatar_url"] or ""
        if url.startswith("https://"):
            return RedirectResponse(url, status_code=302, headers=IMG_CACHE)
        raise ApiError(404, "not_found", "No avatar.")
    name = {"poster": "poster.jpg", "fanart": "fanart.jpg", "banner": "fanart.jpg"}.get(kind)
    if not name or not ch["folder"]:
        raise ApiError(404, "not_found", "No such image.")
    return _image(os.path.join(config.ROOT, ch["folder"], name))


@app.get("/api/channels/{cid}/poster.jpg")
def api_poster_file(cid: str):
    ch = snap().channels.get(cid)
    return _image(os.path.join(config.ROOT, ch["folder"], "poster.jpg") if ch is not None and ch["folder"] else None)


@app.get("/api/channels/{cid}/banner.jpg")
def api_banner_file(cid: str):
    ch = snap().channels.get(cid)
    return _image(os.path.join(config.ROOT, ch["folder"], "fanart.jpg") if ch is not None and ch["folder"] else None)


@app.get("/api/videos/{vid}/thumb.jpg")
def api_thumb_file(vid: str):
    r = snap().videos.get(vid)
    ok = r is not None and r["path"] and r["state"] in IN_PLEX
    return _image(os.path.splitext(r["path"])[0] + ".jpg" if ok else None)


# ------------------------------------------------------------------ network (proxies for YouTube traffic)
def _net_call(fn, *a):
    try:
        return fn(*a)
    except ValueError as e:
        raise ApiError(400, "bad_request", str(e)) from None
    except KeyError:
        raise ApiError(404, "not_found", "That proxy doesn't exist.") from None


@app.get("/api/network")
def api_network(request: Request):
    return net.public(full=request.state.via == "session")


@app.patch("/api/network")
async def api_patch_network(request: Request):
    b = await body(request)
    allowed = {"mode", "selected", "order", "fail_threshold", "cooldown_minutes", "rotate_hours", "check_preferred",
               "never_direct"}
    return _net_call(net.patch, {k: v for k, v in b.items() if k in allowed}, security.who(request))


@app.post("/api/network/proxies")
async def api_add_proxy(request: Request):
    b = await body(request)
    return _net_call(net.add_proxy, b.get("name"), b.get("url"), security.who(request))


@app.patch("/api/network/proxies/{pid}")
async def api_patch_proxy(pid: str, request: Request):
    b = await body(request)
    return _net_call(net.update_proxy, pid, b.get("name"), b.get("url") or None, security.who(request))


@app.delete("/api/network/proxies/{pid}")
def api_delete_proxy(pid: str, request: Request):
    return _net_call(net.delete_proxy, pid, security.who(request))


@app.post("/api/network/proxies/{pid}/test")
async def api_test_proxy(pid: str, request: Request):
    # Deliberately reads nothing from the request but the line id: the target is fixed (net._TEST_URL).
    try:
        return await asyncio.to_thread(net.test_line, pid, "user:" + str(security.who(request)), True)
    except net.TestFailed as e:
        code = "rate_limited" if "Too many" in str(e) else "proxy_failed"
        raise ApiError(429 if code == "rate_limited" else 502, code, str(e)) from None


@app.api_route("/api/{rest:path}", methods=["GET", "POST", "PATCH", "PUT", "DELETE"])
def api_unknown(rest: str):
    raise ApiError(404, "not_found", "Not found.")


# ------------------------------------------------------------------ the UI (last: it catches everything else)
if os.path.isdir(WEB_DIR):
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")

