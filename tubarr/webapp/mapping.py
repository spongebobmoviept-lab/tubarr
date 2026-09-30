"""The worker's DB rows -> the objects in web/API.md (Video, VideoDetail, ChannelSummary, ChannelDetail)."""
import functools
import json
import os
import re
from urllib.parse import quote

from .. import config
from . import store
from .store import IN_PLEX, iso, ts

TOPICS = [("cars", "Cars & Builds"), ("tech", "Tech & PCs"), ("science", "Science & Engineering"), ("space", "Space"),
          ("aviation", "Aviation"), ("gaming", "Gaming"), ("history", "History & Stories"), ("makers", "Makers & DIY"),
          ("music", "Music"), ("other", "Other")]
TOPIC_ID = {label: tid for tid, label in TOPICS}
TOPIC_LABEL = dict(TOPICS)
GRACE_S = 3 * 86400

# What each failure looks like in the UI (API.md error codes) and whether Retry makes sense.
ERRORS = {
    "members_only": ("Members only: it needs a paid channel membership.", False),
    "age_restricted": ("Age-restricted: YouTube wants a signed-in account, and Tubarr downloads signed out.", True),
    "unavailable": ("YouTube says this video isn't available any more.", False),
    "geo_blocked": ("YouTube blocks this video in this country.", False),
    "http_403": ("YouTube refused the download (HTTP 403).", True),
    "network": ("The connection to YouTube dropped.", True),
    "ffmpeg": ("The downloaded streams didn't merge into a good file.", True),
    "disk_full": ("The library ran out of space.", True),
    "plex": ("Plex didn't pick up the file.", True),
    "unknown": ("The download failed.", True),
}


def classify(msg):
    t = (msg or "").lower()
    if "members" in t or "join this channel" in t:
        return "members_only"
    if "confirm your age" in t or "age-restricted" in t or "age restricted" in t or "inappropriate for some users" in t:
        return "age_restricted"
    if "403" in t or "forbidden" in t:
        return "http_403"
    if "not made this video available in your country" in t or "geo" in t and "block" in t:
        return "geo_blocked"
    if "unavailable" in t or "has been removed" in t or "private video" in t or "does not exist" in t \
            or "no longer available" in t or "terminated" in t:
        return "unavailable"
    if "no space" in t or "quota" in t and "disk" in t:
        return "disk_full"
    if "merge failed" in t or "ffmpeg" in t or "ffprobe" in t or "bad download" in t or "av1 slipped" in t:
        return "ffmpeg"
    if "timed out" in t or "timeout" in t or "connection" in t or "network" in t or "temporary failure" in t \
            or "errno" in t or "reset by peer" in t:
        return "network"
    return "unknown"


def error_of(r):
    code = classify(r["reason"])
    msg, retryable = ERRORS[code]
    at = ts(r["updated_at"])
    return {"code": code, "message": msg, "detail": (r["reason"] or "")[:500] or None, "attempts": r["attempts"] or 0,
            "last_attempt_at": iso(at), "next_retry_at": None, "retryable": retryable}


def resolution(r):
    h = r["height"]
    if not h and r["video_format"]:
        m = re.search(r"\b(\d{3,4})p", r["video_format"])
        h = int(m.group(1)) if m else None
    return ("%dp" % h) if h else None


def episode_code(r):
    if r["season"] and r["episode"]:
        return "S%04dE%06d" % (r["season"], r["episode"])
    return None


def _jmeta(row):
    try:
        v = json.loads(row["meta"] or "{}")
        return v if isinstance(v, dict) else {}
    except (TypeError, ValueError):
        return {}


def _v(t):
    return int(t or 0)


@functools.lru_cache(maxsize=100000)
def _has_thumb(path):
    """The worker publishes the episode .jpg before the video, so once a video is in Plex this answer never changes
    (a missing thumbnail means its download failed; the UI then draws a placeholder instead of a 404)."""
    return os.path.isfile(os.path.splitext(path)[0] + ".jpg")


YT_THUMB = "https://i.ytimg.com/vi/%s/hqdefault.jpg"


