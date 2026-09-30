"""Network lines for YouTube traffic: Direct plus 0..N proxies, with modes Direct / One proxy / Failover / Rotate.

Configuration (settings.json "network", written by the web UI only; no secrets in it):
  mode             direct | single | failover | rotate
  proxies          [{id, name, scheme}]: each proxy's host/port/user/password live ENCRYPTED in the secret store
                   (vault key "proxy:<id>", a JSON of the parsed components; never the raw string the user typed)
  order            line ids, first = preferred ("direct" is a line too)
  selected         the proxy for mode single
  fail_threshold   failover: consecutive connection failures on the active line before switching
  cooldown_minutes failover: how long before the preferred line is tried again (doubles after a failed return)
  rotate_hours     rotate: take turns this often
  check_preferred  failover: one YouTube lookup through the preferred line before going back to it
  never_direct     never fall back to Direct automatically (default: on as soon as any proxy has ever been added,
                   and it stays on after the last proxy is deleted until Direct mode is picked explicitly). When
                   every allowed line is down, or none is left, the downloader PAUSES with an alert instead of
                   leaking the real IP.
  had_proxy        set when a proxy is added, cleared only by choosing mode "direct" (keeps the automatic
                   never-direct on after the last proxy is deleted)

Runtime state (/data/network_state.json) is owned by the worker; the web app only reads it.

Where the protections live (for review):
  * URL parsing/validation ............. parse_proxy_url()
  * components stored, URL rebuilt ...... _store_proxy(), _comps(), _url()
  * YouTube-only use, explicit proxies .. ytdlp_proxy(), yt_session(), yt_get() (+ _YT_HOSTS allowlist)
  * never-direct / pause instead of leak  _candidates(), active_line(), NetworkPaused
  * switch rate cap + minimum dwell ..... _may_switch(), _switch()
  * switching only between downloads .... tick(busy) is a no-op while busy (worker passes busy)
  * fixed-target, rate-limited test ..... test_line(), _TEST_URL, _test_rate_ok()
Local traffic (Plex, the PO-token sidecar, Trimarr, localhost) never uses these functions, so it can't be proxied;
YouTube traffic never falls back to environment proxy variables (trust_env=False, explicit yt-dlp `proxy`).
"""
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
from urllib.parse import quote, unquote, urlsplit

import requests

from . import audit, config, redact, settings, vault

log = logging.getLogger("tubarr.net")
STATE_PATH = os.path.join(config.DATA, "network_state.json")
SCHEMES = ("socks5h", "socks5", "http", "https")
MODES = ("direct", "single", "failover", "rotate")
MAX_URL_LEN = 512
MAX_PROXIES = 20
MAX_SWITCHES_PER_HOUR = 6
MIN_DWELL_S = 10 * 60
BOT_FLAG_S = 24 * 3600
_TEST_URL = "https://api.ipify.org"                  # the ONLY URL the test button (and the IP check) ever fetches
_TEST_TIMEOUT = 10
_TEST_PER_MIN = 5
_TEST_PAD = _TEST_TIMEOUT                           # web UI tests: every failure takes this long (no timing oracle)
_TEST_KEYS_MAX = 50                                  # rate-limit buckets kept in memory
_MAX_HOPS = 5                                        # yt_get: redirects followed, each re-checked against _YT_HOSTS
_YT_HOSTS = ("youtube.com", "youtu.be", "ytimg.com", "ggpht.com", "googlevideo.com", "googleusercontent.com",
             "youtube-nocookie.com")
_LOCK = threading.RLock()
_TESTS = {}                                          # rate-limit key -> [timestamps]
_BAD = re.compile(r"[\x00-\x20\x7f\"'`<>\\^{}|;$()\[\]]")   # control chars, whitespace, shell/URL-trick characters
DEFAULTS = {"mode": "direct", "proxies": [], "order": ["direct"], "selected": None, "fail_threshold": 3,
            "cooldown_minutes": 360, "rotate_hours": 6.0, "check_preferred": True, "never_direct": None,
            "had_proxy": False}


class NetworkPaused(Exception):
    """No allowed line is usable (e.g. never-direct is on and every proxy is down): YouTube traffic must wait."""


