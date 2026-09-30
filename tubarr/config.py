"""Runtime configuration: paths from the environment, Plex connection from the web setup. Nothing secret is logged.

Plex is optional. Its address, library and path mapping are saved by the first-run setup in /data/settings.json
(falling back to the PLEX_URL / PLEX_SECTION / PLEX_ROOT environment variables); the token only ever lives
encrypted in /data/credentials.json (see vault.py). `config.PLEX_URL`, `config.PLEX_TOKEN`, `config.PLEX_SECTION`
and `config.PLEX_ROOT` are looked up live on every access (module __getattr__), so a change made in the web UI
reaches the worker process within seconds, without a restart.
"""
import os

ROOT = os.environ.get("TUBARR_ROOT", "/youtube")             # the library folder inside the container
DATA = os.environ.get("TUBARR_DATA", "/data")                # DB, settings, logs, caches, secret.key
STAGING = os.path.join(ROOT, ".staging")                     # same filesystem as ROOT, so publishing is one rename
SCRATCH = os.environ.get("TUBARR_SCRATCH", os.path.join(STAGING, "_work"))   # download work area
MIN_FREE_BYTES = float(os.environ.get("TUBARR_MIN_FREE_GB", "30")) * 1e9      # don't start a download below this

TZ = os.environ.get("TZ", "UTC")                             # upload dates are shown in this time zone
MAX_HEIGHT = 1080
RATE_LIMIT = int(os.environ.get("TUBARR_RATELIMIT", "0") or 0)   # bytes/s per download; 0 = no limit

SPONSORBLOCK_API = "https://sponsor.ajay.app"
SB_CUT = ()    # Tubarr never cuts anything: videos stay exactly as uploaded (ad trimming is Trimarr's optional job)
SB_MARK = ()   # and no SponsorBlock chapters either
SB_ALL = SB_CUT + SB_MARK + ("preview", "filler", "music_offtopic", "poi_highlight", "hook")

USER_AGENT = "Tubarr/0.1 (self-hosted)"
BGUTIL_URL = os.environ.get("TUBARR_POT_PROVIDER_URL", "http://bgutil-provider:4416")   # PO-token sidecar

_ENV_PLEX = {"PLEX_URL": ("plex_url", ""), "PLEX_SECTION": ("plex_section", "YouTube"),
             "PLEX_ROOT": ("plex_root", "")}


def _plex_value(name):
    from . import settings
    key, default = _ENV_PLEX[name]
    v = settings.overrides().get(key)
    if v is None:
        v = os.environ.get(name, default)
    v = (v or "").strip()
    if name == "PLEX_URL":
        v = v.rstrip("/")
    if name == "PLEX_ROOT" and not v:
        v = ROOT                                             # same path in both containers unless told otherwise
    return v


def __getattr__(name):
    if name in _ENV_PLEX:
        return _plex_value(name)
    if name == "PLEX_TOKEN":
        from . import vault
        return vault.get("plex_token") or ""
    raise AttributeError(name)


def plex_enabled():
    """True when a Plex server and token are configured (everything Plex-related is skipped otherwise)."""
    return bool(_plex_value("PLEX_URL")) and bool(__getattr__("PLEX_TOKEN"))
