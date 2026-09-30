"""Tubarr background worker - the container's main process.

Retention: the library FILLS to the fill target and then rolls.
  * New uploads first: every channel's long-form RSS feed (UULF) is polled about every 5 minutes (jittered); new
    uploads jump the queue. Upcoming premieres/streams are retried shortly after their start time. Shorts,
    livestream replays and members-only videos are skipped.
  * Backlog: newest published first across all channels, at a human pace (one download at a time by default, a
    random pause between downloads, a daily cap for backlog downloads) until the library reaches the fill target.
  * At the target it rolls: a newer video only comes in if the library's oldest deletable video goes out. Never
    deleted: "keep forever" items, each channel's newest N, anything being played right now (when Plex is
    connected), and anything that has disappeared from YouTube.
  * Videos that disappear from YouTube (removed, private, terminated, copyright) are detected by a daily per-channel
    check, confirmed by one probe, flagged, labelled in Plex and protected forever.
Downloads: optional self-tuning concurrency (TUBARR_CONCURRENCY / TUBARR_CONCURRENCY_MAX, default 1 = one at a time).
Status: /data/status.json every 10 s; one log line per QUEUED/START/DOWNLOADED/DONE/SKIP/FAIL/PRUNE/REMOVED/TUNE.
"""
import csv
import itertools
import json
import logging
import os
import queue
import random
import re
import shutil
import statistics
import threading
import time
from datetime import datetime, timezone

import requests

from . import account, catalog, claimq, config, db, net, nfo, pipeline, plexhttp, redact, settings, ytdl

CONC_START = int(os.environ.get("TUBARR_CONCURRENCY", "1"))
CONC_MAX = max(1, int(os.environ.get("TUBARR_CONCURRENCY_MAX", os.environ.get("TUBARR_CONCURRENCY", "1"))))
CONC_MIN = min(3, CONC_MAX)
TUNE_EVERY = 180
FINALIZERS = int(os.environ.get("TUBARR_FINALIZERS", "3"))
MAX_PENDING = int(os.environ.get("TUBARR_MAX_PENDING", "8"))   # downloaded, waiting to finalize
POLL_MIN = float(os.environ.get("TUBARR_POLL_MIN", "5"))
SOFT_CAP = settings.defaults()["soft_cap_gb"] * 1e9                     # fill target (Settings -> Downloads)
PROTECT_NEWEST = int(os.environ.get("TUBARR_PROTECT_NEWEST", "3"))
LIST_DEPTH = int(os.environ.get("TUBARR_LIST_DEPTH", "30"))      # uploads listed per channel for the backlog
REMOVED_CHECK_H = 24
DEFAULT_RATE = 1.25e6                                            # bytes per second of video (~4.5 GB/h, 4K VP9)
SUBS = os.environ.get("TUBARR_SUBS", os.path.join(config.DATA, "import", "subscriptions.csv"))   # optional drop-in
STATUS = os.path.join(config.DATA, "status.json")
PAUSE_FLAG = os.path.join(config.DATA, "downloads.paused")          # web UI: pause new downloads
# Human pace: a daily ceiling for backlog downloads plus a randomized gap between individual downloads, one video
# at a time -- the way a person saving things occasionally would, never a burst. Bulk, scraper-like volume is what
# gets an IP address flagged by YouTube; slowing down is the fix. See claim().
NIGHTLY_CAP = settings.defaults()["nightly_cap"]
NEW_UPLOAD_WINDOW = 48 * 3600                   # newer than this = a new upload: not held by the daily cap


def is_fresh(v, now=None):
    return int(v["priority"] or 0) >= 2 or int(v["published_ts"] or 0) >= (now or time.time()) - NEW_UPLOAD_WINDOW
MIN_GAP_MIN_H = settings.defaults()["min_gap_min_h"]
MIN_GAP_MAX_H = settings.defaults()["min_gap_max_h"]
REMOVED_LABEL = "Removed from YouTube"
PROXY_ERR = re.compile(r"(?i)proxy|socks|tunnel connection failed|407")
TRANSIENT = re.compile(r"more expected|timed out|Connection reset|IncompleteRead|Remote end closed|Connection aborted|"
                       r"download failed at byte|Temporary failure|Connection refused|EOF occurred|Read timed out")
# GONE_PAT (what counts as "gone from YouTube") is shared with ytcheck.py, so there is exactly one definition.

os.makedirs(os.path.join(config.DATA, "logs"), exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(threadName)s %(name)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler(),
                              logging.FileHandler(os.path.join(config.DATA, "logs", "worker.log"))])
