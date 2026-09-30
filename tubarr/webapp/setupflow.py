"""First-run setup: connect Plex (Sign in with Plex, or address + token), pick the library, finish.

Sign in with Plex uses plex.tv's documented PIN flow: create a PIN, the person approves it at app.plex.tv/auth (or
enters the 4-character code at plex.tv/link), Tubarr polls the PIN until it carries a token. That plex.tv account
token is only kept in memory (in _PINS, for 15 minutes) and never sent back to the browser. When the person picks
one of their servers, only that server's own access token is stored (encrypted, vault.py), and the account token is
dropped.

The stored token belongs to one server: changing the address to a different origin (scheme, host or port) wipes it,
unless the new address is one of the connections Sign in with Plex found for that server (then that server's token
is stored). Plex requests never follow redirects (plexhttp.py), so the token can't be walked to another host.
"""
import hashlib
import logging
import threading
import time
from urllib.parse import quote, urlsplit

import requests

from .. import audit, config, plexhttp, settings, vault
from . import plexinfo

log = logging.getLogger("tubarr.webapp.setup")
PLEX_TV = "https://plex.tv"
PRODUCT = "Tubarr"
_PINS = {}                       # pin id -> {"code", "expires", "servers": [...], "token": str|None}
_LOCK = threading.Lock()


class SetupError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def client_id():
    """A stable, non-secret client identifier for this install (derived from the install key)."""
    return "tubarr-" + hashlib.sha256(vault.derived_key("plex-client-id")).hexdigest()[:24]


def _headers(token=None):
    h = {"Accept": "application/json", "X-Plex-Product": PRODUCT, "X-Plex-Client-Identifier": client_id(),
         "X-Plex-Device-Name": "Tubarr", "X-Plex-Platform": "Web"}
    if token:
        h["X-Plex-Token"] = token
    return h


def state(snap_channels=0, pending=0):
    p = plexinfo.status(fresh=False) if config.plex_enabled() else None
    s = settings.load()
    ov = settings.overrides()
    plex = {"configured": config.plex_enabled(), "url": config.PLEX_URL or None, "token_set": vault.has("plex_token"),
            "library": config.PLEX_SECTION if config.plex_enabled() else None,
            "plex_root": ov.get("plex_root") or None,
            "state": (p or {}).get("state", "not_configured") if p else "not_configured",
            "server_name": (p or {}).get("server_name"), "message": (p or {}).get("message")}
    return {"done": bool(s.get("setup_done")), "plex": plex, "channels": snap_channels,
            "subscriptions_pending": pending, "media_path": config.ROOT}


