"""The aggregate API objects: Status, Timeline, Activity, History, Storage, Estimate, Settings, Topics."""
import base64
import hashlib
import math
import os
import re
import shutil
import statistics
import time

from .. import claimq, config, settings as tsettings
from . import VERSION, mapping, store
from .mapping import Ctx
from .store import IN_PLEX, iso, local_day, local_midnight, ts


# ------------------------------------------------------------------ worker status (status.json)
def _norm(s):
    return store._norm(s)


def active_jobs(st, snap):
    """status.json `active` (download slots + finalizers) -> job objects, matched to DB rows by channel + title
    (status.json carries no video id yet; if the worker adds `video`, it's used directly)."""
    jobs = []
    for a in st.get("active") or []:
        vid = a.get("video") or a.get("video_id")
        if not vid:
            vid = snap.inflight.get((_norm(a.get("channel")), _norm(a.get("title")))) \
                or snap.inflight_title.get(_norm(a.get("title")))
        fin = str(a.get("worker", "")).startswith("f") or a.get("stage") == "finalizing"
        pct = a.get("pct")
        prog = round(max(0.0, min(1.0, pct / 100.0)), 4) if isinstance(pct, (int, float)) else None
        total = int((a.get("GB") or 0) * 1e9) or None
        speed = int((a.get("MBps") or 0) * 1e6)
        done = int(prog * total) if prog is not None and total else None
        eta = int((total - done) / speed) if speed > 0 and total and done is not None else None
        jobs.append({"video_id": vid if vid in snap.videos else None, "slot": a.get("worker"),
                     "channel_title": a.get("channel"), "title": a.get("title"),
                     "stage": "plex" if fin else "download", "worker_stage": a.get("stage"),
                     "progress": prog, "bytes_done": done, "bytes_total": total,
                     "speed_bps": speed if not fin else None, "eta_seconds": eta,
                     "running_seconds": a.get("for_s"), "new_upload": bool(a.get("new_upload"))})
    return jobs


def lead_job(jobs):
    """The job to feature: a new upload that's downloading, else the download furthest along, else anything."""
    have = [j for j in jobs if j.get("video_id")]
    dl = [j for j in have if j["stage"] == "download"]
    return (next((j for j in dl if j["new_upload"]), None)
            or max(dl, key=lambda j: j["progress"] or 0, default=None)
            or (have[0] if have else (jobs[0] if jobs else None)))


def jobs_by_video(jobs):
    return {j["video_id"]: j for j in jobs if j["video_id"]}


def attach_videos(snap, jobs):
    by = jobs_by_video(jobs)
    for j in jobs:
        j["video"] = mapping.video(snap, j["video_id"], by.get(j["video_id"])) if j["video_id"] else None
    return jobs


def usage(st):
    """(used, cap) bytes of the library dataset: the worker's own numbers while it runs, else statvfs."""
    yf = st.get("youtube_folder") or {}
    fresh = (DATA_AGE() or 1e9) < store.STATUS_STALE_S
    if yf.get("quota_GB") and fresh:
        return int(yf["used_GB"] * 1e9), int(yf["quota_GB"] * 1e9)
    try:
        du = shutil.disk_usage(config.ROOT)
        return du.used, du.total
    except OSError:
        if yf.get("quota_GB"):
            return int(yf["used_GB"] * 1e9), int(yf["quota_GB"] * 1e9)
    return 0, 3 * 1024 ** 4


def DATA_AGE():
    return store.DATA.status_age()


def target_bytes(st):
    cap = (st.get("retention") or {}).get("soft_cap_GB")
    return int(tsettings.get("soft_cap_gb") * 1e9)          # live: the page shows a new target at once


def last_removed(snap):
    def build():
        best = None
        for vid, r in snap.videos.items():
            if r["state"] == "pruned" and store.removed_reason(r["reason"]) in ("watched", "making_room"):
                t = ts(r["updated_at"]) or 0
                if best is None or t > best[0]:
                    best = (t, vid)
        return best
    return snap.cached("last_removed", build)