class TestFailed(Exception):
    pass


# ------------------------------------------------------------------ validation
def parse_proxy_url(raw):
    """Strictly parse a proxy URL -> {scheme, host, port, username, password}. Raises ValueError (generic text).

    Accepts scheme://[user[:password]@]host:port with an optional trailing "/", nothing else: no path, query,
    fragment, whitespace, control or shell characters, no second '@', IPv6 only in brackets, port 1-65535.
    """
    if not isinstance(raw, str):
        raise ValueError("Enter the proxy address as text.")
    if len(raw) > MAX_URL_LEN * 2 or any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise ValueError("The proxy address contains characters that aren't allowed.")
    s = raw.strip(" ")
    if not s or len(s) > MAX_URL_LEN:
        raise ValueError("Enter a proxy address (at most %d characters)." % MAX_URL_LEN)
    if _BAD.search(s.replace("[", "").replace("]", "")) or any(c in s for c in "\r\n\t"):
        raise ValueError("The proxy address contains characters that aren't allowed.")
    m = re.match(r"^([A-Za-z][A-Za-z0-9+.\-]*)://(.*)$", s)
    if not m:
        raise ValueError("Start the address with socks5h://, socks5://, http:// or https://.")
    scheme, rest = m.group(1).lower(), m.group(2)
    if scheme not in SCHEMES:
        raise ValueError("Only socks5h://, socks5://, http:// and https:// proxies are supported.")
    if rest.endswith("/"):
        rest = rest[:-1]
    if any(c in rest for c in "/?#"):
        raise ValueError("A proxy address can't have a path, query or fragment.")
    if rest.count("@") > 1:
        raise ValueError("The proxy address has more than one '@'. Percent-encode special characters in the "
                         "username or password (e.g. @ as %40).")
    try:
        u = urlsplit("%s://%s" % (scheme, rest))
        port = u.port
        host = u.hostname
    except ValueError:
        raise ValueError("The proxy address isn't valid (check the host and port).") from None
    if not host or port is None or not (1 <= port <= 65535):
        raise ValueError("Include the proxy's host and port, e.g. socks5h://host:1080.")
    hostpart = rest.rsplit("@", 1)[-1]
    if hostpart.startswith("["):
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise ValueError("That IPv6 address isn't valid.") from None
    elif ":" in host:
        raise ValueError("Put an IPv6 address in brackets, e.g. socks5h://[2001:db8::1]:1080.")
    else:
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            if len(host) > 253 or not re.match(r"^(?=.{1,253}$)([A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)"
                                               r"(\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*\.?$", host):
                raise ValueError("The proxy host must be a host name or an IP address.")
    user = unquote(u.username) if u.username else None
    pw = unquote(u.password) if u.password else None
    for part in (user, pw):
        if part is not None and (len(part) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in part)):
            raise ValueError("The proxy username or password contains characters that aren't allowed.")
    return {"scheme": scheme, "host": host.lower().rstrip("."), "port": port, "username": user, "password": pw}


def _url(c):
    """Rebuild the URL from validated components (percent-encoding the credentials)."""
    host = "[%s]" % c["host"] if ":" in c["host"] else c["host"]
    auth = ""
    if c.get("username") is not None:
        auth = quote(c["username"], safe="")
        if c.get("password") is not None:
            auth += ":" + quote(c["password"], safe="")
        auth += "@"
    return "%s://%s%s:%d" % (c["scheme"], auth, host, int(c["port"]))


def _masked(c):
    if not c:
        return None
    host = "[%s]" % c["host"] if ":" in c["host"] else c["host"]
    return "%s://%s%s:%d" % (c["scheme"], "***@" if c.get("username") else "", host, int(c["port"]))


def _comps(pid):
    raw = vault.get("proxy:" + pid)
    if not raw:
        return None
    try:
        c = json.loads(raw)
        return c if isinstance(c, dict) and c.get("scheme") in SCHEMES else None
    except ValueError:
        return None


def _store_proxy(pid, comps):
    vault.put("proxy:" + pid, json.dumps(comps, sort_keys=True))
    for part in (comps.get("password"), comps.get("username")):
        redact.remember(part)
    redact.remember(_url(comps))