def youtube_thumb(vid):
    """Not in the library (queued, downloading, failed, removed): YouTube's own thumbnail, straight from its image CDN
    (hqdefault always exists; it's 4:3 with bars that the UI's object-fit: cover crops away). Offline, the UI's
    placeholder art shows instead."""
    return YT_THUMB % vid


# Trimarr's per-video results ({video_id: {"state", "removed_seconds", "undo_available"}}), kept fresh by addons.
TRIMS = {}


class PlexLinks:
    """Plex web links need the server's machine id; plexinfo fills it in when it has checked the server once."""
    machine_id = None

    @classmethod
    def item(cls, rating_key):
        if not rating_key or not cls.machine_id:
            return None
        return "%s/web/index.html#!/server/%s/details?key=%s" % (config.PLEX_URL.rstrip("/"), cls.machine_id,
                                                                  quote("/library/metadata/%s" % rating_key, safe=""))


# ------------------------------------------------------------------ videos
def video(snap, vid, job=None):
    r, m = snap.videos[vid], snap.meta[vid]
    st, a = r["state"], m.astate
    in_plex = st in IN_PLEX
    running = a == "downloading"
    stage = progress = None
    size = None
    if running:
        stage = (job or {}).get("stage") or ("plex" if st == "processing" else "download")
        progress = (job or {}).get("progress")
        size = (job or {}).get("bytes_total") or snap.estimate(r)
    elif in_plex:
        size = r["size"]
    elif a == "queued":
        size = snap.estimate(r)
    pub = ts(r["published_ts"]) or ts(r["published_at"])
    if st == "waiting" and r["retry_at"]:
        pub = float(r["retry_at"]) - 300
    detail = store.skip_detail(a, r["reason"])
    if st == "upgrade":
        detail = "In Plex; queued for a better-quality (up to 4K) copy."
    elif st == "upgrading":
        detail = "In Plex; downloading a better-quality (up to 4K) copy to replace it."
    elif st == "done" and r["reason"] and ("kept at" in r["reason"] or "no better" in r["reason"]):
        detail = r["reason"][0].upper() + r["reason"][1:] + "."
    return {
        "id": vid, "channel_id": r["channel_id"], "channel_title": snap.channel_title(r["channel_id"]),
        "title": r["title"] or vid, "upload_date": m.day, "published_at": iso(pub),
        "added_at": iso(ts(r["created_at"]) or ts(r["queued_at"])), "activity_at": iso(m.act), "backfill": m.backfill,
        "episode": episode_code(r), "duration_seconds": int(r["duration"]) if r["duration"] else None,
        "state": a, "stage": stage, "progress": progress,
        "download_now": a == "queued" and (r["priority"] or 0) >= 3,
        "wait_until": iso(float(r["retry_at"])) if st == "waiting" and r["retry_at"] else None,
        "size_bytes": int(size) if size else None,
        "resolution": resolution(r) if in_plex else None,
        "sponsorblock_cut_seconds": r["sb_cut_seconds"] if in_plex else None,
        "trim": TRIMS.get(vid), "watched": bool(r["watched"]), "keep": m.keep,
        "protected": snap.protected.get(vid) if a == "downloaded" else None,
        "youtube_gone": {"at": iso(ts(r["removed_at"])), "reason": m.gone} if m.gone else None,
        "series": None, "edited": False,
        "error": error_of(r) if a == "failed" else None,
        "removed_reason": store.removed_reason(r["reason"]) if st == "pruned" else None,
        "removed_at": iso(ts(r["updated_at"])) if st == "pruned" else None,
        "detail": detail,
        "thumbnail_url": ("/api/img/video/%s?v=%d" % (vid, _v(ts(r["finished_at"]) or ts(r["updated_at"]))))
        if in_plex and r["path"] and _has_thumb(r["path"]) else youtube_thumb(vid),
        "youtube_url": "https://www.youtube.com/watch?v=" + vid,
        "plex_url": PlexLinks.item(r["plex_rating_key"]) if in_plex else None,
    }