def fill_obj(st, snap, used, target):
    paused = bool((st.get("retention") or {}).get("backlog_paused"))
    rolling = paused or used >= target
    lr = last_removed(snap)
    nx = snap.roll_order(1)
    return {"state": "rolling" if rolling else "filling", "target_bytes": target, "reach_date": snap.reach_date(),
            "backlog_paused": paused,
            "last_removed": {"video": mapping.video(snap, lr[1]), "at": iso(lr[0]),
                             "reason": store.removed_reason(snap.videos[lr[1]]["reason"])} if lr else None,
            "next_removal": {"video": mapping.video(snap, nx[0][0]), "reason": nx[0][1]} if nx else None}


def storage_head(st, snap):
    used, cap = usage(st)
    target = target_bytes(st)
    return {"used_bytes": used, "cap_bytes": cap, "target_bytes": target, "free_bytes": max(0, cap - used),
            "fill": fill_obj(st, snap, used, target)}


def queue_rows(snap):
    """Queued videos in EXACTLY the worker's claim() order and eligibility (tubarr/claimq.py, the same SQL)."""
    def build():
        return [r["id"] for r in claimq.eligible(store.conn()) if r["id"] in snap.videos]
    return snap.cached("queue", build)


def library_full(st):
    """Roughly worker.claim()'s est > room: the library is at its fill target."""
    used, _cap = usage(st)
    return used >= target_bytes(st) - 5e9


def older_than_library(snap, st, vid):
    """The full-library guard (claimq.blocked_when_full): True if this video waits because it's older than
    everything the roll could remove."""
    if not library_full(st):
        return False
    oldest = snap.cached("oldest_in_library", lambda: claimq.oldest_in_library(store.conn()))
    return claimq.blocked_when_full(snap.videos[vid], oldest)


def pacing_obj(st, snap, jobs):
    """Human pace, from status.json (worker) + what's next and what just finished."""
    p = dict(st.get("pacing") or {})
    q = queue_rows(snap)
    nxt = q[0] if q else None
    p["next_up"] = mapping.video(snap, nxt) if nxt else None
    p["next_up_waits_older"] = bool(nxt and older_than_library(snap, st, nxt))
    fin = snap.cached("recent_done", lambda: sorted(((ts(r["finished_at"]), vid) for vid, r in snap.videos.items()
                                                     if r["finished_at"] and r["state"] in IN_PLEX), reverse=True)[:5])
    p["recent_done"] = [{"video": mapping.video(snap, vid), "at": iso(t)} for t, vid in fin if t]
    return p


def downloader_obj(st, snap, jobs):
    age = DATA_AGE()
    running = age is not None and age < store.STATUS_STALE_S
    bo = st.get("backoff") or {}
    ev = (bo.get("events") or [None])[-1]
    backoff = {"active": bool(bo.get("active")), "resume_in_seconds": bo.get("resume_in_s") or 0,
               "hold_until": bo.get("hold_until"), "last_event": ev} if (bo.get("active") or bo.get("hold_until")) else None
    paused = bool((st.get("retention") or {}).get("backlog_paused"))
    q = queue_rows(snap)
    counts = snap.cached("dl_counts", lambda: {
        "failed": sum(1 for r in snap.videos.values() if r["state"] == "failed"),
        "upcoming": sum(1 for r in snap.videos.values() if r["state"] == "waiting"),
        "backlog": sum(1 for v in queue_rows(snap) if (snap.videos[v]["priority"] or 0) < 1)})
    dl = [j for j in jobs if j["stage"] == "download"]
    pace = pacing_obj(st, snap, jobs)
    nda = ts(pace.get("next_download_at"))
    # the daily cap only holds back the backlog (worker.is_fresh): a new upload / pushed-by-hand video at the head
    # of the queue still downloads after the normal gap, so show the countdown, not "limit reached"
    head = snap.videos.get(q[0]) if q else None
    fresh = bool(head) and ((head["priority"] or 0) >= 2 or (head["published_ts"] or 0) >= time.time() - 48 * 3600)
    rush = bool(head) and (head["priority"] or 0) >= 3        # "Download now": starts within seconds, no pace wait
    dl_paused = os.path.exists(os.path.join(config.DATA, "downloads.paused"))   # the Pause downloads button's flag
    wait = (None if jobs or not q or rush else "backoff" if bo.get("active") else "daily_cap" if pace.get("at_cap") and not fresh
            else "pace" if nda and nda > time.time() else "older" if pace.get("next_up_waits_older") else None)
    state = ("stopped" if not running else "downloading" if jobs else "paused" if dl_paused else "waiting" if wait else "idle")
    first = lead_job(jobs)
    return {
        "state": state, "paused": dl_paused, "worker_running": running,
        "current": {"video_id": first["video_id"], "channel_id": first["video"]["channel_id"] if first.get("video") else None,
                    "channel_title": first["channel_title"], "title": first["title"],
                    "thumbnail_url": first["video"]["thumbnail_url"] if first.get("video") else None,
                    "stage": first["stage"], "progress": first["progress"], "speed_bps": first["speed_bps"],
                    "eta_seconds": first["eta_seconds"]} if first else None,
        "active": jobs, "active_count": len(dl), "finalizing_count": len(jobs) - len(dl),
        "speed_bps": int((st.get("speed_MBps_now") or 0) * 1e6),
        "concurrency": st.get("concurrency"), "concurrency_max": st.get("concurrency_max") or store.CONC_MAX,
        "concurrency_reason": st.get("concurrency_reason"), "backoff": backoff, "backlog_paused": paused,
        "queue_count": len(q), "waiting_count": counts["backlog"] if paused else 0,
        "failed_count": counts["failed"], "upcoming_count": counts["upcoming"], "overnight": None,
        "wait_reason": wait, "pacing": pace,
    }