# ------------------------------------------------------------------ configuration
def cfg():
    n = settings.overrides().get("network")
    out = json.loads(json.dumps(DEFAULTS))
    if isinstance(n, dict):
        out.update({k: v for k, v in n.items() if k in DEFAULTS})
    ids = [p["id"] for p in out["proxies"] if isinstance(p, dict) and p.get("id")]
    out["order"] = [x for x in out["order"] if x == "direct" or x in ids]
    for x in ["direct"] + ids:
        if x not in out["order"]:
            out["order"].append(x)
    return out


def never_direct(c):
    """Explicit setting, else automatic: on as soon as any proxy exists or has existed (a proxy outage, or deleting
    the last proxy, mustn't silently switch YouTube traffic to the real IP). Picking mode "direct" resets it."""
    if c["never_direct"] is not None:
        return bool(c["never_direct"])
    return bool(c["proxies"]) or bool(c.get("had_proxy"))


def _save_cfg(c):
    c = {k: c[k] for k in DEFAULTS}
    settings.save({"network": c})


def _name(c, lid):
    if lid == "direct":
        return "Direct"
    for p in c["proxies"]:
        if p["id"] == lid:
            return p["name"]
    return "(removed)"


def _candidates(c):
    """Lines the automatic modes may use, in order. Direct is excluded when never_direct is on."""
    if c["mode"] == "direct":
        return ["direct"]
    if c["mode"] == "single":
        return [c["selected"]] if c["selected"] and _comps(c["selected"]) else []
    return [x for x in c["order"] if (x != "direct" or not never_direct(c)) and (x == "direct" or _comps(x))]


# ------------------------------------------------------------------ state (worker writes, web app reads)
def state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(st):
    st["updated_at"] = time.time()
    vault._write_private(STATE_PATH, json.dumps(st, indent=1).encode())


def active_line():
    """The line YouTube traffic uses now. Raises NetworkPaused when nothing allowed is usable."""
    c = cfg()
    st = state()
    cands = _candidates(c)
    if st.get("paused") and c["mode"] != "direct":
        raise NetworkPaused(st.get("paused_reason") or "No network line is usable.")
    a = st.get("active")
    if a in cands:
        return a
    if cands:
        return cands[0]
    raise NetworkPaused("No proxy is set up for the chosen network mode.")


def line_url(lid):
    if lid == "direct":
        return None
    comps = _comps(lid)
    if not comps:
        raise NetworkPaused("That proxy is missing.")
    return _url(comps)


def ytdlp_proxy():
    """yt-dlp's `proxy` option for the active line: "" = direct (explicitly ignoring env proxies), else the URL."""
    return line_url(active_line()) or ""


def _proxies_for(lid):
    u = line_url(lid)
    return {"http": u, "https": u} if u else {}


def yt_session(lid=None):
    s = requests.Session()
    s.trust_env = False                              # environment proxy variables never apply to YouTube traffic
    s.proxies = _proxies_for(lid or active_line())
    s.headers["User-Agent"] = config.USER_AGENT
    return s


def _is_youtube(url):
    try:
        h = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return any(h == d or h.endswith("." + d) for d in _YT_HOSTS)


def yt_get(url, **kw):
    """GET a YouTube URL through the active line. Refuses non-YouTube hosts (this is not a general fetcher); redirects
    are followed by hand, at most _MAX_HOPS, and every hop must again be a YouTube host."""
    if not _is_youtube(url):
        raise ValueError("not a YouTube URL")
    kw.setdefault("timeout", 30)
    kw.pop("allow_redirects", None)
    with yt_session() as s:
        for _ in range(_MAX_HOPS + 1):
            r = s.get(url, allow_redirects=False, **kw)
            if not r.is_redirect:
                return r
            nxt = requests.compat.urljoin(url, r.headers.get("location") or "")
            r.close()
            if not _is_youtube(nxt) or not nxt.startswith("https://"):
                raise ValueError("redirected away from YouTube")
            url = nxt
            kw.pop("params", None)                       # already part of the redirect target
        raise ValueError("too many redirects")


