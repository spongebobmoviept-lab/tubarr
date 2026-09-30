"""ffprobe / ffmpeg helpers: probing, keyframes near the cut points, H.264 IDR detection, decode and audio checks."""
import json
import math
import os
import re
import subprocess
from fractions import Fraction


class MediaError(RuntimeError):
    pass


def run(cmd, timeout=None, check=True, binary=False):
    kw = {} if binary else {"text": True, "errors": "replace"}
    p = subprocess.run(cmd, capture_output=True, timeout=timeout, **kw)
    if check and p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace") if binary else p.stderr
        raise MediaError("%s failed (exit %d): %s" % (os.path.basename(cmd[0]), p.returncode, err.strip()[-1200:]))
    return p


def probe(path, timeout=180):
    p = run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-show_chapters", "-of", "json", path],
            timeout=timeout)
    info = json.loads(p.stdout or "{}")
    if not info.get("format") or not info.get("streams"):
        raise MediaError("ffprobe found no streams in %s" % os.path.basename(path))
    return info


def duration(info):
    return float(info["format"]["duration"])


def start_time(info):
    try:
        return float(info["format"].get("start_time") or 0.0)
    except ValueError:
        return 0.0


def video_stream(info):
    for s in info["streams"]:
        if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic"):
            return s
    raise MediaError("no video stream")


def has_attachments(info):
    return any(s.get("codec_type") == "attachment" for s in info["streams"])


def av_signature(info):
    """What must be identical before and after a trim: every video/audio/subtitle stream's type, codec and shape."""
    sig = []
    for s in info["streams"]:
        t = s.get("codec_type")
        if t == "video" and not (s.get("disposition") or {}).get("attached_pic"):
            sig.append(["video", s.get("codec_name"), s.get("width"), s.get("height")])
        elif t == "audio":
            sig.append(["audio", s.get("codec_name"), s.get("sample_rate"), s.get("channels")])
        elif t == "subtitle":
            sig.append(["subtitle", s.get("codec_name")])
    return sig


def chapters(info):
    out = []
    for c in info.get("chapters") or []:
        try:
            out.append((float(c["start_time"]), float(c["end_time"]), ((c.get("tags") or {}).get("title") or "").strip()))
        except (KeyError, ValueError):
            continue
    return out


class Keyframes:
    """Keyframes of the main video stream. Only small windows around the cut points are read (a few MB even for
    4K), widening if a window holds no usable keyframe. For H.264 a piece may only START on an IDR frame: YouTube's
    H.264 marks open-GOP I-frames as keyframes too, and starting on one breaks the first frames after the join."""

    def __init__(self, path, info):
        v = video_stream(info)
        self.path, self.index = path, int(v["index"])
        self.tb = Fraction(v.get("time_base") or "1/1000")
        self.codec = v.get("codec_name")
        self.duration = duration(info)
        self.keys = {}          # pts -> dts (ints, stream time base)
        self.idr = {}           # pts -> bool (H.264 only)
        self.spans = []         # [(a, b)] seconds already read
        self.needs_idr = self.codec == "h264"

    def sec(self, pts):
        return float(Fraction(pts) * self.tb)

    def _covered(self, a, b):
        return any(x <= a + 1e-6 and b <= y + 1e-6 for x, y in self.spans)

    def load(self, spans):
        todo = []
        for a, b in spans:
            a, b = max(0.0, a), min(self.duration + 5.0, b)
            if b > a and not self._covered(a, b):
                todo.append((a, b))
        if not todo:
            return
        iv = ",".join("%.3f%%%.3f" % ab for ab in todo)
        out = run(["ffprobe", "-v", "error", "-select_streams", str(self.index), "-read_intervals", iv,
                   "-show_entries", "packet=pts,dts,flags", "-of", "compact=p=0", self.path], timeout=900).stdout
        for line in out.splitlines():
            kv = dict(x.split("=", 1) for x in line.strip().split("|") if "=" in x)
            if "K" not in kv.get("flags", "") or kv.get("pts") in (None, "", "N/A"):
                continue
            pts = int(kv["pts"])
            dts = kv.get("dts")
            self.keys[pts] = int(dts) if dts not in (None, "", "N/A") else pts
        if self.needs_idr:
            self._trace_idr(todo)
        self.spans += todo

    def _trace_idr(self, spans):
        """Frame type of each keyframe packet from the H.264 bitstream itself (NAL unit type 5 = IDR)."""
        for a, b in spans:
            p = run(["ffmpeg", "-nostdin", "-hide_banner", "-v", "info", "-copyts", "-ss", "%.3f" % a, "-to", "%.3f" % b,
                     "-i", self.path, "-map", "0:%d" % self.index, "-c", "copy", "-bsf:v", "trace_headers",
                     "-f", "null", "-"], timeout=900, check=False)
            cur, seen = None, 0
            for line in p.stderr.splitlines():
                if "Packet:" in line:
                    m = re.search(r"\bpts (-?\d+)", line)
                    cur = int(m.group(1)) if (m and "key frame" in line) else None
                elif cur is not None and "nal_unit_type" in line:
                    try:
                        t = int(line.rsplit("=", 1)[1])
                    except ValueError:
                        continue
                    if t in (1, 5):
                        self.idr[cur] = (t == 5)
                        seen += 1
                        cur = None
            if not seen and any(a - 1 <= self.sec(k) <= b + 1 for k in self.keys):
                raise MediaError("could not read the H.264 frame types around %.1f s" % a)

    def clean(self, pts):
        return self.idr.get(pts, False) if self.needs_idr else True

    def at_or_before(self, t):
        """(sec, pts, dts) of the last keyframe at or before t. (0.0, None, None) = the start of the file."""
        for w in (20.0, 90.0, 400.0, None):
            lo = 0.0 if w is None else max(0.0, t - w)
            self.load([(lo, t + 1.0)])
            c = [p for p in self.keys if lo - 1e-6 <= self.sec(p) <= t + 1e-6]
            if c:
                p = max(c)
                return (0.0, None, None) if self.sec(p) <= 1e-6 else (self.sec(p), p, self.keys[p])
            if lo <= 0.0:
                break
        return 0.0, None, None

    def clean_start_at_or_after(self, t):
        """(sec, pts, dts) of the first keyframe at or after t that decoding can start from (H.264: IDR).
        None = there is none before the end of the file."""
        for w in (20.0, 90.0, 400.0, None):
            hi = self.duration + 5.0 if w is None else t + w
            self.load([(max(0.0, t - 1.0), hi)])
            c = [p for p in self.keys if t - 1e-6 <= self.sec(p) <= hi + 1e-6 and self.clean(p)]
            if c:
                p = min(c)
                return self.sec(p), p, self.keys[p]
            if hi >= self.duration:
                break
        return None


