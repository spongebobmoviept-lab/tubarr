"""Folder and file naming for the Plex "YouTube" library.

Layout (one show per channel, one season per upload year, episodes numbered by upload date):
    <Channel>/Season 2026/<Channel> - S2026E092601 - <Title>.mp4
Episode number = MMDD * 100 + same-day index (01..99), e.g. the 2nd upload on Sep 26 = E092602.
It is derived from the upload date alone, so numbers never change and files are never renamed.
"""
import re
import unicodedata
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import config

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def valid_video_id(vid):
    return isinstance(vid, str) and bool(VIDEO_ID.match(vid))


def check_video_id(vid):
    """Raise ValueError unless `vid` is a YouTube video id (it becomes part of file and folder paths)."""
    if not valid_video_id(vid):
        raise ValueError("not a YouTube video id: %r" % (str(vid)[:40],))
    return vid


def sanitize(name, max_bytes=150):
    """Filesystem- and Samba-safe name. Keeps letters of any language, drops emoji/symbols and path characters."""
    name = unicodedata.normalize("NFC", name or "")
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "S" or ch in "&+$%#@!'")
    name = _BAD.sub(" ", name)
    name = name.replace("’", "'").replace("“", "").replace("”", "")
    name = re.sub(r"\s+", " ", name).strip(" .-_")
    b = name.encode("utf-8")
    if len(b) > max_bytes:
        name = b[:max_bytes].decode("utf-8", "ignore").rstrip(" .-_") + "…"
    return name or "Untitled"


def local_date(ts=None, upload_date=None):
    """Upload date in the configured time zone (TZ) (a 9 pm ET upload is that day, not tomorrow's UTC date)."""
    if ts:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(ZoneInfo(config.TZ)).date()
    if upload_date:
        return datetime.strptime(upload_date, "%Y%m%d").date()
    raise ValueError("no date")


def episode_number(d, same_day_index):
    if not 1 <= same_day_index <= 99:
        raise ValueError("same-day index must be 1..99")
    return (d.month * 100 + d.day) * 100 + same_day_index


def episode_code(d, same_day_index):
    return "S%04dE%06d" % (d.year, episode_number(d, same_day_index))


def show_folder(channel_title):
    return sanitize(channel_title, 120)


def season_folder(year):
    return "Season %04d" % year


def episode_basename(show, d, same_day_index, title):
    return "%s - %s - %s" % (show, episode_code(d, same_day_index), sanitize(title, 140))
