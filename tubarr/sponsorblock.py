"""SponsorBlock: fetch segments (privacy-preserving hash-prefix API) and turn them into a cut plan.
sponsor / selfpromo / interaction are removed; intro / outro are kept and become chapters (Skip buttons)."""
import hashlib
import json
import logging

import requests

from . import config

log = logging.getLogger("tubarr.sb")


def fetch(video_id):
    """All segments for this video (any category/action), or [] if none. Raises on network errors."""
    prefix = hashlib.sha256(video_id.encode()).hexdigest()[:4]
    r = requests.get("%s/api/skipSegments/%s" % (config.SPONSORBLOCK_API, prefix),
                     params={"categories": json.dumps(list(config.SB_ALL)),
                             "actionTypes": json.dumps(["skip", "poi", "chapter", "mute"])},
                     headers={"User-Agent": config.USER_AGENT}, timeout=20)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    for v in r.json():
        if v.get("videoID") == video_id:
            return v.get("segments") or []
    return []


def _merge(ranges, gap=0.25):
    out = []
    for s, e in sorted(ranges):
        if out and s <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [tuple(x) for x in out]


def plan(segments, duration, min_cut=1.0, edge=1.5, max_skew=2.0):
    """Returns dict(cuts=[(s,e)], keep=[(s,e)], marks=[{'category','start','end'}], ignored=[...]).
    Segments whose recorded videoDuration differs from ours by more than `max_skew` seconds are ignored
    (the video was edited after they were submitted, so their times may be wrong)."""
    cuts, marks, ignored = [], [], []
    for sg in segments:
        s, e = (float(x) for x in sg["segment"])
        vd = float(sg.get("videoDuration") or 0)
        cat, act = sg.get("category"), sg.get("actionType")
        if vd and abs(vd - duration) > max_skew:
            ignored.append({"category": cat, "segment": [s, e], "why": "videoDuration %.1f vs %.1f" % (vd, duration)})
            continue
        s, e = max(0.0, s), min(duration, e)
        if cat in config.SB_CUT and act == "skip":
            if e - s >= min_cut:
                cuts.append((s, e))
            else:
                ignored.append({"category": cat, "segment": [s, e], "why": "shorter than %.1fs" % min_cut})
        elif cat in config.SB_MARK and act == "skip" and e - s >= 1.0:
            marks.append({"category": cat, "start": s, "end": e})
    cuts = _merge(cuts)
    # snap cuts that nearly touch the start/end of the video to it
    cuts = [(0.0 if s < edge else s, duration if duration - e < edge else e) for s, e in cuts]
    keep, t = [], 0.0
    for s, e in cuts:
        if s - t > 0.05:
            keep.append((t, s))
        t = max(t, e)
    if duration - t > 0.05:
        keep.append((t, duration))
    # an intro/outro that is entirely inside a cut disappears with it
    marks = [m for m in marks if not any(s <= m["start"] and m["end"] <= e for s, e in cuts)]
    return {"cuts": cuts, "keep": keep, "marks": marks, "ignored": ignored,
            "removed_seconds": round(sum(e - s for s, e in cuts), 3)}
