"""SponsorBlock: the same crowd-sourced data the browser extension uses, fetched through the privacy-preserving
hash-prefix endpoint (only the first 4 hex characters of sha256(videoID) leave the house)."""
import hashlib
import json
import threading
import time

import requests

from . import config

# Fetched once per check; filtering to the chosen categories happens locally, so changing categories never needs
# a re-fetch. Only actionType "skip" is ever used ("full" labels, "mute", "poi" and "chapter" are not cuts).
FETCH_CATEGORIES = ["sponsor", "selfpromo", "interaction", "intro", "outro", "preview", "filler",
                    "music_offtopic", "hook"]
MIN_INTERVAL = 1.0          # seconds between requests (polite; the extension makes one per video view)

_LOCK = threading.Lock()
_LAST = [0.0]


class SponsorBlockError(RuntimeError):
    pass


def fetch(video_id):
    """[{category, actionType, start, end, videoDuration, votes, locked, UUID, timeSubmitted}] for this video ([] if
    none). timeSubmitted (epoch ms) is kept when the API sends it; otherwise the planner uses when Trimarr first saw the
    segment (first_seen, stamped by stamp_first_seen)."""
    with _LOCK:
        wait = _LAST[0] + MIN_INTERVAL - time.time()
        if wait > 0:
            time.sleep(wait)
        _LAST[0] = time.time()
    prefix = hashlib.sha256(video_id.encode("utf-8")).hexdigest()[:4]
    try:
        r = requests.get("%s/api/skipSegments/%s" % (config.SPONSORBLOCK_API, prefix),
                         params={"categories": json.dumps(FETCH_CATEGORIES), "actionTypes": json.dumps(["skip"])},
                         headers={"User-Agent": config.USER_AGENT}, timeout=20)
    except requests.RequestException as e:
        raise SponsorBlockError("SponsorBlock unreachable: %s" % e) from None
    if r.status_code == 404:
        return []
    if r.status_code == 429:
        raise SponsorBlockError("SponsorBlock rate limit (HTTP 429)")
    if r.status_code != 200:
        raise SponsorBlockError("SponsorBlock HTTP %d" % r.status_code)
    try:
        data = r.json()
    except ValueError:
        raise SponsorBlockError("SponsorBlock sent invalid JSON") from None
    for v in data if isinstance(data, list) else []:
        if v.get("videoID") == video_id:
            out = []
            for s in v.get("segments") or []:
                try:
                    out.append({"category": s.get("category"), "actionType": s.get("actionType") or "skip",
                                "start": float(s["segment"][0]), "end": float(s["segment"][1]),
                                "videoDuration": float(s.get("videoDuration") or 0), "votes": s.get("votes"),
                                "locked": s.get("locked"), "UUID": s.get("UUID"),
                                "timeSubmitted": s.get("timeSubmitted")})
                except (KeyError, TypeError, ValueError, IndexError):
                    continue
            return out
    return []


def stamp_first_seen(segments, previous=None, now=None):
    """Adds first_seen (epoch s) to each segment: carried over from the previous fetch (same UUID), else now. Lets
    the planner enforce a minimum segment age when SponsorBlock doesn't send timeSubmitted."""
    now = time.time() if now is None else now
    seen = {p.get("UUID"): p.get("first_seen") for p in (previous or []) if isinstance(p, dict) and p.get("UUID")}
    for sg in segments or []:
        first = seen.get(sg.get("UUID")) if sg.get("UUID") else None
        sg["first_seen"] = first if isinstance(first, (int, float)) else now
    return segments
