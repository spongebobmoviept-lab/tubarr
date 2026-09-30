"""The cut: ONE ffmpeg stream-copy pass (no re-encode) that joins the kept pieces of the original.

The pieces are listed in an ffconcat file that points at the original several times:
    file '<original>'        one entry per kept piece
    inpoint  <pts of the piece's first keyframe, rounded UP to the microsecond>
    outpoint <DTS of the keyframe where the next cut starts, rounded DOWN>
    duration <exact length of the piece, from presentation timestamps>
The inpoint is a real keyframe (H.264: IDR), so every piece starts with a clean frame. The outpoint uses the
keyframe's decode timestamp, so with B-frames the first frame of the ad is never pulled in. The explicit duration
keeps the output timeline exact, which is what the caption and chapter shifts rely on.
All streams of a piece get the same offset, so audio/video sync can't drift across joins.
"""
import math
import os
from fractions import Fraction

from . import media

META_SKIP = {"encoder", "major_brand", "minor_version", "compatible_brands", "duration"}


def _us(x, how):
    v = x * 1000000
    n = math.ceil(v) if how == "up" else math.floor(v) if how == "down" else round(v)
    sign = "-" if n < 0 else ""
    n = abs(int(n))
    return "%s%d.%06d" % (sign, n // 1000000, n % 1000000)


def _quote(path):
    return "'" + path.replace("'", "'\\''") + "'"


def _esc(v):
    v = str(v)
    for ch in ("\\", "=", ";", "#", "\n"):
        v = v.replace(ch, "\\" + ch)
    return v


def concat_list(src, info, plan):
    tb = Fraction(plan["tb"])
    file_start = Fraction(media.start_time(info)).limit_denominator(1000000)
    lines = ["ffconcat version 1.0"]
    for p in plan["pieces"]:
        lines.append("file " + _quote(src))
        begin = Fraction(p["start_pts"]) * tb if p["start_pts"] is not None else None
        if begin is not None and begin > 0:
            lines.append("inpoint " + _us(begin, "up"))
        if p["end_pts"] is not None:
            end = Fraction(p["end_pts"]) * tb
            lines.append("outpoint " + _us(Fraction(p["end_dts"]) * tb, "down"))
            lines.append("duration " + _us(end - (begin if begin is not None else file_start), "nearest"))
    return "\n".join(lines) + "\n"


def write_ffmetadata(path, tags, chapters):
    """Global tags of the original plus the remapped chapters [(start, end, title)] in output seconds."""
    lines = [";FFMETADATA1"]
    for k, v in tags.items():
        if v in (None, "") or k.lower() in META_SKIP or k.upper().startswith("DURATION"):
            continue
        lines.append("%s=%s" % (_esc(k), _esc(v)))
    for s, e, t in chapters:
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", "START=%d" % round(s * 1000), "END=%d" % round(e * 1000),
                  "title=%s" % _esc(t)]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def cut(src, info, plan, out_path, workdir, chapters, io_limit_mb_s):
    """Writes the trimmed file to out_path. Returns ffmpeg's warnings (a list of lines)."""
    list_path = os.path.join(workdir, "pieces.ffconcat")
    meta_path = os.path.join(workdir, "meta.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        f.write(concat_list(src, info, plan))
    write_ffmetadata(meta_path, (info["format"].get("tags") or {}), chapters)
    size = int(info["format"].get("size") or os.path.getsize(src))
    dur = max(1.0, media.duration(info))
    readrate = max(2.0, (float(io_limit_mb_s) * 1e6) / (size / dur))      # x realtime that equals the MB/s cap
    mp4 = out_path.lower().endswith(".mp4")
    # -copyts: keep the concat timeline exactly as built (starts at 0). Without it ffmpeg re-bases on the earliest
    # packet, and an MP4's AAC priming packet (-1024 samples) then shifts the video 23 ms against the audio.
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "warning", "-y", "-copyts",
           "-readrate", "%.2f" % readrate, "-f", "concat", "-safe", "0", "-i", list_path,
           "-f", "ffmetadata", "-i", meta_path]
    attach = media.has_attachments(info)
    if attach:
        cmd += ["-i", src]
    cmd += ["-map", "0:V", "-map", "0:a", "-map", "0:s?"]
    if attach:
        cmd += ["-map", "2:t"]
    cmd += ["-map_metadata", "1", "-map_chapters", "1", "-c", "copy"]
    cmd += (["-movflags", "+faststart", "-f", "mp4"] if mp4 else ["-f", "matroska"]) + [out_path]
    p = media.run(cmd, timeout=6 * 3600)
    return [ln.strip() for ln in p.stderr.splitlines() if ln.strip()]
