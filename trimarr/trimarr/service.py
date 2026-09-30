"""The service: one process, one container. A pass = scan the library, re-check SponsorBlock for what's due, trim
what's eligible one video at a time. Then it sleeps (default 24 h) and wakes only for API requests and an hourly
look at expired originals.

Off (the default, until trimming is switched on in Tubarr: Settings -> Trim ads) means idle: no passes, no library
scan, no SponsorBlock request, no file changes. It only answers Tubarr's API and does what a signed-in user asks for
there (a single trim, undo, check or "Run a pass now"). The one exception is originals Trimarr itself kept from
earlier trims: their keep period still runs out while it's off."""
import collections
import logging
import signal
import threading
import time

from . import api, db, settings, sponsorblock, trimmer
from .trimmer import iso

log = logging.getLogger("trimarr")


class Service:
    def __init__(self):
        self.jobs = collections.deque()
        self.jobs_lock = threading.Lock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.pass_requested = False
        self.state = "starting"
        self.current = None
        self.last_pass = None
        self.next_pass_at = None
        self.sponsorblock_error = None

    # ------------------------------------------------------------------ requests from the API
    def submit(self, kind, vid=None, **kw):
        with self.jobs_lock:
            self.jobs.append((kind, vid, kw))
        self.wake.set()

    def queued(self):
        with self.jobs_lock:
            return [{"action": k, "video_id": v} for k, v, _ in self.jobs]

    def request_pass(self):
        self.pass_requested = True
        self.wake.set()

    def do_jobs(self):
        while True:
            with self.jobs_lock:
                if not self.jobs:
                    return
                kind, vid, kw = self.jobs.popleft()
            try:
                if kind == "trim":
                    self._trim(vid, manual=True, force=kw.get("force", False))
                elif kind == "undo":
                    self.current = {"video_id": vid, "action": "undo", "step": "restoring", "started_at": iso(time.time())}
                    trimmer.undo(vid)
                elif kind == "check":
                    trimmer.check(vid)
            except trimmer.Skip as e:
                db.upsert(vid, detail=str(e))
                db.event("info", "%s not done: %s" % (kind, e), vid)
            except Exception as e:                                   # never let one request kill the service
                log.exception("%s %s failed", kind, vid)
                db.event("error", "%s failed: %s" % (kind, e), vid)
            finally:
                self.current = None

    def _trim(self, vid, manual=False, force=False):
        """Returns 'trimmed' or 'failed'; raises trimmer.Skip."""
        row = db.video(vid) or {}
        self.current = {"video_id": vid, "action": "trim", "channel": row.get("channel"), "title": row.get("title"),
                        "step": "starting", "started_at": iso(time.time()), "manual": manual}
        try:
            trimmer.trim(vid, manual=manual, force=force,
                         progress=lambda step: self.current and self.current.update(step=step))
            return "trimmed"
        except trimmer.Skip:
            raise
        except trimmer.TrimError as e:
            trimmer.fail(vid, e)
        except Exception as e:
            log.exception("trim %s crashed", vid)
            trimmer.fail(vid, "%s: %s" % (type(e).__name__, e))
        finally:
            self.current = None
        return "failed"

    # ------------------------------------------------------------------ the pass
    def run_pass(self):
        s = settings.load()
        stats = {"started_at": iso(time.time()), "finished_at": None, "checked": 0, "trimmed": 0, "failed": 0,
                 "skipped": 0, "errors": 0}
        self.last_pass = stats
        blocked_until = None
        self.state = "scanning"
        trimmer.housekeeping()
        trimmer.sync_library()
        self.state = "checking"
        for r in trimmer.due_for_check(s):
            self.do_jobs()
            if self.stop.is_set():
                return None
            try:
                trimmer.check(r["video_id"], s)
                stats["checked"] += 1
                self.sponsorblock_error = None
            except sponsorblock.SponsorBlockError as e:
                self.sponsorblock_error = {"at": iso(time.time()), "message": str(e)}
                stats["errors"] += 1
                log.warning("SponsorBlock: %s (checks resume next pass)", e)
                break
            except Exception as e:
                stats["errors"] += 1
                log.warning("check %s failed: %s", r["video_id"], e)
        s = settings.load()
        if s["enabled"] and not s["paused"]:
            self.state = "trimming"
            skip = set()
            while not self.stop.is_set():
                self.do_jobs()
                s = settings.load()
                if not s["enabled"] or s["paused"]:
                    break
                r = trimmer.next_candidate(s, skip)
                if not r:
                    break
                try:
                    stats[self._trim(r["video_id"])] += 1
                except trimmer.Skip as e:
                    skip.add(r["video_id"])
                    stats["skipped"] += 1
                    db.upsert(r["video_id"], detail=str(e))
                    if "originals cap" in str(e):
                        nxt = db.one("SELECT MIN(backup_expires) AS t FROM videos WHERE backup_path IS NOT NULL")
                        blocked_until = nxt["t"] if nxt and nxt["t"] else None
                        break
        stats["finished_at"] = iso(time.time())
        log.info("pass done: %s", stats)
        return blocked_until

    def active(self):
        """A pass may run: trimming is on, or someone asked for a pass."""
        return bool(settings.load()["enabled"]) or self.pass_requested

    @staticmethod
    def keeps_originals():
        return db.one("SELECT 1 AS x FROM videos WHERE backup_path IS NOT NULL LIMIT 1") is not None

    def idle(self, wait=60):
        """Trimming is off: wait for API requests, the switch-on or a requested pass. Touches nothing on its own (only
        originals kept from earlier trims still expire, hourly)."""
        self.state = "off"
        self.next_pass_at = None
        last_hk = time.time()
        while not self.stop.is_set() and not self.active():
            self.wake.wait(timeout=wait)
            self.wake.clear()
            self.do_jobs()
            if time.time() - last_hk > 3600:
                last_hk = time.time()
                if self.keeps_originals():
                    trimmer.housekeeping()

    def run_forever(self):
        api.start(self)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: (self.stop.set(), self.wake.set()))
        log.info("Trimarr service up (API on :%s)", api.port())
        api.wait_for_token(stop=self.stop)
        while not self.stop.is_set():
            if not self.active():
                log.info("automatic trimming is off: idle (no scans, no SponsorBlock requests) until it's switched on "
                         "in Tubarr")
                self.idle()
                continue
            self.pass_requested = False
            try:
                blocked_until = self.run_pass()
            except Exception as e:
                log.exception("pass failed: %s", e)
                blocked_until = None
            s = settings.load()
            nxt = time.time() + s["pass_interval_hours"] * 3600
            if blocked_until:
                nxt = min(nxt, blocked_until + 120)
            self.next_pass_at = nxt
            self.state = "sleeping"
            last_hk = time.time()
            while not self.stop.is_set() and time.time() < nxt and not self.pass_requested:
                self.wake.wait(timeout=60)
                self.wake.clear()
                self.do_jobs()
                if not settings.load()["enabled"]:
                    break                                           # switched off: go idle now
                if time.time() - last_hk > 3600:
                    trimmer.housekeeping()
                    last_hk = time.time()
        self.state = "stopped"
        log.info("Trimarr service stopped")
