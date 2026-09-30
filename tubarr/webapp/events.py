"""Live updates (API.md "Live updates"): one poller turns DB and status.json changes into Server-Sent Events.

Every second (only while someone is connected): the DB's data_version is checked; when it moved, the snapshot is
rebuilt and diffed against the previous one (video.added / video.state / video.removed, channel.*, history).
status.json (the worker rewrites it every 10 s) drives `status`, `storage` and `video.progress`. The Connect YouTube
state drives `sync`. A big burst (the worker re-queuing thousands of rows at start-up) becomes one `resync` event:
the UI then refetches the open screen, exactly as after a reconnect.
"""
import asyncio
import json
import logging
import time

from . import VERSION, mapping, security, store, views

log = logging.getLogger("tubarr.webapp.events")
RECHECK = 15                     # seconds: re-validate the sign-in of an open stream (sign-out / key revoked -> close)
BURST = 300                      # more video changes than this in one step -> a single `resync`
QUEUE_MAX = 2000


class Hub:
    def __init__(self):
        self.clients = set()
        self.seq = 0

    def msg(self, event, data):
        self.seq += 1
        return "id: %d\nevent: %s\ndata: %s\n\n" % (self.seq, event, json.dumps(data, separators=(",", ":")))

    def subscribe(self):
        q = asyncio.Queue(QUEUE_MAX)
        self.clients.add(q)
        return q

    def unsubscribe(self, q):
        self.clients.discard(q)

    def publish(self, event, data):
        if not self.clients:
            return
        m = self.msg(event, data)
        for q in list(self.clients):
            try:
                q.put_nowait(m)
            except asyncio.QueueFull:                   # a stalled client: drop its backlog, make it refetch
                while not q.empty():
                    q.get_nowait()
                q.put_nowait(self.msg("resync", {}))


HUB = Hub()


def _video_fps(snap):
    def build():
        return {vid: (tuple(r), snap.protected.get(vid), snap.channel_title(r["channel_id"]))
                for vid, r in snap.videos.items()}
    return snap.cached("fp", build)


def _channel_map(snap, ctx):
    out = {cid: mapping.channel_summary(snap, cid, ctx) for cid in snap.channels}
    for h in snap.pseudo:
        out[h] = mapping.channel_summary(snap, h, ctx)
    return out


def _strip(status):
    s = dict(status)
    s.pop("server_time", None)
    return json.dumps(s, sort_keys=True, default=str)


class Poller:
    def __init__(self, data):
        self.data = data
        self.snap = None
        self.channels = None
        self.hist_top = None
        self.status_key = None
        self.storage_key = None
        self.sync_key = None
        self.progress = {}
        self.task = None

    def reset(self):
        self.snap = self.channels = self.hist_top = None
        self.status_key = self.storage_key = self.sync_key = None
        self.progress = {}

    async def run(self):
        while True:
            await asyncio.sleep(1.0)
            if not HUB.clients:
                self.reset()                            # nothing to diff against when someone connects again
                continue
            try:
                await asyncio.to_thread(self.step)
            except Exception as e:                      # keep streaming; the next step retries
                log.warning("event step failed: %s", e)

    def step(self):
        """Runs in a worker thread; publishing is thread-hopped back onto the loop."""
        out = []
        snap = self.data.snapshot()
        st = self.data.status()
        ctx = mapping.Ctx(st)
        # --- DB changes
        if self.snap is None:
            self.snap, self.channels = snap, _channel_map(snap, ctx)
            self.hist_top = snap.history()[0][:3] if snap.history() else None
        elif snap is not self.snap:
            old, new = _video_fps(self.snap), _video_fps(snap)
            added = [v for v in new if v not in old]
            changed = [v for v in new if v in old and new[v] != old[v]]
            removed = [v for v in old if v not in new]
            if len(added) + len(changed) + len(removed) > BURST:
                out.append(("resync", {}))
            else:
                jobs = views.jobs_by_video(views.active_jobs(st, snap))
                out += [("video.added", {"video": mapping.video(snap, v, jobs.get(v))}) for v in added]
                out += [("video.state", {"video": mapping.video(snap, v, jobs.get(v))}) for v in changed]
                out += [("video.removed", {"id": v, "channel_id": self.snap.videos[v]["channel_id"]}) for v in removed]
            chans = _channel_map(snap, ctx)
            for cid, c in chans.items():
                if cid not in self.channels:
                    out.append(("channel.added", {"channel": c}))
                elif c != self.channels[cid]:
                    out.append(("channel.updated", {"channel": c}))
            out += [("channel.removed", {"id": cid}) for cid in self.channels if cid not in chans]
            self.channels = chans
            hist = snap.history()
            if hist and self.hist_top is not None:
                fresh = []
                for it in hist[:25]:
                    if it[:3] == self.hist_top:
                        break
                    fresh.append(it)
                out += [("history", {"item": views.history_item(snap, it)}) for it in reversed(fresh)]
            self.hist_top = hist[0][:3] if hist else None
            self.snap = snap
        # --- status.json: progress, status, storage
        jobs = views.active_jobs(st, snap)
        prog = {}
        for j in jobs:
            if not j["video_id"]:
                continue
            key = (j["stage"], j["progress"], j["bytes_done"], j["speed_bps"])
            prog[j["video_id"]] = key
            if self.progress.get(j["video_id"]) != key:
                out.append(("video.progress", {"id": j["video_id"], "stage": j["stage"], "progress": j["progress"],
                                               "bytes_done": j["bytes_done"], "bytes_total": j["bytes_total"],
                                               "speed_bps": j["speed_bps"], "eta_seconds": j["eta_seconds"]}))
        self.progress = prog
        status = views.status_obj(self.data)
        key = _strip(status)
        if key != self.status_key:
            self.status_key = key
            out.append(("status", status))
            sk = json.dumps(status["storage"], sort_keys=True, default=str)
            if sk != self.storage_key:
                self.storage_key = sk
                out.append(("storage", status["storage"]))
        if out:
            self.loop.call_soon_threadsafe(self._publish, out)

    @staticmethod
    def _publish(items):
        for ev, data in items:
            HUB.publish(ev, data)

    def start(self):
        self.loop = asyncio.get_running_loop()
        self.task = asyncio.create_task(self.run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass


async def stream(request, data):
    """The /api/events body: hello, status, then whatever the poller publishes (ping every 15 s)."""
    q = HUB.subscribe()
    try:
        yield HUB.msg("hello", {"version": VERSION, "build": views.build_stamp(), "server_time": store.now_iso()})
        status = await asyncio.to_thread(views.status_obj, data)
        yield HUB.msg("status", status)
        checked = time.monotonic()
        while True:
            ping = False
            try:
                m = await asyncio.wait_for(q.get(), timeout=15)
            except asyncio.TimeoutError:
                m, ping = HUB.msg("ping", {}), True
            if ping or time.monotonic() - checked >= RECHECK:
                checked = time.monotonic()
                via, _ = await asyncio.to_thread(security.identify, request)
                if via is None:                          # signed out, password changed or API key revoked
                    break
            yield m
            if await request.is_disconnected():
                break
    finally:
        HUB.unsubscribe(q)
