"""Command line.  python -m trimarr <command>

  serve                          the service: API + daily passes (the container's default command)
  dry-run [--channel X] [--json] what it WOULD cut, per video and in total (also: --dry-run)
  trim <video|file> [--force] [--plan]   trim one now (--plan: only show the exact cut plan)
  undo <video|file>              put the original back (video + captions), in place
  approve <video|file> | --all   delete kept original(s) now instead of waiting for the expiry
  status                         counters, settings, kept originals
  settings [key=value ...]       show or change settings (values are JSON: enabled=true, categories='["sponsor"]')
  channel <channel id|folder> on|off|default
  list [--state cuttable,...]    videos and their state

<video|file> = a YouTube video ID, or the file's path (/youtube/..., the same path as Plex sees it, or
relative to the library).
"""
import argparse
import json
import logging
import logging.handlers
import os
import sys
import time

from . import config, db, library, settings, sponsorblock, trimmer, views


def setup_logging(verbose=False):
    os.makedirs(config.LOG_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    try:
        fh = logging.handlers.RotatingFileHandler(os.path.join(config.LOG_DIR, "trimarr.log"), maxBytes=5_000_000,
                                                  backupCount=3)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        pass
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def find_video(arg, sync=True):
    """Row for a video ID or a path (scans the library once if needed)."""
    r = db.video(arg)
    if r:
        return r
    path = library.resolve(arg)
    if path:
        r = db.one("SELECT * FROM videos WHERE path=?", (path,))
        if r:
            return r
        if sync:
            trimmer.sync_library()
            return find_video(arg, sync=False)
    elif sync:
        trimmer.sync_library()
        return find_video(arg, sync=False)
    raise SystemExit("not found in the library: %s" % arg)


def mmss(sec):
    sec = int(round(sec or 0))
    return "%d:%02d:%02d" % (sec // 3600, sec // 60 % 60, sec % 60) if sec >= 3600 else "%d:%02d" % (sec // 60, sec % 60)


def check_all(include_young=True, exact=False):
    s = settings.load()
    trimmer.sync_library()
    due = trimmer.due_for_check(s, include_young=include_young)
    print("checking %d videos against SponsorBlock ..." % len(due), flush=True)
    for i, r in enumerate(due, 1):
        try:
            trimmer.check(r["video_id"], s, exact=exact)
        except sponsorblock.SponsorBlockError as e:
            print("  SponsorBlock problem, stopping checks: %s" % e)
            break
        except Exception as e:
            print("  %s: check failed: %s" % (r["video_id"], e))
        if i % 25 == 0:
            print("  %d/%d" % (i, len(due)), flush=True)


def print_report(rep):
    t = rep["totals"]
    s = settings.load()
    print()
    print("TRIMARR DRY RUN  %s  automatic trimming is %s  categories: %s" % (
        time.strftime("%Y-%m-%d %H:%M %Z"), "ON" if s["enabled"] else "OFF", ", ".join(s["categories"])))
    print("rules: segments >= %gs, never more than %.0f%% of a video, uploads >= %gh old, files >= %gmin old, cuts "
          "widened to keyframes" % (s["min_segment_seconds"], s["max_removed_fraction"] * 100,
                                    s["min_upload_age_hours"], s["min_file_age_minutes"]))
    print()
    print("%-24s %-10s %8s %7s %7s %6s %5s  %-10s %s" % ("CHANNEL", "UPLOADED", "LENGTH", "SB s", "CUT s", "+KEY",
                                                        "%", "STATE", "TITLE"))
    for v in rep["videos"]:
        rem = v["trimmed"]["removed_seconds"] if v["trimmed"] else v["cut_seconds"]
        print("%-24s %-10s %8s %7.1f %7.1f %6.1f %5.1f  %-10s %s" % (
            (v["channel"] or "")[:24], v["upload_date"] or "", mmss(v["duration_seconds"]),
            v["sponsorblock_seconds"] or 0, rem or 0, v["extra_seconds"] or 0, (v["removed_fraction"] or 0) * 100,
            v["state"], (v["title"] or "")[:60]))
    w = t["would_trim"]
    print()
    print("LIBRARY   %d videos (%.1f h); %d checked, %d not checked yet (file under 30 min old or not reached)" % (
        t["videos"], t["library_seconds"] / 3600, t["checked"], t["not_checked"]))
    print("WOULD CUT %d videos: %s (%.0f s) = SponsorBlock %.0f s + %.0f s widened to keyframes" % (
        w["videos"], mmss(w["seconds"]), w["seconds"], w["sponsorblock_seconds"], w["extra_seconds"]))
    print("          (+KEY is 0 for plans estimated from SponsorBlock times; use --exact for keyframe-exact numbers."
          " Measured so far: about 2 s extra per cut edge.)")
    print("WAITING   %d videos under 24 h old with %.0f s to cut later" % (t["waiting"]["videos"], t["waiting"]["seconds"]))
    print("SUSPICIOUS %d (over the %.0f%% limit, left alone)   CLEAN %d   FAILED %d" % (
        t["suspicious"]["videos"], s["max_removed_fraction"] * 100, t["clean"], t["failed"]))
    print("TRIMMED   %d videos, %s removed" % (t["trimmed"]["videos"], mmss(t["trimmed"]["seconds"])))
    if rep["by_category_seconds"]:
        print("BY CATEGORY (SponsorBlock seconds): " + ", ".join(
            "%s %.0f s" % kv for kv in rep["by_category_seconds"].items()))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--dry-run":
        argv[0] = "dry-run"
    ap = argparse.ArgumentParser(prog="trimarr", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    p = sub.add_parser("dry-run")
    p.add_argument("--channel")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-check", action="store_true", help="report from stored data only")
    p.add_argument("--exact", action="store_true", help="keyframe-exact plans (reads a few MB per cut; slower)")
    p = sub.add_parser("trim")
    p.add_argument("video")
    p.add_argument("--force", action="store_true", help="trim even if over the 40%% limit")
    p.add_argument("--plan", "--dry-run", action="store_true", help="only print the exact cut plan")
    p = sub.add_parser("undo")
    p.add_argument("video")
    p = sub.add_parser("approve")
    p.add_argument("video", nargs="?")
    p.add_argument("--all", action="store_true")
    sub.add_parser("status")
    p = sub.add_parser("settings")
    p.add_argument("pairs", nargs="*")
    p = sub.add_parser("channel")
    p.add_argument("channel")
    p.add_argument("value", choices=["on", "off", "default"])
    p = sub.add_parser("list")
    p.add_argument("--state")
    a = ap.parse_args(argv)
    setup_logging(a.verbose)

    if a.cmd == "serve":
        from .service import Service
        Service().run_forever()
        return 0

    if a.cmd == "dry-run":
        if not a.no_check:
            check_all(include_young=True, exact=a.exact)
        rep = views.report(channel=a.channel)
        if a.json:
            print(json.dumps(rep, indent=2))
        else:
            print_report(rep)
        return 0

    if a.cmd == "trim":
        r = find_video(a.video)
        if a.plan:
            from . import media, planner
            s = settings.load()
            info = media.probe(r["path"])
            segs = sponsorblock.stamp_first_seen(sponsorblock.fetch(r["video_id"]), r["segments"])
            plan = planner.make_plan(segs, media.duration(info), s, media.Keyframes(r["path"], info))
            print(json.dumps({k: plan[k] for k in ("duration", "segments", "ignored", "cuts", "pieces", "sb_seconds",
                                                   "cut_seconds", "extra_seconds", "fraction", "suspicious")}, indent=2))
            return 0
        try:
            rep = trimmer.trim(r["video_id"], manual=True, force=a.force,
                               progress=lambda st: print("  ..." + st, flush=True))
        except trimmer.Skip as e:
            print("not trimmed: %s" % e)
            return 2
        except trimmer.TrimError as e:
            trimmer.fail(r["video_id"], e)
            print("FAILED (original untouched): %s" % e)
            return 1
        print(json.dumps(rep, indent=2))
        return 0

    if a.cmd == "undo":
        r = find_video(a.video)
        try:
            res = trimmer.undo(r["video_id"])
        except trimmer.Skip as e:
            print("not undone: %s" % e)
            return 2
        print("restored: %s\n(Tubarr asks Plex to rescan it within a minute or two)" % res["restored"])
        return 0

    if a.cmd == "approve":
        rows = db.query("SELECT * FROM videos WHERE backup_path IS NOT NULL") if a.all else [find_video(a.video)]
        for r in rows:
            try:
                trimmer.approve(r["video_id"])
                print("original deleted: %s" % r["path"])
            except trimmer.Skip as e:
                print("%s: %s" % (r["video_id"], e))
        return 0

    if a.cmd == "status":
        s = settings.load()
        kb, kn = trimmer.originals_bytes()
        print(json.dumps({"settings": s, "counts": views.status_counts(),
                          "originals": {"count": kn, "gb": round(kb / 1e9, 2)}}, indent=2))
        return 0

    if a.cmd == "settings":
        if a.pairs:
            patch = {}
            for kv in a.pairs:
                k, _, v = kv.partition("=")
                try:
                    patch[k] = json.loads(v)
                except ValueError:
                    patch[k] = v
            try:
                settings.update(patch)
            except settings.SettingsError as e:
                raise SystemExit(str(e))
        print(json.dumps(settings.load(), indent=2))
        return 0

    if a.cmd == "channel":
        cid = a.channel
        row = db.one("SELECT channel_id FROM videos WHERE channel_id=? OR folder=? OR channel=? LIMIT 1", (cid, cid, cid))
        cid = row["channel_id"] if row else cid
        settings.set_channel(cid, {"on": True, "off": False, "default": None}[a.value])
        print("%s: %s -> automatic trimming %s" % (cid, a.value, "ON" if settings.channel_setting(settings.load(), cid)[1]
                                                    else "OFF"))
        return 0

    if a.cmd == "list":
        sql, args = "SELECT * FROM videos WHERE state != 'gone'", []
        if a.state:
            st = a.state.split(",")
            sql += " AND state IN (%s)" % ",".join("?" * len(st))
            args = st
        for r in db.query(sql + " ORDER BY channel, upload_date DESC", args):
            print("%-11s %-10s %-24s %6.1f s  %s" % (r["video_id"], r["state"], (r["channel"] or "")[:24],
                                                    r["cut_seconds"] or 0, r["title"]))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