# ------------------------------------------------------------------ Sign in with Plex (PIN)
def start_pin():
    try:
        r = plexhttp.post(PLEX_TV + "/api/v2/pins", headers=_headers(), data={"strong": "false"}, timeout=15)
        r.raise_for_status()
        j = r.json()
    except Exception as e:
        raise SetupError(502, "internal", "plex.tv didn't answer (%s). Try again, or enter the address and a token "
                                          "by hand." % type(e).__name__) from None
    pid, code = int(j["id"]), j["code"]
    with _LOCK:
        now = time.time()
        for k in [k for k, v in _PINS.items() if v["expires"] < now - 600]:
            del _PINS[k]
        _PINS[pid] = {"code": code, "expires": now + 900, "servers": None, "token": None, "resources": []}
    url = "https://app.plex.tv/auth#?clientID=%s&code=%s&context%%5Bdevice%%5D%%5Bproduct%%5D=%s" % (
        quote(client_id()), quote(code), quote(PRODUCT))
    return {"pin_id": pid, "code": code, "auth_url": url, "link_url": "https://plex.tv/link",
            "expires_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() + 900))}


def _servers(token):
    r = plexhttp.get(PLEX_TV + "/api/v2/resources", headers=_headers(token),
                     params={"includeHttps": 1, "includeRelay": 1}, timeout=20)
    r.raise_for_status()
    out, res = [], []
    for x in r.json():
        if "server" not in (x.get("provides") or ""):
            continue
        conns = [{"uri": c.get("uri"), "local": bool(c.get("local")), "relay": bool(c.get("relay"))}
                 for c in x.get("connections") or [] if c.get("uri")]
        conns.sort(key=lambda c: (c["relay"], not c["local"]))
        out.append({"name": x.get("name"), "owned": bool(x.get("owned")), "connections": conns})
        res.append({"uris": [c["uri"] for c in conns], "token": x.get("accessToken")})
    return out, res


def poll_pin(pid):
    with _LOCK:
        p = _PINS.get(pid)
    if not p:
        return {"state": "expired", "servers": []}
    if p["servers"] is not None:
        return {"state": "authorized", "servers": p["servers"]}
    if time.time() > p["expires"]:
        return {"state": "expired", "servers": []}
    try:
        r = plexhttp.get(PLEX_TV + "/api/v2/pins/%d" % pid, headers=_headers(), timeout=15)
        r.raise_for_status()
        token = r.json().get("authToken")
    except Exception:
        return {"state": "waiting", "servers": []}
    if not token:
        return {"state": "waiting", "servers": []}
    audit.write("setup", "plex.signed_in", "Sign in with Plex approved")
    try:
        servers, res = _servers(token)
    except Exception:
        servers, res = [], []
    with _LOCK:
        p.update(servers=servers, resources=res, token=token)       # memory only, until a server is chosen
    return {"state": "authorized", "servers": servers}


def _server_token_for(url):
    """If the URL is a connection of a server found through Sign in with Plex: that server's own access token (or,
    only if plex.tv gave none, the account token of that sign-in). Else None."""
    with _LOCK:
        for p in _PINS.values():
            for r in p.get("resources") or []:
                if url.rstrip("/") in [u.rstrip("/") for u in r["uris"]]:
                    return r.get("token") or p.get("token")
    return None


def _forget_account_tokens():
    with _LOCK:
        for p in _PINS.values():
            p["token"] = None
            for r in p.get("resources") or []:
                r["token"] = None


def _origin(url):
    try:
        u = urlsplit((url or "").strip())
        return (u.scheme.lower(), (u.hostname or "").lower(), u.port or {"http": 80, "https": 443}.get(u.scheme.lower()))
    except ValueError:
        return None


# ------------------------------------------------------------------ address, library
def _check_url(url):
    url = (url or "").strip().rstrip("/")
    try:
        u = urlsplit(url)
        ok = (u.scheme in ("http", "https") and bool(u.hostname) and not u.username and not u.password
              and u.path in ("", "/") and "?" not in url and "#" not in url)   # just scheme://host[:port]
        u.port                                          # raises ValueError for a bad port
    except ValueError:
        ok = False
    if not ok:
        raise SetupError(400, "bad_request", "Use Plex's address like http://192.0.2.10:32400 (http or https).")
    return url


def save_plex(url, token=None):
    url = _check_url(url)
    scoped = _server_token_for(url)
    if token:
        vault.put("plex_token", token.strip())
    elif scoped:
        vault.put("plex_token", scoped)
        _forget_account_tokens()                    # the server token is all Tubarr keeps
    elif _origin(url) != _origin(config.PLEX_URL) and vault.has("plex_token"):
        vault.put("plex_token", "")                 # a token never follows the address to another host
        log.warning("Plex address changed to a different server: the saved Plex token was removed")
        audit.write("setup", "plex.token_cleared", "the Plex address moved to a different host")
    settings.save({"plex_url": url})
    st = plexinfo.status(fresh=True)
    out = dict(state_plex(st), libraries=libraries() if st.get("state") == "connected" else [])
    return out


def state_plex(st=None):
    return state()["plex"] if st is None else dict(state()["plex"], state=st.get("state"),
                                                   server_name=st.get("server_name"), message=st.get("message"))


def libraries():
    if not config.plex_enabled():
        raise SetupError(409, "conflict", "Connect Plex first.")
    try:
        r = plexhttp.get(config.PLEX_URL + "/library/sections",
                         headers={"Accept": "application/json", "X-Plex-Token": config.PLEX_TOKEN}, timeout=15)
        r.raise_for_status()
        dirs = r.json().get("MediaContainer", {}).get("Directory") or []
    except Exception as e:
        raise SetupError(502, "internal", "Plex didn't list its libraries (%s)." % type(e).__name__) from None
    return [{"key": str(d.get("key")), "title": d.get("title"), "agent": d.get("agent"),
             "agent_label": plexinfo.AGENTS.get(d.get("agent"), d.get("agent")),
             "paths": [loc.get("path") for loc in d.get("Location") or [] if loc.get("path")]}
            for d in dirs if d.get("type") == "show"]


def save_library(title, plex_root):
    title = (title or "").strip()
    if not title or len(title) > 200:
        raise SetupError(400, "bad_request", "Pick a library.")
    root = (plex_root or "").strip().rstrip("/") or None
    if root and not root.startswith("/") and not (len(root) > 2 and root[1] == ":"):
        raise SetupError(400, "bad_request", "The folder must be a full path, as Plex sees it.")
    settings.save({"plex_section": title, "plex_root": root or config.ROOT})
    return state_plex(plexinfo.status(fresh=True))


def clear_plex():
    vault.put("plex_token", "")
    settings.save({"plex_url": ""})
    plexinfo.status(fresh=True)
    return state_plex()


def complete():
    settings.save({"setup_done": True})
    return {"done": True}
