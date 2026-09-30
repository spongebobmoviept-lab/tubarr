"""Read-only data layer for the web app.

* The worker's SQLite DB is opened with `mode=ro` (SQLite itself refuses every write) and `PRAGMA query_only`.
  The web app never calls db.connect() (that one runs migrations, i.e. writes). Changes by the worker are noticed
  cheaply through `PRAGMA data_version`.
* /data/status.json (rewritten by the worker every 10 s with an atomic rename) is cached by mtime.
* A Snapshot is an in-memory copy of channels / subs / videos (light columns only) plus everything derived from them
  (API states, sort keys, per-channel stats, protections). It's rebuilt at most about once a second and only when the
  DB changed, so every request and the event stream share one consistent picture.
"""
import functools
import json
import logging
import math
import os
import pathlib
import re
import sqlite3
import threading
import time
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import config, settings as tsettings

log = logging.getLogger("tubarr.webapp.store")

# The worker's knobs: same env vars and defaults as worker.py (never import worker.py here: importing it opens the
# worker's log file and starts nothing useful for the web app).
SOFT_CAP = tsettings.defaults()["soft_cap_gb"] * 1e9
PROTECT_NEWEST = int(os.environ.get("TUBARR_PROTECT_NEWEST", "3"))
POLL_MIN = float(os.environ.get("TUBARR_POLL_MIN", "5"))
CONC_START = int(os.environ.get("TUBARR_CONCURRENCY", "1"))
CONC_MAX = max(1, int(os.environ.get("TUBARR_CONCURRENCY_MAX", os.environ.get("TUBARR_CONCURRENCY", "1"))))
CONC_MIN = min(3, CONC_MAX)
DEFAULT_RATE = 1.25e6                     # bytes per second of video when nothing has been measured yet (worker.py)
STATUS_STALE_S = 60                       # status.json older than this = the worker isn't running
TZ = ZoneInfo(config.TZ)

IN_PLEX = ("done", "upgrade", "upgrading", "recut")          # DB states whose file is in the library
RUNNING = ("downloading", "processing", "upgrading")


# ------------------------------------------------------------------ time
@functools.lru_cache(maxsize=262144)
def _parse(s):
    s = s.strip()
    if not s:
        return None
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        return float(s)
    x = s[:-1] + "+00:00" if s.endswith("Z") else re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", s)
    try:
        d = datetime.fromisoformat(x)
    except ValueError:
        try:
            d = datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=TZ)          # the worker's naive stamps are local (the container's TZ)
    return d.timestamp()


