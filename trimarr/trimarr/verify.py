"""Checks a trimmed file BEFORE it may replace the original. Every check must pass, or the original stays.

1. ffprobe reads it.
2. new duration = old duration - removed, within 1.5 s.
3. the same video/audio/subtitle streams (codec, size, sample rate, channels).
4. it decodes cleanly: the first 5 s, the last 5 s, and 5 s on both sides of every join. If a window shows decoder
   errors, the same content in the ORIGINAL is decoded too: errors the original has as well are not the cut's fault.
5. audio alignment on both sides of every join: the trimmed file's audio must match the original's at the mapped
   time (lag 0 +/- 40 ms). This proves the timeline mapping the captions and chapters use.
6. size sanity (not bigger than the original, not absurdly small).
"""
import os

from . import media

DUR_TOLERANCE = 1.5
WINDOW = 5.0
LAG_OK_MS = 40.0


def windows(plan):
    """Per kept piece: output and source ranges."""
    out, o = [], 0.0
    for p in plan["pieces"]:
        n = p["end"] - p["start"]
        out.append({"out": (o, o + n), "src": (p["start"], p["end"])})
        o += n
    return out


def verify(src, src_info, out_path, plan):
    rep = {"problems": [], "notes": []}
    prob, notes = rep["problems"], rep["notes"]
    try:
        info = media.probe(out_path)
    except media.MediaError as e:
        prob.append("ffprobe can't read the trimmed file: %s" % e)
        rep["ok"] = False
        return rep, None
    dur, old = media.duration(info), media.duration(src_info)
    expected = sum(p["end"] - p["start"] for p in plan["pieces"])
    rep.update(duration_before=round(old, 3), duration_after=round(dur, 3), expected=round(expected, 3),
               removed=round(old - dur, 3))
    if abs(dur - expected) > DUR_TOLERANCE:
        prob.append("duration is %.2f s, expected %.2f s" % (dur, expected))
    a, b = media.av_signature(src_info), media.av_signature(info)
    if a != b:
        prob.append("streams differ: before %s, after %s" % (a, b))
    size_before = int(src_info["format"].get("size") or os.path.getsize(src))
    size_after = os.path.getsize(out_path)
    rep.update(size_before=size_before, size_after=size_after)
    kept_frac = expected / old if old else 1.0
    if size_after > size_before * 1.02 or size_after < size_before * kept_frac * 0.5:
        prob.append("size %d bytes is implausible (original %d, %.0f%% kept)" % (size_after, size_before, kept_frac * 100))

    # 4 + 5: decode and audio checks, windows always inside one kept piece
    ws = windows(plan)
    decode, sync = [], []
    for i, w in enumerate(ws):
        (o0, o1), (s0, s1) = w["out"], w["src"]
        n = min(WINDOW, o1 - o0)
        sides = [("start" if i == 0 else "after join %d" % i, o0, s0)]
        sides.append(("end" if i == len(ws) - 1 else "before join %d" % (i + 1), o1 - n, s1 - n))
        for name, ot, st in sides:
            errs = media.decode_errors(out_path, ot, n)
            if errs:
                orig = media.decode_errors(src, st, n)
                if orig and set(orig) >= set(errs):
                    notes.append("%s: decoder messages also present in the original (%s)" % (name, errs[0][:120]))
                    errs = []
            decode.append({"where": name, "at": round(ot, 3), "ok": not errs, "errors": errs[:3]})
            if errs:
                prob.append("decode errors at %s (%.1f s): %s" % (name, ot, errs[0][:200]))
            if n >= 2.0:                                           # audio alignment, 0.3 s away from the join
                a0 = ot + 0.3 if name.startswith(("start", "after")) else ot
                ln = n - 0.3
                res = media.align(media.pcm(out_path, a0, ln), media.pcm(src, st + (a0 - ot), ln))
                res.update(where=name, at=round(a0, 3))
                sync.append(res)
                if res["result"] == "ok" and (abs(res["lag_ms"]) > LAG_OK_MS or res["corr"] < 0.6):
                    prob.append("audio at %s doesn't match the original (lag %.0f ms, corr %.2f)"
                                % (name, res["lag_ms"], res["corr"]))
    rep["decode_checks"], rep["sync_checks"] = decode, sync
    if not any(x["result"] == "ok" for x in sync):
        notes.append("audio alignment not measurable (silence)")
    rep["ok"] = not prob
    return rep, info