def today_obj(snap):
    mid = local_midnight()
    def build():
        o = {"added": 0, "bytes": 0, "backfill": 0, "backfill_bytes": 0, "removed": 0}
        for vid, r in snap.videos.items():
            fin = ts(r["finished_at"])
            if fin and fin >= mid and (r["state"] in IN_PLEX or r["state"] == "pruned"):
                if snap.meta[vid].backfill:
                    o["backfill"] += 1
                    o["backfill_bytes"] += r["size"] or 0
                else:
                    o["added"] += 1
                    o["bytes"] += r["size"] or 0
            if r["state"] == "pruned" and (ts(r["updated_at"]) or 0) >= mid and store.removed_reason(r["reason"]) != "manual":
                o["removed"] += 1
        return o
    return snap.cached(("today", mid), build)


def feed_obj(st):
    nu = st.get("new_uploads") or {}
    interval = nu.get("poll_minutes") or store.POLL_MIN
    last = ts(nu.get("last_poll"))
    med = nu.get("upload_to_plex_minutes_median")
    return {"interval_minutes": interval, "last_check_at": iso(last),
            "next_check_at": iso(last + interval * 60) if last else None,
            "typical_minutes_to_plex": round(med) if isinstance(med, (int, float)) else None,
            "samples": nu.get("samples") or 0}


def counts_obj(snap):
    def build():
        C = snap.channels
        return {"channels": len(C) + len(snap.pseudo), "videos": sum(s["count"] for s in snap.stats.values()),
                "pending_removal": sum(1 for c in C.values() if c["pending_removal_at"] and not c["gone_at"]),
                "channel_errors": sum(1 for s in snap.pseudo.values() if s["state"] == "error"),
                "gone_channels": sum(1 for c in C.values() if c["gone_at"]),
                "gone_videos": sum(1 for v, r in snap.videos.items() if r["state"] in IN_PLEX and snap.meta[v].gone)}
    return snap.cached("counts", build)


_PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WEB = os.environ.get("TUBARR_WEB_DIR") or next(
    (p for p in (os.path.join(os.path.dirname(_PKG), "web"), "/app/web") if os.path.isfile(os.path.join(p, "index.html"))),
    os.path.join(os.path.dirname(_PKG), "web"))
_BUILD = {"at": 0.0, "v": None}


def build_stamp():
    """A short fingerprint of the deployed UI + web-app code (file names, sizes, mtimes). The browser reloads itself
    once when it changes, so a deploy reaches open pages without anyone pressing refresh. Cached for 3 s."""
    now = time.monotonic()
    if _BUILD["v"] and now - _BUILD["at"] < 3:
        return _BUILD["v"]
    h = hashlib.sha1()
    for base, exts in ((_WEB, (".js", ".css", ".html")), (os.path.dirname(os.path.abspath(__file__)), (".py",))):
        for dp, dn, fn in os.walk(base):
            dn[:] = sorted(d for d in dn if d not in ("mock", "vendor", "__pycache__"))
            for f in sorted(fn):
                if f.endswith(exts):
                    try:
                        s = os.stat(os.path.join(dp, f))
                    except OSError:
                        continue
                    h.update(("%s/%s:%d:%d;" % (dp, f, s.st_mtime_ns, s.st_size)).encode())
    _BUILD.update(at=now, v=h.hexdigest()[:12])
    return _BUILD["v"]


