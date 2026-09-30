"""Everything that CHANGES Tubarr's state, in one place.

The web app (tubarr/webapp) calls these functions; it never writes the
worker's DB or the library themselves (the web app opens the DB read-only). The web app runs in its own process
beside the worker, so:
  * DB writes use db.connect() (autocommit, short statements; the worker's threads share the DB);
  * library file changes happen under pipeline.FILE_LOCK, which is also a cross-process lock (flock), so a delete
    here can never race the worker's finalizer / season organizer / pruner on the same file;
  * anything slow (YouTube lookups for a whole channel, deleting a show folder, Plex refreshes, re-polish) runs in a
    short-lived thread or a child process and the call returns as soon as the change is recorded.
Rules kept here: never delete a video that is playing, never touch anything that disappeared from YouTube
unless "Delete now" was pressed for it, never mass-remove channels on a failed sync (the sync only flags; removal
happens after a grace period and can be undone).

Unimplemented actions raise NotImplementedError(<what>); the web API answers 501 "Coming soon: <what>".
Priorities in the videos table: 0 = backlog, 1 = new upload (RSS), 2 = asked for by hand (bypasses channel
limits, goes first, makes room at the cap).
"""
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime

import requests

from . import config, db, naming, nfo, pipeline, plexhttp, redact, settings

log = logging.getLogger("tubarr.actions")
LIST_DEPTH = int(os.environ.get("TUBARR_LIST_DEPTH", "30"))
PROTECT_NEWEST = int(os.environ.get("TUBARR_PROTECT_NEWEST", "3"))
PAUSE_FLAG = os.path.join(config.DATA, "downloads.paused")
PH = lambda: {"X-Plex-Token": config.PLEX_TOKEN, "Accept": "application/json"}
_LOCAL = threading.local()


class _Unset:
    def __repr__(self):
        return "UNSET"


UNSET = _Unset()


class ActionError(Exception):
    """A user-facing refusal: status (HTTP), code (API error code), message (one plain sentence)."""

    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _todo(what):
    raise NotImplementedError(what)


# ------------------------------------------------------------------ helpers
def _db():
    c = getattr(_LOCAL, "c", None)
    if c is None:
        c = _LOCAL.c = db.connect()
        c.execute("""CREATE TABLE IF NOT EXISTS removed_channels(channel_id TEXT PRIMARY KEY, handle TEXT, title TEXT,
                     removed_at TEXT, reason TEXT)""")
    return c


def _video(c, vid):
    v = c.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
    if not v:
        raise ActionError(404, "not_found", "Video not found.")
    return v


def _channel(c, cid):
    ch = c.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
    if not ch:
        raise ActionError(404, "not_found", "Channel not found.")
    return ch


def _meta(row):
    try:
        m = json.loads(row["meta"] or "{}")
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def _bg(name, fn, *args):
    t = threading.Thread(target=_guarded, args=(name, fn) + args, name=name, daemon=True)
    t.start()
    return t


def _guarded(name, fn, *args):
    try:
        fn(*args)
    except Exception as e:
        log.warning("%s failed: %s", name, e)


def _need_plex():
    if not config.plex_enabled():
        raise ActionError(409, "conflict", "Plex isn't connected. Connect it in Settings -> Plex.")


def _section_key():
    _need_plex()
    r = plexhttp.get(config.PLEX_URL + "/library/sections", headers=PH(), timeout=20).json()
    return next(s["key"] for s in r["MediaContainer"]["Directory"] if s["title"] == config.PLEX_SECTION)


def _plex_scan(folder_local):
    if not config.plex_enabled():
        return
    try:
        plexhttp.get("%s/library/sections/%s/refresh" % (config.PLEX_URL, _section_key()), headers=PH(), timeout=20,
                     params={"path": config.PLEX_ROOT + folder_local[len(config.ROOT):]})
    except Exception as e:
        log.warning("plex scan failed: %s", e)


def playing_paths():
    """Library files someone is playing in Plex right now, or None if Plex can't be asked (empty without Plex)."""
    if not config.plex_enabled():
        return set()
    try:
        r = plexhttp.get(config.PLEX_URL + "/status/sessions", headers=PH(), timeout=15).json()
    except Exception:
        return None
    out = set()
    for m in r["MediaContainer"].get("Metadata", []):
        for med in m.get("Media", []):
            for part in med.get("Part", []):
                f = part.get("file", "")
                if f.startswith(config.PLEX_ROOT):
                    out.add(config.ROOT + f[len(config.PLEX_ROOT):])
    return out


def _need_not_playing(paths, what="it"):
    playing = playing_paths()
    if playing is None:
        raise ActionError(409, "conflict", "Plex didn't answer, so Tubarr can't check that nobody is watching %s. "
                                           "Try again in a minute." % what)
    if any(p in playing for p in paths):
        raise ActionError(409, "conflict", "Someone is watching %s right now." % what)


def _remove_files(path):
    """The video and its sidecars. Returns bytes freed. Caller holds pipeline.FILE_LOCK."""
    freed = 0
    if not path:
        return 0
    base, ext = os.path.splitext(path)
    for e in (ext, ".jpg", ".nfo", ".en.srt"):
        p = base + e
        if os.path.exists(p):
            freed += os.path.getsize(p)
            os.remove(p)
    return freed