# ------------------------------------------------------------------ the test button / IP check
def _test_rate_ok(key):
    now = time.time()
    with _LOCK:
        if len(_TESTS) > _TEST_KEYS_MAX:                 # prune idle buckets; never let the dict grow unbounded
            for k in [k for k, v in _TESTS.items() if not v or now - v[-1] >= 60]:
                del _TESTS[k]
            while len(_TESTS) > _TEST_KEYS_MAX:
                del _TESTS[next(iter(_TESTS))]
        ts = [t for t in _TESTS.get(key, []) if now - t < 60]
        if len(ts) >= _TEST_PER_MIN:
            _TESTS[key] = ts
            return False
        ts.append(now)
        _TESTS[key] = ts
        return True


TEST_FAILED = "The test failed: no answer through this line."


def test_line(lid, rate_key="system", pad=False):
    """Fetch the public IP through one line. ONLY _TEST_URL is ever requested; no redirects; 10 s timeout.
    -> {"ok": True, "ip", "latency_ms"}; raises TestFailed. For the web UI (pad=True) every failure has the same
    message and takes the full _TEST_TIMEOUT, so the button can't be used to tell a closed port from a refused
    login or a slow host (no port scanning through a proxy)."""
    if not _test_rate_ok(str(rate_key)):
        raise TestFailed("Too many tests. Wait a minute.")
    if lid != "direct" and not _comps(lid):
        raise TestFailed("That proxy doesn't exist.")
    t0 = time.monotonic()
    try:
        return _test_once(lid, t0)
    except TestFailed as e:
        if not pad:
            raise
        time.sleep(max(0.0, _TEST_PAD - (time.monotonic() - t0)))
        raise TestFailed(TEST_FAILED) from e


def _test_once(lid, t0):
    try:
        with requests.Session() as s:
            s.trust_env = False
            s.max_redirects = 0
            s.proxies = _proxies_for(lid)
            r = s.get(_TEST_URL, timeout=_TEST_TIMEOUT, allow_redirects=False, stream=True,
                      headers={"User-Agent": config.USER_AGENT, "Accept": "text/plain"})
            body = r.raw.read(64, decode_content=True) if r.status_code == 200 else b""
            r.close()
    except requests.exceptions.ProxyError as e:
        raise TestFailed("The proxy refused the login." if "407" in str(e) else "Couldn't connect through the proxy.") from None
    except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout):
        raise TestFailed("Timed out.") from None
    except Exception:
        raise TestFailed("Couldn't connect.") from None
    if r.status_code == 407:
        raise TestFailed("The proxy refused the login.")
    if r.status_code != 200:
        raise TestFailed("Couldn't connect.")
    try:
        ip = str(ipaddress.ip_address(body.decode("ascii", "strict").strip()))
    except (UnicodeDecodeError, ValueError):
        raise TestFailed("Couldn't read the IP address.") from None
    return {"ok": True, "ip": ip, "latency_ms": int((time.monotonic() - t0) * 1000)}


# ------------------------------------------------------------------ worker: failures, switching
def report(kind, detail=""):
    """Worker: the result of a YouTube request on the active line. kind: ok | conn | bot."""
    with _LOCK:
        st = state()
        try:
            lid = active_line()
        except NetworkPaused:
            return
        fails = st.setdefault("fails", {})
        if kind == "ok":
            fails[lid] = 0
            if st.get("returned_at") and time.time() - st["returned_at"] > 24 * 3600:
                st.pop("returned_at", None)
                st["preferred_backoff"] = 1
        elif kind == "conn":
            fails[lid] = int(fails.get(lid, 0)) + 1
        elif kind == "bot":
            st.setdefault("flagged", {})[lid] = time.time()
            st["want_switch"] = "YouTube bot check on %s" % _name(cfg(), lid)
            if st.get("returned_at") and time.time() - st["returned_at"] < 24 * 3600:
                st["preferred_backoff"] = min(int(st.get("preferred_backoff", 1)) * 2, 28)
        _save_state(st)


def _may_switch(st, now):
    sw = [t for t in st.get("switches", []) if now - t < 3600]
    st["switches"] = sw
    if len(sw) >= MAX_SWITCHES_PER_HOUR:
        return False
    return now - float(st.get("since_ts") or 0) >= MIN_DWELL_S or not st.get("active")


