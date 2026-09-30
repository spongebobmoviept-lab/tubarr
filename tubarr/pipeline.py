"""One channel / one video, end to end, into the Plex library folder.
Download + processing happen in the work folder (TUBARR_SCRATCH, default <library>/.staging/_work). The finished
video is written straight into the library's .staging (same filesystem as the library, so the final rename is atomic);
sidecars go first, the video last, so Plex never sees a half-written file or a video without its art/NFO."""
import fcntl
import json
import logging
import os
import shutil
import tempfile
import threading
import time

from . import art, config, db, naming, nfo, subtitles, summarize, ytdl

log = logging.getLogger("tubarr.pipeline")
os.umask(0o002)
_ART = {}
class _LibraryLock:
    """Re-entrant in-process lock + an exclusive flock on a lock file, so the worker (finalize publish, season
    organizer, pruner) and the web app (actions: delete, edits) never move/delete the same library file at once."""

    def __init__(self, path):
        self._r, self._path, self._depth, self._fd = threading.RLock(), path, 0, None

    def __enter__(self):
        self._r.acquire()
        if self._depth == 0:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            self._fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o664)
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        self._depth += 1
        return self

    def __exit__(self, *exc):
        self._depth -= 1
        if self._depth == 0:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
        self._r.release()


FILE_LOCK = _LibraryLock(os.path.join(config.DATA, "locks", "library.lock"))
STREAM = os.environ.get("TUBARR_STREAM", "1") != "0"   # stream-mux downloads (library pool written once)
SIDECARS = (".mkv", ".nfo", ".jpg", ".en.srt")
BOT_RETRY_S = 1800                                      # a video that hit a bot check waits this long before a retry


def show_dir(folder):
    return os.path.join(config.ROOT, folder)


def unique_folder(c, cid, title):
    """The show folder for a NEW channel: its sanitized name, or "<name> [<channel id>]" when another channel already
    uses that folder (sanitizing can make different titles identical: "Gaming 🎮" / "Gaming ⚡" -> "Gaming", and
    emoji-only titles -> "Untitled"). Compared case-insensitively, as on Samba/macOS/Windows filesystems."""
    base = naming.show_folder(title)
    taken = {(r["folder"] or "").casefold() for r in
             c.execute("SELECT folder FROM channels WHERE id<>? AND folder IS NOT NULL", (cid,)).fetchall()}
    if base.casefold() not in taken:
        return base
    return naming.sanitize("%s [%s]" % (base[:100], cid), 150)


def ensure_root():
    os.makedirs(config.STAGING, exist_ok=True)
    pi = os.path.join(config.ROOT, ".plexignore")
    want = ".staging/*\n.staging/*/*\n.staging/*/*/*\n.work/*\n.work/*/*\n"
    if not os.path.exists(pi) or open(pi).read() != want:
        with open(pi, "w") as f:
            f.write(want)


def _art_for(chrow):
    a = _ART.get(chrow["id"])
    if a is None:
        avatar = banner = None
        try:
            avatar = art.fetch(chrow["avatar_url"]) if chrow["avatar_url"] else None
            banner = art.fetch(chrow["banner_url"]) if chrow["banner_url"] else None
        except Exception as e:
            log.warning("channel art fetch failed: %s", e)
        a = _ART[chrow["id"]] = art.ChannelArt(chrow["title"], avatar, banner, subtitle=chrow["topic"] or chrow["category"] or "",
                                               tagline=art.tagline_of(chrow["description"]))
    return a