def status_obj(data, youtube_brief=None):
    snap, st = data.snapshot(), data.status()
    jobs = attach_videos(snap, active_jobs(st, snap))
    age = data.status_age()
    return {
        "version": VERSION, "build": build_stamp(), "server_time": store.now_iso(), "timezone": config.TZ,
        "storage": storage_head(st, snap), "youtube": youtube_brief,
        "downloader": downloader_obj(st, snap, jobs), "today": today_obj(snap), "feed": feed_obj(st),
        "counts": counts_obj(snap),
        "worker": {"running": age is not None and age < store.STATUS_STALE_S, "updated_at": iso(ts(st.get("updated"))),
                   "uptime_seconds": st.get("uptime_s")},
    }


# ------------------------------------------------------------------ timeline
def _enc(key, vid):
    return base64.urlsafe_b64encode(("%r|%s" % (key, vid)).encode()).decode().rstrip("=")


def _dec(cur):
    try:
        raw = base64.urlsafe_b64decode(cur + "=" * (-len(cur) % 4)).decode()
        k, vid = raw.rsplit("|", 1)
        return float(k), vid
    except Exception:
        return None


def timeline(data, q):
    snap = data.snapshot()
    st = data.status()
    jobs = jobs_by_video(active_jobs(st, snap))
    M, V = snap.meta, snap.videos
    sort = q.get("sort") or "activity"
    states = set(x for x in (q.get("state") or "").split(",") if x)
    chans = set(q.get("channel") or [])
    text = (q.get("q") or "").strip().lower()
    kept, gone, backfill = q.get("kept") == "1", q.get("gone") == "1", q.get("backfill")
    d_from, d_to = q.get("from"), q.get("to")
    rows = []
    for vid, m in M.items():
        if states and m.astate not in states:
            continue
        r = V[vid]
        if chans and r["channel_id"] not in chans:
            continue
        if kept and not m.keep:
            continue
        if gone and not m.gone:
            continue
        if backfill in ("0", "1") and m.backfill != (backfill == "1"):
            continue
        k = m.act if sort == "activity" else (m.pub or 0.0)
        if d_from or d_to:
            day = local_day(k) if sort == "activity" else m.day
            if not day or (d_from and day < d_from) or (d_to and day > d_to):
                continue
        if text and text not in (r["title"] or "").lower() and text not in snap.channel_title(r["channel_id"]).lower():
            continue
        rows.append((k, vid))
    rows.sort(reverse=True)
    total = len(rows)
    cur = _dec(q.get("cursor")) if q.get("cursor") else None
    start = 0
    if cur:
        lo, hi = 0, len(rows)                         # first row strictly after the cursor in (key, id) desc order
        while lo < hi:
            mid = (lo + hi) // 2
            if rows[mid] > cur:
                lo = mid + 1
            else:
                hi = mid
        start = lo
        if start < len(rows) and rows[start] == cur:
            start += 1
    limit = max(1, min(200, int(q.get("limit") or 60)))
    page = rows[start:start + limit]
    nxt = _enc(*page[-1]) if start + limit < len(rows) and page else None
    return {"videos": [mapping.video(snap, vid, jobs.get(vid)) for _, vid in page], "next": nxt, "total": total}


# ------------------------------------------------------------------ activity + history
def history_item(snap, it):
    t, ev, vid, detail = it
    r = snap.videos[vid]
    if ev == "failed":
        detail = mapping.error_of(r)["message"]
    return {"id": "h_%s_%s" % (ev, vid), "at": iso(t), "event": ev, "video": mapping.video(snap, vid),
            "size_bytes": r["size"] if ev in ("downloaded", "removed") else None, "sponsorblock_cut_seconds": None,
            "detail": detail}


def history(data, before=None, limit=50):
    snap = data.snapshot()
    items = snap.history()
    if before:
        b = ts(before)
        items = [x for x in items if (x[0] or 0) < b] if b else items
    limit = max(1, min(200, int(limit or 50)))
    page = items[:limit]
    return {"history": [history_item(snap, x) for x in page],
            "next_before": iso(page[-1][0]) if len(items) > limit and page else None}