for noisy in ("urllib3", "plexapi"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
redact.install()                                 # secrets never reach the log files
log = logging.getLogger("tubarr.worker")

LOCK = threading.Lock()
STOP = threading.Event()
ACTIVE = {}                                   # download slot n -> live progress dict
FIN = {}                                      # finalizer m -> item being finalized
INFLIGHT = {}                                 # video id -> estimated bytes not yet in the library
PROC_Q = queue.PriorityQueue()                # (new upload first, arrival order, job) waiting for a finalizer
SEQ = itertools.count()
RECENT, PRUNED = [], []
SPEED = []                                    # (ts, aggregate bytes/s) samples from the status writer
FIN_T = []                                    # (ts, finalize seconds)
PUSH = []                                     # (ts, reason) YouTube push-back events
LATENCY = []                                  # minutes from upload to in-Plex (new uploads)
BK = {"until": 0.0, "delay": 0, "fail403": 0, "events": []}
TUNE = {"conc": min(CONC_START, CONC_MAX), "peak": min(CONC_START, CONC_MAX), "reason": "start", "at": time.time(),
        "hold_until": 0.0, "flat": 0, "prev_rate": None, "best_rate": 0.0, "best_conc": CONC_START,
        "settled_at": 0.0, "io_prev": None}
T0 = time.time()
DISC = {"resolved": 0, "errors": 0, "total": 0, "pass": 0, "last_poll": None, "poll_minutes": POLL_MIN}
STATE = {"backlog_paused": False, "watched": set(), "watched_at": 0, "removed_last": None, "next_claim_at": 0.0,
         "claimed": {}}                            # vid -> claim time, until its slot shows it in ACTIVE


# ------------------------------------------------------------------ helpers
def gb(n):
    return round((n or 0) / 1e9, 2)


# Trimarr (optional) keeps each untrimmed original as a hard link in .trim-originals. Once the trimmed file replaces
# the library copy, that link is the only one left (nlink 1) and its blocks are extra. Those backups get their own
# allowance, so they don't count against the library's fill target.
ORIGINALS_ALLOWANCE = float(os.environ.get("TUBARR_ORIGINALS_ALLOWANCE_GB", "0")) * 1e9
_ORIG_CACHE = [0.0, 0]


def originals_bytes():
    now = time.time()
    if now - _ORIG_CACHE[0] < 60:
        return _ORIG_CACHE[1]
    tot = 0
    for dp, dns, fns in os.walk(os.path.join(config.ROOT, ".trim-originals")):
        for f in fns:
            try:
                s = os.lstat(os.path.join(dp, f))
                if s.st_nlink == 1:
                    tot += s.st_blocks * 512
            except OSError:
                pass
    _ORIG_CACHE[:] = [now, tot]
    return tot


def used_bytes():
    # statvfs on the library filesystem, minus Trimarr's kept originals (they have their own allowance)
    return shutil.disk_usage(config.ROOT).used - originals_bytes()


def io_pressure():
    """Host I/O pressure ('some' avg60, %) - a cheap stand-in for pool latency visible from inside the container."""
    try:
        line = open("/proc/pressure/io").readline()
        return float(re.search(r"avg60=([\d.]+)", line).group(1))
    except Exception:
        return None


def pushback(reason, bot=False):
    """YouTube push-back: drop 2 slots (min 3) and hold 15 min; a real bot check also pauses everything briefly."""
    with LOCK:
        reason = redact.text(str(reason))
        PUSH.append((time.time(), str(reason)[:160]))
        old = TUNE["conc"]
        TUNE["conc"] = max(CONC_MIN, TUNE["conc"] - 2)
        TUNE["hold_until"] = time.time() + 900
        TUNE["reason"] = "push-back: %s" % str(reason)[:100]
        TUNE["at"] = time.time()
        if bot:
            account.note_failure(reason)
            BK["delay"] = min(3600, max(120, BK["delay"] * 2 if BK["delay"] else 120))
            BK["until"] = time.time() + BK["delay"]
        BK["fail403"] = 0
        BK["events"] = (BK["events"] + ["%s %s -> %d slots%s" % (time.strftime("%H:%M:%S"), str(reason)[:120], TUNE["conc"],
                                        ", pause %ds" % BK["delay"] if bot else "")])[-20:]
    log.warning("YOUTUBE PUSH-BACK -> %d -> %d slots, hold 15 min%s: %s", old, TUNE["conc"],
                " + pause %ds" % BK["delay"] if bot else "", str(reason)[:200])


def success():
    with LOCK:
        BK["fail403"] = 0


def wait_backoff():
    while not STOP.is_set() and time.time() < BK["until"]:
        time.sleep(5)


# ------------------------------------------------------------------ Plex
_SECTION = {}
PH = lambda: {"X-Plex-Token": config.PLEX_TOKEN, "Accept": "application/json"}


def section_key():
    if _SECTION.get("for") != (config.PLEX_URL, config.PLEX_SECTION):     # the setup page can change these live
        _SECTION.clear()
        _SECTION["for"] = (config.PLEX_URL, config.PLEX_SECTION)
    if "key" not in _SECTION:
        r = plexhttp.get(config.PLEX_URL + "/library/sections", headers=PH(), timeout=20).json()
        _SECTION["key"] = next(s["key"] for s in r["MediaContainer"]["Directory"] if s["title"] == config.PLEX_SECTION)
    return _SECTION["key"]


def plex_scan(folder_local):
    """Partial scan of one folder (the folder as Plex sees it: PLEX_ROOT/...); autoEmptyTrash drops deleted files."""
    if not config.plex_enabled():
        return
    try:
        path = config.PLEX_ROOT + folder_local[len(config.ROOT):]
        plexhttp.get("%s/library/sections/%s/refresh" % (config.PLEX_URL, section_key()), params={"path": path},
                     headers=PH(), timeout=20)
    except Exception as e:
        log.warning("plex scan failed: %s", e)


def map_rating_keys(c):
    k = section_key()
    r = plexhttp.get("%s/library/sections/%s/all" % (config.PLEX_URL, k), params={"type": 4}, headers=PH(), timeout=60).json()
    for ep in r["MediaContainer"].get("Metadata", []):
        for m in ep.get("Media", []):
            for p in m.get("Part", []):
                f = p.get("file", "")
                if f.startswith(config.PLEX_ROOT):
                    c.execute("UPDATE videos SET plex_rating_key=? WHERE path=?", (ep["ratingKey"], config.ROOT + f[len(config.PLEX_ROOT):]))
    c.commit()


def refresh_watched(c):
    """Everything watched by ANY account (server history), mapped to our videos."""
    if not config.plex_enabled():
        return STATE["watched"]
    if time.time() - STATE["watched_at"] < 600:
        return STATE["watched"]
    try:
        map_rating_keys(c)
        watched, start = set(), 0
        while True:
            r = plexhttp.get(config.PLEX_URL + "/status/sessions/history/all", params={"librarySectionID": section_key()},
                             headers=dict(PH(), **{"X-Plex-Container-Start": str(start), "X-Plex-Container-Size": "500"}),
                             timeout=60).json()
            items = r["MediaContainer"].get("Metadata", [])
            watched |= {str(x.get("ratingKey")) for x in items}
            if len(items) < 500:
                break
            start += 500
        STATE["watched"], STATE["watched_at"] = watched, time.time()
        if watched:
            c.execute("UPDATE videos SET watched=1 WHERE plex_rating_key IN (%s)" % ",".join("?" * len(watched)), list(watched))
            c.commit()
    except Exception as e:
        log.warning("watched refresh failed: %s", e)
    return STATE["watched"]


def plex_label(c, v, label):
    """Add a Plex label to an episode (so e.g. 'Removed from YouTube' is easy to find in Plex)."""
    if not config.plex_enabled():
        return
    try:
        rk = v["plex_rating_key"]
        if not rk:
            map_rating_keys(c)
            rk = c.execute("SELECT plex_rating_key FROM videos WHERE id=?", (v["id"],)).fetchone()[0]
        if rk:
            plexhttp.put("%s/library/metadata/%s" % (config.PLEX_URL, rk), headers=PH(), timeout=20,
                         params={"type": 4, "id": rk, "label[0].tag.tag": label, "label.locked": 1})
    except Exception as e:
        log.warning("plex label failed: %s", e)


# ------------------------------------------------------------------ space / retention
def rate_for(c, channel_id):
    r = c.execute("SELECT SUM(size), SUM(out_duration) FROM videos WHERE state='done' AND channel_id=? AND out_duration>0",
                  (channel_id,)).fetchone()
    if r[0] and r[1] and r[1] > 600:
        return r[0] / r[1]
    g = c.execute("SELECT SUM(size), SUM(out_duration) FROM videos WHERE state='done' AND out_duration>0").fetchone()
    return (g[0] / g[1]) if g[0] and g[1] and g[1] > 3600 else DEFAULT_RATE


def estimate(c, v):
    return int((v["duration"] or 600) * rate_for(c, v["channel_id"]) * 1.05)


def playing_paths():
    """Library files being played right now (by path: ratingKeys aren't always mapped yet), or None."""
    from . import actions
    p = actions.playing_paths()
    if p is None:
        log.warning("sessions check failed; pruning nothing this round")
    return p


def playing_keys():
    if not config.plex_enabled():
        return set()
    try:
        r = plexhttp.get(config.PLEX_URL + "/status/sessions", headers=PH(), timeout=15).json()
        return {str(m.get("ratingKey")) for m in r["MediaContainer"].get("Metadata", [])}
    except Exception as e:
        log.warning("sessions check failed (%s); pruning nothing this round", e)
        return None


# ---- safety check before ANY automatic delete: a video that was taken down (DMCA, removed, private) is exactly the
# one worth keeping, so YouTube is asked live first.
SKIP_UNTIL = {}                                   # video id -> epoch: couldn't confirm it's still on YouTube, retry later
from .ytcheck import youtube_check, GONE_PAT       # noqa: E402  (live check, shared with actions.py)


def prune_candidates(c):
    """Deletable: not keep-forever, not a channel's newest PROTECT_NEWEST, not playing now, not removed from
    YouTube, not from a channel that is gone from YouTube, not waiting on an inconclusive YouTube check."""
    playing = playing_paths()
    if playing is None:
        return []
    out = []
    for ch in c.execute("SELECT id FROM channels ch WHERE gone_at IS NULL AND COALESCE((CASE WHEN json_valid(ch.meta) THEN json_extract(ch.meta,'$.retention.mode') END),'') <> 'forever'").fetchall():
        vids = c.execute("""SELECT * FROM videos WHERE state='done' AND channel_id=? ORDER BY local_date DESC, episode DESC""",
                         (ch[0],)).fetchall()
        out += [v for v in vids[PROTECT_NEWEST:] if not v["keep_forever"] and not v["removed_at"]
                and v["path"] not in playing and SKIP_UNTIL.get(v["id"], 0) < time.time()]
    return out


CHECKING = set()  # video ids currently mid youtube_check() -- see delete_video()'s dedup guard below


def delete_video(c, v, why, check=True):
    """Automatic delete. First asks YouTube live whether this exact video is still up: only a confirmed-available
    video is deleted; a taken-down one is protected forever; an unclear answer postpones the delete."""
    if check:
        if SKIP_UNTIL.get(v["id"], 0) > time.time():
            return 0
        # The check is a real YouTube request: never spend one while the shared backoff (BK) is active.
        if time.time() < BK["until"]:
            return 0
        # Several slots may pick the same candidate at once: claim the id so only one check runs.
        with LOCK:
            if v["id"] in CHECKING:
                return 0
            CHECKING.add(v["id"])
        try:
            res = youtube_check(v["id"])
        finally:
            with LOCK:
                CHECKING.discard(v["id"])
        if res is None:
            SKIP_UNTIL[v["id"]] = time.time() + 6 * 3600
            log.info("KEEP (for now) %s: couldn't confirm it's still on YouTube -> not deleted (%s); retry in 6 h",
                     v["title"], why)
            return 0
        if isinstance(res, tuple) and res[0] == "bot":
            # A bot/rate-limit signal on the check feeds the SAME shared backoff a download failure would, and is
            # never read as "taken down" (a missing PO token can look like "Video unavailable").
            SKIP_UNTIL[v["id"]] = time.time() + 6 * 3600
            pushback("check: %s" % res[1], bot=True)
            log.info("KEEP (for now) %s: bot/rate-limit signal on the pre-delete check -> not deleted, backing off (%s)",
                     v["title"], why)
            return 0
        if res is not True:
            mark_removed(c, v, "checked before delete: " + res[1])
            return 0
    freed = 0
    with pipeline.FILE_LOCK:                          # the season organizer may have just moved it: re-read the path
        v = c.execute("SELECT * FROM videos WHERE id=?", (v["id"],)).fetchone()
        base = os.path.splitext(v["path"] or "")[0]
        if base:
            for ext in (os.path.splitext(v["path"])[1], ".jpg", ".nfo", ".en.srt"):
                p = base + ext
                if os.path.exists(p):
                    freed += os.path.getsize(p)
                    os.remove(p)
        db.upsert(c, "videos", v["id"], state="pruned", reason=why, updated_at=db.now())
    ch = c.execute("SELECT title FROM channels WHERE id=?", (v["channel_id"],)).fetchone()
    log.info("PRUNE %s | %s | %.2f GB | %s", ch and ch["title"], v["title"], freed / 1e9, why)
    with LOCK:
        PRUNED.insert(0, {"channel": ch and ch["title"], "title": v["title"], "GB": gb(freed), "why": why, "at": db.now()})
        del PRUNED[30:]
    if v["path"]:
        plex_scan(os.path.dirname(v["path"]))
    return freed


def make_room(c, need):
    """Free at least `need` bytes by deleting the globally oldest eligible video, one at a time (watched or not,
    whichever channel). Stops cleanly while the shared YouTube backoff is active (the pre-delete check would only
    return 0 and this loop would spin)."""
    freed = 0
    while freed < need:
        if time.time() < BK["until"]:
            log.info("make_room: pausing for the shared YouTube backoff (%.0fs left), %.1f GB still needed",
                     BK["until"] - time.time(), (need - freed) / 1e9)
            break
        cands = prune_candidates(c)
        if not cands:
            log.warning("cannot free %.1f GB more: nothing left that may be deleted", (need - freed) / 1e9)
            break
        v = min(cands, key=lambda x: (x["local_date"] or "", x["episode"] or 0))
        freed += delete_video(c, v, "space: oldest in the library")
    return freed


# ------------------------------------------------------------------ discovery + new uploads
def import_csv(c):
    """Optional drop-in file /data/import/subscriptions.csv: a Google Takeout subscriptions.csv (Channel Id, Channel
    Url, Channel Title) or a simple CSV with a `handle` column. New rows are looked up by resolve_channels()."""
    if not os.path.exists(SUBS):
        return
    with open(SUBS, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    base = c.execute("SELECT COALESCE(MAX(csv_order), 0) FROM subs").fetchone()[0] or 0
    for i, r in enumerate(rows):
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
        h = r.get("handle") or r.get("channel id") or ""
        if not h and "/@" in r.get("channel url", ""):
            h = "@" + r["channel url"].split("/@", 1)[1].split("/")[0]
        if h and not c.execute("SELECT 1 FROM subs WHERE handle=?", (h,)).fetchone():
            c.execute("INSERT INTO subs(handle, title, csv_order, state) VALUES(?,?,?,'new')",
                      (h, r.get("title") or r.get("channel title") or "", base + i + 1))
    c.commit()


def queue_entries(c, chrow, entries, priority=0):
    """Queue a channel's uploads (newest first, rank 0 = newest). Skips Shorts, lives, members-only, known videos."""
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
            if e.get("timestamp"):
                c.execute("UPDATE videos SET published_ts=? WHERE id=? AND published_ts IS NULL", (e["timestamp"], vid))
        if not row or row["state"] not in ("skipped", "skipped_members_only"):
            rank += 1
    return added


def resolve_channels(c):
    import_csv(c)
    subs = c.execute("SELECT * FROM subs ORDER BY csv_order").fetchall()
    DISC["total"] = len(subs)
    DISC["resolved"] = DISC["errors"] = 0
    for s in subs:
        if STOP.is_set():
            return
        wait_backoff()
        while net.blocked() and not STOP.is_set():
            time.sleep(30)
        if s["state"] == "removed":
            continue
        if s["state"] == "ok" and s["channel_id"]:
            DISC["resolved"] += 1
            continue
        try:
            ch = ytdl.channel_tab(s["handle"], limit=LIST_DEPTH)
            if c.execute("SELECT 1 FROM channels WHERE id=?", (ch.get("channel_id"),)).fetchone():
                c.execute("UPDATE subs SET channel_id=?, state='ok', error=NULL, checked_at=? WHERE handle=?",
                          (ch.get("channel_id"), db.now(), s["handle"]))
                DISC["resolved"] += 1
                continue
            chrow = pipeline.ensure_channel(c, ch, source="csv-import", use_llm=False)
            db.upsert(c, "channels", chrow["id"], csv_order=s["csv_order"], last_checked=db.now())
            c.execute("UPDATE subs SET channel_id=?, state='ok', error=NULL, checked_at=? WHERE handle=?",
                      (chrow["id"], db.now(), s["handle"]))
            c.commit()
            n = queue_entries(c, chrow, ch["entries"])
            DISC["resolved"] += 1
            log.info("CHANNEL %s (%s): %d uploads queued", chrow["title"], s["handle"], n)
        except ytdl.BotCheck as e:
            pushback(e, bot=True)
        except Exception as e:
            DISC["errors"] += 1
            c.execute("UPDATE subs SET state='error', error=?, checked_at=? WHERE handle=?", (str(e)[:300], db.now(), s["handle"]))
            c.commit()
            log.warning("CHANNEL %s failed: %s", s["handle"], e)
        time.sleep(random.uniform(4, 10))              # imports are looked up gently, a few seconds apart
    DISC["pass"] = 1


def enforce_channel_limits(c):
    """Per-channel settings (web UI): videos over the channel's length limit are skipped; videos outside a
    count / days / since retention window leave the library (never protected ones: keep forever, the newest
    PROTECT_NEWEST, gone from YouTube, playing now)."""
    rows = c.execute("SELECT * FROM channels WHERE gone_at IS NULL AND (keep_count IS NOT NULL OR max_duration > 0 "
                     "OR (json_valid(meta) AND json_extract(meta,'$.retention.mode') IN ('days','since')))").fetchall()
    if not rows:
        return
    from . import actions
    playing = playing_paths()
    for ch in rows:
        if ch["max_duration"] and ch["max_duration"] > 0:
            n = c.execute("UPDATE videos SET state='skipped', reason=? WHERE channel_id=? AND state='queued' "
                          "AND COALESCE(priority,0) < 2 AND duration > ?",
                          ("too long: over %d min (channel setting)" % (ch["max_duration"] // 60), ch["id"], ch["max_duration"])).rowcount
            if n:
                log.info("LIMIT %s: %d queued videos over %d min skipped", ch["title"], n, ch["max_duration"] // 60)
        if playing is None:
            continue
        for v, why in actions.retention_victims(c, ch, playing):
            delete_video(c, v, why)


def pending_removals(c):
    """Channels flagged for removal (unsubscribed / removed in the UI) whose grace period is over -> deleted."""
    from . import actions
    for ch in c.execute("SELECT * FROM channels WHERE pending_removal_at IS NOT NULL").fetchall():
        try:
            grace = int(json.loads(ch["meta"] or "{}").get("removal_grace_days", 3))
        except Exception:
            grace = 3
        try:
            since = datetime.strptime(ch["pending_removal_at"], "%Y-%m-%dT%H:%M:%S%z").timestamp()
        except Exception:
            continue
        if time.time() - since < grace * 86400:
            continue
        try:
            actions.delete_channel_now(ch["id"], _wait=True)
            log.info("CHANNEL REMOVED %s: %d-day grace period over", ch["title"], grace)
        except actions.ActionError as e:
            log.info("channel removal %s postponed: %s", ch["title"], e.message)


def rss_poller():
    """Every ~POLL_MIN minutes (jittered): each channel's long-form feed; new uploads jump the queue."""
    c = db.connect()
    resolve_channels(c)
    while not STOP.is_set():
        t_cycle = time.time()
        if net.blocked():
            time.sleep(60)
            continue
        if c.execute("SELECT 1 FROM subs WHERE state='new' LIMIT 1").fetchone():
            resolve_channels(c)                   # channels imported from the web UI since the last pass
        chans = c.execute("SELECT * FROM channels WHERE gone_at IS NULL AND COALESCE(enabled,1)=1 "
                          "AND pending_removal_at IS NULL").fetchall()
        random.shuffle(chans)
        gap = POLL_MIN * 60 / max(1, len(chans))
        for ch in chans:
            if STOP.is_set():
                return
            try:
                feed, code = catalog.rss(playlist_id="UULF" + ch["id"][2:])     # UULF = long-form uploads only
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
                    log.info("QUEUED %s | %s | NEW UPLOAD (front of queue)", ch["title"], x["title"])
            except Exception as e:
                log.warning("RSS %s: %s", ch["title"], e)
            time.sleep(gap * random.uniform(0.6, 1.4))
        DISC["last_poll"] = db.now()
        try:
            enforce_channel_limits(c)
            pending_removals(c)
        except Exception as e:
            log.warning("channel limits: %s", e)
        rest = POLL_MIN * 60 - (time.time() - t_cycle)
        time.sleep(max(5, rest))


def removed_checker():
    """Daily per channel (spread over the day): library videos no longer listed on the channel get ONE probe; if
    YouTube confirms they're gone they're flagged, labelled and protected. A gone channel is protected as a whole."""
    c = db.connect()
    time.sleep(600)                                   # let the startup rush settle first
    while not STOP.is_set():
        due = c.execute("""SELECT * FROM channels WHERE gone_at IS NULL AND (removed_checked_at IS NULL OR removed_checked_at < ?)
                           ORDER BY COALESCE(removed_checked_at, '') LIMIT 1""",
                        (datetime.fromtimestamp(time.time() - REMOVED_CHECK_H * 3600).strftime("%Y-%m-%dT%H:%M:%S"),)).fetchone()
        n_ch = max(1, c.execute("SELECT COUNT(*) FROM channels WHERE gone_at IS NULL").fetchone()[0])
        if not due:
            time.sleep(600)
            continue
        db.upsert(c, "channels", due["id"], removed_checked_at=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
        lib = c.execute("SELECT * FROM videos WHERE channel_id=? AND state='done' AND removed_at IS NULL", (due["id"],)).fetchall()
        if lib:
            try:
                listed = {e.get("id") for e in ytdl.channel_tab(due["id"], limit=LIST_DEPTH * 2)["entries"]}
            except ytdl.BotCheck as e:
                pushback(e, bot=True)
                continue
            except Exception as e:
                if GONE_PAT.search(str(e)):
                    c.execute("UPDATE channels SET gone_at=?, gone_reason=? WHERE id=?", (db.now(), str(e)[:200], due["id"]))
                    c.commit()
                    log.warning("CHANNEL GONE %s: %s -> all its videos are protected, polling stopped", due["title"], str(e)[:160])
                    for v in lib:
                        mark_removed(c, v, "channel gone: %s" % str(e)[:120])
                else:
                    log.warning("removed-check %s: %s", due["title"], e)
                continue
            for v in [v for v in lib if v["id"] not in listed]:
                if time.time() < BK["until"]:
                    break                              # known backoff window -- don't burn requests into it
                with LOCK:
                    if v["id"] in CHECKING:
                        continue
                    CHECKING.add(v["id"])
                try:
                    res = youtube_check(v["id"])
                finally:
                    with LOCK:
                        CHECKING.discard(v["id"])
                if isinstance(res, tuple) and res[0] == "bot":
                    # Same as delete_video()'s pre-delete check: a bot/rate-limit signal here is real
                    # YouTube traffic hitting the same limit as downloads -> the shared backoff.
                    pushback("removed-check: %s" % res[1], bot=True)
                elif isinstance(res, tuple) and res[0] == "gone":
                    mark_removed(c, v, res[1])
                time.sleep(2)
        STATE["removed_last"] = db.now()
        time.sleep(max(60, REMOVED_CHECK_H * 3600 / n_ch * random.uniform(0.8, 1.2)))


def mark_removed(c, v, reason):
    c.execute("UPDATE videos SET removed_at=?, removed_reason=?, keep_forever=1 WHERE id=?", (db.now(), reason, v["id"]))
    c.commit()
    ch = c.execute("SELECT title FROM channels WHERE id=?", (v["channel_id"],)).fetchone()
    log.warning("REMOVED FROM YOUTUBE %s | %s | %s -> protected, labelled", ch and ch["title"], v["title"], reason[:120])
    try:                                              # NFO tag + Plex label so they're easy to find
        rep = json.loads(v["report"] or "{}")
        base = os.path.splitext(v["path"])[0]
        nfo.episode(base + ".nfo", v["id"], v["title"], v["summary"] or rep.get("summary") or "", v["local_date"],
                    v["season"], v["episode"], ch and ch["title"], v["out_duration"], rep.get("category"), tags=[REMOVED_LABEL])
    except Exception as e:
        log.warning("removed NFO tag %s: %s", v["id"], e)
    plex_label(c, v, REMOVED_LABEL)


# ------------------------------------------------------------------ downloads
def claim(c, reserved=False):
    """Next video across the whole library, newest published first (claimq.py: the same order the web UI shows).
    If it pushes the library over the fill target, make_room() evicts the globally oldest video (outside this lock);
    the download itself never waits for that. 'waiting' premieres come back once their start time has passed.
    `reserved` (slot 0, only with more than one slot) keeps one slot free for fresh uploads.

    Pace: at most NIGHTLY_CAP backlog downloads per 24 h and a random MIN_GAP_MIN_H..MIN_GAP_MAX_H pause between
    downloads. New uploads (NEW_UPLOAD_WINDOW) and "download first" videos aren't held by the daily cap.
    """
    with LOCK:
        now = time.time()
        # "Download now" (priority 3): the very next download, skipping the pace gap and the daily cap -- but still
        # strictly one at a time: it waits while any download is running (or was just claimed and hasn't shown up
        # in ACTIVE yet), then starts right after it. Only videos someone pressed the button for get this.
        rush = None
        busy = bool(ACTIVE) or any(now - t < 3600 for t in STATE["claimed"].values())
        if not busy and c.execute("SELECT 1 FROM videos WHERE COALESCE(priority,0) >= 3 "
                                  "AND state IN ('queued','upgrade') LIMIT 1").fetchone():
            r = claimq.eligible(c, reserved, limit=1)
            if r and int(r[0]["priority"] or 0) >= 3:
                rush = r[0]
        if rush is None and now < STATE["next_claim_at"]:
            return None, 0
        since = datetime.fromtimestamp(now - 86400).strftime("%Y-%m-%dT%H:%M:%S")
        recent = c.execute("SELECT COUNT(*) FROM videos WHERE started_at IS NOT NULL AND started_at >= ?", (since,)).fetchone()[0]
        c.execute("UPDATE videos SET state='queued' WHERE state IN ('waiting','retry') AND retry_at <= ?", (int(time.time()),))
        rows = [rush] if rush is not None else claimq.eligible(c, reserved, limit=1)   # shared with the web UI's queue
        if not rows:
            return None, 0
        v = rows[0]
        # The daily cap is for the BACKLOG only: a video uploaded in the last NEW_UPLOAD_WINDOW, or one pushed to the
        # front by hand, still downloads at the normal one-at-a-time gap after the cap is reached.
        if recent >= NIGHTLY_CAP and not is_fresh(v, now):
            return None, 0
        est = estimate(c, v)
        room = SOFT_CAP - used_bytes() - sum(INFLIGHT.values())
        STATE["backlog_paused"] = est > room              # informational only now -- never gates the claim
        if est > room:
            # Full library: only swap if this video is NEWER than the oldest one we'd evict -- never evict a newer
            # video to make room for an older one. Downloads pushed by hand (priority 2+) always go ahead.
            if claimq.blocked_when_full(v, claimq.oldest_in_library(c)):
                return None, 0
            STATE["pending_prune"] = est - room
        STATE["next_claim_at"] = now + random.uniform(MIN_GAP_MIN_H, MIN_GAP_MAX_H) * 3600
        STATE["claimed"][v["id"]] = now
        c.execute("UPDATE videos SET state=?, started_at=?, attempts=COALESCE(attempts,0)+1 WHERE id=?",
                  ("upgrading" if v["state"] == "upgrade" else "downloading", db.now(), v["id"]))
        c.commit()
        return v["id"], est


def make_hook(n):
    def hook(d):
        a = ACTIVE.get(n)
        if not a:
            return
        fid = (d.get("info_dict") or {}).get("format_id", "?")
        a.setdefault("parts", {})[fid] = {"done": d.get("downloaded_bytes") or 0,
                                          "total": d.get("total_bytes") or d.get("total_bytes_estimate") or 0,
                                          "speed": (d.get("speed") or 0) if d.get("status") == "downloading" else 0}
        a["stage"] = "downloading" if d.get("status") == "downloading" else "processing"
    return hook


def download_worker(n):
    """Download slot n (slot 0 is reserved for new uploads): claim -> fetch streams -> hand to finalizers -> next."""
    c = db.connect()
    # with a single slot (one-at-a-time pace) it can't be kept for new uploads only, or the backlog never downloads
    # at all; the claim order already puts new uploads first
    reserved = (n == 0 and CONC_MAX > 1)
    while not STOP.is_set():
        wait_backoff()
        if n >= TUNE["conc"] or os.path.exists(PAUSE_FLAG):
            time.sleep(3)
            continue
        # backpressure: backlog slots wait while finalizing lags behind; the new-upload slot never waits for it
        if (not reserved and PROC_Q.qsize() >= MAX_PENDING) or shutil.disk_usage(config.SCRATCH).free < config.MIN_FREE_BYTES:
            time.sleep(3)
            continue
        if account.load()["state"] in account.SLOW_STATES and ACTIVE:   # flagged: one at a time
            time.sleep(5)
            continue
        if net.blocked():                     # no allowed network line (e.g. every proxy down, never-direct on)
            time.sleep(15)
            continue
        vid, est = claim(c, reserved)
        if STATE.get("pending_prune"):
            need, STATE["pending_prune"] = STATE["pending_prune"], 0
            make_room(c, need)
        if not vid:
            time.sleep(5 if reserved else 10)
            continue
        v = c.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
        ch = c.execute("SELECT * FROM channels WHERE id=?", (v["channel_id"],)).fetchone()
        ACTIVE[n] = {"video": vid, "channel": ch["title"], "title": v["title"], "since": time.time(), "stage": "starting",
                     "est": est, "new_upload": bool(v["priority"])}
        INFLIGHT[vid] = est
        STATE["claimed"].pop(vid, None)
        log.info("START %s | %s | est %.2f GB%s", ch["title"], v["title"], est / 1e9,
                 " | DOWNLOAD NOW" if (v["priority"] or 0) >= 3 else " | NEW UPLOAD" if v["priority"] else "")
        handed = False
        try:
            job = pipeline.download_stage(c, ch, vid, hook=make_hook(n))
            size = sum(os.path.getsize(p) for p in (job["vfile"], job["afile"]) if p)
            log.info("DOWNLOADED %s | %s | %.2f GB in %ds (%.1f MB/s) -> finalize queue (%d waiting)",
                     ch["title"], v["title"], size / 1e9, job["dl_s"], size / 1e6 / max(job["dl_s"], 1), PROC_Q.qsize())
            job.update(size=size, est=est, attempts=v["attempts"], priority=v["priority"], published_ts=v["published_ts"])
            try:
                pipeline.save_job(job)                # a restart finalizes it instead of downloading it again
            except Exception as e:
                log.warning("save job %s: %s", vid, e)
            PROC_Q.put((0 if v["priority"] else 1, next(SEQ), job))
            handed = True
            account.note_success()
            net.report("ok")
        except pipeline.Skip as e:
            row = c.execute("SELECT state FROM videos WHERE id=?", (vid,)).fetchone()
            log.info("SKIP %s | %s | %s (%s)", ch["title"], v["title"], e, row["state"])
            db.upsert(c, "videos", vid, finished_at=db.now())
        except ytdl.BotCheck as e:
            pushback(e, bot="429" not in str(e))
            net.report("bot")
        except net.NetworkPaused as e:
            log.warning("NETWORK PAUSED %s | %s | %s -> back in the queue", ch["title"], v["title"], e)
            db.upsert(c, "videos", vid, state="queued", attempts=max(0, (v["attempts"] or 1) - 1))
        except Exception as e:
            msg = redact.text(str(e))
            if PROXY_ERR.search(msg) or TRANSIENT.search(msg):
                net.report("conn")
            log.error("FAIL %s | %s | %s", ch["title"], v["title"], msg[:300])
            account.note_failure(msg)
            if "403" in msg:
                with LOCK:
                    BK["fail403"] += 1
                if BK["fail403"] >= 3:
                    pushback("3 downloads in a row got HTTP 403")
            st = c.execute("SELECT state FROM videos WHERE id=?", (vid,)).fetchone()["state"]
            if st == "failed" and TRANSIENT.search(msg):
                # a dropped/truncated connection is YouTube's CDN, not the video: never a permanent failure; back off
                k = (v["transient_fails"] or 0) + 1
                db.upsert(c, "videos", vid, state="retry", attempts=max(0, (v["attempts"] or 1) - 1), transient_fails=k,
                          retry_at=int(time.time() + min(6 * 3600, 300 * 2 ** (k - 1))))
            elif st == "failed" and (v["attempts"] or 0) < 3:        # other errors: try again later, max 3 times
                db.upsert(c, "videos", vid, state="retry", retry_at=int(time.time() + 300 * max(1, v["attempts"] or 1)))
        finally:
            ACTIVE.pop(n, None)
            if not handed:
                INFLIGHT.pop(vid, None)


def finalize_worker(m):
    """Finalize pool: one merge into the library, NFO/thumbnail/subtitles, publish, Plex scan. No LLM waits."""
    c = db.connect()
    while not STOP.is_set():
        try:
            _, _, job = PROC_Q.get(timeout=5)
        except queue.Empty:
            continue
        ch = job["chrow"]
        title = (job["info"].get("title") or job["vid"])
        if not c.execute("SELECT 1 FROM channels WHERE id=?", (ch["id"],)).fetchone() or \
                c.execute("SELECT 1 FROM removed_channels WHERE channel_id=?", (ch["id"],)).fetchone():
            log.info("DROP %s | %s | channel was deleted", ch["title"], title)
            shutil.rmtree(job["work"], ignore_errors=True)
            shutil.rmtree(os.path.join(config.STAGING, job["vid"]), ignore_errors=True)
            c.execute("DELETE FROM videos WHERE id=?", (job["vid"],))
            INFLIGHT.pop(job["vid"], None)
            continue
        FIN[m] = {"video": job["vid"], "channel": ch["title"], "title": title, "since": time.time(), "stage": "finalizing",
                  "GB": gb(job.get("size"))}
        t0 = time.time()
        try:
            rep = pipeline.finalize_stage(c, job, use_llm=False)
            fin_s = time.time() - t0
            FIN_T.append((time.time(), fin_s))
            lat = None
            pub = job.get("published_ts") or job["info"].get("release_timestamp") or job["info"].get("timestamp")
            if job.get("priority") and pub:
                lat = (time.time() - pub) / 60.0
                if lat < 24 * 60:
                    LATENCY.append((time.time(), lat))
            log.info("DONE %s | %s | %s %s | %.2f GB | download %ds, finalize %ds (merge %ss)%s%s",
                     ch["title"], rep.get("title"), rep.get("res"), rep.get("vcodec"), rep["size"] / 1e9,
                     job["dl_s"], fin_s, rep.get("merge_s"), " | fallback format" if rep.get("fallback") else "",
                     " | NEW UPLOAD, %.0f min after upload" % lat if lat is not None else "")
            with LOCK:
                RECENT.insert(0, {"video": job.get("vid"), "channel": ch["title"], "title": rep.get("title"), "res": rep.get("res"),
                                  "codec": rep.get("vcodec"), "GB": gb(rep["size"]), "secs": int(time.time() - job["t_start"]),
                                  "download_s": job["dl_s"], "fallback": rep.get("fallback"), "at": db.now(),
                                  "new_upload": bool(job.get("priority"))})
                del RECENT[30:]
            db.upsert(c, "videos", job["vid"], finished_at=db.now())
            plex_scan(os.path.dirname(os.path.dirname(rep["path"])))
            success()
        except Exception as e:
            log.error("FAIL finalize %s | %s | %s", ch["title"], title, str(e)[:300])
            st = c.execute("SELECT state FROM videos WHERE id=?", (job["vid"],)).fetchone()["state"]
            if st == "failed" and (job.get("attempts") or 0) < 3:
                db.upsert(c, "videos", job["vid"], state="retry", retry_at=int(time.time() + 300))
        finally:
            FIN.pop(m, None)
            INFLIGHT.pop(job["vid"], None)


# ------------------------------------------------------------------ Plex organization (periodic)
def organizer():
    """Every 15 min: series/year seasons (seasons.py) for channels with new videos or still-provisional numbers;
    hourly: topics, collections, genres and season titles (organize.py). Never downloads anything."""
    from . import organize, seasons
    c = db.connect()
    c.executescript(seasons.SCHEMA)
    last_topics = 0.0
    STOP.wait(120)
    gate = os.path.join(config.DATA, "organizer.off")  # create this file to pause the organizer
    import importlib
    from . import playlists
    mods, stamp = (playlists, seasons, organize), {}
    while not STOP.is_set():
        if os.path.exists(gate) or os.environ.get("TUBARR_ORGANIZER", "1") == "0":
            STOP.wait(60)
            continue
        for mod in mods:                                 # hot reload: organizer code updates need no worker restart
            try:
                mt = os.path.getmtime(mod.__file__)
                if stamp.get(mod.__name__, mt) != mt:
                    importlib.reload(mod)
                    log.info("organizer: reloaded %s", mod.__name__)
                stamp[mod.__name__] = mt
            except Exception as e:
                log.warning("organizer: reload %s failed: %s", mod.__name__, e)
        try:
            for ch in c.execute("""SELECT ch.* FROM channels ch WHERE ch.gone_at IS NULL AND EXISTS (
                                     SELECT 1 FROM videos v WHERE v.channel_id=ch.id AND v.state='done'
                                     AND (COALESCE(v.finished_at,'') > COALESCE(ch.organized_at,'') OR v.episode >= 10000))
                                   ORDER BY ch.csv_order""").fetchall():
                if STOP.is_set():
                    return
                try:
                    chg = seasons.organize_channel(c, ch)
                except Exception as e:
                    log.warning("organizer %s: %s", ch["title"], e)
                    continue
                if chg:
                    log.info("SEASONS %s: %d videos placed (%s)", ch["title"], len(chg),
                             "; ".join("%s -> %s" % (x.split("/")[0], y.split("/")[0]) for _, x, y in chg[:3]))
                db.upsert(c, "channels", ch["id"], organized_at=db.now())
                time.sleep(1)
            if time.time() - last_topics > 3600:
                organize.run(c)
                last_topics = time.time()
        except Exception as e:
            log.warning("organizer: %s", e)
        STOP.wait(900)


# ------------------------------------------------------------------ self-tuning concurrency
def tuner():
    """Every ~3 min: +1 slot while throughput improves (>= 5%), no push-back, finalize median < 60 s and I/O pressure
    isn't climbing; two flat steps -> settle one below the peak; re-probe after 30 min settled."""
    while not STOP.is_set():
        time.sleep(TUNE_EVERY)
        now = time.time()
        win = [s for t, s in SPEED if t > now - TUNE_EVERY]
        rate = statistics.mean(win) if win else 0.0
        fins = [s for t, s in FIN_T if t > now - TUNE_EVERY]
        fin_med = statistics.median(fins) if fins else 0.0
        pb = [r for t, r in PUSH if t > now - TUNE_EVERY]
        io = io_pressure()
        io_ok = io is None or TUNE["io_prev"] is None or (io < 70 and io < TUNE["io_prev"] + 15)
        TUNE["io_prev"] = io
        prev = TUNE["prev_rate"]
        TUNE["prev_rate"] = rate
        TUNE["last"] = {"rate_MBps": round(rate / 1e6, 1), "finalize_median_s": round(fin_med, 1), "io_pressure": io,
                        "pushback": len(pb)}
        if now < TUNE["hold_until"] or pb:
            continue
        if rate > TUNE["best_rate"]:
            TUNE["best_rate"], TUNE["best_conc"] = rate, TUNE["conc"]
        if TUNE["settled_at"]:
            if now - TUNE["settled_at"] > 1800 and TUNE["conc"] < CONC_MAX:     # conditions change: probe again
                TUNE["settled_at"], TUNE["flat"] = 0.0, 0
                set_conc(TUNE["conc"] + 1, "re-probe after 30 min settled")
            continue
        improving = prev is not None and rate >= prev * 1.05
        if improving and fin_med < 60 and io_ok and TUNE["conc"] < CONC_MAX:
            TUNE["flat"] = 0
            set_conc(TUNE["conc"] + 1, "throughput +%.0f%% (%.0f MB/s), finalize %.0fs, io %s" % (
                (rate / prev - 1) * 100 if prev else 0, rate / 1e6, fin_med, io))
        elif prev is not None:
            TUNE["flat"] += 1
            if TUNE["flat"] >= 2 or fin_med >= 60 or not io_ok:
                target = max(CONC_MIN, TUNE["peak"] - 1)
                TUNE["settled_at"] = now
                set_conc(target, "settled one below peak %d (%.0f MB/s, finalize %.0fs, io %s)" % (
                    TUNE["peak"], rate / 1e6, fin_med, io))


def set_conc(n, why):
    n = max(CONC_MIN, min(CONC_MAX, n))
    if n != TUNE["conc"]:
        log.info("TUNE concurrency %d -> %d: %s", TUNE["conc"], n, why)
    TUNE["conc"] = n
    TUNE["peak"] = max(TUNE["peak"], n)
    TUNE["reason"] = why
    TUNE["at"] = time.time()


# ------------------------------------------------------------------ live settings (web page) + account back-off
def _apply_settings(first=False):
    """Copy /data/settings.json (env values as defaults) into the knobs claim()/make_room() read, and slow right
    down while the download account is flagged. Runs every few seconds: no restart needed."""
    global NIGHTLY_CAP, MIN_GAP_MIN_H, MIN_GAP_MAX_H, SOFT_CAP, PROTECT_NEWEST
    s = settings.load()
    acc = account.load()["state"]
    gmin, gmax = float(s["min_gap_min_h"]), float(s["min_gap_max_h"])
    if acc in account.SLOW_STATES:
        gmin, gmax = max(gmin, account.FLAGGED_GAP_H[0]), max(gmax, account.FLAGGED_GAP_H[1])
    new = (int(s["nightly_cap"]), gmin, max(gmin, gmax), float(s["soft_cap_gb"]) * 1e9, int(s["protect_newest"]))
    old = (NIGHTLY_CAP, MIN_GAP_MIN_H, MIN_GAP_MAX_H, SOFT_CAP, PROTECT_NEWEST)
    if new != old:
        NIGHTLY_CAP, MIN_GAP_MIN_H, MIN_GAP_MAX_H, SOFT_CAP, PROTECT_NEWEST = new
        if not first:
            log.info("SETTINGS now: max %d downloads/24 h, %.1f-%.1f min between downloads, fill target %.0f GB, "
                     "protect newest %d%s", NIGHTLY_CAP, MIN_GAP_MIN_H * 60, MIN_GAP_MAX_H * 60, SOFT_CAP / 1e9,
                     PROTECT_NEWEST, " (download account %s: slow pace)" % acc if acc in account.SLOW_STATES else "")
        with LOCK:
            now = time.time()
            if acc in account.SLOW_STATES and STATE.get("pace_acc") not in account.SLOW_STATES:
                # just flagged: the next download waits a full slow gap, whatever was scheduled before
                STATE["next_claim_at"] = max(STATE["next_claim_at"], now + random.uniform(gmin, gmax) * 3600)
            elif STATE["next_claim_at"] > now + MIN_GAP_MAX_H * 3600:
                STATE["next_claim_at"] = now + random.uniform(MIN_GAP_MIN_H, MIN_GAP_MAX_H) * 3600
    STATE["pace_acc"] = acc


def settings_watcher():
    while not STOP.is_set():
        try:
            _apply_settings()
            account.ensure_alert()
            busy = bool(ACTIVE) or bool(FIN) or any(time.time() - t < 3600 for t in STATE["claimed"].values())
            net.tick(busy)                        # proxy failover/rotation: only ever between downloads
        except Exception as e:
            log.warning("settings: %s", e)
        STOP.wait(5)


def pacing_status(c):
    since = datetime.fromtimestamp(time.time() - 86400).strftime("%Y-%m-%dT%H:%M:%S")
    started = c.execute("SELECT COUNT(*) FROM videos WHERE started_at IS NOT NULL AND started_at >= ?", (since,)).fetchone()[0]
    nxt = STATE["next_claim_at"]
    return {"next_download_at": datetime.fromtimestamp(nxt).astimezone().isoformat(timespec="seconds")
            if nxt > time.time() else None,
            "gap_min_minutes": round(MIN_GAP_MIN_H * 60, 2), "gap_max_minutes": round(MIN_GAP_MAX_H * 60, 2),
            "started_last_24h": started, "cap_24h": NIGHTLY_CAP, "at_cap": started >= NIGHTLY_CAP,
            "one_at_a_time": CONC_MAX <= 1 or account.load()["state"] in account.SLOW_STATES,
            "network_paused": net.blocked(),
            "slowed_by_account": account.load()["state"] in account.SLOW_STATES}


# ------------------------------------------------------------------ status
def status_writer():
    c = db.connect()
    hist = []
    while not STOP.is_set():
        try:
            counts = {r[0]: r[1] for r in c.execute("SELECT state, COUNT(*) FROM videos GROUP BY state")}
            done = c.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM videos WHERE state='done'").fetchone()
            removed = c.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM videos WHERE state='done' AND removed_at IS NOT NULL").fetchone()
            gone = c.execute("SELECT COUNT(*) FROM channels WHERE gone_at IS NOT NULL").fetchone()[0]
            active, speed = [], 0.0
            for n, a in sorted(ACTIVE.items()):
                parts = list(a.get("parts", {}).values())
                tot, dn, sp = sum(p["total"] for p in parts), sum(p["done"] for p in parts), sum(p["speed"] for p in parts)
                speed += sp
                active.append({"worker": n, "video": a.get("video"), "channel": a["channel"], "title": a["title"], "stage": a.get("stage"),
                               "pct": round(100.0 * dn / tot, 1) if tot else None, "MBps": round(sp / 1e6, 2),
                               "GB": gb(tot or a.get("est")), "for_s": int(time.time() - a["since"]),
                               "new_upload": a.get("new_upload", False)})
            for m, f in sorted(FIN.items()):
                active.append({"worker": "f%d" % m, "video": f.get("video"), "channel": f["channel"], "title": f["title"], "stage": "finalizing",
                               "pct": None, "MBps": 0.0, "GB": f["GB"], "for_s": int(time.time() - f["since"])})
            now = time.time()
            SPEED.append((now, speed))
            del SPEED[:-400]
            del FIN_T[:-400]
            del PUSH[:-100]
            hist.append((now, done[1]))
            hist[:] = [h for h in hist if h[0] > now - 900]
            rate15 = (hist[-1][1] - hist[0][1]) / max(1, hist[-1][0] - hist[0][0]) if len(hist) > 1 else 0
            lat = [l for t, l in LATENCY[-20:]]
            du = shutil.disk_usage(config.ROOT)
            st = {"updated": db.now(), "uptime_s": int(now - T0),
                  "retention": {"soft_cap_GB": gb(SOFT_CAP), "protect_newest": PROTECT_NEWEST,
                                "backlog_paused": STATE["backlog_paused"]},
                  "concurrency": TUNE["conc"], "concurrency_peak": TUNE["peak"], "concurrency_max": CONC_MAX,
                  "concurrency_reason": TUNE["reason"], "concurrency_changed": datetime.fromtimestamp(TUNE["at"]).strftime("%H:%M:%S"),
                  "tuner": TUNE.get("last"), "workers_allowed": TUNE["conc"], "reserved_new_upload_slots": 1,
                  "backoff": {"active": now < BK["until"], "resume_in_s": max(0, int(BK["until"] - now)),
                              "hold_until": datetime.fromtimestamp(TUNE["hold_until"]).strftime("%H:%M:%S") if TUNE["hold_until"] > now else None,
                              "events": BK["events"][-5:]},
                  "counts": counts, "queue": counts.get("queued", 0) + counts.get("upgrade", 0),
                  "skipped_members_only": counts.get("skipped_members_only", 0), "waiting_premieres": counts.get("waiting", 0),
                  "finalize_waiting": PROC_Q.qsize(),
                  "finished": done[0], "library_GB": gb(done[1]),
                  "speed_MBps_now": round(speed / 1e6, 2), "library_growth_MBps_15min": round(rate15 / 1e6, 2),
                  "new_uploads": {"poll_minutes": POLL_MIN, "last_poll": DISC["last_poll"],
                                  "upload_to_plex_minutes_median": round(statistics.median(lat), 1) if lat else None,
                                  "samples": len(lat)},
                  "removed_from_youtube": {"videos": removed[0], "GB": gb(removed[1]), "channels_gone": gone,
                                           "check": "each channel once per %d h, spread over the day" % REMOVED_CHECK_H,
                                           "last_check": STATE["removed_last"]},
                  "youtube_folder": {"used_GB": gb(du.used - originals_bytes()),
                                     "quota_GB": gb(du.total - ORIGINALS_ALLOWANCE), "free_GB": gb(du.free),
                                     "trim_originals_GB": gb(originals_bytes()),
                                     "trim_originals_allowance_GB": gb(ORIGINALS_ALLOWANCE)},
                  "discovery": DISC, "active": active, "recent": RECENT[:15], "pruned": PRUNED[:10],
                  "pacing": pacing_status(c), "account": account.public()}
            tmp = STATUS + ".tmp"
            with open(tmp, "w") as f:
                json.dump(st, f, indent=1)
            os.replace(tmp, STATUS)
        except Exception as e:
            log.warning("status: %s", e)
        time.sleep(10)


def startup(c):
    """Reset interrupted work. Downloads that finished but weren't finalized (job.json next to the streams) are
    recovered and finalized; everything else in scratch/staging is a leftover and removed."""
    pipeline.ensure_root()                            # .staging/.work hidden from Plex
    os.makedirs(config.SCRATCH, exist_ok=True)
    recovered = []
    for d in os.listdir(config.SCRATCH):
        p = os.path.join(config.SCRATCH, d)
        job = pipeline.load_job(c, p) if os.path.isdir(p) else None
        if job:
            recovered.append(job)
        else:
            shutil.rmtree(p, ignore_errors=True)
    keep = [j["vid"] for j in recovered]
    c.execute("UPDATE videos SET state='queued' WHERE state IN ('downloading','processing') AND id NOT IN (%s)"
              % ",".join("?" * len(keep)), keep)
    c.execute("UPDATE videos SET state='upgrade' WHERE state='upgrading'")
    c.execute("UPDATE videos SET state='queued', attempts=0 WHERE state IN ('failed','retry') AND COALESCE(reason,'') NOT LIKE '%members-only%'")
    c.execute("UPDATE videos SET state='skipped_members_only' WHERE state IN ('failed','queued') AND "
              "(COALESCE(reason,'') LIKE '%members-only%' OR COALESCE(reason,'') LIKE '%Join this channel%')")
    c.execute("UPDATE videos SET attempts=0 WHERE state='queued'")
    for r in c.execute("SELECT id, height, video_format FROM videos WHERE state='done' AND json_extract(report, '$.res') IS NULL").fetchall():
        h = r["height"] or 0
        if not h and r["video_format"]:
            try:
                h = int(r["video_format"].split()[2].split("p")[0])
            except Exception:
                h = 0
        if h and h < 2160 and not c.execute("SELECT 1 FROM videos WHERE id=? AND reason LIKE 'kept at 1080p%'", (r["id"],)).fetchone():
            c.execute("UPDATE videos SET state='upgrade', rank=COALESCE(rank,0) WHERE id=?", (r["id"],))
    c.commit()
    streamed = {j["vid"] for j in recovered if j.get("merged")}    # their final.mkv lives in .staging/<vid>
    for d in os.listdir(config.STAGING):
        p = os.path.join(config.STAGING, d)
        if os.path.abspath(p) != os.path.abspath(config.SCRATCH) and d not in streamed:
            shutil.rmtree(p, ignore_errors=True)
    return recovered


def main():
    c = db.connect()
    _apply_settings(first=True)                    # web-page settings before the first claim
    recovered = startup(c)
    for job in recovered:
        INFLIGHT[job["vid"]] = job.get("est") or 0
        PROC_Q.put((0 if job.get("priority") else 1, next(SEQ), job))
    log.info("worker starting: concurrency %d (self-tuning, ceiling %d, 1 slot reserved for new uploads), %d finalizers, "
             "RSS every %.0f min, rate limit %s, soft cap %.0f GB, protect newest %d, scratch %s",
             TUNE["conc"], CONC_MAX, FINALIZERS, POLL_MIN,
             ("%.1f MB/s each" % (config.RATE_LIMIT / 1e6)) if config.RATE_LIMIT else "none", SOFT_CAP / 1e9,
             PROTECT_NEWEST, config.SCRATCH)
    if recovered:
        log.info("recovered %d downloaded videos from before the restart -> finalize queue", len(recovered))
    threads = [threading.Thread(target=status_writer, name="status", daemon=True),
               threading.Thread(target=settings_watcher, name="settings", daemon=True),
               threading.Thread(target=rss_poller, name="rss", daemon=True),
               threading.Thread(target=removed_checker, name="removed", daemon=True),
               threading.Thread(target=tuner, name="tuner", daemon=True),
               threading.Thread(target=organizer, name="organizer", daemon=True)]
    threads += [threading.Thread(target=download_worker, args=(n,), name="dl%d" % n, daemon=True) for n in range(CONC_MAX)]
    threads += [threading.Thread(target=finalize_worker, args=(m,), name="fin%d" % m, daemon=True) for m in range(FINALIZERS)]
    for t in threads:
        t.start()
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        STOP.set()


if __name__ == "__main__":
    main()