def _requeue(c, vid, priority):
    c.execute("UPDATE videos SET state='queued', attempts=0, transient_fails=0, reason=NULL, retry_at=NULL, "
              "queued_at=?, priority=? WHERE id=?", (db.now(), priority, vid))


# ------------------------------------------------------------------ videos
def set_keep_forever(video_id, keep):
    """Keep forever on/off. Videos that disappeared from YouTube stay protected regardless (removed_at)."""
    c = _db()
    _video(c, video_id)
    c.execute("UPDATE videos SET keep_forever=? WHERE id=?", (1 if keep else 0, video_id))


def retry_video(video_id, front=False, now=False):
    """A failed video back into the queue (front = next up; now = "Download now", priority 3)."""
    c = _db()
    v = _video(c, video_id)
    if v["state"] == "skipped_members_only":
        raise ActionError(409, "conflict", "Members-only videos can't be downloaded signed out.")
    if v["removed_at"]:
        raise ActionError(409, "conflict", "That video is gone from YouTube.")
    if v["state"] not in ("failed", "retry", "queued"):
        raise ActionError(409, "conflict", "Only failed videos can be retried.")
    _requeue(c, video_id, 3 if now else 2 if front else (v["priority"] or 0))


def download_video(video_id, front=False, now=False):
    """"Download anyway" (a skipped stream replay / too-long video), "Download again" (removed), or move a queued
    video to the front. Requests made by hand get priority 2: they skip the channel's limits and go first."""
    c = _db()
    v = _video(c, video_id)
    st, why = v["state"], v["reason"] or ""
    if st == "skipped_members_only":
        raise ActionError(409, "conflict", "Members-only videos can't be downloaded signed out.")
    if st == "skipped" and ("Short" in why or "too short" in why):
        raise ActionError(409, "conflict", "Shorts are always skipped.")
    if st in ("done", "upgrade"):
        raise ActionError(409, "conflict", "That video is already in Plex.")
    if st in ("downloading", "processing", "upgrading"):
        raise ActionError(409, "conflict", "That video is downloading right now.")
    if st == "waiting":
        raise ActionError(409, "conflict", "That premiere hasn't started yet. Tubarr queues it automatically.")
    if v["removed_at"]:
        raise ActionError(409, "conflict", "That video is gone from YouTube, so it can't be downloaded again.")
    if st == "queued" and not front and not now:
        return
    # now = "Download now" (priority 3): the worker starts it as the very next download, skipping the pace gap and
    # the daily cap (still one at a time). front = "Download first" (priority 2): front of the queue, normal pace.
    _requeue(c, video_id, 3 if now else 2)


def delete_video(video_id, reason="manual"):
    """"Delete now": the file + sidecars, state 'pruned', Plex partial scan. Works on protected videos too (the user
    pressed it) but never while it's playing. On a video that isn't downloaded yet it means "don't download it"."""
    c = _db()
    v = _video(c, video_id)
    st = v["state"]
    if st == "pruned":
        return
    if st in ("downloading", "processing", "upgrading"):
        raise ActionError(409, "conflict", "It's downloading right now. Delete it once it's in Plex.")
    if st != "done":
        c.execute("UPDATE videos SET state='pruned', reason=?, updated_at=? WHERE id=?",
                  ("manual: removed from the queue in the web UI", db.now(), video_id))
        return
    _need_not_playing([v["path"]])
    with pipeline.FILE_LOCK:
        v = _video(c, video_id)                               # the season organizer may have just moved it
        freed = _remove_files(v["path"])
        c.execute("UPDATE videos SET state='pruned', reason=?, updated_at=? WHERE id=?",
                  ("manual: deleted in the web UI", db.now(), video_id))
    db.event(c, "info", "deleted in the web UI: %s (%.2f GB)" % (v["title"], freed / 1e9), v["channel_id"], video_id)
    if v["path"]:
        _bg("plex-scan", _plex_scan, os.path.dirname(v["path"]))


def update_video_meta(video_id, title=UNSET, summary=UNSET):
    """Edits of an episode's title / summary (None = back to YouTube's). Stored in the report JSON
    (edited_title / edited_summary), written to the episode NFO and pushed to Plex (locked). Files keep their names."""
    c = _db()
    v = _video(c, video_id)
    rep = json.loads(v["report"] or "{}")
    for key, val in (("edited_title", title), ("edited_summary", summary)):
        if val is UNSET:
            continue
        if val is None or not str(val).strip():
            rep.pop(key, None)
        else:
            rep[key] = str(val).strip()
    c.execute("UPDATE videos SET report=? WHERE id=?", (json.dumps(rep), video_id))
    if v["state"] != "done" or not v["path"]:
        return
    ch = c.execute("SELECT * FROM channels WHERE id=?", (v["channel_id"],)).fetchone()
    t = rep.get("edited_title") or v["title"]
    s = rep.get("edited_summary") or v["summary"] or rep.get("summary") or ""
    base = os.path.splitext(v["path"])[0]
    with pipeline.FILE_LOCK:
        if os.path.exists(base + ".nfo"):
            nfo.episode(base + ".nfo", video_id, t, s, v["local_date"], v["season"], v["episode"], ch["title"],
                        v["out_duration"], ch["topic"] or rep.get("category"),
                        tags=["Removed from YouTube"] if v["removed_at"] else ())
    _bg("plex-meta", _push_episode_meta, v["path"], t, s, "edited_title" in rep, "edited_summary" in rep)


