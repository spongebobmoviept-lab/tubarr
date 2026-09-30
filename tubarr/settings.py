"""Settings changed from the web page (Settings, first-run setup), kept in /data/settings.json.

The file only holds what was changed; anything missing falls back to the container's environment value (the
defaults below), so deleting the file restores the env behaviour. Both processes read it live:
  * the worker re-reads it every few seconds (worker.settings_watcher) -> daily cap, gap between downloads, fill
    target, protected newest, quality apply without a restart;
  * the web app writes it (actions.update_settings, the setup routes) and shows it (views.settings).
Never stored here: secrets. The Plex token, proxy credentials and the Discord webhook link are encrypted in
/data/credentials.json (vault.py); load() fills notifications.discord_webhook_url in from there for internal use only
(the web API never returns it). The file is written with mode 600.
"""
import json
import os
import threading

from . import config, vault

PATH = os.path.join(config.DATA, "settings.json")
WEBHOOK_KEY = "discord_webhook"                  # vault name of the Discord webhook link (a secret: anyone can post)
_LOCK = threading.Lock()
_CACHE = {"mtime": None, "data": {}}


def _env(name, default, cast):
    try:
        return cast(os.environ.get(name, default))
    except (TypeError, ValueError):
        return cast(default)


def _flag(name, default):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def defaults():
    """The env values (unchanged by the web page)."""
    return {
        "nightly_cap": _env("TUBARR_DAILY_CAP", os.environ.get("TUBARR_NIGHTLY_CAP", "40"), int),   # backlog downloads per 24 h
        "min_gap_min_h": _env("TUBARR_GAP_MIN_MINUTES", "10", float) / 60.0,   # random pause between downloads
        "min_gap_max_h": _env("TUBARR_GAP_MAX_MINUTES", "20", float) / 60.0,
        "soft_cap_gb": _env("TUBARR_FILL_TARGET_GB", os.environ.get("TUBARR_SOFT_CAP_GB", "1000"), float),
        "protect_newest": _env("TUBARR_PROTECT_NEWEST", "3", int),
        "max_height": _env("TUBARR_MAX_HEIGHT", "2160", int),                   # 2160 = 4K, 1080, 720 ...
        "allow_av1": _flag("TUBARR_ALLOW_AV1", "0"),                            # off: never download AV1
        "setup_done": False,
        "notifications": {"discord_webhook_url": "",
                          "events": {"download_failed": True, "channel_changes": True, "signin_needed": True,
                                     "storage_warning": True, "daily_summary": False, "each_download": False}},
    }


def overrides():
    """What was changed from the web page (the file), cached by mtime."""
    with _LOCK:
        try:
            m = os.stat(PATH).st_mtime_ns
            if m != _CACHE["mtime"]:
                with open(PATH, encoding="utf-8") as f:
                    d = json.load(f)
                _CACHE["data"], _CACHE["mtime"] = d if isinstance(d, dict) else {}, m
        except (OSError, ValueError):
            _CACHE["data"], _CACHE["mtime"] = {}, None
        return dict(_CACHE["data"])


def load():
    """Effective settings: env defaults with the web page's changes on top."""
    out = defaults()
    for k, v in overrides().items():
        if k == "notifications" and isinstance(v, dict):
            n = out["notifications"]
            if isinstance(v.get("discord_webhook_url"), str):         # an older settings.json (moved on next save)
                n["discord_webhook_url"] = v["discord_webhook_url"]
            if isinstance(v.get("events"), dict):
                n["events"].update({e: bool(x) for e, x in v["events"].items() if e in n["events"]})
        elif k in out:
            out[k] = v
    hook = vault.get(WEBHOOK_KEY)
    if hook:
        out["notifications"]["discord_webhook_url"] = hook
    return out


def get(key):
    return load()[key]


def save(changes):
    """Merge `changes` (already validated) into the file. Values equal to the env default are dropped, so the file
    only ever lists real changes."""
    with _LOCK:
        cur = {}
        try:
            with open(PATH, encoding="utf-8") as f:
                cur = json.load(f)
            if not isinstance(cur, dict):
                cur = {}
        except (OSError, ValueError):
            pass
        base = defaults()
        for k, v in changes.items():
            if k != "notifications" and base.get(k) == v:
                cur.pop(k, None)
            elif k == "notifications" and isinstance(v, dict):
                if "discord_webhook_url" in v:                    # the secret goes to the vault, never this file
                    vault.put(WEBHOOK_KEY, (v.get("discord_webhook_url") or "").strip())
                cur[k] = dict(cur.get(k) if isinstance(cur.get(k), dict) else {},
                              **{kk: vv for kk, vv in v.items() if kk != "discord_webhook_url"})
            else:
                cur[k] = v
        if isinstance(cur.get("notifications"), dict):
            cur["notifications"].pop("discord_webhook_url", None)
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        vault._write_private(PATH, json.dumps(cur, indent=1, sort_keys=True).encode())
        _CACHE["mtime"] = None