def _switch(st, to, reason, c, now):
    frm = st.get("active")
    st.update(active=to, since_ts=now, reason=reason, paused=False, paused_reason=None)
    st.setdefault("switches", []).append(now)
    st.setdefault("fails", {})[to] = 0
    if frm and frm != to:
        st["last_switch"] = {"at": now, "from_name": _name(c, frm), "to_name": _name(c, to), "reason": reason}
        audit.write("system", "network.switch", "%s -> %s: %s" % (_name(c, frm), _name(c, to), reason))
        log.warning("NETWORK %s -> %s: %s", _name(c, frm), _name(c, to), reason)
    if c["mode"] == "rotate":
        st["rotate_at"] = now + float(c["rotate_hours"]) * 3600
    st["ip"] = None
    threading.Thread(target=_ip_check, args=(to,), name="ipcheck", daemon=True).start()


def _pause(st, reason, c):
    if not st.get("paused"):
        audit.write("system", "network.paused", reason)
        log.error("NETWORK PAUSED: %s", reason)
        try:
            from . import notify
            notify.send("download_failed", "Tubarr paused downloads: %s" % reason)
        except Exception:
            pass
    st.update(paused=True, paused_reason=reason)


def _ip_check(lid):
    try:
        r = test_line(lid, rate_key="worker")
    except TestFailed:
        return
    with _LOCK:
        st = state()
        if st.get("active") == lid:
            st["ip"] = {"ip": r["ip"], "latency_ms": r["latency_ms"], "checked_at": time.time()}
            _save_state(st)


def _usable(lid, st, now):
    return now - float((st.get("flagged") or {}).get(lid, 0)) > BOT_FLAG_S


def _preferred_ok(lid):
    """One real YouTube lookup through the preferred line (failover return check)."""
    if lid != "direct" and not _comps(lid):
        return False
    try:
        import yt_dlp
        from . import ytdl
        with yt_dlp.YoutubeDL(ytdl._params(skip_download=True, proxy=line_url(lid) or "")) as y:
            y.extract_info("https://www.youtube.com/watch?v=" + os.environ.get("TUBARR_CHECK_VIDEO", "jNQXAC9IVRw"),
                           download=False, process=False)
        return True
    except Exception:
        return False


def tick(busy):
    """Worker, every few seconds: apply mode changes, failover, return-to-preferred and rotation. Never switches
    while a download is running (busy), never more than MAX_SWITCHES_PER_HOUR, never before MIN_DWELL_S."""
    if busy:
        return
    with _LOCK:
        c, st, now = cfg(), state(), time.time()
        cands = _candidates(c)
        a = st.get("active")
        changed = False
        sig = json.dumps(c, sort_keys=True)
        if st.get("cfg_sig") != sig:                         # settings changed: retry a paused line right away
            st["cfg_sig"], st["paused_retry_at"] = sig, 0
            changed = True
        if not cands:
            if c["mode"] != "direct":
                _pause(st, "No proxy is set up for the chosen network mode.", c)
            _save_state(st)
            return
        if a not in cands:                                   # first run, or the configuration changed
            ok = [x for x in cands if _usable(x, st, now)] or cands
            _switch(st, ok[0], "network settings changed", c, now)
            st["switches"] = st["switches"][:-1]              # a settings change doesn't count against the cap
            _save_state(st)
            return
        fails = int((st.get("fails") or {}).get(a, 0))
        want = st.pop("want_switch", None)
        if c["mode"] in ("failover", "single", "rotate") and (fails >= int(c["fail_threshold"]) or want):
            reason = want or "%d connection failures in a row on %s" % (fails, _name(c, a))
            nxt = [x for x in cands if x != a and _usable(x, st, now)] if c["mode"] != "single" else []
            if nxt and _may_switch(st, now):
                _switch(st, nxt[0], reason, c, now)
                if cands[0] != nxt[0]:
                    st["preferred_retry_at"] = now + float(c["cooldown_minutes"]) * 60 * int(st.get("preferred_backoff", 1))
                changed = True
            elif not nxt and (never_direct(c) or c["mode"] == "single"):
                _pause(st, "%s. No other allowed line is available." % reason, c)
                changed = True
            elif not nxt:
                changed = True                                # nothing to switch to; keep going on this line
            else:
                st["want_switch"] = want or reason            # switch cap reached: try again later
        elif st.get("paused") and c["mode"] != "direct":
            if now >= float(st.get("paused_retry_at") or 0):
                st["paused_retry_at"] = now + 600
                for x in cands:
                    try:
                        test_line(x, rate_key="worker")
                    except TestFailed:
                        continue
                    _switch(st, x, "line reachable again", c, now)
                    break
                changed = True
        elif c["mode"] == "failover" and a != cands[0] and now >= float(st.get("preferred_retry_at") or 0):
            pref = cands[0]
            if _usable(pref, st, now) and _may_switch(st, now) and (not c["check_preferred"] or _preferred_ok(pref)):
                _switch(st, pref, "the preferred line works again", c, now)
                st["returned_at"] = now
            else:
                st["preferred_retry_at"] = now + float(c["cooldown_minutes"]) * 60 * int(st.get("preferred_backoff", 1))
            changed = True
        elif c["mode"] == "rotate" and len(cands) > 1 and now >= float(st.get("rotate_at") or now + 1):
            nxt = [x for x in cands[cands.index(a) + 1:] + cands[:cands.index(a)] if _usable(x, st, now)]
            if nxt and _may_switch(st, now):
                _switch(st, nxt[0], "rotation (every %g h)" % float(c["rotate_hours"]), c, now)
            else:
                st["rotate_at"] = now + 600
            changed = True
        elif c["mode"] == "rotate" and not st.get("rotate_at"):
            st["rotate_at"] = now + float(c["rotate_hours"]) * 3600
            changed = True
        if changed or now - float(st.get("updated_at") or 0) > 60:
            _save_state(st)


