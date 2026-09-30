"""JSON shapes shared by the API and the CLI (see API.md), and the dry-run report."""
import os
import time

from . import config, db, settings
from .trimmer import iso, originals_bytes

WITH_CUTS = ("cuttable", "suspicious", "trimmed", "failed", "waiting", "undone")


def _cuts(plan):
    return [{"start": round(c["start"], 3), "end": round(c["end"], 3), "seconds": round(c["end"] - c["start"], 3),
             "categories": c.get("categories") or [], "sponsorblock": c.get("sb") or []}
            for c in (plan or {}).get("cuts") or []]


def video(r, s, detail=False):
    plan = r.get("plan") or {}
    own, eff = settings.channel_setting(s, r["channel_id"])
    trimmed = None
    if r["state"] == "trimmed":
        trimmed = {"at": iso(r["trimmed_at"]),
                   "removed_seconds": round((r["before_duration"] or 0) - (r["after_duration"] or 0), 3),
                   "duration_before": r["before_duration"], "duration_after": r["after_duration"],
                   "size_before": r["before_size"], "size_after": r["after_size"],
                   "original_kept_until": iso(r["backup_expires"]) if r["backup_path"] else None,
                   "undo_available": bool(r["backup_path"] and os.path.exists(r["backup_path"]))}
    d = {"id": r["video_id"], "channel_id": r["channel_id"], "channel": r["channel"], "title": r["title"],
         "upload_date": r["upload_date"],
         "file": os.path.relpath(r["path"], config.ROOT) if r["path"] else None,
         "state": r["state"], "detail": r["detail"],
         "duration_seconds": r["before_duration"] if r["state"] == "trimmed" else r["duration"],
         "size_bytes": r["size"], "checked_at": iso(r["checked_at"]),
         "sponsorblock_seconds": plan.get("sb_seconds"), "cut_seconds": plan.get("cut_seconds"),
         "extra_seconds": plan.get("extra_seconds"), "removed_fraction": plan.get("fraction"),
         "cuts": _cuts(plan), "trimmed": trimmed, "auto_trim": eff,
         "swapped_at": iso(r.get("swapped_at")), "swapped_ts": r.get("swapped_at")}
    if detail:
        d.update(segments=r["segments"], ignored=plan.get("ignored") or [], plan_exact=plan.get("exact"),
                 report=r["report"])
    return d


def status_counts():
    out = {k: 0 for k in ("new", "waiting", "clean", "cuttable", "suspicious", "trimmed", "failed", "undone", "gone")}
    for r in db.query("SELECT state, COUNT(*) AS n FROM videos GROUP BY state"):
        out[r["state"]] = r["n"]
    return out


def report(channel=None):
    """What Trimarr WOULD cut (the dry run) and what it has cut, per video and in total."""
    s = settings.load()
    rows = db.query("SELECT * FROM videos WHERE state != 'gone' ORDER BY channel, upload_date DESC")
    if channel:
        rows = [r for r in rows if r["channel_id"] == channel or r["folder"] == channel]
    tot = {"videos": len(rows), "library_seconds": 0.0, "checked": 0, "not_checked": 0,
           "would_trim": {"videos": 0, "seconds": 0.0, "sponsorblock_seconds": 0.0, "extra_seconds": 0.0},
           "waiting": {"videos": 0, "seconds": 0.0}, "suspicious": {"videos": 0, "seconds": 0.0},
           "trimmed": {"videos": 0, "seconds": 0.0}, "failed": 0, "clean": 0}
    by_cat, by_ch, vids = {}, {}, []
    for r in rows:
        plan = r["plan"] or {}
        tot["library_seconds"] += (r["before_duration"] if r["state"] == "trimmed" else r["duration"]) or 0
        if r["checked_at"]:
            tot["checked"] += 1
        else:
            tot["not_checked"] += 1
        cut = plan.get("cut_seconds") or 0.0
        ch = by_ch.setdefault(r["channel_id"], {"channel_id": r["channel_id"], "channel": r["channel"],
                                                "auto_trim": settings.channel_setting(s, r["channel_id"])[1],
                                                "videos": 0, "would_trim_videos": 0, "would_trim_seconds": 0.0,
                                                "trimmed_videos": 0, "trimmed_seconds": 0.0})
        ch["videos"] += 1
        st = r["state"]
        if st == "cuttable":
            w = tot["would_trim"]
            w["videos"] += 1
            w["seconds"] += cut
            w["sponsorblock_seconds"] += plan.get("sb_seconds") or 0
            w["extra_seconds"] += plan.get("extra_seconds") or 0
            ch["would_trim_videos"] += 1
            ch["would_trim_seconds"] += cut
        elif st == "waiting" and plan.get("cuts"):
            tot["waiting"]["videos"] += 1
            tot["waiting"]["seconds"] += cut
        elif st == "suspicious":
            tot["suspicious"]["videos"] += 1
            tot["suspicious"]["seconds"] += cut
        elif st == "trimmed":
            rem = (r["before_duration"] or 0) - (r["after_duration"] or 0)
            tot["trimmed"]["videos"] += 1
            tot["trimmed"]["seconds"] += rem
            ch["trimmed_videos"] += 1
            ch["trimmed_seconds"] += rem
        elif st == "failed":
            tot["failed"] += 1
        elif st == "clean":
            tot["clean"] += 1
        if st in ("cuttable", "trimmed", "suspicious") or (st == "waiting" and plan.get("cuts")):
            for sg in plan.get("segments") or []:
                by_cat[sg["category"]] = by_cat.get(sg["category"], 0.0) + (sg["end"] - sg["start"])
            vids.append(video(r, s))
    for k in ("would_trim", "waiting", "suspicious", "trimmed"):
        for kk in tot[k]:
            if kk != "videos":
                tot[k][kk] = round(tot[k][kk], 1)
    tot["library_seconds"] = round(tot["library_seconds"], 1)
    chans = sorted(by_ch.values(), key=lambda c: -(c["would_trim_seconds"] + c["trimmed_seconds"]))
    for c in chans:
        c["would_trim_seconds"], c["trimmed_seconds"] = round(c["would_trim_seconds"], 1), round(c["trimmed_seconds"], 1)
    kb, kn = originals_bytes()
    return {"generated_at": iso(time.time()), "enabled": s["enabled"], "categories": s["categories"],
            "totals": tot, "by_category_seconds": {k: round(v, 1) for k, v in sorted(by_cat.items())},
            "by_channel": chans, "originals": {"count": kn, "bytes": kb}, "videos": vids}
