"""The library on disk: finds episode videos and reads their NFOs (video ID, title, upload date).

Only <Channel>/<Season …>/<file>.mkv|.mp4 is looked at. Every hidden name (.staging, .work, .trim-work,
.trim-originals, dot files) is skipped at every level, so Tubarr's work area is never touched.
"""
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import config


VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")         # a YouTube video ID; it becomes a folder name, so nothing else
CHANNEL_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def valid_video_id(vid):
    return isinstance(vid, str) and VIDEO_ID.fullmatch(vid) is not None


def _xml(path):
    try:
        return ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None


def _youtube_id(root):
    for u in root.findall("uniqueid"):
        if (u.get("type") or "").lower() == "youtube" and (u.text or "").strip():
            return u.text.strip()
    return None


def read_show(show_dir):
    r = _xml(os.path.join(show_dir, "tvshow.nfo"))
    if r is None:
        return None, os.path.basename(show_dir)
    cid = _youtube_id(r)
    return (cid if cid and CHANNEL_ID.fullmatch(cid) else None), (r.findtext("title") or "").strip() or os.path.basename(show_dir)


def read_episode(nfo_path):
    r = _xml(nfo_path)
    if r is None:
        return {}
    vid = _youtube_id(r)
    return {"video_id": vid if valid_video_id(vid) else None, "title": (r.findtext("title") or "").strip() or None,
            "upload_date": (r.findtext("aired") or r.findtext("premiered") or "").strip()[:10] or None}


def is_hidden(path):
    rel = os.path.relpath(path, config.ROOT)
    return rel.startswith("..") or any(p.startswith(".") for p in rel.split(os.sep))


def sidecar_subtitles(video_path):
    """Subtitle files that belong to this video: <base>.srt, <base>.en.srt, <base>.en.forced.srt, …"""
    d, base = os.path.dirname(video_path), os.path.splitext(os.path.basename(video_path))[0]
    try:
        names = os.listdir(d)
    except OSError:
        return []
    return sorted(os.path.join(d, n) for n in names
                  if n.lower().endswith(".srt") and (n[:-4] == base or n.startswith(base + ".")) and not n.startswith("."))


def entry(path, show=None):
    """One video file -> dict (or None if it isn't a library episode)."""
    if is_hidden(path) or not path.lower().endswith(config.VIDEO_EXTS):
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    rel = os.path.relpath(path, config.ROOT)
    folder = rel.split(os.sep)[0]
    if show is None:
        show = read_show(os.path.join(config.ROOT, folder))
    cid, ctitle = show
    ep = read_episode(os.path.splitext(path)[0] + ".nfo")
    return {"path": path, "folder": folder, "channel_id": cid or ("folder:" + folder), "channel": ctitle,
            "video_id": ep.get("video_id"), "title": ep.get("title") or os.path.splitext(os.path.basename(path))[0],
            "upload_date": ep.get("upload_date"), "size": st.st_size, "mtime": st.st_mtime, "inode": st.st_ino}


def scan():
    """Every episode video in the library (hidden folders skipped)."""
    out = []
    try:
        shows = sorted(os.listdir(config.ROOT))
    except OSError:
        return out
    for folder in shows:
        show_dir = os.path.join(config.ROOT, folder)
        if folder.startswith(".") or not os.path.isdir(show_dir):
            continue
        show = read_show(show_dir)
        for dp, dns, fns in os.walk(show_dir):
            dns[:] = sorted(d for d in dns if not d.startswith("."))
            for f in sorted(fns):
                if f.startswith(".") or not f.lower().endswith(config.VIDEO_EXTS):
                    continue
                e = entry(os.path.join(dp, f), show)
                if e:
                    out.append(e)
    return out


def file_age_ok(mtime, s, now=None):
    return (now or time.time()) - mtime >= s["min_file_age_minutes"] * 60


def upload_eligible_at(upload_date, mtime, s):
    """When the video is at least `min_upload_age_hours` old. The upload moment is bounded from above by the end
    of its upload day (the configured time zone) and by when Tubarr saved the file (it can't be saved before upload)."""
    latest = mtime
    if upload_date:
        try:
            d = datetime.strptime(upload_date, "%Y-%m-%d").replace(tzinfo=ZoneInfo(config.TZ))
            latest = min(latest, (d + timedelta(days=1)).timestamp())
        except ValueError:
            pass
    return latest + s["min_upload_age_hours"] * 3600


def resolve(arg):
    """A video ID, or a path in any of the three views (Trimarr's, Tubarr's, Plex's), or relative to the library."""
    if not arg:
        return None
    for prefix in (config.ROOT, config.PLEX_ROOT):
        if arg.startswith(prefix + "/"):
            return os.path.join(config.ROOT, arg[len(prefix) + 1:])
    if "/" in arg or arg.lower().endswith(config.VIDEO_EXTS):
        return os.path.join(config.ROOT, arg.lstrip("/"))
    return None