def _plex_episode_key(path):
    """ratingKey of the Plex episode for a library file (searching its show only)."""
    key = _section_key()
    show = os.path.basename(os.path.dirname(os.path.dirname(path)))
    r = plexhttp.get("%s/library/sections/%s/all" % (config.PLEX_URL, key), params={"type": 4, "show.title": show},
                     headers=PH(), timeout=30).json()
    want = config.PLEX_ROOT + path[len(config.ROOT):]
    for ep in r["MediaContainer"].get("Metadata", []):
        for m in ep.get("Media", []):
            if any(p.get("file") == want for p in m.get("Part", [])):
                return ep["ratingKey"]
    return None


def _push_episode_meta(path, title, summary, lock_title, lock_summary):
    rk = _plex_episode_key(path)
    if rk:
        plexhttp.put("%s/library/metadata/%s" % (config.PLEX_URL, rk), headers=PH(), timeout=20,
                     params={"type": 4, "id": rk, "title.value": title, "title.locked": 1 if lock_title else 0,
                             "summary.value": summary, "summary.locked": 1 if lock_summary else 0})


def video_thumbnail_variants(video_id):
    _todo("Choosing another thumbnail")


def set_video_thumbnail(video_id, variant=None, image=None, content_type=None):
    _todo("Changing the thumbnail")


# ------------------------------------------------------------------ channels
def _resolve_ref(q):
    """A channel link / @handle / UC id / video link -> something ytdl.channel_tab() accepts."""
    q = q.strip()
    m = re.search(r"(UC[\w-]{22})", q)
    if m:
        return m.group(1)
    m = re.search(r"youtube\.com/(@[^/?#\s]+)", q) or re.match(r"^(@[^/?#\s]+)$", q)
    if m:
        return m.group(1)
    m = re.search(r"youtube\.com/(c|user)/([^/?#\s]+)", q)
    if m:
        return "https://www.youtube.com/%s/%s" % (m.group(1), m.group(2))
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/live/)([\w-]{11})", q)
    if m:
        import yt_dlp
        from . import ytdl
        with yt_dlp.YoutubeDL(ytdl._params(skip_download=True)) as y:
            info = y.extract_info("https://www.youtube.com/watch?v=" + m.group(1), download=False, process=False)
        if info.get("channel_id"):
            return info["channel_id"]
    if re.match(r"^[\w.\-]{3,}$", q):
        return "@" + q
    raise ActionError(400, "bad_request", "That doesn't look like a YouTube channel link or @handle.")


def _queue_listing(c, chrow, entries, priority=0):
    """Like the worker's queue_entries(): the channel's uploads, newest first (rank 0 = newest); Shorts (< 61 s),
    streams and members-only are left out; known videos just get their rank."""
    rank, added = 0, 0
    for e in entries[:LIST_DEPTH]:
        if (e.get("duration") or 0) < 61 or e.get("live_status") in ("is_live", "is_upcoming", "was_live", "post_live"):
            continue
        vid = e.get("id")
        row = c.execute("SELECT state FROM videos WHERE id=?", (vid,)).fetchone()
        if not row:
            members = e.get("availability") in ("subscriber_only", "premium_only", "needs_auth")
            db.upsert(c, "videos", vid, channel_id=chrow["id"], title=e.get("title"), duration=e.get("duration"),
                      state="skipped_members_only" if members else "queued", rank=rank, queued_at=db.now(),
                      created_at=db.now(), attempts=0, priority=priority, published_ts=e.get("timestamp"),
                      reason="members only" if members else None)
            if members:
                continue
            added += 1
        elif row["state"] not in ("skipped", "pruned", "skipped_members_only"):
            db.upsert(c, "videos", vid, rank=rank)
        if not row or row["state"] not in ("skipped", "skipped_members_only"):
            rank += 1
    return added