def activity(data):
    snap, st = data.snapshot(), data.status()
    jobs = attach_videos(snap, active_jobs(st, snap))
    d = downloader_obj(st, snap, jobs)
    paused = d["backlog_paused"]
    q = queue_rows(snap)
    queue = []
    for i, vid in enumerate(q[:200]):
        r = snap.videos[vid]
        reason = ("new_upload" if (r["priority"] or 0) >= 1 else "upgrade" if r["state"] == "upgrade"
                  else "retry" if (r["attempts"] or 0) > 0 or "retry" in (r["reason"] or "") else "backfill")
        queue.append({"position": i + 1, "video": mapping.video(snap, vid), "reason": reason,
                      "waits_for": "older" if i == 0 and d.get("wait_reason") == "older" else None,
                      "estimated_bytes": snap.estimate(r), "added_at": iso(ts(r["queued_at"]) or ts(r["created_at"]))})
    failed = sorted((r for r in snap.videos.values() if r["state"] == "failed"),
                    key=lambda r: ts(r["updated_at"]) or 0, reverse=True)
    first = lead_job(jobs)
    first = first if first and first.get("video") else None
    current = None
    if first:
        current = {"video": first["video"], "stage": first["stage"], "stages": ["download", "plex"],
                   "progress": first["progress"], "bytes_done": first["bytes_done"], "bytes_total": first["bytes_total"],
                   "speed_bps": first["speed_bps"], "eta_seconds": first["eta_seconds"], "resolution": None,
                   "started_at": iso(time.time() - (first["running_seconds"] or 0)), "sponsorblock_cut_seconds": None}
    return {"downloader": d, "current": current, "active": jobs, "queue": queue, "queue_total": len(q),
            "failed": [{"video": mapping.video(snap, r["id"]), "error": mapping.error_of(r),
                        "failed_at": iso(ts(r["updated_at"]))} for r in failed[:200]],
            "failed_total": len(failed),
            "history": history(data)["history"]}


# ------------------------------------------------------------------ storage
def storage(data):
    snap, st = data.snapshot(), data.status()
    head = storage_head(st, snap)
    V, M = snap.videos, snap.meta

    def build():
        o = {"video_count": 0, "backfill_count": 0, "watched_count": 0, "watched_bytes": 0, "kept_count": 0,
             "preserved": {"count": 0, "bytes": 0}}
        for vid, r in V.items():
            if r["state"] not in IN_PLEX:
                continue
            m = M[vid]
            o["video_count"] += 1
            o["backfill_count"] += 1 if m.backfill else 0
            if r["watched"]:
                o["watched_count"] += 1
                o["watched_bytes"] += r["size"] or 0
            o["kept_count"] += 1 if m.keep else 0
            if m.gone:
                o["preserved"]["count"] += 1
                o["preserved"]["bytes"] += r["size"] or 0
        o["protected_count"] = len(snap.protected)
        big = sorted(((s["size"], cid) for cid, s in snap.stats.items() if s["size"]), reverse=True)[:12]
        o["by_channel"] = [{"channel_id": cid, "title": snap.channel_title(cid), "size_bytes": size,
                            "video_count": snap.stats[cid]["count"],
                            "poster_url": mapping.channel_summary(snap, cid)["poster_url"],
                            "retention_mode": "count" if snap.channels[cid]["keep_count"] else "fill"}
                           for size, cid in big]
        pruned = sorted((r for r in V.values() if r["state"] == "pruned"
                         and store.removed_reason(r["reason"]) in ("watched", "making_room", "retention")),
                        key=lambda r: ts(r["updated_at"]) or 0, reverse=True)[:10]
        o["recently_removed"] = [{"video": mapping.video(snap, r["id"]), "size_bytes": r["size"],
                                  "reason": store.removed_reason(r["reason"]), "at": iso(ts(r["updated_at"]))} for r in pruned]
        # 30 days, oldest first, zero days included
        mids = [local_midnight(days_ago=i) for i in range(29, -1, -1)]
        days = [{"date": local_day(m + 3600), "new_videos": 0, "backfill_videos": 0, "removed_videos": 0, "bytes": 0,
                 "removed_bytes": 0} for m in mids]
        index = {d["date"]: d for d in days}
        for vid, r in V.items():
            fin = ts(r["finished_at"])
            if fin and fin >= mids[0] and (r["state"] in IN_PLEX or r["state"] == "pruned"):
                d = index.get(local_day(fin))
                if d:
                    d["backfill_videos" if M[vid].backfill else "new_videos"] += 1
                    d["bytes"] += r["size"] or 0
            if r["state"] == "pruned":
                t = ts(r["updated_at"])
                if t and t >= mids[0] and store.removed_reason(r["reason"]) != "manual":
                    d = index.get(local_day(t))
                    if d:
                        d["removed_videos"] += 1
                        d["removed_bytes"] += r["size"] or 0
        o["daily"] = days
        return o
    o = dict(snap.cached(("storage", local_midnight()), build))
    week = o["daily"][-7:]
    growth = sum(d["bytes"] - d["removed_bytes"] for d in week) / 7.0
    rolling = head["fill"]["state"] == "rolling"
    o.update(head)
    o["path"] = config.ROOT
    o["sponsorblock_saved_seconds"] = 0
    o["next_to_remove"] = [{"video": mapping.video(snap, vid), "size_bytes": V[vid]["size"], "reason": why,
                            "watched_at": None} for vid, why in snap.roll_order(10)]
    o["projection"] = {"avg_bytes_per_day": int(growth),
                       "days_to_target": (math.ceil((head["target_bytes"] - head["used_bytes"]) / growth)
                                          if growth > 0 and not rolling and head["used_bytes"] < head["target_bytes"] else None)}
    return o


