"""A channel's own playlists -> "real series" detection (for series seasons in Plex).
Real series: created by the channel (its Playlists tab), >= 3 videos, every video with a known channel from that
channel, titles numbered (Ep / Part / # / Episode / Day / S1E2 ...) or the list in upload order; generic lists
(Popular, All uploads, Shorts, "Videos", mixes, best-of ...) are skipped."""
import logging
import re

import yt_dlp

from . import ytdl

log = logging.getLogger("tubarr.playlists")
GENERIC = re.compile(r"(?i)^(popular|all|all uploads|uploads|videos?|shorts?|live|streams?|favou?rites|liked|mix|playlist)\b|"
                     r"\b(shorts|best of|highlights|compilation|clips|reactions?|podcast clips|mix(es)?|random|misc)\b")
NUMBERED = re.compile(r"(?i)(\b(ep(isode)?|part|pt|day|week|chapter|vol(ume)?|season|s\d+\s*e)\s*\.?\s*#?\s*\d+|#\s*\d+\b|\(\s*\d+\s*\)|\b\d+\s*/\s*\d+\b)")


def channel_playlists(channel_id, limit=60):
    """[(playlist_id, title, count)] from the channel's Playlists tab (flat, one request or two)."""
    opts = ytdl._params(extract_flat="in_playlist", playlistend=limit, noplaylist=False)
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info("https://www.youtube.com/channel/%s/playlists" % channel_id, download=False)
    out = []
    for e in info.get("entries") or []:
        pid = e.get("id") or ""
        if pid.startswith(("PL", "OLAK")) or e.get("_type") == "url" and "list=" in (e.get("url") or ""):
            out.append((pid, (e.get("title") or "").strip(), e.get("playlist_count")))
    return out


def playlist_entries(pid, limit=300):
    opts = ytdl._params(extract_flat="in_playlist", playlistend=limit, noplaylist=False,
                        extractor_args={"youtubetab": {"approximate_date": [""]}})
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info("https://www.youtube.com/playlist?list=" + pid, download=False)
    return [{"id": e.get("id"), "title": e.get("title") or "", "channel_id": e.get("channel_id"),
             "ts": e.get("timestamp")} for e in info.get("entries") or [] if e.get("id")]


def is_series(channel_id, title, entries):
    """(True/False, why)."""
    if not title or GENERIC.search(title):
        return False, "generic title"
    if len(entries) < 3:
        return False, "fewer than 3 videos"
    if len(entries) > 250:
        return False, "too big to be one series"
    foreign = [e for e in entries if e["channel_id"] and e["channel_id"] != channel_id]
    if foreign:
        return False, "%d videos from other channels" % len(foreign)
    numbered = sum(1 for e in entries if NUMBERED.search(e["title"]))
    if numbered >= max(3, int(0.6 * len(entries))):
        return True, "numbered titles (%d/%d)" % (numbered, len(entries))
    ts = [e["ts"] for e in entries if e["ts"]]
    if len(ts) >= 3 and (ts == sorted(ts)):
        # a topical list is usually in upload order too: only a real series carries its name in (almost) every title
        named = sum(1 for e in entries if carries_name(title, e["title"]))
        if named >= max(3, int(0.8 * len(entries))):
            return True, "in upload order, series name in %d/%d titles" % (named, len(entries))
        return False, "in upload order, but only %d/%d titles carry the name" % (named, len(entries))
    return False, "not numbered, not chronological"


EPNUM = re.compile(r"(?i)\b(?:ep(?:isode)?|part|pt|day|chapter|vol(?:ume)?|week)\s*\.?\s*#?\s*(\d{1,4})\b|#\s*(\d{1,4})\b|"
                   r"\bs\d+\s*e(\d{1,4})\b")


def title_number(title):
    m = EPNUM.search(title or "")
    return int(next(g for g in m.groups() if g)) if m else None


def logical_order(entries):
    """(entries oldest-part-first, {video_id: episode number from the title} or {}).
    A playlist listed newest-first (Ep03, Ep02, Ep01) is reversed. Title numbers are used only when EVERY entry
    has one and they're unique, so they can't collide with position-based numbers."""
    nums = [title_number(e["title"]) for e in entries]
    seq = [n for n in nums if n is not None]
    down = sum(1 for a, b in zip(seq, seq[1:]) if b < a)
    up = sum(1 for a, b in zip(seq, seq[1:]) if b > a)
    if down > up:
        entries, nums = entries[::-1], nums[::-1]
    use = all(n is not None for n in nums) and len(set(nums)) == len(nums) and all(n > 0 for n in nums)
    return entries, ({e["id"]: n for e, n in zip(entries, nums)} if use else {})


def _words(t):
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", t or "")
    return {w for w in re.sub(r"[^a-z0-9]+", " ", t.lower()).split() if len(w) >= 3 or w.isdigit()}


def carries_name(playlist_title, video_title):
    """>= 75% of the playlist name's words appear in the video title."""
    w = _words(playlist_title)
    return bool(w) and len(w & set(re.sub(r"[^a-z0-9]+", " ", (video_title or "").lower()).split())) >= max(1, -(-3 * len(w) // 4))