def add_channel(query, settings=None, source="manual"):
    """Add a channel by URL / @handle / UC id / video URL (signed out). Returns the channel id.
    source: 'manual' (Add channel) | 'youtube-sub' (the subscription sync) | 'csv-import' (Import)."""
    from . import ytdl
    c = _db()
    ref = _resolve_ref(query)
    known = None
    if ref.startswith("UC"):
        known = c.execute("SELECT id, pending_removal_at FROM channels WHERE id=?", (ref,)).fetchone()
    elif ref.startswith("@"):
        known = c.execute("SELECT id, pending_removal_at FROM channels WHERE lower(handle)=lower(?)", (ref,)).fetchone()
    if known and not known["pending_removal_at"]:
        raise ActionError(409, "conflict", "That channel is already in Tubarr.")
    try:
        ch = ytdl.channel_tab(ref, limit=LIST_DEPTH)
    except ytdl.BotCheck:
        raise ActionError(503, "youtube_busy", "YouTube asked Tubarr to slow down. Try again in a few minutes.")
    except Exception as e:
        raise ActionError(404, "not_found", "YouTube doesn't know that channel (%s)." % redact.text(str(e))[:120])
    cid = ch.get("channel_id")
    if not cid:
        raise ActionError(404, "not_found", "That link isn't a YouTube channel.")
    row = c.execute("SELECT id, pending_removal_at FROM channels WHERE id=?", (cid,)).fetchone()
    if row and not row["pending_removal_at"]:
        raise ActionError(409, "conflict", "That channel is already in Tubarr.")
    if row and row["pending_removal_at"]:
        restore_channel(cid)
        return cid
    tomb = c.execute("SELECT * FROM removed_channels WHERE channel_id=?", (cid,)).fetchone()
    if tomb and source == "youtube-sub":
        raise ActionError(409, "conflict", "That channel was deleted in Tubarr; add it by hand to bring it back.")
    chrow = pipeline.ensure_channel(c, ch, source=source, use_llm=False)
    order = (c.execute("SELECT COALESCE(MAX(csv_order), 0) + 1 FROM channels").fetchone()[0])
    db.upsert(c, "channels", cid, csv_order=order, last_checked=db.now(), enabled=1)
    handle = ch.get("uploader_id") or (ref if ref.startswith("@") else None)
    if handle and not c.execute("SELECT 1 FROM subs WHERE handle=?", (handle,)).fetchone():
        c.execute("INSERT INTO subs(handle, title, csv_order, channel_id, state, checked_at) VALUES(?,?,?,?, 'ok', ?)",
                  (handle, chrow["title"], order, cid, db.now()))
    c.execute("DELETE FROM removed_channels WHERE channel_id=?", (cid,))
    n = _queue_listing(c, chrow, ch.get("entries") or [])
    db.event(c, "info", "channel added (%s): %d uploads queued" % (source, n), cid)
    if settings:
        update_channel_settings(cid, settings)
    return cid


def flag_channel_removed(channel_id, reason="unsubscribed", grace_days=3):
    """Start the removal countdown: nothing new downloads; after `grace_days` the worker deletes it."""
    c = _db()
    ch = _channel(c, channel_id)
    if ch["pending_removal_at"]:
        return
    meta = _meta(ch)
    meta.update(removal_reason=reason, removal_grace_days=grace_days)
    c.execute("UPDATE channels SET pending_removal_at=?, meta=? WHERE id=?", (db.now(), json.dumps(meta), channel_id))
    db.event(c, "info", "channel flagged for removal (%s, %d days)" % (reason, grace_days), channel_id)


def restore_channel(channel_id):
    """Undo a pending removal (an 'unsubscribed' channel is then left alone by the subscription sync)."""
    c = _db()
    ch = _channel(c, channel_id)
    meta = _meta(ch)
    if meta.get("removal_reason") == "unsubscribed":
        meta["kept_while_unsubscribed"] = True
    meta.pop("removal_reason", None)
    meta.pop("removal_grace_days", None)
    c.execute("UPDATE channels SET pending_removal_at=NULL, enabled=1, meta=? WHERE id=?", (json.dumps(meta), channel_id))
    db.event(c, "info", "channel removal undone", channel_id)


def delete_channel_now(channel_id, _wait=False, manual=False):
    """Delete a channel for good: its library folder, the show in Plex, its rows (a tombstone keeps the
    subscription sync from re-adding it)."""
    c = _db()
    ch = _channel(c, channel_id)
    show = pipeline.show_dir(ch["folder"]) if ch["folder"] else None
    paths = [r[0] for r in c.execute("SELECT path FROM videos WHERE channel_id=? AND state='done'", (channel_id,))]
    _need_not_playing(paths, "a video from this channel")
    c.execute("UPDATE channels SET enabled=0, pending_removal_at=COALESCE(pending_removal_at, ?) WHERE id=?",
              (db.now(), channel_id))
    c.execute("INSERT OR REPLACE INTO removed_channels VALUES(?,?,?,?,?)",
              (channel_id, ch["handle"], ch["title"], db.now(), _meta(ch).get("removal_reason") or "manual"))
    t = _bg("delete-channel", _delete_channel_files, channel_id, show, manual)
    if _wait:
        t.join()


def _folder_is_own(c, channel_id, show):
    """True only for a show folder directly inside the library that no other channel uses (case-insensitively)."""
    root = os.path.realpath(config.ROOT)
    real = os.path.realpath(show)
    if os.path.dirname(real) != root or real == root:
        return False
    folder = os.path.basename(real)
    other = c.execute("SELECT 1 FROM channels WHERE id<>? AND lower(folder)=lower(?)", (channel_id, folder)).fetchone()
    return other is None


