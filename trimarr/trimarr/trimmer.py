"""One video at a time: check SponsorBlock, plan, cut, verify, swap in place, shift captions. Trimarr holds no Plex
credentials: each swap is stamped (swapped_at) and Tubarr, which already has its own scoped Plex token, asks Plex to
rescan the folder (tubarr/webapp/addons.py). Plus undo, approve (delete a kept original now), and housekeeping (expired originals, interrupted runs)."""
import fcntl
import logging
import os
import shutil
import time
from contextlib import contextmanager

from . import captions, chapters, config, cutter, db, library, media, planner, settings, sponsorblock, verify

log = logging.getLogger("trimarr")
PLEXIGNORE = "*\n*/*\n*/*/*\n*/*/*/*\n"


class Skip(Exception):
    """Not trimmed now, and not an error (too new, nothing to cut, being watched, originals cap, ...)."""


class TrimError(Exception):
    """A trim was attempted and failed. The original is untouched."""


@contextmanager
def job_lock(block=True):
    """One trim/undo/cleanup at a time, also between the service and a one-off CLI container."""
    os.makedirs(config.DATA, exist_ok=True)
    f = open(config.LOCK_PATH, "a")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if block else fcntl.LOCK_NB))
        except BlockingIOError:
            raise Skip("another trim is running") from None
        yield
    finally:
        f.close()


def _ensure_dir(d):
    os.makedirs(d, exist_ok=True)
    pi = os.path.join(d, ".plexignore")             # belt and braces: Plex never scans the work/originals folders
    if not os.path.exists(pi):
        with open(pi, "w") as f:
            f.write(PLEXIGNORE)


def _stat(path):
    try:
        return os.stat(path)
    except FileNotFoundError:
        return None


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)) if t else None


# ----------------------------------------------------------------------------------------------- library state
def sync_library():
    """Scan the library: add new videos, notice files Tubarr replaced or removed. Returns the scan entries."""
    entries = [e for e in library.scan() if e["video_id"]]
    seen = set()
    for e in entries:
        vid = e["video_id"]
        seen.add(vid)
        row = db.video(vid)
        base = {k: e[k] for k in ("path", "channel_id", "channel", "folder", "title", "upload_date")}
        stamp = {"size": e["size"], "mtime": e["mtime"], "inode": e["inode"]}
        changed = lambda fields: any(row.get(k) != v for k, v in fields.items())   # noqa: E731 (write only on change)
        if row is None:
            db.upsert(vid, state="new", **base, **stamp)
        elif row["state"] == "trimmed":
            if e["inode"] != row["after_inode"]:          # Tubarr replaced it (e.g. a better download)
                drop_backup(row, "the library file was replaced after trimming")
                db.upsert(vid, state="new", detail="file replaced after trimming; checked again", checked_at=None,
                          plan=None, trimmed_at=None, after_inode=None, **base, **stamp)
            elif changed(base):
                db.upsert(vid, **base)
        elif row["state"] == "undone":
            if changed(dict(base, **stamp)):
                db.upsert(vid, **base, **stamp)
        elif (row["inode"], row["size"]) != (e["inode"], e["size"]):
            db.upsert(vid, state="new", detail="file changed; checked again", checked_at=None, plan=None,
                      attempts=0, **base, **stamp)
        elif changed(dict(base, **stamp)):
            db.upsert(vid, **base, **stamp)
    for row in db.query("SELECT * FROM videos WHERE state != 'gone'"):
        if row["video_id"] not in seen and not (row["path"] and os.path.exists(row["path"])):
            drop_backup(row, "the video left the library")
            db.upsert(row["video_id"], state="gone", detail="no longer in the library (removed by Tubarr)")
    return entries