def video_detail(snap, vid, job=None):
    out = video(snap, vid, job)
    row = store.conn().execute("SELECT description, summary, report FROM videos WHERE id=?", (vid,)).fetchone() \
        if "description" in _video_cols() else None
    desc = (row["description"] if row else None) or ""
    summary = (row["summary"] if row else None) or ""
    rep = {}
    if row is not None and row["report"]:
        try:
            rep = json.loads(row["report"])
        except ValueError:
            rep = {}
    out.update(youtube_title=out["title"], description=desc, summary=summary or rep.get("summary") or desc,
               locked_fields=[], file={"codec": rep.get("vcodec"), "resolution": rep.get("res"),
                                       "subtitles": bool(rep.get("subtitles")), "chapters": rep.get("chapters"),
                                       "fps": rep.get("fps"), "audio": rep.get("audio")} if rep else None)
    return out


_VCOLS = {}


def _video_cols():
    if "v" not in _VCOLS:
        _VCOLS["v"] = {r[1] for r in store.conn().execute("PRAGMA table_info(videos)")}
    return _VCOLS["v"]


# ------------------------------------------------------------------ channels
def being_deleted(ch):
    """"Delete everything right away" was pressed: the files are going in the background; hide it now."""
    return ch["enabled"] == 0 and bool(ch["pending_removal_at"])


def _status(ch, s):
    if ch["gone_at"]:
        return "gone", None
    if ch["enabled"] == 0 and not ch["pending_removal_at"]:
        return "removed", "Removed. Only videos that are kept forever or gone from YouTube are left."
    if ch["pending_removal_at"]:
        return "pending_removal", None
    return "monitored", None


def sub_error_text(err):
    t = (err or "").lower()
    if "does not have a videos tab" in t or "no videos tab" in t or "has no videos" in t:
        return "This channel has no long-form videos (only Shorts), so there's nothing to download."
    if "not found" in t or "404" in t or "does not exist" in t:
        return "YouTube says this channel doesn't exist (renamed or deleted?). Check the handle."
    if "terminated" in t:
        return "YouTube terminated this channel."
    return "Tubarr couldn't read this channel: %s" % (err or "unknown error")[:160]


class Ctx:
    """Per-request context from outside the DB: the feed's last full check (status.json) and, once Connect YouTube
    has synced, the set of subscribed channel ids."""

    def __init__(self, status=None, subscribed_ids=None):
        self.feed_last = ts(((status or {}).get("new_uploads") or {}).get("last_poll"))
        self.subs = subscribed_ids

    def subscribed(self, ch):
        if self.subs is not None:
            return ch["id"] in self.subs or (ch["handle"] or "").lower() in self.subs
        return ch["source"] in ("youtube-sub", "csv-import")


def channel_summary(snap, cid, ctx=None):
    ctx = ctx or Ctx()
    ch = snap.channels.get(cid)
    if ch is None:
        return _pseudo_summary(snap, snap.pseudo[cid])
    s = snap.stats.get(cid) or {}
    meta = _jmeta(ch)
    status, detail = _status(ch, s)
    removal = None
    if status == "pending_removal":
        t = ts(ch["pending_removal_at"])
        removal = {"reason": meta.get("removal_reason") or "unsubscribed", "requested_at": iso(t),
                   "delete_at": iso(t + GRACE_S) if t else None}
    gone = {"at": iso(ts(ch["gone_at"])), "reason": "terminated" if "terminat" in (ch["gone_reason"] or "").lower()
            else "deleted", "detail": ch["gone_reason"]} if ch["gone_at"] else None
    handle = ch["handle"]
    feed = None if ch["gone_at"] or ch["pending_removal_at"] else ctx.feed_last
    last_checked = max([x for x in (ts(ch["last_checked"]), feed) if x] or [0]) or None
    return {
        "id": cid, "title": meta.get("title") or ch["title"] or cid, "handle": handle,
        "url": "https://www.youtube.com/" + handle if handle and handle.startswith("@")
        else "https://www.youtube.com/channel/" + cid,
        "subscribers": ch["subscribers"], "status": status, "status_detail": detail, "removal": removal,
        "sync_progress": None, "video_count": s.get("count", 0), "size_bytes": s.get("size", 0),
        "queued_count": s.get("queued", 0) + s.get("running", 0), "failed_count": s.get("failed", 0),
        "retention_mode": "count" if ch["keep_count"] else "fill", "keep_forever": False, "gone": gone,
        "topic": TOPIC_ID.get(meta.get("topic") or ch["topic"]) if (meta.get("topic") or ch["topic"]) else None,
        "topic_source": "user" if meta.get("topic") else "auto",
        "subscribed": ctx.subscribed(ch), "added_at": iso(ts(ch["added_at"])), "last_checked_at": iso(last_checked),
        "last_upload_at": iso(s.get("last_up")), "last_download_at": iso(s.get("last_dl")),
        "poster_url": "/api/img/channel/%s/poster?v=%d" % (quote(cid), _v(ts(ch["art_updated_at"])))
        if ch["folder"] else None,
    }


