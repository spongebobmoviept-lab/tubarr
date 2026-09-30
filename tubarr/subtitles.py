"""English subtitles for every video: YouTube's manual English subtitles when they exist, otherwise YouTube's
automatic English captions (the original-language speech recognition, never machine translations), cleaned into
plain SRT. Auto-captions arrive as "rolling" karaoke cues (each cue repeats the previous line plus the new words);
they are de-duplicated so every spoken line appears once. Output: <episode>.en.srt (Plex lists it as English)."""
import re


from .chapters import to_output

_TIME = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})")
_TAG = re.compile(r"</?[^>]+>")
_INLINE_TS = re.compile(r"<\d{2}:\d{2}:\d{2}[.,]\d{3}>")


def _t(s):
    m = _TIME.match(s.strip())
    h, mi, se, ms = m.groups()
    return int(h or 0) * 3600 + int(mi) * 60 + int(se) + int(ms) / 1000.0


def _clean(line):
    line = _INLINE_TS.sub("", line)
    line = _TAG.sub("", line)
    line = line.replace("&lt;", "<").replace("&gt;", ">").replace("&nbsp;", " ").replace("&amp;", "&")
    return _TAG.sub("", line).strip()             # decoding must not bring markup back (e.g. &lt;font ...&gt;)


def parse_vtt(text):
    """[(start, end, [lines])] with tags removed."""
    cues = []
    # cue blocks are separated by EMPTY lines only; YouTube puts whitespace-only lines INSIDE auto-caption cues
    for block in re.split(r"\n\n+", text.replace("\r", "")):
        lines = [l for l in block.split("\n") if l.strip()]
        for i, l in enumerate(lines):
            if "-->" in l:
                a, b = l.split("-->")
                body = [_clean(x) for x in lines[i + 1:]]
                body = [x for x in body if x]
                if body:
                    cues.append((_t(a), _t(b.split()[0]), body))
                break
    return cues


def dedupe_rolling(cues):
    """YouTube auto-captions: keep only the lines that are NEW in each cue, drop the 10 ms transition cues."""
    out, prev = [], []
    for s, e, lines in cues:
        if e - s < 0.05:
            prev = lines
            continue
        new = [l for l in lines if l not in prev]
        prev = lines
        if new:
            out.append([s, e, new])
    for i in range(len(out) - 1):                 # a line stays up until the next one starts (no overlaps)
        out[i][1] = min(out[i][1], out[i + 1][0]) if out[i + 1][0] > out[i][0] else out[i][1]
    return [(s, e, ls) for s, e, ls in out if e - s >= 0.2]


def _fmt(t):
    ms = int(round(t * 1000))
    return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)


def write_srt(cues, srt_path, ranges=None, min_len=0.3):
    out = []
    for s, e, lines in cues:
        body = "\n".join(lines) if isinstance(lines, list) else lines
        if ranges:
            for a, b in ranges:
                ss, ee = max(s, a), min(e, b)
                if ee - ss >= min_len:
                    out.append((to_output(ss, ranges), to_output(ee, ranges), body))
        else:
            out.append((s, e, body))
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, (s, e, body) in enumerate(out, 1):
            f.write("%d\n%s --> %s\n%s\n\n" % (i, _fmt(s), _fmt(e), body))
    return len(out)


def vtt_to_srt(vtt_path, srt_path, ranges=None, auto=False):
    with open(vtt_path, encoding="utf-8", errors="replace") as f:
        cues = parse_vtt(f.read())
    return write_srt(dedupe_rolling(cues) if auto else cues, srt_path, ranges)


def pick_track(info):
    """('manual'|'auto', lang, vtt_url) or None. Manual English first; else the ORIGINAL English speech track
    (never an auto-translation - lower quality and the usual source of YouTube 429s)."""
    for lang, tracks in (info.get("subtitles") or {}).items():
        if lang.split("-")[0] == "en" and lang != "live_chat":
            url = next((t["url"] for t in tracks if t.get("ext") == "vtt"), None)
            if url:
                return "manual", lang, url
    auto = info.get("automatic_captions") or {}
    video_lang = (info.get("language") or "").split("-")[0]
    for lang in ("en-orig", "en") if video_lang in ("", "en") else ("en-orig",):
        tracks = auto.get(lang)
        if tracks:
            url = next((t["url"] for t in tracks if t.get("ext") == "vtt"), None)
            if url and "tlang=" not in url:
                return "auto", lang, url
    return None


def fetch_english(info, srt_path):
    """Download + convert the best English track. Returns {'kind', 'lang', 'cues'} or None."""
    pick = pick_track(info)
    if not pick:
        return None
    kind, lang, url = pick
    from . import net
    r = net.yt_get(url, timeout=30)
    r.raise_for_status()
    cues = parse_vtt(r.text)
    if kind == "auto":
        cues = dedupe_rolling(cues)
    n = write_srt(cues, srt_path)
    return {"kind": kind, "lang": lang, "cues": n} if n else None
