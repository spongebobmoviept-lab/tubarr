"""SponsorBlock segments -> a cut plan.

Rules:
  * only the configured categories (default sponsor, selfpromo), only actionType "skip";
  * only trusted segments: locked by a SponsorBlock VIP, or at least `min_votes` (default 1) net votes AND at least
    `min_segment_age_hours` (default 24) old (timeSubmitted when SponsorBlock sends it, else when Trimarr first saw
    the segment). Fresh, unvoted submissions are where wrong or malicious segments live;
  * segments shorter than 1 s are ignored; segments submitted for a different-length version of the video
    (SponsorBlock's videoDuration differs from ours by more than 2 s) are ignored, their times may be wrong;
  * more than 40% removed = suspicious, never trimmed automatically.

Keyframe rule (stream copy, no re-encode). Every removed range is widened OUTWARD to keyframes:
    the cut starts at the last keyframe at or before the ad start, and ends at the first keyframe at or after the ad
    end (H.264: the first IDR frame).
So no ad audio or video is left, and the extra content lost is less than one keyframe interval on each side
(YouTube: about 3-5 s apart, at most about 8 s; on average about half of that is lost per edge).
Housekeeping: ads less than 0.5 s apart are one cut; an ad within 1.5 s of the start/end runs to it; a kept piece
shorter than 1 s between two cuts is dropped too.
"""
import time

EDGE = 1.5          # seconds: an ad this close to the start/end is extended to it
MERGE_GAP = 0.5     # seconds: ads closer than this are one cut
MIN_KEEP = 1.0      # seconds: a kept piece shorter than this is removed with its neighbours
MAX_SKEW = 2.0      # seconds: SponsorBlock's recorded video length vs ours
MIN_VOTES_FLOOR = 1
MIN_AGE_FLOOR_H = 24.0
FIRST_SEEN_GRACE = 3600.0   # seconds


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def trust_problem(sg, s, now=None):
    """None if the segment may be used, else a short reason why not."""
    locked = _num(sg.get("locked")) == 1
    if locked:
        return None                          # locked = reviewed by a SponsorBlock VIP
    min_votes = max(MIN_VOTES_FLOOR, _num(s.get("min_votes")) or MIN_VOTES_FLOOR)
    votes = _num(sg.get("votes"))
    if votes is None or votes < min_votes:
        return "not locked and fewer than %g votes" % min_votes
    min_age = max(MIN_AGE_FLOOR_H, _num(s.get("min_segment_age_hours")) or MIN_AGE_FLOOR_H)
    ts, grace = _num(sg.get("timeSubmitted")), 0.0
    ts = ts / 1000.0 if ts and ts > 1e11 else ts         # SponsorBlock sends epoch milliseconds
    if not ts:                                           # fallback: when Trimarr first saw it. The daily re-check
        ts, grace = _num(sg.get("first_seen")), FIRST_SEEN_GRACE   # runs a few minutes early, hence the grace
    if not ts:
        return "submission time unknown"
    if (time.time() if now is None else now) - ts < min_age * 3600 - grace:
        return "submitted less than %g h ago" % min_age
    return None


def sponsor_ranges(segments, duration, s, now=None):
    """(used, ignored, merged) where merged = [[start, end, {categories}]] in source seconds."""
    cats = set(s["categories"])
    min_len = float(s["min_segment_seconds"])
    used, ignored, ranges = [], [], []
    for sg in segments or []:
        if sg.get("actionType", "skip") != "skip" or sg.get("category") not in cats:
            continue
        a, b = float(sg["start"]), float(sg["end"])
        vd = float(sg.get("videoDuration") or 0)
        rec = {"category": sg["category"], "start": round(a, 3), "end": round(b, 3)}
        why = trust_problem(sg, s, now)
        if why:
            ignored.append(dict(rec, why=why))
            continue
        if vd and abs(vd - duration) > MAX_SKEW:
            ignored.append(dict(rec, why="submitted for a %.0f s version of the video (this file is %.0f s)" % (vd, duration)))
            continue
        a, b = max(0.0, a), min(duration, b)
        if b - a < min_len:
            ignored.append(dict(rec, why="shorter than %g s" % min_len))
            continue
        used.append(rec)
        ranges.append([a, b, {sg["category"]}])
    ranges.sort(key=lambda r: r[0])
    merged = []
    for a, b, c in ranges:
        if merged and a <= merged[-1][1] + MERGE_GAP:
            merged[-1][1] = max(merged[-1][1], b)
            merged[-1][2] |= c
        else:
            merged.append([a, b, set(c)])
    return used, ignored, merged