# ------------------------------------------------------------------ estimate (read-only "what would this cost?")
def _inside(ret, rows, today):
    """Which of a channel's listed rows (newest first) a retention override keeps."""
    mode = (ret or {}).get("mode") or "fill"
    if mode in ("fill", "forever"):
        return set(r["id"] for r in rows)
    if mode == "new_only":
        return set(r["id"] for r in rows if r["state"] in IN_PLEX or r["state"] in store.RUNNING
                   or (r["priority"] or 0) >= 1)
    if mode == "count":
        n = max(0, int(ret.get("count") or 0))
        return set(r["id"] for r in rows[:n])
    if mode == "days":
        cut = local_day(time.time() - max(0, int(ret.get("days") or 0)) * 86400)
    elif mode == "since":
        cut = ret.get("since") or "0000-00-00"
    else:
        return set(r["id"] for r in rows)
    out, last_day = set(), None
    for r in rows:                                 # undated backlog rows inherit the date of the newer row above
        day = r["local_date"] or last_day
        if day is None or day >= cut:
            out.add(r["id"])
        last_day = day or last_day
    return out


def estimate(data, body):
    snap, st = data.snapshot(), data.status()
    used, cap = usage(st)
    target = target_bytes(st)
    V, M = snap.videos, snap.meta
    cid = body.get("channel_id")
    if cid:
        if cid not in snap.channels:
            return None
        rows = [V[v] for v in snap.by_channel.get(cid, ()) if not M[v].astate.startswith("skipped")
                and M[v].astate not in ("upcoming",) and V[v]["state"] != "failed"]
        ret = body["retention"] if "retention" in body else mapping.channel_settings(snap.channels[cid])["retention"]
        keep = _inside(ret, rows, None)
        cur = sum(r["size"] or 0 for r in rows if r["state"] in IN_PLEX)
        add = [r for r in rows if r["id"] in keep and r["state"] not in IN_PLEX and r["state"] != "pruned"
               or r["id"] in keep and r["state"] == "pruned" and (ret or {}).get("mode") not in (None, "fill")]
        remove = [r for r in rows if r["state"] in IN_PLEX and r["id"] not in keep and r["id"] not in snap.protected]
        add_b = sum(snap.estimate(r) for r in add)
        rem_b = sum(r["size"] or 0 for r in remove)
        kept_rows = [r for r in rows if r["id"] in keep and (r["state"] in IN_PLEX or r in add)]
        days = [r["local_date"] for r in kept_rows if r["local_date"]]
        projected = cur + add_b - rem_b
        return {"scope": "channel", "channels_affected": 1, "current_bytes": cur, "projected_bytes": projected,
                "add": {"count": len(add), "bytes": add_b}, "remove": {"count": len(remove), "bytes": rem_b},
                "kept_count": len(kept_rows), "oldest_date": min(days) if days else None,
                "reach_date": snap.reach_date() if not ret or ret.get("mode") == "fill" else None,
                "total": {"used_bytes": used, "projected_bytes": used + add_b - rem_b, "cap_bytes": cap,
                          "target_bytes": target},
                "listed_only": True, "estimated": bool(add)}
    # defaults: where would the fill stop for a different target?
    new_target = int(body.get("fill_target_bytes") or target)
    backlog = [V[v] for v in queue_rows(snap)]
    room = new_target - used
    add_n = add_b = 0
    for r in backlog:
        e = snap.estimate(r)
        if room - e < 0:
            break
        room -= e
        add_n += 1
        add_b += e
    rem_n = rem_b = 0
    if new_target < used:
        need = used - new_target
        for vid, _ in snap.roll_order(10 ** 6):
            if rem_b >= need:
                break
            rem_n += 1
            rem_b += V[vid]["size"] or 0
    return {"scope": "defaults", "channels_affected": sum(1 for c in snap.channels.values() if not c["keep_count"]),
            "current_bytes": used, "projected_bytes": used + add_b - rem_b,
            "add": {"count": add_n, "bytes": add_b}, "remove": {"count": rem_n, "bytes": rem_b},
            "kept_count": sum(s["count"] for s in snap.stats.values()) + add_n - rem_n, "oldest_date": None,
            "reach_date": snap.reach_date(),
            "total": {"used_bytes": used, "projected_bytes": used + add_b - rem_b, "cap_bytes": cap,
                      "target_bytes": new_target},
            "listed_only": True, "estimated": True}