def ensure_channel(c, ch, source="csv-import", category=None, force_art=False, use_llm=True):
    """ch = ytdl.channel_tab() result. Show folder, generated poster + background, tvshow.nfo, DB row."""
    ensure_root()
    cid = ch["channel_id"]
    title = (ch.get("channel") or ch.get("title") or cid).strip()
    row = c.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
    folder = row["folder"] if row and row["folder"] else unique_folder(c, cid, title)
    urls = {t.get("id"): t.get("url") for t in ch.get("thumbnails") or []}
    category = category or (row["category"] if row else None)
    db.upsert(c, "channels", cid, handle=ch.get("uploader_id"), title=title, folder=folder,
              source=row["source"] if row else source, added_at=row["added_at"] if row else db.now(),
              description=ch.get("description"), subscribers=ch.get("channel_follower_count"), category=category,
              avatar_url=urls.get("avatar_uncropped"), banner_url=urls.get("banner_uncropped"))
    row = c.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
    d = show_dir(folder)
    os.makedirs(d, exist_ok=True)
    if force_art or not os.path.exists(os.path.join(d, "poster.jpg")):
        _ART.pop(cid, None)
        a = _art_for(row)
        art.save_jpg(a.poster(), os.path.join(d, "poster.jpg"))
        art.save_jpg(a.background(), os.path.join(d, "fanart.jpg"))
        db.upsert(c, "channels", cid, art_updated_at=db.now())
    if not row["summary"] or force_art:
        plot, src = summarize.show(title, ch.get("description") or "", use_llm=use_llm)
        db.upsert(c, "channels", cid, summary=plot, summary_source=src)
        row = c.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
    nfo.tvshow(os.path.join(d, "tvshow.nfo"), cid, title, row["summary"], genre=category, handle=ch.get("uploader_id"))
    return row


def ensure_season(c, chrow, year, force=False):
    d = show_dir(chrow["folder"])
    sd = os.path.join(d, naming.season_folder(year))
    os.makedirs(sd, exist_ok=True)
    p1, p2 = os.path.join(d, "Season%04d.jpg" % year), os.path.join(sd, "poster.jpg")
    if force or not os.path.exists(p1):
        im = _art_for(chrow).poster(year=year)
        art.save_jpg(im, p1)
        art.save_jpg(im, p2)
    nfo.season(os.path.join(sd, "season.nfo"), year)
    return sd


def publish(files, dest, vid, own=()):
    """files: {final_name: source_path} in publish order (video last). Sources outside .staging are copied in
    first; then everything is renamed into place (same filesystem, so each rename is atomic).
    Never overwrites a file that isn't this video's own: an existing target is only replaced when its path is in
    `own` (the video's current files, e.g. an upgrade); anything else raises before a single file moves."""
    naming.check_video_id(vid)
    own = {os.path.abspath(p) for p in own}
    for name in files:
        target = os.path.abspath(os.path.join(dest, name))
        if os.path.dirname(target) != os.path.abspath(dest):
            raise RuntimeError("refusing a file name outside the season folder: %r" % name)
        if os.path.lexists(target) and target not in own:
            raise RuntimeError("refusing to overwrite another video's file: %s" % target)
    stage = os.path.join(config.STAGING, vid)
    os.makedirs(stage, exist_ok=True)
    os.makedirs(dest, exist_ok=True)
    staged = {}
    for name, src in files.items():
        if os.path.dirname(os.path.abspath(src)) == os.path.abspath(stage):
            staged[name] = src
        else:
            staged[name] = os.path.join(stage, name + ".part")
            shutil.copyfile(src, staged[name])
    for name, p in staged.items():
        os.replace(p, os.path.join(dest, name))


def add_subtitles(c, vid, info=None):
    """English subtitles for an already-published video (manual, else cleaned auto-captions); drops the .en.srt
    next to the video. One metadata-only lookup if `info` isn't given. Returns the result dict or None."""
    v = c.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
    if not v or not v["path"]:
        return None
    if info is None:
        import yt_dlp
        with yt_dlp.YoutubeDL(ytdl._params(skip_download=True)) as y:
            info = y.extract_info("https://www.youtube.com/watch?v=" + vid, download=False)
    dest = os.path.splitext(v["path"])[0] + ".en.srt"
    res = subtitles.fetch_english(info, dest + ".part")
    if res:
        os.replace(dest + ".part", dest)
        rep = json.loads(v["report"] or "{}")
        rep["subtitles"] = dict(res, added_later=db.now())
        db.upsert(c, "videos", vid, report=rep)
    elif os.path.exists(dest + ".part"):
        os.remove(dest + ".part")
    return res




def meta_file(path, info, show, d, summary):
    """FFMETADATA for the mux: title/show/date/summary tags + YouTube's own chapters."""
    vid = info.get("id") or ""
    write_ffmetadata(path, {"title": (info.get("title") or vid).strip(), "artist": show, "album_artist": show,
                            "date": d.isoformat(), "description": summary, "synopsis": summary,
                            "genre": (info.get("categories") or [None])[0], "network": "YouTube",
                            "comment": "https://www.youtube.com/watch?v=" + vid},
                     [(x.get("start_time") or 0, x.get("end_time") or 0, (x.get("title") or "").strip() or "Chapter")
                      for x in (info.get("chapters") or []) if (x.get("end_time") or 0) > (x.get("start_time") or 0)])


