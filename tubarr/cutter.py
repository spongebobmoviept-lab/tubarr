"""Frame-accurate cutting that keeps the original quality ("smart cut").

Inputs are the separate video-only and audio-only downloads. Every kept range is split into
    [head: re-encode] [middle: stream copy] [tail: re-encode]
at real random-access points (H.264: IDR frames only; VP9: keyframes). Only the partial GOPs at the cut points
(a few seconds) are re-encoded - libx264 (sps-id=1, so its parameter sets sit beside the original ones) for H.264,
libvpx-vp9 for VP9 - and everything else is the untouched YouTube bitstream. Audio is trimmed sample-accurately
(10 ms fades at the joins) and re-encoded once to AAC 192k. No cuts: plain remux, no re-encode at all.
The result is verified: duration, codecs, and a decode check starting at every piece's own keyframe.
"""
import bisect
import json
import logging
import os
import subprocess
from fractions import Fraction

log = logging.getLogger("tubarr.cut")


def sh(cmd, timeout=7200):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError("%s failed (%d): %s" % (os.path.basename(cmd[0]), p.returncode, p.stderr[-3000:]))
    return p.stdout, p.stderr


def probe(path):
    return json.loads(sh(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-show_chapters", "-of", "json", path])[0])


def _esc(s):
    s = str(s)
    for ch in ("\\", "=", ";", "#", "\n"):
        s = s.replace(ch, "\\" + ch)
    return s


def write_ffmetadata(path, tags, chapters):
    """tags: dict of global tags; chapters: [(start_s, end_s, title)] in OUTPUT time."""
    lines = [";FFMETADATA1"] + ["%s=%s" % (k, _esc(v)) for k, v in tags.items() if v not in (None, "")]
    for s, e, t in chapters:
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", "START=%d" % round(s * 1000), "END=%d" % round(e * 1000), "title=%s" % _esc(t)]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


class Source:
    """The video-only source file (plus its audio companion)."""

    def __init__(self, path, audio=None):
        self.path, self.audio = path, audio
        self.info = probe(path)
        self.v = next(s for s in self.info["streams"] if s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic"))
        self.codec = self.v["codec_name"]
        self.fps = Fraction(self.v.get("avg_frame_rate") or "0/1") or Fraction(self.v["r_frame_rate"])
        self.duration = float(self.info["format"]["duration"])
        if audio:
            ad = float(probe(audio)["format"]["duration"])
            self.duration = max(self.duration, ad) if abs(ad - self.duration) < 2 else self.duration
        out, _ = sh(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time,flags,pos,size",
                     "-of", "compact=p=0", path])
        self.pk, keypos = [], {}                      # decode order: (pts, is_key)
        for line in out.splitlines():
            kv = dict(x.split("=", 1) for x in line.split("|") if "=" in x)
            if kv.get("pts_time") in (None, "", "N/A"):
                continue
            is_key = "K" in kv.get("flags", "")
            self.pk.append((float(kv["pts_time"]), is_key))
            if is_key and kv.get("pos", "N/A") != "N/A":
                keypos[len(self.pk) - 1] = (int(kv["pos"]), int(kv["size"]))
        self.pts = sorted(p for p, _ in self.pk)     # presentation order
        self.vend = (self.pts[-1] + (1 / float(self.fps) if self.fps else 0)) if self.pts else self.duration
        # H.264: only IDR keyframes are safe splice points (after a non-IDR I-frame, P-frames may still reference -
        # and MMCO-unref - frames from before it, which a splice would have replaced).
        self.idr = self._idr_map(keypos) if self.codec == "h264" else {}
        self.all_keys = sum(1 for _, k in self.pk if k)
        self.keys = [(i, p) for i, (p, k) in enumerate(self.pk) if k and self.idr.get(i, True)]
        self.key_pts = [p for _, p in self.keys]
        self.frame = 1.0 / float(self.fps) if self.fps else 1 / 30
        self.gop = self._gop_stats()

    def _idr_map(self, keypos):
        res = {}
        if not self.path.endswith((".mp4", ".m4v")):
            return res
        with open(self.path, "rb") as f:
            for i, (pos, size) in keypos.items():
                off, kind = 0, None
                while off + 5 <= size:
                    f.seek(pos + off)
                    hdr = f.read(5)
                    n = int.from_bytes(hdr[:4], "big")
                    t = hdr[4] & 0x1F
                    if n <= 0 or off + 4 + n > size:
                        break
                    if t in (1, 5):
                        kind = (t == 5)
                        break
                    off += 4 + n
                if kind is not None:
                    res[i] = kind
        return res

    def _gop_stats(self):
        kp = self.key_pts
        gaps = [b - a for a, b in zip(kp, kp[1:])] or [0]
        leading = 0
        for i, p in self.keys[1:40]:
            leading += sum(1 for q, _ in self.pk[i + 1:i + 6] if q < p - 1e-6)
        return {"keyframes": self.all_keys, "splice_points": len(kp), "gop_avg_s": round(sum(gaps) / len(gaps), 3),
                "gop_max_s": round(max(gaps), 3), "open_gop_leading_frames": leading}

    def snap(self, t):
        return bisect.bisect_left(self.pts, t - self.frame / 2)


def plan_ranges(src, keep):
    """The exact source ranges the output will contain (frame-snapped). Chapters and subtitles map through these."""
    if not keep or (len(keep) == 1 and keep[0][0] <= 0.05 and keep[0][1] >= src.duration - 0.05):
        return [(0.0, src.duration)]
    out = []
    for a, b in keep:
        i, j = src.snap(a), (src.snap(b) if b < src.vend - src.frame / 2 else len(src.pts))
        if j - i < 2:
            continue
        out.append((src.pts[i], src.pts[j] if j < len(src.pts) else src.duration))
    return out


def _x264(src):
    """x264 settings whose frame-reorder depth equals the source's, so DTS stays monotonic across every splice."""
    v = src.v
    lvl = v.get("level") or 40
    hb = int(v.get("has_b_frames") or 0)
    if hb > 2:
        raise RuntimeError("source reorder depth %d is not reproducible; use keyframe mode" % hb)
    bf = {0: "bframes=0", 1: "bframes=2:b-pyramid=none", 2: "bframes=3:b-pyramid=normal"}[hb]
    a = ["-c:v", "libx264", "-preset", "slow", "-crf", "16", "-pix_fmt", "yuv420p", "-profile:v", "high",
         "-level:v", "%d.%d" % (lvl // 10, lvl % 10), "-x264-params", "sps-id=1:" + bf]
    return a + _color(v)


def _vp9(src):
    v = src.v
    pix = v.get("pix_fmt") or "yuv420p"
    w = v.get("width") or 1920
    tiles = 2 if w >= 2560 else 1
    return (["-c:v", "libvpx-vp9", "-pix_fmt", pix, "-crf", "18", "-b:v", "0", "-deadline", "good", "-cpu-used", "4",
             "-row-mt", "1", "-tile-columns", str(tiles), "-auto-alt-ref", "1", "-lag-in-frames", "16"]
            + (["-profile:v", "2"] if "10" in pix else []) + _color(v))


def _color(v):
    a = []
    for k, opt in (("color_primaries", "-color_primaries"), ("color_transfer", "-color_trc"), ("color_space", "-colorspace")):
        if v.get(k) and v[k] != "unknown":
            a += [opt, v[k]]
    if v.get("color_range") == "tv":
        a += ["-color_range", "tv"]
    return a


def _audio_graph(ranges, label):
    n = len(ranges)
    parts = ["%sasplit=%d%s" % (label, n, "".join("[s%d]" % i for i in range(n)))] if n > 1 else []
    for i, (a, b) in enumerate(ranges):
        src = "[s%d]" % i if n > 1 else label
        f = "%satrim=start=%.6f:end=%.6f,asetpts=PTS-STARTPTS" % (src, a, b)
        if i > 0: f += ",afade=t=in:d=0.01"
        if i < n - 1: f += ",afade=t=out:st=%.6f:d=0.01" % max(0.0, b - a - 0.01)
        parts.append(f + "[p%d]" % i)
    parts.append("".join("[p%d]" % i for i in range(n)) + "concat=n=%d:v=0:a=1[aout]" % n)
    return ";".join(parts)


def keyframe_ranges(src, ranges):
    """Fallback plan with no re-encode at all: each kept range shrinks to the splice points (keyframes/IDRs)
    inside it. Loses up to one GOP at each cut edge, but is always clean."""
    out = []
    for A, B in ranges:
        eof = B >= src.duration - src.frame / 2
        k1 = next((p for p in src.key_pts if p >= A - 1e-6), None)
        k2 = src.duration if eof else max((p for p in src.key_pts if p <= B + 1e-6), default=None)
        if k1 is not None and k2 is not None and k2 - k1 > 1.0:
            out.append((k1, k2))
    return out


def build(src, ranges, out_path, meta_path, workdir, mode="smart"):
    """src: Source; ranges: from plan_ranges() (or keyframe_ranges() with mode='keyframe').
    Returns a report dict (report['ok'] must be True to publish)."""
    rep = {"codec": src.codec, "width": src.v.get("width"), "height": src.v.get("height"), "fps": str(src.fps),
           "gop": src.gop, "source_duration": round(src.duration, 3)}
    mux = ["-movflags", "+faststart"] if out_path.endswith(".mp4") else []
    ain = ["-i", src.audio] if src.audio else []
    amap = ["-map", "1:a:0"] if src.audio else ["-map", "0:a:0?"]
    if len(ranges) == 1 and ranges[0][0] <= 0.05 and ranges[0][1] >= src.duration - 0.05:
        meta_idx = "2" if src.audio else "1"
        sh(["ffmpeg", "-v", "error", "-y", "-i", src.path] + ain + ["-i", meta_path, "-map", "0:v:0"] + amap +
           ["-map_metadata", meta_idx, "-map_chapters", meta_idx, "-c", "copy", "-metadata:s:a:0", "language=eng"] + mux + [out_path])
        rep.update(mode="remux", expected=round(src.duration, 3))
        return _verify(out_path, rep, [])

    enc = (_x264(src) if src.codec == "h264" else _vp9(src)) if mode == "smart" else None
    ext = ".ts" if src.codec == "h264" else ".mkv"
    pieces, joins, t_out = [], [], 0.0
    list_lines = []
    for A, B in ranges:
        eof = B >= src.duration - src.frame / 2
        endp = src.vend if eof else B
        k1 = next((p for p in src.key_pts if p >= A - 1e-6), None)
        k2 = max((p for p in src.key_pts if p <= endp + 1e-6), default=None)
        plan = []
        if k1 is None or k1 >= endp - 1e-6:
            plan.append(("enc", A, endp))
        else:
            if k1 > A + 1e-6: plan.append(("enc", A, k1))
            if eof: plan.append(("copy", k1, None))
            else:
                if k2 > k1 + 1e-6: plan.append(("copy", k1, k2))
                if endp > k2 + 1e-6: plan.append(("enc", k2, endp))
        for kind, s, e in plan:
            cnt = (len(src.pts) if e is None else src.snap(e)) - src.snap(s)
            if cnt <= 0:
                continue
            dur = cnt / float(src.fps)
            joins.append((round(t_out, 3), dur))
            if kind == "copy" and src.codec == "h264":
                ki = next(ix for ix, kp in src.keys if abs(kp - s) < 1e-6)
                ke = len(src.pk) if e is None else next(ix for ix, kp in src.keys if abs(kp - e) < 1e-6)
                if ke - ki != cnt:
                    raise RuntimeError("open GOP around %.3f (copy %d vs %d frames); refusing to stream-copy" % (s, ke - ki, cnt))
                p = os.path.join(workdir, "piece%03d.ts" % len(pieces))
                sh(["ffmpeg", "-v", "error", "-y", "-ss", "%.6f" % (s + src.frame / 4), "-i", src.path, "-map", "0:v:0",
                    "-c:v", "copy", "-frames:v", str(cnt), "-f", "mpegts", p])
                list_lines.append("file '%s'\nduration %.6f" % (p, dur))
            elif kind == "copy":                   # VP9: read straight from the source, no duplicate file
                list_lines.append("file '%s'\ninpoint %.6f" % (src.path, s) + ("" if e is None else "\noutpoint %.6f" % e))
            else:
                if enc is None:
                    raise RuntimeError("keyframe mode needs keyframe-aligned ranges (got an encode piece at %.3f)" % s)
                p = os.path.join(workdir, "piece%03d%s" % (len(pieces), ext))
                sh(["ffmpeg", "-v", "error", "-y", "-ss", "%.6f" % max(0.0, s - src.frame / 2), "-i", src.path,
                    "-map", "0:v:0", "-frames:v", str(cnt), "-fps_mode", "passthrough"] + enc +
                   (["-f", "mpegts"] if ext == ".ts" else []) + [p])
                list_lines.append("file '%s'\nduration %.6f" % (p, dur))
            pieces.append({"kind": kind, "from": round(s, 3), "to": None if e is None else round(e, 3), "frames": cnt})
            t_out += dur
    list_path = os.path.join(workdir, "concat.txt")
    with open(list_path, "w") as f:
        f.write("\n".join(list_lines) + "\n")
    rep["pieces"] = pieces
    rep["reencoded_seconds"] = round(sum(p["frames"] for p in pieces if p["kind"] == "enc") / float(src.fps), 3)
    expected = sum(b - a for a, b in ranges)
    alabel = "[1:a:0]" if src.audio else "[0:a:0]"
    cmd = (["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", list_path] +
           (["-i", src.audio] if src.audio else ["-i", src.path]) + ["-i", meta_path,
           "-filter_complex", _audio_graph(ranges, "[1:a:0]"), "-map", "0:v:0", "-map", "[aout]",
           "-map_metadata", "2", "-map_chapters", "2", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
           "-metadata:s:a:0", "language=eng"] + mux + [out_path])
    sh(cmd)
    rep.update(mode="smartcut" if mode == "smart" else "keyframe-copy",
               kept=[(round(a, 3), round(b, 3)) for a, b in ranges], expected=round(expected, 3))
    return _verify(out_path, rep, joins)


def _verify(path, rep, joins):
    info = probe(path)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    dur = float(info["format"]["duration"])
    rep.update(out_duration=round(dur, 3), out_video=v["codec_name"], out_profile=v.get("profile"),
               out_res="%sx%s" % (v.get("width"), v.get("height")), out_audio=a and a["codec_name"],
               out_size=int(info["format"]["size"]), chapters=len(info.get("chapters", [])))
    errs = []
    for t, d in (joins or [])[:60]:
        _, err = sh(["ffmpeg", "-v", "error", "-ss", "%.3f" % (t + 0.001), "-i", path, "-t", "%.2f" % min(d + 2.0, 12.0),
                     "-map", "0:v:0", "-f", "null", "-"])
        if err.strip(): errs.append({"at": t, "err": err.strip()[:300]})
    rep["decode_checks"] = len(joins or [])
    rep["decode_errors"] = errs
    rep["joins"] = [t for t, _ in (joins or [])]
    out, _ = sh(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=dts", "-of", "csv=p=0", path])
    dts = [int(x) for x in out.split() if x.strip() not in ("", "N/A")]
    rep["dts_nonmonotonic"] = sum(1 for a_, b_ in zip(dts, dts[1:]) if b_ <= a_)   # every frame needs its own time slot
    drift = abs(dur - rep["expected"])
    rep["ok"] = (drift < 0.35 and not errs and not rep["dts_nonmonotonic"] and rep["out_video"] in ("h264", "vp9")
                 and rep["out_audio"] in ("aac", "opus"))
    if not rep["ok"]:
        log.error("verify failed: %s", {k: rep[k] for k in ("expected", "out_duration", "decode_errors", "out_video", "out_audio")})
    return rep