def _delete_channel_files(channel_id, show, manual=False):
    c = db.connect()
    # Rule: never delete a video that's been taken down / DMCA'd. Ask YouTube about every video first:
    # taken-down ones are protected (the channel stays with just those), an unclear answer postpones the whole removal.
    from .ytcheck import youtube_check
    if manual:
        # Someone pressed "Delete everything right away" and confirmed it (the dialog names the kept-forever
        # and removed-from-YouTube videos): everything goes, no YouTube check. Automatic removals (the 3-day
        # grace, the subscription sync) still take the checked path below.
        n = c.execute("SELECT COUNT(*) FROM videos WHERE channel_id=? AND state='done' AND "
                      "(keep_forever=1 OR removed_at IS NOT NULL)", (channel_id,)).fetchone()[0]
        if n:
            log.info("channel %s deleted by hand, including %d kept / removed-from-YouTube video(s)", channel_id, n)
    kept = []
    for v in ([] if manual else c.execute("SELECT * FROM videos WHERE channel_id=? AND state='done'", (channel_id,)).fetchall()):
        if v["keep_forever"] or v["removed_at"]:
            kept.append(v["id"])
            continue
        res = youtube_check(v["id"])
        if res is None:
            log.info("channel removal %s postponed: couldn't confirm %s is still on YouTube", channel_id, v["id"])
            return                                    # pending_removal_at stays set -> retried next cycle
        if res is not True:
            c.execute("UPDATE videos SET removed_at=?, removed_reason=?, keep_forever=1 WHERE id=?",
                      (db.now(), "checked before channel removal: " + res[1], v["id"]))
            c.commit()
            log.warning("REMOVED FROM YOUTUBE %s -> kept forever (channel removal): %s", v["id"], res[1][:120])
            kept.append(v["id"])
        time.sleep(1)
    if kept:
        with pipeline.FILE_LOCK:
            for v in c.execute("SELECT * FROM videos WHERE channel_id=? AND state='done'", (channel_id,)).fetchall():
                if v["id"] not in kept:                          # confirmed still on YouTube above
                    _remove_files(v["path"])
                    db.upsert(c, "videos", v["id"], state="pruned", reason="channel removed", updated_at=db.now())
        c.execute("UPDATE channels SET pending_removal_at=NULL WHERE id=?", (channel_id,))
        db.event(c, "info", "channel removed, but %d taken-down video(s) kept forever" % len(kept), channel_id)
        if show:
            _plex_scan(show)
        return
    with pipeline.FILE_LOCK:
        if show and os.path.isdir(show):
            if _folder_is_own(c, channel_id, show):
                shutil.rmtree(show, ignore_errors=True)
            else:                                     # shared with another channel (or odd): only this one's files
                for v in c.execute("SELECT path FROM videos WHERE channel_id=? AND path IS NOT NULL",
                                   (channel_id,)).fetchall():
                    _remove_files(v["path"])
                log.warning("channel %s: show folder %s is shared, removed only this channel's videos", channel_id, show)
        c.execute("DELETE FROM videos WHERE channel_id=? AND state NOT IN ('downloading','processing','upgrading')", (channel_id,))
        try:
            c.execute("DELETE FROM series WHERE channel_id=?", (channel_id,))
        except Exception:
            pass
        c.execute("UPDATE subs SET state='removed', checked_at=? WHERE channel_id=?", (db.now(), channel_id))
        busy = c.execute("SELECT 1 FROM videos WHERE channel_id=?", (channel_id,)).fetchone()
        if not busy:
            c.execute("DELETE FROM channels WHERE id=?", (channel_id,))
    db.event(c, "info", "channel deleted in the web UI", channel_id)
    if show:
        _plex_scan(show)


def retention_victims(c, ch, playing):
    """[(video row, why)] of a channel's library videos outside its own retention window (count / days / since),
    never protected ones: keep forever, the newest PROTECT_NEWEST, gone from YouTube, playing now."""
    meta = _meta(ch)
    ret = meta.get("retention") or {}
    vids = c.execute("SELECT * FROM videos WHERE state='done' AND channel_id=? ORDER BY local_date DESC, episode DESC",
                     (ch["id"],)).fetchall()
    out = {}
    if ch["keep_count"]:
        for v in vids[max(ch["keep_count"], 1):]:
            out[v["id"]] = (v, "retention: keep the newest %d" % ch["keep_count"])
    cutoff = None
    if ret.get("mode") == "days" and ret.get("days"):
        cutoff = datetime.fromtimestamp(time.time() - 86400 * int(ret["days"])).strftime("%Y-%m-%d")
    elif ret.get("mode") == "since" and ret.get("since"):
        cutoff = str(ret["since"])[:10]
    if cutoff:
        for v in vids:
            if (v["local_date"] or "9999") < cutoff:
                out[v["id"]] = (v, "retention: only since %s" % cutoff)
    newest = {v["id"] for v in vids[:settings.get("protect_newest")]}
    return [(v, why) for vid, (v, why) in out.items()
            if vid not in newest and not v["keep_forever"] and not v["removed_at"] and v["path"] not in playing]


def apply_retention(channel_id, retention=UNSET):
    """Apply the channel's (new) window now: returns {"removed": n, "queued": n}."""
    c = _db()
    ch = _channel(c, channel_id)
    if retention is not UNSET:
        update_channel_settings(channel_id, {"retention": retention})
        ch = _channel(c, channel_id)
    playing = playing_paths()
    removed = 0
    if playing is not None:
        for v, why in retention_victims(c, ch, playing):
            with pipeline.FILE_LOCK:
                v = c.execute("SELECT * FROM videos WHERE id=?", (v["id"],)).fetchone()
                if not v or v["state"] != "done":
                    continue
                _remove_files(v["path"])
                c.execute("UPDATE videos SET state='pruned', reason=?, updated_at=? WHERE id=?", (why, db.now(), v["id"]))
            removed += 1
        if removed and ch["folder"]:
            _plex_scan(pipeline.show_dir(ch["folder"]))
    queued = _bring_back(c, ch)
    return {"removed": removed, "queued": queued}