def write_ffmetadata(path, tags, chapters):
    def esc(s):
        s = str(s)
        for ch in ("\\", "=", ";", "#", "\n"):
            s = s.replace(ch, "\\" + ch)
        return s
    lines = [";FFMETADATA1"] + ["%s=%s" % (k, esc(v)) for k, v in tags.items() if v not in (None, "")]
    for s, e, t in chapters:
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", "START=%d" % round(s * 1000), "END=%d" % round(e * 1000), "title=%s" % esc(t)]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def probe(path):
    import subprocess
    out = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-show_chapters", "-of", "json", path],
                         capture_output=True, text=True, timeout=300)
    if out.returncode:
        raise RuntimeError("ffprobe failed: %s" % out.stderr[-300:])
    return json.loads(out.stdout)


class Skip(Exception):
    """The video is not downloaded (Short, live replay, no better version for an upgrade)."""


def download_stage(c, chrow, vid, hook=None, published_ts=None):
    """Stage 1 (download slots): extract + fetch the separate video/audio streams (+ English subtitles) onto the
    work folder in ONE yt-dlp session. Returns a job dict for finalize_stage(). Raises Skip for skipped videos."""
    naming.check_video_id(vid)                               # it becomes part of the work / staging paths
    cid = chrow["id"]
    work = tempfile.mkdtemp(prefix=vid + "-", dir=config.SCRATCH)
    row = c.execute("SELECT created_at, path, state, height, video_format FROM videos WHERE id=?", (vid,)).fetchone()
    old_path = row["path"] if row and row["path"] and os.path.exists(row["path"]) else None
    upgrade = bool(row and row["state"] in ("upgrade", "upgrading") and old_path)
    old_h = (row["height"] or 0) if row else 0
    if upgrade and not old_h and row["video_format"]:
        try:
            old_h = int(row["video_format"].split()[2].split("p")[0])
        except Exception:
            old_h = 0
    db.upsert(c, "videos", vid, channel_id=cid, state="downloading", updated_at=db.now(),
              created_at=row["created_at"] if row and row["created_at"] else db.now())
    q = {"2160p": 2160, "1440p": 1440, "1080p": 1080, "720p": 720}.get(_channel_meta(chrow).get("quality"))
    v = ytdl.PlainVideo(vid, work, work, hook=hook, max_height=q or ytdl.MAX_HEIGHT)
    ok = False
    try:
        t0 = time.time()
        info = v.extract()
        sk = ytdl.skip_reason(info)
        forced = (row_prio(c, vid) or 0) >= 2
        if sk and forced and sk[0] == "skipped" and "Short" not in sk[1] and "too short" not in sk[1]:
            sk = None                                         # "Download anyway" (a live replay, ...)
        if sk and sk[1] == "livestream replay" and chrow["skip_lives"] == 0:
            sk = None                                         # channel setting: include livestream replays
        maxd = chrow["max_duration"] or 0
        if not sk and not forced and maxd > 0 and (info.get("duration") or 0) > maxd:
            sk = ("skipped", "too long: over %d min (channel setting)" % (maxd // 60))
        if sk:
            state, reason = sk
            extra = {} if upgrade else {"title": info.get("title"), "duration": info.get("duration")}
            if state == "waiting" and not upgrade:
                extra["retry_at"] = int((info.get("release_timestamp") or time.time() + 1800) + 300)
            db.upsert(c, "videos", vid, state="done" if upgrade else state, reason=reason, updated_at=db.now(), **extra)
            db.event(c, "info", "skipped: %s" % reason, cid, vid)
            raise Skip(reason)
        vf, af = v.chosen()
        if upgrade:                        # predict the best non-AV1 height before spending a download on it
            pv, _ = ytdl.choose_formats(info)
            vf = vf or pv
        if upgrade and vf and (vf.get("height") or 0) <= old_h:
            db.upsert(c, "videos", vid, state="done", updated_at=db.now(), reason="no better non-AV1 format than %sp" % old_h)
            raise Skip("no better format than %sp" % old_h)
        merged = None
        if STREAM and v.streamable():
            # the video stream goes straight into ffmpeg -> final.mkv in the library's .staging: written once,
            # never read back (the old path wrote the streams, then read them and wrote the merged file again)
            d = naming.local_date(info.get("timestamp") or published_ts, info.get("upload_date"))
            summary, _src = summarize.episode((info.get("title") or vid).strip(), chrow["title"], info.get("description") or "",
                                              [x["title"] for x in (info.get("chapters") or [])], use_llm=False)
            meta = os.path.join(work, "meta.txt")
            meta_file(meta, info, chrow["title"], d, summary)
            stage = os.path.join(config.STAGING, vid)
            shutil.rmtree(stage, ignore_errors=True)
            os.makedirs(stage, exist_ok=True)
            merged = os.path.join(stage, "final.mkv")
            try:
                vf, af = v.stream_mux(merged, meta, hook)
                vfile, afile, srts = merged, None, []
            except ytdl.BotCheck:
                raise
            except Exception as e:
                if "stream mux failed" not in str(e):
                    raise
                log.warning("%s: %s -> normal two-file download", vid, str(e)[:200])
                shutil.rmtree(stage, ignore_errors=True)
                merged = None
                v._clean()
                vfile, afile, srts = v.download()
                vf, af = v.chosen()
        else:
            vfile, afile, srts = v.download()
            vf, af = v.chosen()                               # the fallback may have picked another format
        db.upsert(c, "videos", vid, state="processing", updated_at=db.now())
        ok = True
        return {"vid": vid, "chrow": chrow, "work": work, "info": info, "vfile": vfile, "afile": afile, "srts": srts,
                "vf": vf, "af": af, "fallback": v.fallback_used, "old_path": old_path, "upgrade": upgrade,
                "published_ts": published_ts, "dl_s": round(time.time() - t0, 1), "t_start": t0, "merged": merged}
    except Skip:
        raise
    except ytdl.BotCheck:
        # back in the queue, but not at its head: a later retry_at, so the next claim is a different video
        db.upsert(c, "videos", vid, state="done" if upgrade else "retry", reason="bot check (will retry)",
                  retry_at=None if upgrade else int(time.time() + BOT_RETRY_S), updated_at=db.now())
        raise
    except Exception as e:
        msg = str(e)
        if isinstance(e, ytdl.AgeRestricted) or ytdl.is_age_restricted(msg):
            db.upsert(c, "videos", vid, state="done" if upgrade else "skipped", reason="age-restricted",
                      updated_at=db.now())
            db.event(c, "info", "skipped: age-restricted", cid, vid)
            raise Skip("age-restricted")
        if not upgrade and ("members-only" in msg or "Join this channel" in msg):
            db.upsert(c, "videos", vid, state="skipped_members_only", reason="members only", updated_at=db.now())
            raise Skip("members only")
        if not upgrade and ("will begin in" in msg or "Premieres in" in msg or "live event will begin" in msg):
            db.upsert(c, "videos", vid, state="waiting", reason="upcoming: " + msg[:120], retry_at=int(time.time() + 1800),
                      updated_at=db.now())
            raise Skip("upcoming premiere/stream")
        db.upsert(c, "videos", vid, state="done" if upgrade else "failed", reason=msg[:500], updated_at=db.now())
        db.event(c, "error", "download failed: %s" % e, cid, vid)
        raise
    finally:
        v.close()
        if not ok:
            shutil.rmtree(work, ignore_errors=True)
            shutil.rmtree(os.path.join(config.STAGING, vid), ignore_errors=True)


def _channel_meta(chrow):
    try:
        m = json.loads(chrow["meta"] or "{}")
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def row_prio(c, vid):
    r = c.execute("SELECT priority FROM videos WHERE id=?", (vid,)).fetchone()
    return r["priority"] if r else 0


def save_job(job):
    """Persist a downloaded job next to its streams, so a worker restart finalizes it instead of downloading again."""
    import yt_dlp
    j = {k: v for k, v in job.items() if k != "chrow"}
    j["channel_id"] = job["chrow"]["id"]
    info = {k: v for k, v in job["info"].items() if k not in ("formats", "requested_formats", "requested_downloads",
                                                              "heatmap", "_format_sort_fields")}
    ac = job["info"].get("automatic_captions") or {}
    info["automatic_captions"] = {k: v for k, v in ac.items() if k.startswith("en")}
    j["info"] = yt_dlp.YoutubeDL.sanitize_info(info)
    for k in ("vf", "af"):
        j[k] = yt_dlp.YoutubeDL.sanitize_info(job[k]) if job.get(k) else None
    tmp = os.path.join(job["work"], "job.json.part")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(j, f, default=str)
    os.replace(tmp, os.path.join(job["work"], "job.json"))


def load_job(c, workdir):
    """A saved job from a scratch dir if it can still be finalized (video still 'processing', streams complete)."""
    try:
        with open(os.path.join(workdir, "job.json"), encoding="utf-8") as f:
            j = json.load(f)
        row = c.execute("SELECT state FROM videos WHERE id=?", (j["vid"],)).fetchone()
        chrow = c.execute("SELECT * FROM channels WHERE id=?", (j["channel_id"],)).fetchone()
        if not naming.valid_video_id(j.get("vid")) or \
                not row or row["state"] != "processing" or not chrow or not os.path.exists(j["vfile"]) or \
                (j.get("afile") and not os.path.exists(j["afile"])):
            return None
        j["chrow"] = chrow
        return j
    except Exception:
        return None


def finalize_stage(c, job, use_llm=False):
    """Stage 2 (finalize pool): ONE ffmpeg -c copy merge (video + audio + metadata + YouTube's own chapters) written
    straight into the library's .staging, verify, thumbnail, NFO, publish (sidecars first, video last). The summary is
    rules-based (the creator's own description, cleaned; no AI anywhere)."""
    import subprocess
    vid, chrow, work, info = job["vid"], job["chrow"], job["work"], job["info"]
    naming.check_video_id(vid)                               # a saved job is read back from disk after a restart
    cid, show = chrow["id"], chrow["title"]
    stage = os.path.join(config.STAGING, vid)
    if not job.get("merged"):
        shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage, exist_ok=True)
    vf, af, vfile, afile, srts = job["vf"], job["af"], job["vfile"], job["afile"], job["srts"]
    report = {"video_id": vid, "channel": show}
    try:
        d = naming.local_date(info.get("timestamp") or job["published_ts"], info.get("upload_date"))
        idx = db.allocate_episode(c, cid, vid, d)
        ep = naming.episode_number(d, idx)
        title = (info.get("title") or vid).strip()
        db.upsert(c, "videos", vid, local_date=d.isoformat(), title=title)
        summary, summary_src = summarize.episode(title, show, info.get("description") or "",
                                                 [x["title"] for x in (info.get("chapters") or [])], use_llm=use_llm)
        category = (info.get("categories") or [None])[0]
        t0 = time.time()
        if job.get("merged"):
            media, merge_s = job["merged"], 0.0               # already muxed while downloading
        else:
            media = os.path.join(stage, "final.mkv")
            meta = os.path.join(work, "meta.txt")
            meta_file(meta, info, show, d, summary)
            mi = "2" if afile else "1"
            cmd = (["ffmpeg", "-v", "error", "-y", "-i", vfile] + (["-i", afile] if afile else []) + ["-i", meta,
                   "-map", "0:v:0", "-map", "1:a:0" if afile else "0:a:0?", "-map_metadata", mi, "-map_chapters", mi,
                   "-c", "copy", "-metadata:s:a:0", "language=eng", media])
            mp = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            if mp.returncode:
                raise RuntimeError("merge failed: %s" % mp.stderr[-400:])
            merge_s = round(time.time() - t0, 1)
        pr = probe(media)
        vs = next(s for s in pr["streams"] if s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic"))
        aus = next((s for s in pr["streams"] if s["codec_type"] == "audio"), None)
        dur = float(pr["format"].get("duration") or 0)
        if vs["codec_name"] == "av1":
            raise RuntimeError("AV1 slipped through - refusing")
        if not aus or (info.get("duration") and abs(dur - info["duration"]) > 5):
            raise RuntimeError("bad download: audio=%s duration %.1f vs %s" % (bool(aus), dur, info.get("duration")))
        res = "%sx%s" % (vs.get("width"), vs.get("height"))
        report.update(title=title, date=d.isoformat(), video_format=vf and vf.get("format_id"),
                      vcodec=vs["codec_name"], res=res, fps=vf and vf.get("fps"), dynamic_range=vf and vf.get("dynamic_range"),
                      audio=aus["codec_name"], protocol=vf and vf.get("protocol"), fallback=job["fallback"],
                      download_s=job["dl_s"], merge_s=merge_s, duration=round(dur, 1), chapters=len(pr.get("chapters", [])))
        db.upsert(c, "videos", vid, published_at=info.get("timestamp"), duration=info.get("duration"),
                  height=vs.get("height"), vcodec=vs["codec_name"],
                  video_format="%s %s %sp%s" % (vf and vf.get("format_id"), vs["codec_name"], vs.get("height"), vf and vf.get("fps")),
                  audio_format=af and "%s %s" % (af.get("format_id"), af.get("acodec")),
                  description=info.get("description"), summary=summary, summary_source=summary_src)
        thumb = os.path.join(work, "thumb.jpg")
        try:
            report["thumb"] = art.episode_thumb(info, thumb)
        except Exception as e:
            thumb = None
            log.warning("%s: thumbnail failed: %s", vid, e)
        srt = os.path.join(work, "english.srt")               # manual English, else cleaned auto-captions
        try:
            report["subtitles"] = subtitles.fetch_english(info, srt)
        except Exception as e:
            log.warning("%s: subtitles failed: %s", vid, e)
        from . import seasons
        with FILE_LOCK:                                       # numbering + publish are atomic w.r.t. the organizer
            place = None
            try:
                place = seasons.placement(c, chrow, vid, d)
            except Exception as e:
                log.warning("%s: season placement failed (%s); using the date-based number", vid, e)
            season, ep_no, folder, _stitle = place or (d.year, ep, naming.season_folder(d.year), str(d.year))
            if season >= 1900:
                sd = ensure_season(c, chrow, season)
            else:
                sd = os.path.join(show_dir(chrow["folder"]), folder)
                os.makedirs(sd, exist_ok=True)
            base = "%s - %s - %s" % (chrow["folder"], seasons.code_for(season, ep_no), naming.sanitize(title, 140))
            cur = c.execute("SELECT path FROM videos WHERE id=?", (vid,)).fetchone()
            mine = [p for p in (job["old_path"], cur and cur["path"]) if p]
            own = {os.path.splitext(p)[0] + ext for p in mine for ext in SIDECARS}
            if any(os.path.lexists(os.path.join(sd, base + ext)) and os.path.join(sd, base + ext) not in own
                   for ext in SIDECARS):
                base = "%s [%s]" % (base, vid)                # same day, same code and title: keep both videos
            report["code"] = seasons.code_for(season, ep_no)
            files = {}
            if thumb:
                files[base + ".jpg"] = thumb
            if report.get("subtitles"):
                files[base + ".en.srt"] = srt
            nf = os.path.join(work, "episode.nfo")
            nfo.episode(nf, vid, title, summary, d.isoformat(), season, ep_no, show, dur, category)
            files[base + ".nfo"] = nf
            files[base + ".mkv"] = media
            publish(files, sd, vid, own=own)
            final = os.path.join(sd, base + ".mkv")
            for old_path in {job["old_path"], cur and cur["path"]}:
                if old_path and old_path != final and os.path.exists(old_path):
                    ob = os.path.splitext(old_path)[0]
                    os.remove(old_path)                       # replaced (upgrade): drop the old file + its sidecars
                    for ext in (".nfo", ".jpg", ".en.srt"):
                        if ob != os.path.splitext(final)[0] and os.path.exists(ob + ext):
                            os.remove(ob + ext)
                    report["replaced"] = old_path
            report.update(path=final, size=os.path.getsize(final), total_s=round(time.time() - job["t_start"], 1),
                          summary=summary, summary_source=summary_src, category=category)
            db.upsert(c, "videos", vid, state="done", season=season, episode=ep_no, path=final, size=report["size"],
                      out_duration=dur, updated_at=db.now(), report=report, reason=None)
        db.event(c, "info", "done: %s (%s)" % (title, report.get("code")), cid, vid)
        if category and not chrow["category"]:
            db.upsert(c, "channels", cid, category=category)
        return report
    except Exception as e:
        db.upsert(c, "videos", vid, state="done" if job["upgrade"] else "failed", reason=str(e)[:500], updated_at=db.now())
        db.event(c, "error", "finalize failed: %s" % e, cid, vid)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(stage, ignore_errors=True)


def process_video(c, chrow, vid, published_ts=None, hook=None, use_llm=True):
    """Both stages in one call (tools / tests). Returns the report, or None if the video was skipped."""
    try:
        job = download_stage(c, chrow, vid, hook=hook, published_ts=published_ts)
    except Skip:
        return None
    return finalize_stage(c, job, use_llm=use_llm)
