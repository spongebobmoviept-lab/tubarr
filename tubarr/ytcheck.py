"""Live "is this video still on YouTube?" check, run right before ANY automatic delete.

A video that was taken down (DMCA, removed, made private) is exactly the one worth keeping. Only a video YouTube confirms is still public and downloadable may be
deleted; anything taken down is protected forever; an unclear answer means "don't delete now, try again later".
No side effects on import, so both the worker and the web app can use it."""
import re

import yt_dlp

from . import ytdl

# A BARE "Video unavailable" with no further reason is ALSO what yt-dlp reports when YouTube's player API answers
# UNPLAYABLE because the session lacks a PO (proof-of-origin) token -- the same bot-detection condition as "not a
# bot", just with a misleading message. So a video only counts as GONE when a MORE SPECIFIC reason accompanies it
# (private/copyright/terminated/etc); a bare "unavailable" is treated as inconclusive.
GONE_PAT = re.compile(r"(?i)has been removed|no longer available|private video|this video is private|"
                      r"terminated|copyright|violat|does not exist|isn't available|is not available")
AGE_OR_BLOCKED = re.compile(r"(?i)confirm your age|age.restricted|inappropriate for some users|members.only|join this "
                            r"channel|not available in your country|blocked|geo.?restrict|premium")
INCONCLUSIVE = re.compile(r"(?i)not a bot|429|too many requests|timed? ?out|temporar|network|connection|resolve|"
                          r"unable to download (api|webpage)|http error 5\d\d|ssl|reset by peer")
# Note: a bare "Video unavailable" with no more specific reason now matches
# NEITHER GONE_PAT nor INCONCLUSIVE above, and falls through to the
# `return None` at the bottom of the except block -- i.e. it's treated the
# same as any other unrecognized/ambiguous error: don't delete, retry later.


def youtube_check(vid):
    """True             -> confirmed: yt-dlp resolves playable video formats, it's public, and oEmbed agrees
    ("gone", reason) -> taken down / DMCA / private / unlisted / age-locked / blocked: keep it forever
    None             -> couldn't tell for an ordinary reason: don't delete, retry later
    ("bot", reason)  -> inconclusive SPECIFICALLY because of a bot/rate-limit signal -- distinct from plain
                        None so the caller can feed this back into the shared download backoff (see worker.py's
                        delete_video(): this check used to be a one-way consumer of that backoff, never a
                        contributor to it, even though it's real yt-dlp traffic hitting the exact same limit)."""
    url = "https://www.youtube.com/watch?v=" + vid
    try:
        with yt_dlp.YoutubeDL(ytdl._params(skip_download=True)) as y:
            info = y.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        s = str(e)
        if INCONCLUSIVE.search(s):
            return ("bot", s[:200])
        if GONE_PAT.search(s) or AGE_OR_BLOCKED.search(s):
            return ("gone", s.replace("ERROR: ", "")[:200])
        # A bare, unqualified "Video unavailable" with no other specific
        # reason -- see module docstring: this is ALSO how YouTube's own
        # missing-PO-token/bot handling shows up, not just a real takedown.
        # Treated the same as a confirmed bot signal, not a shrug.
        return ("bot", s[:200])
    except Exception:
        return None
    if (info.get("availability") or "public") != "public":
        return ("gone", "availability on YouTube: %s" % info.get("availability"))   # private/unlisted/needs_auth/...
    if not [f for f in (info.get("formats") or []) if f.get("vcodec") not in (None, "none")]:
        return None
    try:                                              # second, independent signal
        from . import net
        r = net.yt_get("https://www.youtube.com/oembed", params={"url": url, "format": "json"}, timeout=15)
        if r.status_code in (403, 404):
            return None                               # yt-dlp says fine, oEmbed says gone: conflicting -> keep for now
    except Exception:
        return None
    return True