def _bring_back(c, ch):
    """Videos a settings change lets in again: 'too long' ones now within the limit, stream replays when replays are
    on, retention-pruned ones inside the new window. They go back into the queue as backlog."""
    n = 0
    maxd = ch["max_duration"] or 0
    for v in c.execute("SELECT id, duration, reason, rank, published_ts FROM videos WHERE channel_id=? AND "
                       "((state='skipped' AND (reason LIKE 'too long%' OR reason='livestream replay')) OR "
                       "(state='pruned' AND reason LIKE 'retention%'))", (ch["id"],)).fetchall():
        why = v["reason"] or ""
        ok = False
        if why.startswith("too long"):
            ok = not maxd or (v["duration"] or 0) <= maxd
        elif why == "livestream replay":
            ok = ch["skip_lives"] == 0
        elif why.startswith("retention"):
            ok = not ch["keep_count"] or (v["rank"] if v["rank"] is not None else 999) < ch["keep_count"]
            ret = _meta(ch).get("retention") or {}
            if ret.get("mode") in ("days", "since") or (not ch["keep_count"] and not ret):
                ok = ok and not ret.get("mode") in ("days", "since")
        if ok:
            _requeue(c, v["id"], 0)
            n += 1
    return n


def update_channel_settings(channel_id, settings):
    """Per-channel overrides: retention, max_duration_minutes, quality, include_live, sponsorblock (None = default).
    Stored in keep_count / max_duration (seconds) / skip_lives + channels.meta; applied at once."""
    c = _db()
    ch = _channel(c, channel_id)
    meta, sets = _meta(ch), {}
    if "retention" in settings:
        r = settings["retention"]
        if r is None or (isinstance(r, dict) and r.get("mode") in (None, "fill")):
            meta.pop("retention", None)
            sets["keep_count"] = None
        else:
            mode = r.get("mode") if isinstance(r, dict) else None
            if mode == "count":
                n = int(r.get("count") or 0)
                if not 1 <= n <= 5000:
                    raise ActionError(400, "bad_request", "Keep between 1 and 5000 videos.")
                meta["retention"], sets["keep_count"] = {"mode": "count", "count": n}, n
            elif mode == "days":
                d = int(r.get("days") or 0)
                if not 1 <= d <= 36500:
                    raise ActionError(400, "bad_request", "Pick a number of days.")
                meta["retention"], sets["keep_count"] = {"mode": "days", "days": d}, None
            elif mode == "since":
                s = str(r.get("since") or "")[:10]
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", s):
                    raise ActionError(400, "bad_request", "Pick a date.")
                meta["retention"], sets["keep_count"] = {"mode": "since", "since": s}, None
            elif mode in ("forever", "new_only"):
                meta["retention"], sets["keep_count"] = {"mode": mode}, None
            else:
                raise ActionError(400, "bad_request", "Unknown retention mode.")
    if "max_duration_minutes" in settings:
        m = settings["max_duration_minutes"]
        sets["max_duration"] = int(m) * 60 if m else None
    if "quality" in settings:
        q = settings["quality"]
        if q is None:
            meta.pop("quality", None)
        elif q in ("2160p", "1440p", "1080p", "720p"):
            meta["quality"] = q
        else:
            raise ActionError(400, "bad_request", "Unknown quality.")
    if "include_live" in settings:
        sets["skip_lives"] = 0 if settings["include_live"] else 1
    if "sponsorblock" in settings:
        if settings["sponsorblock"] is None:
            meta.pop("sponsorblock", None)
        else:
            meta["sponsorblock"] = list(settings["sponsorblock"])
    sets["meta"] = json.dumps(meta)
    c.execute("UPDATE channels SET %s WHERE id=?" % ",".join("%s=?" % k for k in sets), list(sets.values()) + [channel_id])
    db.event(c, "info", "channel settings changed: %s" % ", ".join(sorted(settings)), channel_id)
    _bg("apply-retention", apply_retention, channel_id)


def update_channel_meta(channel_id, meta):
    """Edits of the show: title, summary, genre, topic (None reverts). Stored in channels.meta["edits"];
    tvshow.nfo is rewritten and the show is updated (and locked) in Plex. The folder name never changes."""
    c = _db()
    ch = _channel(c, channel_id)
    m = _meta(ch)
    edits = m.get("edits") or {}
    for k in ("title", "summary", "genre"):
        if k in meta:
            if meta[k] is None or not str(meta[k]).strip():
                edits.pop(k, None)
            else:
                edits[k] = str(meta[k]).strip()
    m["edits"] = edits
    sets = {"meta": json.dumps(m)}
    if "topic" in meta:
        sets["topic"] = meta["topic"]                      # None -> the organizer assigns one again
    c.execute("UPDATE channels SET %s WHERE id=?" % ",".join("%s=?" % k for k in sets), list(sets.values()) + [channel_id])
    ch = _channel(c, channel_id)
    title = edits.get("title") or ch["title"]
    summary = edits.get("summary") or ch["summary"]
    genre = edits.get("genre") or ch["topic"]
    d = pipeline.show_dir(ch["folder"])
    if os.path.isdir(d):
        nfo.tvshow(os.path.join(d, "tvshow.nfo"), channel_id, title, summary, genre=genre, handle=ch["handle"])
    _bg("plex-show-meta", _push_show_meta, ch["folder"], title, summary, genre, ch["topic"], set(edits))


