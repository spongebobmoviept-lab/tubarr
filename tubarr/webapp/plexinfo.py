"""Plex status for Settings -> Plex (read-only: identity, the YouTube section, counts). Cached for 60 s."""
import logging
import threading
import time

import requests

from .. import config, plexhttp
from . import store
from .mapping import PlexLinks

log = logging.getLogger("tubarr.webapp.plex")
AGENTS = {"tv.plex.agents.nfo.series": "Plex NFO Series (local files only)",
          "tv.plex.agents.series": "Plex Series", "com.plexapp.agents.none": "Personal Media Shows"}
SCANNERS = {"Plex TV Series": "Plex TV Series"}
_CACHE = {"at": 0.0, "data": None}
_LOCK = threading.Lock()


def _get(path, token=True, **params):
    h = {"Accept": "application/json"}
    if token and config.PLEX_TOKEN:
        h["X-Plex-Token"] = config.PLEX_TOKEN
    r = plexhttp.get(config.PLEX_URL.rstrip("/") + path, headers=h, params=params, timeout=6)
    r.raise_for_status()
    return r.json().get("MediaContainer") or {}


def _count(key, kind):
    mc = _get("/library/sections/%s/all" % key, type=kind, **{"X-Plex-Container-Start": 0, "X-Plex-Container-Size": 0})
    return mc.get("totalSize", mc.get("size"))


def check():
    out = {"state": "unreachable", "url": config.PLEX_URL or None, "server_name": None, "version": None,
           "checked_at": store.now_iso(), "message": None, "library": None}
    if not config.PLEX_URL:
        out.update(state="not_configured", message="Plex isn't connected. Connect it in Settings -> Plex (Run setup).")
        return out
    try:
        ident = _get("/identity", token=False)
        PlexLinks.machine_id = ident.get("machineIdentifier") or PlexLinks.machine_id
        out["version"] = ident.get("version")
    except Exception as e:
        out["message"] = "Tubarr can't reach Plex at %s (%s)." % (config.PLEX_URL, type(e).__name__)
        return out
    if not config.PLEX_TOKEN:
        out.update(state="unauthorized", message="No Plex token is saved. Sign in with Plex again in the setup.")
        return out
    try:
        root = _get("/")
        out["server_name"] = root.get("friendlyName")
        secs = _get("/library/sections").get("Directory") or []
        sec = next((s for s in secs if s.get("title") == config.PLEX_SECTION), None)
        out["state"] = "connected"
        if sec is None:
            out["library"] = {"state": "missing", "name": config.PLEX_SECTION}
            out["message"] = "Plex has no library called \"%s\"." % config.PLEX_SECTION
            return out
        key = sec.get("key")
        loc = (sec.get("Location") or [{}])[0].get("path")
        out["library"] = {
            "state": "scanning" if sec.get("refreshing") else "ready", "name": sec.get("title"), "section_id": int(key),
            "path": loc, "agent": AGENTS.get(sec.get("agent"), sec.get("agent")),
            "scanner": SCANNERS.get(sec.get("scanner"), sec.get("scanner")),
            "show_count": _count(key, 2), "episode_count": _count(key, 4),
            "last_scan_at": store.iso(float(sec["scannedAt"])) if sec.get("scannedAt") else None,
            "shared_with_friends": None, "home_users_with_access": None,
            "fields_locked": False,
        }
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else None
        if code == 401:
            out.update(state="unauthorized", message="Plex refused Tubarr's token (HTTP 401).")
        else:
            out.update(state="unreachable", message="Plex answered HTTP %s." % code)
    except Exception as e:
        out.update(state="unreachable", message="Plex didn't answer properly (%s)." % type(e).__name__)
    return out


def status(fresh=False):
    with _LOCK:
        if fresh or _CACHE["data"] is None or time.time() - _CACHE["at"] > 60:
            _CACHE["data"], _CACHE["at"] = check(), time.time()
        return _CACHE["data"]


def warm():
    """Background: learn Plex's machine id early so 'Open in Plex' links work from the first page."""
    try:
        status()
    except Exception as e:
        log.info("plex check at start-up failed: %s", e)