# ------------------------------------------------------------------ settings + topics
def _notifications(n, full):
    """The webhook link is write-only: only whether it's set, and (session only) a short hint to recognise it."""
    url = n.get("discord_webhook_url") or ""
    out = {"events": n["events"], "configured": bool(url)}
    if full:
        m = re.search(r"/webhooks/(\d+)/", url)
        out["hint"] = ("webhook …%s" % m.group(1)[-4:]) if m else ("set" if url else None)
    return out


def settings(data, full=True):
    """GET /api/settings. full=False (API key): the webhook hint is left out too."""
    st = data.status()
    s = tsettings.load()
    return {
        "downloads": {
            "fill_target_bytes": target_bytes(st), "protect_newest": s["protect_newest"],
            "quality": "%dp" % int(s["max_height"]), "max_height": int(s["max_height"]), "allow_av1": bool(s["allow_av1"]),
            "max_duration_minutes": 0, "include_live": False, "skip_shorts": True, "sponsorblock": [],
            "sponsorblock_wait_hours": 0, "check_interval_minutes": (st.get("new_uploads") or {}).get("poll_minutes")
            or store.POLL_MIN,
            "concurrency": {"mode": "auto", "start": store.CONC_START, "max": st.get("concurrency_max") or store.CONC_MAX,
                            "min": store.CONC_MIN, "now": st.get("concurrency"), "reason": st.get("concurrency_reason"),
                            "reserved_for_new_uploads": st.get("reserved_new_upload_slots", 1)},
            "rate_limit_bps": config.RATE_LIMIT or None,
            "formats": "Best up to %dp; VP9, or H.264 when that's the best on offer%s; SDR; AAC audio; MKV." % (
                int(s["max_height"]), "; AV1 allowed" if s["allow_av1"] else "; never AV1"),
            "subtitles": "English (the uploader's, else YouTube's auto-captions), as .en.srt next to each video.",
            "pacing": None,
            "human_pace": {"gap_min_minutes": round(s["min_gap_min_h"] * 60, 2),
                           "gap_max_minutes": round(s["min_gap_max_h"] * 60, 2),
                           "per_day_max": s["nightly_cap"],
                           "one_at_a_time": (st.get("concurrency_max") or store.CONC_MAX) <= 1},
        },
        "notifications": _notifications(s["notifications"], full),
        "read_only": False,
    }


def topics(data):
    snap = data.snapshot()
    n = {}
    for ch in snap.channels.values():
        t = mapping.TOPIC_ID.get(ch["topic"]) if ch["topic"] else None
        if t:
            n[t] = n.get(t, 0) + 1
    return {"topics": [{"id": tid, "label": label, "channel_count": n.get(tid, 0)} for tid, label in mapping.TOPICS],
            "unsorted": sum(1 for ch in snap.channels.values() if not ch["topic"])}