def ts(v):
    """Any stamp the worker writes (db.now() '...-0400', naive local, epoch int/str, ISO 'Z') -> epoch seconds."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return _parse(str(v))


def iso(t):
    if t is None:
        return None
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_day(t):
    return None if t is None else datetime.fromtimestamp(t, TZ).strftime("%Y-%m-%d")


def day_epoch(day, hour=12):
    """'YYYY-MM-DD' (local) -> epoch at `hour` local time that day."""
    try:
        return datetime.strptime(day, "%Y-%m-%d").replace(hour=hour, tzinfo=TZ).timestamp()
    except (TypeError, ValueError):
        return None


def local_midnight(now=None, days_ago=0):
    d = datetime.fromtimestamp(now or time.time(), TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return (d - timedelta(days=days_ago)).timestamp()


def now_iso():
    return iso(time.time())


# ------------------------------------------------------------------ status.json
class StatusFile:
    def __init__(self, path):
        self.path = path
        self._mtime = None
        self._data = {}
        self._lock = threading.Lock()

    def read(self):
        with self._lock:
            try:
                m = os.stat(self.path).st_mtime_ns
            except OSError:
                self._mtime, self._data = None, {}
                return self._data
            if m != self._mtime:
                try:
                    with open(self.path, encoding="utf-8") as f:
                        self._data = json.load(f)
                    self._mtime = m
                except (OSError, ValueError):
                    pass                  # caught mid-replace: keep the previous copy, retry next time
            return self._data

    def age(self):
        u = ts(self.read().get("updated"))
        return None if u is None else max(0.0, time.time() - u)


# ------------------------------------------------------------------ DB
def db_path():
    return os.environ.get("TUBARR_DB") or os.path.join(config.DATA, "tubarr.db")


def open_ro():
    """A read-only connection. `mode=ro`: SQLite refuses every write; query_only: belt and braces."""
    p = os.path.abspath(db_path())
    if not os.path.exists(p):
        raise FileNotFoundError(p)
    c = sqlite3.connect(pathlib.Path(p).as_uri() + "?mode=ro", uri=True, timeout=15, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only=1")
    return c


_TL = threading.local()


def conn():
    """Per-thread read-only connection (FastAPI runs sync endpoints on a thread pool)."""
    c = getattr(_TL, "c", None)
    if c is None:
        c = _TL.c = open_ro()
    return c


VIDEO_COLS = ("id", "channel_id", "title", "published_at", "local_date", "duration", "state", "reason", "season",
              "episode", "path", "size", "out_duration", "height", "vcodec", "video_format", "watched", "created_at",
              "updated_at", "queued_at", "started_at", "finished_at", "attempts", "keep_forever", "priority",
              "retry_at", "removed_at", "removed_reason", "published_ts", "rank", "plex_rating_key", "sb_cut_seconds")
CHANNEL_COLS = ("id", "handle", "title", "folder", "source", "added_at", "enabled", "pending_removal_at", "keep_count",
                "max_duration", "skip_lives", "description", "subscribers", "category", "avatar_url", "banner_url",
                "art_updated_at", "plex_rating_key", "meta", "summary", "summary_source", "csv_order", "last_checked",
                "gone_at", "gone_reason", "removed_checked_at", "topic")
SUB_COLS = ("handle", "title", "csv_order", "channel_id", "state", "error", "checked_at")


def _select(c, table, cols):
    have = {r[1] for r in c.execute("PRAGMA table_info(%s)" % table)}
    if not have:
        return []
    sel = ", ".join(x if x in have else "NULL AS %s" % x for x in cols)
    return c.execute("SELECT %s FROM %s" % (sel, table)).fetchall()


# ------------------------------------------------------------------ state mapping (DB -> API)
def api_state(state, reason):
    if state in ("done", "upgrade", "recut"):
        return "downloaded"
    if state in RUNNING:
        return "downloading"
    if state in ("queued", "discovered"):
        return "queued"
    if state == "waiting":
        return "upcoming"
    if state == "failed":
        return "failed"
    if state == "pruned":
        return "removed"
    if state == "skipped_members_only":
        return "skipped_members"
    if state == "skipped":
        t = (reason or "").lower()
        if "live" in t or "stream" in t:
            return "skipped_live"
        if "too long" in t:
            return "skipped_too_long"
        if "availability=" in t or "login" in t or "members" in t:
            return "skipped_members"
        return "skipped_short"
    return "queued"


def skip_detail(astate, reason):
    t = (reason or "").strip()
    if astate == "skipped_short":
        return "Shorts are always skipped."
    if astate == "skipped_live":
        return "Livestreams and their replays are skipped."
    if astate == "skipped_too_long":
        return "Longer than this channel's length limit."
    if astate == "skipped_members":
        if "availability=" in t:
            return "Not public on YouTube (%s)." % t.split("=", 1)[1].replace("_", " ")
        if "login" in t:
            return "Needs a signed-in account; Tubarr always downloads signed out."
        return "Members only: it needs a paid channel membership."
    return None


def removed_reason(reason):
    t = (reason or "").lower()
    if "watched" in t:
        return "watched"
    if "biggest" in t or "space" in t or "room" in t:
        return "making_room"
    if "retention" in t or "window" in t or "outside" in t:
        return "retention"
    if "unavailable" in t or "gone" in t:
        return "unavailable"
    return "manual"


def gone_reason(text):
    t = (text or "").lower()
    if "channel gone" in t or "terminat" in t:
        return "terminated"
    if "private" in t:
        return "private"
    if "removed by the uploader" in t or "has been removed" in t or "deleted" in t or "no longer available" in t \
            or "does not exist" in t:
        return "deleted"
    return "unavailable"


VM = namedtuple("VM", "astate act pub day backfill gone keep")


def _norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


# ------------------------------------------------------------------ snapshot
class Snapshot:
    """Immutable picture of the DB at one data_version. Build once, read from any thread."""

    def __init__(self, c, version):
        self.version = version
        self.built = time.time()
        c.execute("BEGIN")                                    # one read transaction: a consistent picture
        try:
            self.channels = {r["id"]: r for r in _select(c, "channels", CHANNEL_COLS)}
            self.subs = sorted(_select(c, "subs", SUB_COLS), key=lambda r: (r["csv_order"] is None, r["csv_order"] or 0))
            self.videos = {r["id"]: r for r in _select(c, "videos", VIDEO_COLS)}
        finally:
            c.rollback()
        self._derive()
        self._cache = {}
        self._lock = threading.Lock()

    # -- derived data, computed once per snapshot
    def _derive(self):
        V, C = self.videos, self.channels
        self.meta = {}
        self.by_channel = {}                              # cid -> [vid] newest first
        done_by = {}                                      # cid -> [row] state 'done' (the worker's prune view)
        self.inflight = {}                                # (channel title, title) -> vid, for status.json matching
        self.inflight_title = {}
        for vid, r in V.items():
            st = r["state"]
            a = api_state(st, r["reason"])
            pub = ts(r["published_ts"]) or ts(r["published_at"])
            if st == "waiting" and r["retry_at"]:
                pub = float(r["retry_at"]) - 300              # scheduled start (retry_at = start + 5 min)
            day = r["local_date"] or local_day(pub)
            if pub is None and r["local_date"]:
                pub = day_epoch(r["local_date"])
            cands = [ts(r["created_at"]) or ts(r["queued_at"])]
            if st in IN_PLEX or st == "pruned":
                cands.append(ts(r["finished_at"]))
            if st in ("failed", "pruned", "skipped", "skipped_members_only"):
                cands.append(ts(r["updated_at"]))
            gone = gone_reason(r["removed_reason"]) if r["removed_at"] else None
            if gone:
                cands.append(ts(r["removed_at"]))
            act = max([x for x in cands if x] or [ts(r["updated_at"]) or pub or 0.0])
            keep = bool(r["keep_forever"]) and not r["removed_at"]
            self.meta[vid] = VM(a, act, pub, day, (r["priority"] or 0) < 1, gone, keep)
            self.by_channel.setdefault(r["channel_id"], []).append(vid)
            if st == "done":
                done_by.setdefault(r["channel_id"], []).append(r)
            if st in RUNNING:
                ch = C.get(r["channel_id"])
                self.inflight[(_norm(ch["title"] if ch else ""), _norm(r["title"]))] = vid
                self.inflight_title[_norm(r["title"])] = vid
        M = self.meta
        for cid, vids in self.by_channel.items():
            # newest first. The worker's rank is the listing position (0 = newest) and backlog rows have no date until
            # they land, so rank leads and the upload time breaks ties (new uploads from the feed also get rank 0).
            vids.sort(key=lambda v: (V[v]["rank"] if V[v]["rank"] is not None else 0, -(M[v].pub or 0)))
        # protections, exactly as the worker's prune_candidates(): gone / keep / gone channel / newest N of 'done'
        self.done_by = {}
        self.protected = {}
        for cid, rows in done_by.items():
            rows.sort(key=lambda r: (r["local_date"] or "", r["episode"] or 0), reverse=True)
            self.done_by[cid] = rows
            for i, r in enumerate(rows):
                p = self._protection(r, i)
                if p:
                    self.protected[r["id"]] = p
        for vid, r in V.items():                                # in-Plex states other than 'done'
            if r["state"] in IN_PLEX and r["state"] != "done":
                p = self._protection(r, 99)
                if p:
                    self.protected[vid] = p
        # bytes per second of video, per channel (worker.rate_for)
        tot_s, tot_d, per = 0, 0, {}
        for vid, r in V.items():
            if r["state"] == "done" and (r["out_duration"] or 0) > 0 and r["size"]:
                s, d = per.get(r["channel_id"], (0, 0))
                per[r["channel_id"]] = (s + r["size"], d + r["out_duration"])
                tot_s += r["size"]
                tot_d += r["out_duration"]
        self.global_rate = (tot_s / tot_d) if tot_s and tot_d > 3600 else DEFAULT_RATE
        self.rates = {cid: (s / d) for cid, (s, d) in per.items() if d > 600}
        # pseudo channels: subscriptions the worker hasn't resolved (yet, or because resolving failed)
        known = set(C)
        self.pseudo = {}
        for s in self.subs:
            if s["channel_id"] and s["channel_id"] in known:
                continue
            if s["state"] in ("new", "error") and s["handle"]:
                self.pseudo[s["handle"]] = s
        self.stats = {cid: self._channel_stats(cid) for cid in C}

    def _protection(self, r, newest_index):
        ch = self.channels.get(r["channel_id"])
        if r["removed_at"]:
            return "gone"
        if r["keep_forever"]:
            return "keep"
        if ch is not None and ch["gone_at"]:
            return "channel"
        if newest_index < tsettings.get("protect_newest"):
            return "newest"
        return None

    def _channel_stats(self, cid):
        V, M = self.videos, self.meta
        s = dict(count=0, size=0, queued=0, running=0, failed=0, skipped=0, watched=0, kept=0, protected=0,
                 last_up=None, last_dl=None, oldest=None, newest=None, listed=0, gone_videos=0)
        for vid in self.by_channel.get(cid, ()):
            r, m = V[vid], M[vid]
            s["listed"] += 1
            if m.pub and m.astate != "upcoming" and (s["last_up"] is None or m.pub > s["last_up"]):
                s["last_up"] = m.pub
            st = r["state"]
            if st in IN_PLEX:
                s["count"] += 1
                s["size"] += r["size"] or 0
                s["watched"] += 1 if r["watched"] else 0
                s["kept"] += 1 if m.keep else 0
                s["protected"] += 1 if vid in self.protected else 0
                s["gone_videos"] += 1 if m.gone else 0
                fin = ts(r["finished_at"])
                if fin and (s["last_dl"] is None or fin > s["last_dl"]):
                    s["last_dl"] = fin
                if m.day:
                    s["oldest"] = m.day if s["oldest"] is None or m.day < s["oldest"] else s["oldest"]
                    s["newest"] = m.day if s["newest"] is None or m.day > s["newest"] else s["newest"]
            if st in ("queued", "discovered", "upgrade"):
                s["queued"] += 1
            elif st in RUNNING:
                s["running"] += 1
            elif st == "failed":
                s["failed"] += 1
            elif st in ("skipped", "skipped_members_only"):
                s["skipped"] += 1
        return s

    # -- helpers
    def rate(self, cid):
        return self.rates.get(cid, self.global_rate)

    def estimate(self, r):
        """worker.estimate(): duration x the channel's measured bytes/s x 1.05."""
        return int((r["duration"] or 600) * self.rate(r["channel_id"]) * 1.05)

    def cached(self, key, fn):
        """Memoize per snapshot. Computed outside the lock (builders may use other cached values); a race only
        computes the same value twice."""
        try:
            return self._cache[key]
        except KeyError:
            pass
        v = fn()
        with self._lock:
            return self._cache.setdefault(key, v)

    def channel_title(self, cid):
        ch = self.channels.get(cid)
        if ch is not None:
            return ch["title"] or cid
        s = self.pseudo.get(cid)
        return (s["title"] or cid) if s is not None else cid

    def roll_order(self, limit=10):
        """What the roll would delete next, in order (worker.make_room without the Plex 'playing now' check):
        watched first (oldest upload first), then repeatedly the oldest of whichever channel uses the most space."""
        def build():
            cands = []
            for cid, rows in self.done_by.items():
                ch = self.channels.get(cid)
                if ch is None or ch["gone_at"]:
                    continue
                cands += [r for r in rows[tsettings.get("protect_newest"):] if not r["keep_forever"] and not r["removed_at"]]
            out = []
            watched = sorted((r for r in cands if r["watched"]), key=lambda r: (r["local_date"] or "", r["episode"] or 0))
            out += [(r["id"], "watched") for r in watched[:50]]
            per, sizes = {}, {}
            for r in cands:
                if r["watched"]:
                    continue
                per.setdefault(r["channel_id"], []).append(r)
                sizes[r["channel_id"]] = sizes.get(r["channel_id"], 0) + (r["size"] or 0)
            for rows in per.values():
                rows.sort(key=lambda r: (r["local_date"] or "", r["episode"] or 0))
            pos = {cid: 0 for cid in per}
            while len(out) < 50 and sizes:
                big = max(sizes, key=sizes.get)
                r = per[big][pos[big]]
                pos[big] += 1
                out.append((r["id"], "making_room"))
                sizes[big] -= r["size"] or 0
                if pos[big] >= len(per[big]):
                    del sizes[big]
            return out
        return self.cached("roll", build)[:limit]

    def reach_date(self):
        """How far back the fill has got: the date that at least 90% of channels are complete back to (each
        channel's run of in-Plex videos from its newest listed one). None until 90% of channels have their newest."""
        def build():
            V, M = self.videos, self.meta
            reaches, missing = [], 0
            for cid in self.channels:
                vids = [v for v in self.by_channel.get(cid, ()) if not M[v].astate.startswith("skipped")
                        and M[v].astate not in ("upcoming", "removed")]
                if not vids:
                    continue
                reach = None
                for v in vids:
                    if V[v]["state"] in IN_PLEX:
                        reach = M[v].day or reach
                    else:
                        break
                if reach is None:
                    missing += 1
                else:
                    reaches.append(reach)
            k = math.ceil(0.9 * (len(reaches) + missing))  # at least 90% of channels are complete back to it
            if not reaches or k > len(reaches):
                return None
            reaches.sort()
            return reaches[k - 1]
        return self.cached("reach", build)

    def history(self):
        """Every milestone, newest first: (epoch, event, vid, detail)."""
        def build():
            items = []
            for vid, r in self.videos.items():
                st, m = r["state"], self.meta[vid]
                fin = ts(r["finished_at"])
                if fin and (st in IN_PLEX or st == "pruned"):
                    items.append((fin, "downloaded", vid, None))
                if st == "failed":
                    items.append((ts(r["updated_at"]) or m.act, "failed", vid, r["reason"]))
                elif st in ("skipped", "skipped_members_only"):
                    items.append((ts(r["updated_at"]) or ts(r["created_at"]) or m.act, "skipped", vid,
                                  {"skipped_short": "short", "skipped_live": "live", "skipped_too_long": "too_long",
                                   "skipped_members": "members"}.get(m.astate, "short")))
                elif st == "pruned":
                    items.append((ts(r["updated_at"]) or m.act, "removed", vid, removed_reason(r["reason"])))
                if m.gone:
                    items.append((ts(r["removed_at"]), "gone", vid, m.gone))
            items.sort(key=lambda x: (x[0] or 0, x[2]), reverse=True)
            return items
        return self.cached("history", build)