def due_for_check(s, include_young=False):
    now = time.time()
    due = []
    rows = db.query("SELECT * FROM videos WHERE state IN ('new','waiting','clean','cuttable','suspicious') OR "
                    "(state='failed' AND COALESCE(attempts,0) < 3 AND COALESCE(checked_at,0) < ?)",
                    (now - s["recheck_hours"] * 3600,))
    for r in rows:
        if not r["mtime"] or not library.file_age_ok(r["mtime"], s, now):
            if r["state"] != "new":
                db.upsert(r["video_id"], state="new", detail="file is newer than %d minutes" % s["min_file_age_minutes"])
            continue
        eligible = library.upload_eligible_at(r["upload_date"], r["mtime"], s)
        if now < eligible and not include_young:
            if r["state"] != "waiting":
                db.upsert(r["video_id"], state="waiting", detail="uploaded less than %d h ago; checked from %s"
                          % (s["min_upload_age_hours"], iso(eligible)))
            continue
        if r["state"] in ("new", "waiting", "failed") or not r["checked_at"] or \
                now - r["checked_at"] >= s["recheck_hours"] * 3600 - 600:
            due.append(r)                                  # a failed one gets a fresh check a day later (3 tries)
    due.sort(key=lambda r: (r["upload_date"] or "", r["mtime"] or 0), reverse=True)   # newest uploads first
    return due


def check(vid, s=None, exact=False):
    """Fetch SponsorBlock segments and store the plan. Returns the updated row.
    exact=False (the service): plan from SponsorBlock's times only, no keyframe reads. The keyframe-exact plan is
    made at trim time anyway, and this keeps daily re-checks from reading the busy pool."""
    s = s or settings.load()
    row = db.video(vid)
    path = row["path"]
    st = _stat(path)
    if st is None:
        db.upsert(vid, state="gone", detail="no longer in the library")
        return db.video(vid)
    segs = sponsorblock.stamp_first_seen(sponsorblock.fetch(vid), row["segments"])
    info = media.probe(path)
    dur = media.duration(info)
    plan = planner.make_plan(segs, dur, s)
    if plan["cuts"] and exact:
        plan = planner.make_plan(segs, dur, s, media.Keyframes(path, info))
    now = time.time()
    if not plan["cuts"]:
        state, detail = "clean", None
    elif plan["suspicious"]:
        state, detail = "suspicious", "would remove %.0f%% of the video (limit %.0f%%)" % (
            plan["fraction"] * 100, s["max_removed_fraction"] * 100)
    else:
        state, detail = "cuttable", None
    if now < library.upload_eligible_at(row["upload_date"], st.st_mtime, s):
        state, detail = "waiting", "uploaded less than %d h ago" % s["min_upload_age_hours"]
    db.upsert(vid, state=state, detail=detail, segments=segs, plan=plan, duration=dur, checked_at=now,
              sb_seconds=plan["sb_seconds"], cut_seconds=plan["cut_seconds"])
    return db.video(vid)


def originals_bytes():
    r = db.one("SELECT COALESCE(SUM(before_size),0) AS b, COUNT(*) AS n FROM videos WHERE backup_path IS NOT NULL")
    return int(r["b"]), int(r["n"])


def next_candidate(s, skip=()):
    for r in db.query("SELECT * FROM videos WHERE state='cuttable' ORDER BY upload_date DESC, mtime DESC"):
        if r["video_id"] in skip or (r["attempts"] or 0) >= 3:
            continue
        if not settings.channel_setting(s, r["channel_id"])[1]:
            continue
        if not library.file_age_ok(r["mtime"] or time.time(), s):
            continue
        if time.time() < library.upload_eligible_at(r["upload_date"], r["mtime"], s):
            continue
        return r
    return None


# ----------------------------------------------------------------------------------------------- trim
def _spot_check(orig_cues, new_cues, plan):
    """The first caption after the first cut: where it was, where it is now, and where it should be."""
    if not plan["cuts"] or not orig_cues:
        return None
    after = plan["cuts"][0]["end"]
    for s0, e0, body in orig_cues:
        if s0 >= after + 0.5:
            want = planner.to_output(s0, plan["pieces"])
            got = next(((s1, b1) for s1, e1, b1 in new_cues if b1 == body and abs(s1 - want) < 0.05), None)
            return {"text": body.replace("\n", " ")[:120], "original_at": round(s0, 3), "expected_at": round(want, 3),
                    "trimmed_at": round(got[0], 3) if got else None, "ok": got is not None}
    return None