def _push_show_meta(folder, title, summary, genre, topic, locked):
    key = _section_key()
    r = plexhttp.get("%s/library/sections/%s/all" % (config.PLEX_URL, key), params={"type": 2}, headers=PH(), timeout=30).json()
    want = config.PLEX_ROOT + "/" + folder
    rk = None
    for sh in r["MediaContainer"].get("Metadata", []):
        loc = plexhttp.get("%s/library/metadata/%s" % (config.PLEX_URL, sh["ratingKey"]), headers=PH(), timeout=20).json()
        for l in (loc["MediaContainer"]["Metadata"][0].get("Location") or []):
            if l.get("path") == want:
                rk = sh["ratingKey"]
        if rk:
            break
    if not rk:
        return
    params = {"type": 2, "id": rk, "title.value": title, "title.locked": 1 if "title" in locked else 0,
              "summary.value": summary or "", "summary.locked": 1 if "summary" in locked else 0,
              "genre[0].tag.tag": genre or "", "genre.locked": 1}
    if topic:
        params.update({"collection[0].tag.tag": topic, "collection.locked": 1})
    plexhttp.put("%s/library/metadata/%s" % (config.PLEX_URL, rk), headers=PH(), timeout=20, params=params)


def channel_poster_variants(channel_id):
    _todo("Choosing another poster")


def set_channel_poster(channel_id, variant=None, image=None, content_type=None):
    _todo("Changing the poster")


def _rss_check(c, ch):
    from . import catalog
    feed, code = catalog.rss(playlist_id="UULF" + ch["id"][2:])
    n = 0
    for x in (feed or [])[:5]:
        if c.execute("SELECT 1 FROM videos WHERE id=?", (x["id"],)).fetchone():
            continue
        pub = None
        try:
            pub = int(datetime.fromisoformat(x["published"]).timestamp())
        except Exception:
            pass
        db.upsert(c, "videos", x["id"], channel_id=ch["id"], title=x["title"], state="queued", rank=0, priority=1,
                  queued_at=db.now(), created_at=db.now(), attempts=0, published_ts=pub)
        n += 1
    return n


def refresh_channel(channel_id):
    """Check one channel's feed for new uploads now."""
    c = _db()
    ch = _channel(c, channel_id)
    _bg("refresh-channel", lambda: _rss_check(db.connect(), ch))


def refresh_all_channels():
    """Check every channel's feed now (in the background, about 2 per second)."""
    def run():
        c2 = db.connect()
        for ch in c2.execute("SELECT * FROM channels WHERE gone_at IS NULL AND COALESCE(enabled,1)=1 "
                             "AND pending_removal_at IS NULL").fetchall():
            try:
                _rss_check(c2, ch)
            except Exception as e:
                log.warning("refresh %s: %s", ch["title"], e)
            time.sleep(0.5)
    _bg("refresh-all", run)


def _repolish(args):
    subprocess.Popen([sys.executable, "-m", "tubarr.repolish"] + args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     cwd="/app", start_new_session=True)


def repolish_channel(channel_id):
    """Rebuild a channel's poster, background, season posters and episode text (repolish.py for one channel).
    The channel is passed by id after "--", so nothing from its title can ever be read as an option."""
    c = _db()
    ch = _channel(c, channel_id)
    _repolish(["--art", "--", ch["id"]])


def repolish_all():
    """repolish.py for every channel (text + art, no downloads)."""
    _repolish(["--art", "--all"])


def set_series(channel_id, playlist_id, enabled=None, order=None):
    _todo("Choosing series by hand (Tubarr detects a channel's series playlists on its own)")


# ------------------------------------------------------------------ downloads
def pause_downloads():
    """Stop starting new downloads (running ones finish)."""
    with open(PAUSE_FLAG, "w") as f:
        f.write(db.now())


def resume_downloads():
    if os.path.exists(PAUSE_FLAG):
        os.remove(PAUSE_FLAG)


def retry_failed():
    """Queue every retryable failed video again. Returns how many."""
    c = _db()
    n = c.execute("UPDATE videos SET state='queued', attempts=0, transient_fails=0, reason=NULL, retry_at=NULL, queued_at=? "
                  "WHERE state IN ('failed','retry') AND removed_at IS NULL", (db.now(),)).rowcount
    return n


# ------------------------------------------------------------------ settings, Plex, notifications
def _num(v, lo, hi, what, cast=float):
    try:
        x = cast(v)
    except (TypeError, ValueError):
        raise ActionError(400, "bad_request", "%s must be a number." % what) from None
    if not lo <= x <= hi:
        raise ActionError(400, "bad_request", "%s must be between %s and %s." % (what, lo, hi))
    return x