def blocked():
    try:
        active_line()
        return None
    except NetworkPaused as e:
        return str(e)


# ------------------------------------------------------------------ web UI (admin session only for changes)
def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(float(t))) if t else None


def status():
    c, st = cfg(), state()
    try:
        a = active_line()
    except NetworkPaused:
        a = None
    ip = st.get("ip") if st.get("active") == a else None
    ls = st.get("last_switch")
    return {"active": a, "active_name": _name(c, a) if a else "Paused",
            "ip": (ip or {}).get("ip"), "latency_ms": (ip or {}).get("latency_ms"),
            "since": _iso(st.get("since_ts")), "reason": st.get("paused_reason") if st.get("paused") else st.get("reason"),
            "paused": bool(st.get("paused")) and c["mode"] != "direct",
            "last_switch": dict(ls, at=_iso(ls["at"])) if ls else None,
            "next_rotate_at": _iso(st.get("rotate_at")) if c["mode"] == "rotate" else None,
            "preferred_retry_at": _iso(st.get("preferred_retry_at")) if c["mode"] == "failover" and a != (_candidates(c) or [None])[0] else None,
            "updated_at": _iso(st.get("updated_at")),
            "stale": not st.get("updated_at") or time.time() - float(st["updated_at"]) > 180}


def public(full=True):
    """The GET /api/network shape. full=False (API key): status only, no proxy details and no public IP."""
    if not full:
        st = status()
        st.pop("ip", None)
        return {"status": st}
    c = cfg()
    return {"mode": c["mode"], "order": c["order"], "selected": c["selected"],
            "fail_threshold": c["fail_threshold"], "cooldown_minutes": c["cooldown_minutes"],
            "rotate_hours": c["rotate_hours"], "check_preferred": bool(c["check_preferred"]),
            "never_direct": never_direct(c), "never_direct_auto": c["never_direct"] is None,
            "proxies": [{"id": p["id"], "name": p["name"], "scheme": p.get("scheme"), "masked": _masked(_comps(p["id"]))}
                        for p in c["proxies"]],
            "status": status()}


def _clean_name(name):
    name = (name or "").strip()
    if not name or len(name) > 60 or any(ord(ch) < 32 for ch in name):
        raise ValueError("Give the proxy a name (1 to 60 characters).")
    return name