def _swap(path, inode, new_file, subs):
    """Keep the original (hard link into .trim-originals: no copy, same dataset), then atomically rename the trimmed
    file over the library path. Same for the captions. Returns (backup_path, [[srt, srt_backup], ...])."""
    _ensure_dir(config.ORIGINALS)
    backup = os.path.join(config.ORIGINALS, os.path.relpath(path, config.ROOT))
    os.makedirs(os.path.dirname(backup), exist_ok=True)
    srt_backups = [(sp, os.path.join(config.ORIGINALS, os.path.relpath(sp, config.ROOT)), dst) for sp, dst in subs]
    for p in [backup] + [b for _, b, _ in srt_backups]:
        if os.path.lexists(p):
            os.remove(p)                                   # a stale leftover from an older run
    os.link(path, backup)
    if os.stat(backup).st_ino != inode:                   # Tubarr swapped the file in the last instant
        os.remove(backup)
        raise Skip("the file changed while it was being trimmed")
    if not os.path.exists(os.path.splitext(path)[0] + ".nfo"):
        os.remove(backup)                                  # Tubarr is removing this video right now
        raise Skip("the video is being removed by Tubarr")
    os.replace(new_file, path)                            # <- the swap (atomic: same folder tree, same dataset)
    done = []
    try:
        for sp, b, dst in srt_backups:
            if os.path.exists(sp):
                os.link(sp, b)
                os.replace(dst, sp)
                done.append([sp, b])
    except OSError as e:                                   # never leave video and captions disagreeing
        os.replace(backup, path)
        for sp, b in done:
            os.replace(b, sp)
        raise TrimError("caption swap failed (%s); original restored" % e) from None
    return backup, done