class Data:
    """The shared, lazily refreshed picture: snapshot() + status()."""

    def __init__(self):
        self.status_file = StatusFile(os.path.join(config.DATA, "status.json"))
        self._snap = None
        self._checked = 0.0
        self._lock = threading.Lock()
        self._vconn = None
        self.error = None

    def _version(self):
        if self._vconn is None:
            self._vconn = open_ro()
        return self._vconn.execute("PRAGMA data_version").fetchone()[0], os.stat(db_path()).st_mtime_ns

    def snapshot(self, max_age=1.0):
        with self._lock:
            now = time.time()
            if self._snap is not None and now - self._checked < max_age:
                return self._snap
            self._checked = now
            try:
                ver = self._version()
                if self._snap is None or ver != self._snap.version:
                    t0 = time.time()
                    self._snap = Snapshot(conn(), ver)
                    dt = time.time() - t0
                    if dt > 0.5:
                        log.info("snapshot rebuilt in %.2fs (%d videos)", dt, len(self._snap.videos))
                self.error = None
            except (sqlite3.Error, OSError) as e:
                self.error = str(e)
                if self._vconn is not None:
                    try:
                        self._vconn.close()
                    except sqlite3.Error:
                        pass
                self._vconn = None
                if self._snap is None:
                    raise
                log.warning("DB read failed, serving the last snapshot: %s", e)
            return self._snap

    def status(self):
        return self.status_file.read()

    def status_age(self):
        return self.status_file.age()


DATA = Data()