def _pseudo_summary(snap, sub):
    err = sub["state"] == "error"
    h = sub["handle"]
    return {
        "id": h, "title": sub["title"] or h.lstrip("@"), "handle": h if h.startswith("@") else None,
        "url": "https://www.youtube.com/" + h if h.startswith("@") else None, "subscribers": None,
        "status": "error" if err else "syncing", "status_detail": sub_error_text(sub["error"]) if err else None,
        "removal": None, "sync_progress": None, "video_count": 0, "size_bytes": 0, "queued_count": 0,
        "failed_count": 0, "retention_mode": "fill", "keep_forever": False, "gone": None, "topic": None,
        "topic_source": "auto", "subscribed": True, "added_at": None, "last_checked_at": iso(ts(sub["checked_at"])),
        "last_upload_at": None, "last_download_at": None, "poster_url": None,
    }


def channel_settings(ch):
    if ch is None:
        return {"retention": None, "max_duration_minutes": None, "quality": None, "include_live": None,
                "sponsorblock": None}
    return {"retention": {"mode": "count", "count": ch["keep_count"]} if ch["keep_count"] else None,
            "max_duration_minutes": int(ch["max_duration"] // 60) if ch["max_duration"] else None,
            "quality": None, "include_live": True if ch["skip_lives"] == 0 else None, "sponsorblock": None}


def channel_detail(snap, cid, ctx=None):
    out = channel_summary(snap, cid, ctx)
    ch = snap.channels.get(cid)
    s = snap.stats.get(cid) or {}
    settings = channel_settings(ch)
    eff = {"retention": settings["retention"] or {"mode": "fill"},
           "max_duration_minutes": settings["max_duration_minutes"] or 0, "quality": "2160p",
           "include_live": bool(settings["include_live"]), "sponsorblock": []}
    meta = _jmeta(ch) if ch is not None else {}
    about = (ch["description"] if ch is not None else None) or ""
    out.update({
        "youtube_title": (ch["title"] if ch is not None else out["title"]),
        "about": about, "summary": meta.get("summary") or (ch["summary"] if ch is not None else None) or about,
        "genre": meta.get("genre") or (ch["category"] if ch is not None else None),
        "locked_fields": [k for k in ("title", "summary", "genre", "poster") if meta.get(k)],
        "banner_url": "/api/img/channel/%s/fanart?v=%d" % (quote(cid), _v(ts(ch["art_updated_at"])))
        if ch is not None and ch["folder"] else None,
        "avatar_url": "/api/img/channel/%s/avatar" % quote(cid)
        if ch is not None and (ch["avatar_url"] or "").startswith("https://") else None,
        "youtube_video_count": None,
        "listed_count": s.get("listed", 0),
        "plex_url": PlexLinks.item(ch["plex_rating_key"]) if ch is not None else None,
        "kept_while_unsubscribed": bool(meta.get("kept_while_unsubscribed")),
        "settings": settings, "effective_settings": eff,
        "stats": {"watched_count": s.get("watched", 0), "skipped_count": s.get("skipped", 0),
                  "kept_count": s.get("kept", 0), "protected_count": s.get("protected", 0),
                  "sponsorblock_saved_seconds": 0, "oldest_upload_date": s.get("oldest"),
                  "newest_upload_date": s.get("newest"), "gone_count": s.get("gone_videos", 0)},
    })
    return out
