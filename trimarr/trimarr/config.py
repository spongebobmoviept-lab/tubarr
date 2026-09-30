"""Fixed paths and endpoints (from the environment). Adjustable settings live in settings.py."""
import os
import re

from . import __version__ as VERSION

os.umask(0o002)                                                 # same file modes as Tubarr's (664 / 775)

ROOT = os.environ.get("TRIMARR_ROOT", "/youtube")              # the library as this container sees it (same as Tubarr)
PLEX_ROOT = os.environ.get("PLEX_ROOT", "") or ROOT              # the same folder as Plex sees it (only to accept Plex paths on the CLI)
DATA = os.environ.get("TRIMARR_DATA", "/data")

WORK = os.path.join(ROOT, ".trim-work")                         # same dataset as the library, so the swap is one rename
ORIGINALS = os.path.join(ROOT, ".trim-originals")               # untouched originals, kept for undo
DB_PATH = os.path.join(DATA, "trimarr.db")
SETTINGS_PATH = os.path.join(DATA, "settings.json")
LOG_DIR = os.path.join(DATA, "logs")
LOCK_PATH = os.path.join(DATA, ".job.lock")                     # one trim/undo at a time, also across containers

SPONSORBLOCK_API = os.environ.get("SPONSORBLOCK_API", "https://sponsor.ajay.app").rstrip("/")
USER_AGENT = "Trimarr/%s (self-hosted)" % VERSION

TZ = os.environ.get("TZ", "UTC")

API_HOST = os.environ.get("TRIMARR_HOST", "0.0.0.0")
API_PORT = int(os.environ.get("TRIMARR_PORT", "8791"))
MIN_TOKEN_LEN = 16
DEFAULT_ALLOWED_HOSTS = ("trimarr", "localhost", "127.0.0.1")


LINK_DIR = os.environ.get("TRIMARR_LINK_DIR", "/link")         # shared with Tubarr (read-only here), see below
LINK_TOKEN = "trimarr.token"
_TOKEN_RE = re.compile(r"[!-~]{%d,512}" % MIN_TOKEN_LEN)


def link_token():
    """The token Tubarr created in the shared trimarr-link volume (/link/trimarr.token), or ''."""
    try:
        fd = os.open(os.path.join(LINK_DIR, LINK_TOKEN), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return ""
    try:
        raw = os.read(fd, 520)
    except OSError:
        return ""
    finally:
        os.close(fd)
    tok = raw.decode("ascii", "replace").strip()
    return tok if _TOKEN_RE.fullmatch(tok) else ""


def api_token():
    """The shared secret Tubarr sends as X-Trimarr-Token: TRIMARR_TOKEN if set (an override for advanced setups),
    otherwise the link token Tubarr creates automatically. Read live, so a token that appears (or changes) after
    start is picked up without a restart. Never logged or returned."""
    env = (os.environ.get("TRIMARR_TOKEN") or "").strip()
    return env if env else link_token()


def token_source():
    return "TRIMARR_TOKEN" if (os.environ.get("TRIMARR_TOKEN") or "").strip() else "link"


def allowed_hosts():
    """Host names the API answers to (DNS-rebinding defense): the defaults plus TRIMARR_ALLOWED_HOSTS (comma list)."""
    extra = (os.environ.get("TRIMARR_ALLOWED_HOSTS") or "").split(",")
    return set(DEFAULT_ALLOWED_HOSTS) | {h.strip().lower().rstrip(".") for h in extra if h.strip()}


VIDEO_EXTS = (".mkv", ".mp4")