def decode_errors(path, start, dur, threads=4):
    """Decode video+audio for `dur` seconds from `start`; returns the error lines (empty = clean)."""
    p = run(["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-threads", str(threads), "-ss", "%.3f" % max(0.0, start),
             "-i", path, "-t", "%.3f" % dur, "-map", "0:V", "-map", "0:a?", "-f", "null", "-"], timeout=1800, check=False)
    msgs = [re.sub(r" @ 0x[0-9a-fA-F]+", "", ln.strip()) for ln in p.stderr.splitlines() if ln.strip()]
    if p.returncode != 0 and not msgs:
        msgs = ["ffmpeg exit %d" % p.returncode]
    return msgs


def pcm(path, start, dur, sr=8000):
    """Mono 8 kHz PCM of the first audio stream (for alignment checks)."""
    p = run(["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-ss", "%.3f" % max(0.0, start), "-i", path,
             "-t", "%.3f" % dur, "-map", "0:a:0", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
            timeout=600, binary=True, check=False)
    return p.stdout


def align(a, b, sr=8000, max_lag=0.3):
    """Cross-correlate two PCM snippets: {'result': 'ok', 'lag_ms', 'corr'} or 'silent' / 'too_short'.
    Identical content at the same moment gives lag 0 and corr ~1.0."""
    import numpy as np                              # imported only while verifying (keeps the idle process small)
    x = np.frombuffer(a, dtype="<i2").astype(np.float64)
    y = np.frombuffer(b, dtype="<i2").astype(np.float64)
    n = min(len(x), len(y))
    if n < sr // 2:
        return {"result": "too_short"}
    x, y = x[:n] - x[:n].mean(), y[:n] - y[:n].mean()
    if x.std() < 30 or y.std() < 30:
        return {"result": "silent"}
    size = 1 << int(math.ceil(math.log2(2 * n)))
    cc = np.fft.irfft(np.fft.rfft(x, size) * np.conj(np.fft.rfft(y, size)), size)
    m = min(int(max_lag * sr), n - 1)
    lags = np.concatenate([np.arange(0, m + 1), np.arange(-m, 0)])
    vals = np.concatenate([cc[:m + 1], cc[size - m:]])
    lag = int(lags[int(np.argmax(vals))])
    xs, ys = (x[lag:], y[:n - lag]) if lag >= 0 else (x[:n + lag], y[-lag:])
    corr = float(np.dot(xs, ys) / (np.linalg.norm(xs) * np.linalg.norm(ys) + 1e-9))
    return {"result": "ok", "lag_ms": round(lag * 1000.0 / sr, 1), "corr": round(corr, 3)}