def trim(vid, manual=False, force=False, progress=None):
    """Trim one video. Raises Skip (not now), TrimError (failed, original untouched). Returns the report."""
    step = progress or (lambda *_: None)
    if not library.valid_video_id(vid):                    # the ID becomes a folder name under .trim-work
        raise Skip("not a valid YouTube video ID: %r" % str(vid)[:40])
    s = settings.load()
    with job_lock():
        row = db.video(vid)
        if not row:
            raise Skip("unknown video %s" % vid)
        if row["state"] == "trimmed":
            raise Skip("already trimmed")
        if row["state"] == "undone" and not manual:
            raise Skip("this one was restored by hand; only a manual trim touches it again")
        path = row["path"]
        st = _stat(path)
        if not path or library.is_hidden(path) or st is None:
            db.upsert(vid, state="gone", detail="no longer in the library")
            raise Skip("the file is no longer in the library")
        if not library.file_age_ok(st.st_mtime, s):
            raise Skip("the file is newer than %d minutes" % s["min_file_age_minutes"])
        if not manual and time.time() < library.upload_eligible_at(row["upload_date"], st.st_mtime, s):
            raise Skip("uploaded less than %d h ago" % s["min_upload_age_hours"])

        step("planning")
        info = media.probe(path)
        dur = media.duration(info)
        segs = row["segments"]
        if manual or segs is None or time.time() - (row["checked_at"] or 0) > 3600:
            try:
                segs = sponsorblock.stamp_first_seen(sponsorblock.fetch(vid), row["segments"])
            except sponsorblock.SponsorBlockError as e:
                if segs is None:
                    raise Skip("SponsorBlock is unreachable right now (%s)" % e) from None
                log.warning("SponsorBlock unreachable (%s); using the segments from the last check", e)
        plan = planner.make_plan(segs, dur, s, media.Keyframes(path, info))
        now = time.time()
        db.upsert(vid, segments=segs, plan=plan, duration=dur, checked_at=now, sb_seconds=plan["sb_seconds"],
                  cut_seconds=plan["cut_seconds"])
        if not plan["cuts"]:
            db.upsert(vid, state="clean", detail=None)
            raise Skip("nothing to cut")
        if plan["suspicious"] and not force:
            db.upsert(vid, state="suspicious", detail="would remove %.0f%% of the video (limit %.0f%%)" % (
                plan["fraction"] * 100, s["max_removed_fraction"] * 100))
            raise Skip("would remove %.0f%% of the video" % (plan["fraction"] * 100))
        free = shutil.disk_usage(config.ROOT).free
        if free < st.st_size * 1.05 + 5e9:
            raise Skip("not enough free space in the library (%.1f GB free)" % (free / 1e9))
        kept, _ = originals_bytes()
        if not manual and s["keep_originals_days"] > 0 and kept + st.st_size > s["max_originals_gb"] * 1e9:
            raise Skip("originals cap reached (%.0f GB kept for undo); waiting for them to expire" % (kept / 1e9))

        work = os.path.join(config.WORK, vid)
        _ensure_dir(config.WORK)
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work)
        out = os.path.join(work, os.path.basename(path))
        try:
            step("cutting")
            src_chaps = media.chapters(info)
            new_len = sum(p["end"] - p["start"] for p in plan["pieces"])
            chaps = chapters.remap(src_chaps, plan["pieces"], new_len)
            t0 = time.time()
            try:
                warnings = cutter.cut(path, info, plan, out, work, chaps, s["io_limit_mb_s"])
            except media.MediaError as e:
                if _stat(path) is None:
                    raise Skip("the file disappeared during the cut") from None
                raise TrimError(str(e)) from None
            wall = time.time() - t0
            step("verifying")
            rep, out_info = verify.verify(path, info, out, plan)
            rep.update(cut_wall_s=round(wall, 1), ffmpeg_warnings=warnings[:20], chapters_before=len(src_chaps),
                       chapters_after=len(chaps), plan_cut_seconds=plan["cut_seconds"], sb_seconds=plan["sb_seconds"])
            if not rep["ok"]:
                raise TrimError("verification failed: " + "; ".join(rep["problems"]))

            step("captions")
            subs, cap_rep = [], []
            for sp in library.sidecar_subtitles(path):
                dst = os.path.join(work, os.path.basename(sp))
                with open(sp, encoding="utf-8-sig", errors="replace") as f:
                    orig_cues = captions.parse(f.read())
                n0, n1 = captions.shift_file(sp, dst, plan["pieces"])
                with open(dst, encoding="utf-8") as f:
                    new_cues = captions.parse(f.read())
                cap_rep.append({"file": os.path.basename(sp), "cues_before": n0, "cues_after": n1,
                                "spot_check": _spot_check(orig_cues, new_cues, plan)})
                subs.append((sp, dst))
            rep["captions"] = cap_rep

            step("swapping")
            st2 = _stat(path)
            if st2 is None:
                raise Skip("the file disappeared during the trim")
            if (st2.st_ino, st2.st_size, st2.st_mtime) != (st.st_ino, st.st_size, st.st_mtime):
                raise Skip("the file changed while it was being trimmed")
            backup, srt_backups = _swap(path, st.st_ino, out, subs)
        except BaseException:
            shutil.rmtree(work, ignore_errors=True)
            raise

        after = os.stat(path)
        now = time.time()
        keep_days = float(s["keep_originals_days"])
        db.upsert(vid, state="trimmed", detail=None, trimmed_at=now, before_duration=dur,
                  after_duration=rep["duration_after"], before_size=st.st_size, after_size=after.st_size,
                  after_inode=after.st_ino, inode=after.st_ino, size=after.st_size, mtime=after.st_mtime,
                  backup_path=backup, backup_srts=srt_backups, backup_expires=now + keep_days * 86400, report=rep,
                  attempts=(row["attempts"] or 0) + 1, swapped_at=now)
        db.event("info", "trimmed %.1f s (%s) from %s - %s" % (dur - rep["duration_after"], ", ".join(
            sorted({c for x in plan["cuts"] for c in x["categories"]})), row["channel"], row["title"]), vid)
        log.info("TRIMMED %s | %s | %.1f s -> %.1f s (-%.1f s)", row["channel"], row["title"], dur,
                 rep["duration_after"], dur - rep["duration_after"])
        if keep_days <= 0:
            drop_backup(db.video(vid), "originals are not kept (keep_originals_days = 0)")

        # The new file's duration was already confirmed locally with ffprobe (verify.verify, before the swap).
        rep["plex"] = {"message": "Tubarr asks Plex to rescan this folder (Trimarr has no Plex access)"}
        db.upsert(vid, report=rep)
        shutil.rmtree(work, ignore_errors=True)
        return rep


def fail(vid, err):
    row = db.video(vid) or {}
    db.upsert(vid, state="failed", detail=str(err)[:500], attempts=(row.get("attempts") or 0) + 1)
    db.event("error", "trim failed: %s" % str(err)[:400], vid)
    log.error("FAILED %s: %s", vid, err)