def make_plan(segments, duration, s, kf=None, now=None):
    """kf: media.Keyframes for an exact (keyframe-snapped) plan; None for an estimate using SponsorBlock's times."""
    used, ignored, merged = sponsor_ranges(segments, duration, s, now)
    sb_seconds = sum(b - a for a, b, _ in merged)
    cuts = []
    for a, b, cats in merged:
        a0, b0 = (0.0 if a < EDGE else a), (duration if duration - b < EDGE else b)
        if kf is None:
            start, start_pts, start_dts = a0, None, None
            end, end_pts, end_dts = b0, None, None
        else:
            start, start_pts, start_dts = (0.0, None, None) if a0 <= 0 else kf.at_or_before(a0)
            nxt = None if b0 >= duration else kf.clean_start_at_or_after(b0)
            end, end_pts, end_dts = (duration, None, None) if nxt is None else nxt
        cuts.append({"start": start, "end": end, "start_pts": start_pts, "start_dts": start_dts, "end_pts": end_pts,
                     "end_dts": end_dts, "sb": [[round(a, 3), round(b, 3)]], "categories": set(cats)})
    cuts.sort(key=lambda c: c["start"])
    out = []
    for c in cuts:                                   # merge cuts that touch, or leave a sliver under MIN_KEEP
        if out and c["start"] - out[-1]["end"] < MIN_KEEP:
            p = out[-1]
            if c["end"] > p["end"]:
                p["end"], p["end_pts"], p["end_dts"] = c["end"], c["end_pts"], c["end_dts"]
            p["sb"] += c["sb"]
            p["categories"] |= c["categories"]
        else:
            out.append(c)
    if out and 0 < out[0]["start"] < MIN_KEEP:
        out[0].update(start=0.0, start_pts=None, start_dts=None)
    if out and 0 < duration - out[-1]["end"] < MIN_KEEP:
        out[-1].update(end=duration, end_pts=None, end_dts=None)

    pieces, pos, pos_pts = [], 0.0, None             # kept pieces in source time
    for c in out:
        if c["start"] - pos > 1e-6:
            pieces.append({"start": pos, "end": c["start"], "start_pts": pos_pts, "end_pts": c["start_pts"],
                           "end_dts": c["start_dts"]})
        pos, pos_pts = c["end"], c["end_pts"]
    if duration - pos > 1e-6:
        pieces.append({"start": pos, "end": duration, "start_pts": pos_pts, "end_pts": None, "end_dts": None})

    kept = sum(p["end"] - p["start"] for p in pieces)
    cut_seconds = max(0.0, duration - kept)
    frac = (max(cut_seconds, sb_seconds) / duration) if duration > 0 else 0.0
    for c in out:
        c["categories"] = sorted(c["categories"])
        c["start"], c["end"] = round(c["start"], 6), round(c["end"], 6)
    return {
        "exact": kf is not None,
        "tb": str(kf.tb) if kf is not None else None,
        "duration": round(duration, 3),
        "segments": used,
        "ignored": ignored,
        "cuts": out,
        "pieces": pieces,
        "sb_seconds": round(sb_seconds, 3),
        "cut_seconds": round(cut_seconds, 3),
        "extra_seconds": round(max(0.0, cut_seconds - sb_seconds), 3),
        "fraction": round(frac, 4),
        "suspicious": frac > float(s["max_removed_fraction"]) or not pieces,
    }


def to_output(t, pieces):
    """Source time -> output time. Inside a removed part it maps to where the content resumes."""
    out = 0.0
    for p in pieces:
        if t < p["start"]:
            return out
        if t <= p["end"]:
            return out + (t - p["start"])
        out += p["end"] - p["start"]
    return out