def add_proxy(name, url, who):
    name = _clean_name(name)
    comps = parse_proxy_url(url)
    with _LOCK:
        c = cfg()
        if len(c["proxies"]) >= MAX_PROXIES:
            raise ValueError("At most %d proxies." % MAX_PROXIES)
        pid = "p_" + secrets.token_hex(4)
        _store_proxy(pid, comps)
        c["proxies"].append({"id": pid, "name": name, "scheme": comps["scheme"]})
        c["order"].append(pid)
        c["had_proxy"] = True
        _save_cfg(c)
    audit.write(who, "network.proxy_added", "proxy '%s' (%s)" % (name, comps["scheme"]))
    return public()


def update_proxy(pid, name, url, who):
    with _LOCK:
        c = cfg()
        p = next((x for x in c["proxies"] if x["id"] == pid), None)
        if not p:
            raise KeyError(pid)
        changes = []
        if name is not None and name != p["name"]:
            new = _clean_name(name)
            changes.append("renamed '%s' -> '%s'" % (p["name"], new))
            p["name"] = new
        if url:
            comps = parse_proxy_url(url)
            _store_proxy(pid, comps)
            p["scheme"] = comps["scheme"]
            changes.append("address changed")
        _save_cfg(c)
    if changes:
        audit.write(who, "network.proxy_changed", "proxy '%s': %s" % (p["name"], "; ".join(changes)))
    return public()


def delete_proxy(pid, who):
    with _LOCK:
        c = cfg()
        p = next((x for x in c["proxies"] if x["id"] == pid), None)
        if not p:
            raise KeyError(pid)
        c["proxies"] = [x for x in c["proxies"] if x["id"] != pid]
        c["order"] = [x for x in c["order"] if x != pid]
        if c["selected"] == pid:
            c["selected"] = None
        _save_cfg(c)
        vault.put("proxy:" + pid, "")
    audit.write(who, "network.proxy_deleted", "proxy '%s'" % p["name"])
    return public()


def patch(fields, who):
    with _LOCK:
        c = cfg()
        before = {k: c[k] for k in ("mode", "selected", "order", "fail_threshold", "cooldown_minutes", "rotate_hours",
                                    "check_preferred", "never_direct")}
        ids = [p["id"] for p in c["proxies"]]
        if "mode" in fields:
            if fields["mode"] not in MODES:
                raise ValueError("Pick one of: direct, single, failover, rotate.")
            c["mode"] = fields["mode"]
            if c["mode"] == "direct":
                c["had_proxy"] = False                   # an explicit choice of Direct ends the automatic guard
        if "selected" in fields:
            if fields["selected"] not in ids + [None]:
                raise ValueError("That proxy doesn't exist.")
            c["selected"] = fields["selected"]
        if "order" in fields:
            o = fields["order"]
            if not isinstance(o, list) or sorted(o) != sorted(["direct"] + ids):
                raise ValueError("The order must list every line exactly once.")
            c["order"] = o
        for k, lo, hi, cast in (("fail_threshold", 1, 20, int), ("cooldown_minutes", 5, 10080, int),
                                ("rotate_hours", 0.5, 168, float)):
            if k in fields:
                try:
                    v = cast(fields[k])
                except (TypeError, ValueError):
                    raise ValueError("%s must be a number." % k) from None
                if not lo <= v <= hi:
                    raise ValueError("%s must be between %s and %s." % (k, lo, hi))
                c[k] = v
        for k in ("check_preferred", "never_direct"):
            if k in fields:
                if not isinstance(fields[k], bool):
                    raise ValueError("%s must be true or false." % k)
                c[k] = fields[k]
        if c["mode"] == "single" and not c["selected"]:
            raise ValueError("Pick the proxy to use.")
        if c["mode"] in ("failover", "rotate") and not ids:
            raise ValueError("Add a proxy first.")
        _save_cfg(c)
        after = {k: c[k] for k in before}
    diff = ["%s: %s -> %s" % (k, before[k], after[k]) for k in before if before[k] != after[k]]
    if diff:
        names = {p["id"]: p["name"] for p in c["proxies"]}
        names["direct"] = "Direct"
        text = "; ".join(diff)
        for pid, nm in names.items():
            text = text.replace(pid, nm)
        audit.write(who, "network.settings", text)
    return public()