# ----------------------------------------------------------------------------------------------- undo / approve
def undo(vid):
    """Put the original back (video and captions), in place. The video is then never trimmed automatically again."""
    with job_lock():
        row = db.video(vid)
        if not row or row["state"] != "trimmed":
            raise Skip("this video isn't trimmed")
        b = row["backup_path"]
        if not b or not os.path.exists(b):
            raise Skip("the original is no longer kept (it expired or was approved)")
        path = row["path"]
        st = _stat(path)
        if st is None:
            raise Skip("the video is no longer in the library")
        if st.st_ino != row["after_inode"]:
            raise Skip("the file changed since it was trimmed; not putting an older copy over it")
        os.replace(b, path)
        for sp, sb in row["backup_srts"] or []:
            if os.path.exists(sb):
                os.replace(sb, sp)
        _prune_empty(os.path.dirname(b))
        st = os.stat(path)
        db.upsert(vid, state="undone", detail="restored by hand; not trimmed automatically again",
                  backup_path=None, backup_srts=None, backup_expires=None, after_inode=None, size=st.st_size,
                  mtime=st.st_mtime, inode=st.st_ino, swapped_at=time.time())
        db.event("info", "undo: original restored for %s - %s" % (row["channel"], row["title"]), vid)
        log.info("UNDO %s | %s", row["channel"], row["title"])
    return {"restored": path, "plex": "Tubarr asks Plex to rescan this folder"}


def _prune_empty(d):
    while d.startswith(config.ORIGINALS + os.sep):
        try:
            if [x for x in os.listdir(d) if x != ".plexignore"]:
                return
            shutil.rmtree(d)
        except OSError:
            return
        d = os.path.dirname(d)


def drop_backup(row, reason):
    """Delete a kept original (video + captions) for good."""
    if not row or not row.get("backup_path"):
        return False
    paths = [row["backup_path"]] + [b for _, b in (row.get("backup_srts") or [])]
    for p in paths:
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
    _prune_empty(os.path.dirname(row["backup_path"]))
    db.upsert(row["video_id"], backup_path=None, backup_srts=None, backup_expires=None)
    db.event("info", "original deleted (%s): %s - %s" % (reason, row.get("channel"), row.get("title")), row["video_id"])
    log.info("ORIGINAL DELETED (%s) | %s | %s", reason, row.get("channel"), row.get("title"))
    return True


def approve(vid, block=True):
    row = db.video(vid)
    if not row or not row["backup_path"]:
        raise Skip("no original is kept for this video")
    try:
        with job_lock(block=block):
            return drop_backup(db.video(vid), "approved")
    except Skip:
        if not block:
            raise Skip("a trim is running right now; try again in a minute") from None
        raise


# ----------------------------------------------------------------------------------------------- housekeeping
def housekeeping():
    now = time.time()
    for row in db.query("SELECT * FROM videos WHERE backup_path IS NOT NULL AND backup_expires IS NOT NULL "
                        "AND backup_expires < ?", (now,)):
        drop_backup(row, "kept %g days" % settings.load()["keep_originals_days"])
    try:
        with job_lock(block=False):
            if os.path.isdir(config.WORK):                 # leftovers of an interrupted run
                for d in os.listdir(config.WORK):
                    if d != ".plexignore":
                        shutil.rmtree(os.path.join(config.WORK, d), ignore_errors=True)
            refs = set()
            for r in db.query("SELECT backup_path, backup_srts FROM videos WHERE backup_path IS NOT NULL"):
                refs.add(r["backup_path"])
                refs.update(b for _, b in (r["backup_srts"] or []))
            for dp, dns, fns in os.walk(config.ORIGINALS):
                for f in fns:
                    p = os.path.join(dp, f)
                    if f == ".plexignore" or p in refs:
                        continue
                    lib = os.path.join(config.ROOT, os.path.relpath(p, config.ORIGINALS))
                    a, b = _stat(lib), _stat(p)
                    if a and b and a.st_ino == b.st_ino:       # an interrupted swap: the library still has this file
                        os.remove(p)
                    else:
                        log.warning("unreferenced file in .trim-originals (left alone): %s", p)
    except Skip:
        pass
