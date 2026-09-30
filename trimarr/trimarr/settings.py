"""Adjustable settings, kept in /data/settings.json. The API and CLI change them; everything reads them live.

Safety rules are floors: a setting can make Trimarr stricter, never looser than the built-in rules
(files at least 30 minutes old, uploads at least 24 hours old, at most 40% of a video removed, only SponsorBlock
segments that are locked or have at least one upvote and were submitted at least 24 hours ago).
"""
import copy
import json
import os
import threading

from . import config

# SponsorBlock categories that may be cut. "intro" is never cut. outro/preview/filler/hook/music_offtopic can be
# opted into; the default is sponsor and selfpromo (like/subscribe reminders are kept).
CUTTABLE = ["sponsor", "selfpromo", "interaction", "outro", "preview", "filler", "music_offtopic", "hook"]

DEFAULTS = {
    "enabled": False,               # master switch for automatic trimming (ships OFF)
    "paused": False,                # stop starting new trims without changing anything else
    "channel_default": True,        # channels without their own setting follow this while `enabled` is on
    "channels": {},                 # {channel_id: true|false}; missing = channel_default
    "categories": ["sponsor", "selfpromo"],
    "min_segment_seconds": 1.0,     # SponsorBlock segments shorter than this are ignored
    "min_votes": 1,                 # an unlocked SponsorBlock segment needs at least this many net votes
    "min_segment_age_hours": 24,    # ...and must be at least this old (fresh submissions are often wrong or abuse)
    "max_removed_fraction": 0.40,   # more than this would be removed -> "suspicious", never trimmed automatically
    "min_upload_age_hours": 24,     # segments arrive hours or days after upload
    "min_file_age_minutes": 30,     # never touch a file Tubarr wrote in the last 30 minutes
    "recheck_hours": 24,            # untrimmed videos are re-checked against SponsorBlock this often
    "pass_interval_hours": 24,      # the service sleeps this long between passes (a pass can also be triggered)
    "keep_originals_days": 7,       # originals stay in .trim-originals for undo, then are deleted
    "max_originals_gb": 150,        # cap on originals kept (they share Tubarr's 3T quota); trimming waits above it
    "io_limit_mb_s": 40,            # read-rate cap for the cut (some filesystems, e.g. ZFS, ignore ionice; this is the real I/O brake)
}

_NUM = {  # key: (min, max)
    "min_segment_seconds": (1.0, 60.0),
    "min_votes": (1.0, 1000.0),
    "min_segment_age_hours": (24.0, 24.0 * 30),
    "max_removed_fraction": (0.05, 0.40),
    "min_upload_age_hours": (24.0, 24.0 * 30),
    "min_file_age_minutes": (30.0, 24.0 * 60),
    "recheck_hours": (1.0, 24.0 * 30),
    "pass_interval_hours": (0.25, 24.0 * 7),
    "keep_originals_days": (0.0, 365.0),
    "max_originals_gb": (0.0, 3000.0),
    "io_limit_mb_s": (5.0, 2000.0),
}
_BOOL = ("enabled", "paused", "channel_default")

_LOCK = threading.RLock()


class SettingsError(ValueError):
    pass


def load():
    with _LOCK:
        s = copy.deepcopy(DEFAULTS)
        try:
            with open(config.SETTINGS_PATH, encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                s.update({k: v for k, v in stored.items() if k in DEFAULTS})
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            pass                     # unreadable file: fall back to the safe defaults (trimming OFF)
        return s


def _save(s):
    os.makedirs(config.DATA, exist_ok=True)
    tmp = config.SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, config.SETTINGS_PATH)


def validate(patch):
    """Returns a clean partial dict or raises SettingsError with one plain sentence."""
    out = {}
    for k, v in (patch or {}).items():
        if k not in DEFAULTS:
            continue                                  # unknown fields are ignored (API convention)
        if k in _BOOL:
            if not isinstance(v, bool):
                raise SettingsError("%s must be true or false." % k)
            out[k] = v
        elif k in _NUM:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise SettingsError("%s must be a number." % k)
            lo, hi = _NUM[k]
            if not lo <= float(v) <= hi:
                raise SettingsError("%s must be between %g and %g." % (k, lo, hi))
            out[k] = float(v)
        elif k == "categories":
            if not isinstance(v, list) or any(c not in CUTTABLE for c in v):
                raise SettingsError("categories must be a list drawn from: %s." % ", ".join(CUTTABLE))
            out[k] = [c for c in CUTTABLE if c in v]
        elif k == "channels":
            if not isinstance(v, dict) or any(not isinstance(x, (bool, type(None))) for x in v.values()):
                raise SettingsError("channels must map a channel id to true, false or null.")
            out[k] = v
    return out


def update(patch):
    with _LOCK:
        clean = validate(patch)
        s = load()
        if "channels" in clean:
            ch = dict(s["channels"])
            for cid, val in clean.pop("channels").items():
                if val is None:
                    ch.pop(cid, None)
                else:
                    ch[cid] = val
            s["channels"] = ch
        s.update(clean)
        _save(s)
        return s


def set_channel(channel_id, value):
    """value: True / False / None (None = follow channel_default)."""
    return update({"channels": {channel_id: value}})


def channel_setting(s, channel_id):
    """(own_setting or None, effective automatic trimming for this channel)."""
    own = s["channels"].get(channel_id)
    eff = bool(s["enabled"]) and bool(s["channel_default"] if own is None else own)
    return own, eff