def update_settings(patch):
    """PATCH /api/settings from the Settings page. Only fields that really changed are stored (/data/settings.json);
    the worker picks them up within seconds."""
    if not isinstance(patch, dict):
        raise ActionError(400, "bad_request", "Send the settings as an object.")
    cur = settings.load()
    ch = {}
    d = patch.get("downloads")
    if isinstance(d, dict):
        if "fill_target_bytes" in d:
            gb = _num(d["fill_target_bytes"], 10e9, 1e15, "The fill target") / 1e9
            if abs(gb - cur["soft_cap_gb"]) > 0.5:
                ch["soft_cap_gb"] = round(gb, 1)
        if "max_height" in d:
            h = _num(d["max_height"], 144, 4320, "Best quality", int)
            if h not in (2160, 1440, 1080, 720):
                raise ActionError(400, "bad_request", "Best quality must be 2160, 1440, 1080 or 720.")
            if h != cur["max_height"]:
                ch["max_height"] = h
        if "allow_av1" in d:
            if not isinstance(d["allow_av1"], bool):
                raise ActionError(400, "bad_request", "Allow AV1 must be true or false.")
            if d["allow_av1"] != cur["allow_av1"]:
                ch["allow_av1"] = d["allow_av1"]
        if "protect_newest" in d:
            n = _num(d["protect_newest"], 0, 50, "Never remove each channel's newest", int)
            if n != cur["protect_newest"]:
                ch["protect_newest"] = n
        p = d.get("human_pace")
        if isinstance(p, dict):
            if "per_day_max" in p:
                n = _num(p["per_day_max"], 1, 1000, "Max downloads per day", int)
                if n != cur["nightly_cap"]:
                    ch["nightly_cap"] = n
            lo_m = round(cur["min_gap_min_h"] * 60, 2)
            hi_m = round(cur["min_gap_max_h"] * 60, 2)
            nlo = _num(p.get("gap_min_minutes", lo_m), 0, 24 * 60, "Minutes between downloads")
            nhi = _num(p.get("gap_max_minutes", hi_m), 0, 24 * 60, "Minutes between downloads")
            if nhi < nlo:
                raise ActionError(400, "bad_request", "The longest pause can't be shorter than the shortest one.")
            if round(nlo, 2) != lo_m:
                ch["min_gap_min_h"] = nlo / 60.0
            if round(nhi, 2) != hi_m:
                ch["min_gap_max_h"] = nhi / 60.0
    n = patch.get("notifications")
    if isinstance(n, dict):
        from . import notify
        ev = dict(cur["notifications"]["events"])
        for k, v in (n.get("events") or {}).items():
            if k in ev:
                ev[k] = bool(v)
        new = {"events": ev}
        if "discord_webhook_url" in n:                    # write-only: absent = keep the saved link, "" = remove it
            url = str(n.get("discord_webhook_url") or "").strip()
            if url and not notify.valid(url):
                raise ActionError(400, "bad_request", "That doesn't look like a Discord webhook link.")
            if url != cur["notifications"]["discord_webhook_url"]:
                new["discord_webhook_url"] = url
        if new != {"events": cur["notifications"]["events"]}:
            ch["notifications"] = new
    if ch:
        settings.save(ch)
        c = _db()
        db.event(c, "info", "settings changed in the web UI: %s" % ", ".join(sorted(ch)))
        log.info("settings changed: %s", {k: v for k, v in ch.items() if k != "notifications"})


def plex_scan():
    """Scan the whole YouTube library in Plex."""
    _need_plex()
    plexhttp.get("%s/library/sections/%s/refresh" % (config.PLEX_URL, _section_key()), headers=PH(), timeout=20)


def test_notification(webhook_url=None):
    """Send one test message to the given (unsaved) or the saved webhook."""
    from . import notify
    url = (webhook_url if isinstance(webhook_url, str) and webhook_url.strip() else
           settings.get("notifications")["discord_webhook_url"] or "").strip()
    if not url:
        raise ActionError(400, "bad_request", "Paste a Discord webhook link first.")
    if not notify.valid(url):
        raise ActionError(400, "bad_request", "That doesn't look like a Discord webhook link.")
    try:
        notify.post(url, "Tubarr test message: notifications work.")
    except Exception as e:
        raise ActionError(502, "internal", "Discord didn't take the message (%s)." % redact.text(str(e))[:120]) from None


# ------------------------------------------------------------------ composites (use the ones above)
_REF = re.compile(r"^(@[A-Za-z0-9][A-Za-z0-9._\-\u00b7]{1,100}|UC[\w-]{22}|"
                  r"https://www\.youtube\.com/(c|user)/[A-Za-z0-9._\-]{1,100})$")


def import_apply(add, remove):
    """Import (Google Takeout CSV / pasted list): the chosen @handles / UC ids are queued as subscriptions; the
    worker looks them up in the background a few seconds apart (a burst of lookups is exactly what YouTube
    flags). The chosen channel ids are flagged for removal (reason 'unsubscribed').
    Returns {"added": n, "flagged_for_removal": n, "queued": True}."""
    if not isinstance(add, list) or not isinstance(remove, list) or len(add) > 5000 or len(remove) > 5000:
        raise ActionError(400, "bad_request", "Send lists of channels.")
    c = _db()
    added = flagged = 0
    order = (c.execute("SELECT COALESCE(MAX(csv_order), 0) FROM subs").fetchone()[0] or 0)
    for h in add:
        h = str(h or "").strip()
        if not _REF.match(h):
            continue
        if c.execute("SELECT 1 FROM subs WHERE lower(handle)=lower(?)", (h,)).fetchone() or \
                c.execute("SELECT 1 FROM channels WHERE id=? OR lower(handle)=lower(?)", (h, h)).fetchone():
            continue
        order += 1
        c.execute("INSERT INTO subs(handle, title, csv_order, state) VALUES(?,?,?,'new')", (h, "", order))
        added += 1
    for cid in remove:
        flag_channel_removed(cid, reason="unsubscribed")
        flagged += 1
    if added:
        db.event(c, "info", "import: %d channels queued for lookup" % added)
    return {"added": added, "flagged_for_removal": flagged, "queued": True}
