"""Web UI accounts: one admin, argon2id password hash, signed server-side sessions, CSRF tokens, an API key.

Stored in /data/auth.json (mode 600):
  username, password_hash (argon2id, never the password), sessions {id: {created, seen}}, api_key_hash (SHA-256 of a
  random 32-byte key; the key itself is shown once and never stored), api_key_created, api_key_hint (last 4).

Session cookie `tubarr_session` (HttpOnly, SameSite=Lax, Secure over https): an itsdangerous-signed payload
{session id, CSRF token}, signed with a key derived from the per-install secret.key. A cookie is only valid while its
session id is still listed in auth.json, so logging out removes that one session and a password change removes
every session. The CSRF token also goes out in the readable cookie `tubarr_csrf`; every state-changing request made
with the session cookie must echo it in the X-CSRF-Token header.

First run: while no admin exists the web app prints a one-time setup code to the container log and the setup form
must quote it (so nobody else on the network can claim a fresh install). For unattended installs the admin can be
seeded from TUBARR_ADMIN_USER + TUBARR_ADMIN_PASSWORD_HASH (an argon2id hash from `python -m tubarr.hashpw`).
"""
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import config, vault

log = logging.getLogger("tubarr.auth")
PATH = os.path.join(config.DATA, "auth.json")
SESSION_COOKIE = "tubarr_session"
CSRF_COOKIE = "tubarr_csrf"
SESSION_DAYS = float(os.environ.get("TUBARR_SESSION_DAYS", "30"))
MIN_PASSWORD = 10
MAX_SESSIONS = 20
_PH = PasswordHasher()                           # argon2-cffi defaults: Argon2id, RFC 9106 low-memory profile
_LOCK = threading.RLock()
_CACHE = {"mtime": None, "data": {}}
_SETUP = {"code": None}


def _eq(a, b):
    """Constant-time compare of two strings as UTF-8 bytes (hmac.compare_digest refuses non-ASCII str)."""
    return hmac.compare_digest(str(a).encode("utf-8", "surrogatepass"), str(b).encode("utf-8", "surrogatepass"))


class AuthError(Exception):
    def __init__(self, status, code, message, retry_after=None):
        super().__init__(message)
        self.status, self.code, self.message, self.retry_after = status, code, message, retry_after


# ------------------------------------------------------------------ storage
def _load():
    try:
        m = os.stat(PATH).st_mtime_ns
    except OSError:
        _CACHE.update(mtime=None, data={})
        return {}
    if m != _CACHE["mtime"]:
        try:
            with open(PATH, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        _CACHE.update(mtime=m, data=d if isinstance(d, dict) else {})
    return _CACHE["data"]


def _save(d):
    vault._write_private(PATH, json.dumps(d, indent=1, sort_keys=True).encode())
    _CACHE["mtime"] = None


def _data():
    with _LOCK:
        return json.loads(json.dumps(_load()))


# ------------------------------------------------------------------ first run
def has_admin():
    d = _load()
    return bool(d.get("username") and d.get("password_hash"))


def seed_from_env():
    """Create the admin from TUBARR_ADMIN_USER / TUBARR_ADMIN_PASSWORD_HASH if none exists yet."""
    h = (os.environ.get("TUBARR_ADMIN_PASSWORD_HASH") or "").strip()
    if not h or has_admin():
        return False
    if not h.startswith("$argon2id$"):
        log.error("TUBARR_ADMIN_PASSWORD_HASH is not an argon2id hash (make one with: python -m tubarr.hashpw); ignored")
        return False
    user = (os.environ.get("TUBARR_ADMIN_USER") or "admin").strip() or "admin"
    with _LOCK:
        d = _data()
        d.update(username=user, password_hash=h, sessions={}, created_at=int(time.time()))
        _save(d)
    log.info("admin account '%s' created from TUBARR_ADMIN_PASSWORD_HASH", user)
    return True


def setup_code():
    """The one-time setup code (generated per web-app start while no admin exists), or None."""
    if has_admin():
        _SETUP["code"] = None
        return None
    if not _SETUP["code"]:
        _SETUP["code"] = "-".join(secrets.token_hex(3).upper() for _ in range(3))
    return _SETUP["code"]


def announce_setup():
    code = setup_code()
    if code:
        # Deliberately logged: the setup code is how the person who runs the container proves they own it.
        log.warning("FIRST RUN: open the web UI and create the admin account. Setup code: %s", code)


def check_password_strength(pw):
    if not isinstance(pw, str) or len(pw) < MIN_PASSWORD:
        raise AuthError(400, "bad_request", "Use a password of at least %d characters." % MIN_PASSWORD)
    if len(pw) > 1024:
        raise AuthError(400, "bad_request", "That password is too long.")


def _clean_user(u):
    u = (u or "").strip()
    if not (1 <= len(u) <= 64) or any(ch.isspace() for ch in u):
        raise AuthError(400, "bad_request", "Use a username of 1 to 64 characters, without spaces.")
    return u


def create_admin(username, password, code):
    with _LOCK:
        if has_admin():
            raise AuthError(409, "conflict", "The admin account already exists. Sign in instead.")
        want = setup_code()
        if not want or not _eq(str(code or "").strip().upper(), want):
            raise AuthError(403, "forbidden", "That setup code is wrong. Find it in the container log (docker logs tubarr).")
        user = _clean_user(username)
        check_password_strength(password)
        d = _data()
        d.update(username=user, password_hash=_PH.hash(password), sessions={}, created_at=int(time.time()))
        _save(d)
        _SETUP["code"] = None
    log.info("admin account created in the web UI")
    return user


def set_password(new_password, username=None):
    """Set the admin password (CLI reset or change in the UI). Every existing session is signed out."""
    check_password_strength(new_password)
    with _LOCK:
        d = _data()
        if username:
            d["username"] = _clean_user(username)
        if not d.get("username"):
            d["username"] = "admin"
        d.update(password_hash=_PH.hash(new_password), sessions={})
        _save(d)
    return d["username"]


# ------------------------------------------------------------------ login + rate limit
# Only the per-client bucket ever locks. The per-username and global buckets just slow sign-in down (a growing delay,
# capped), so somebody hammering the username from elsewhere can't lock the real admin out: the right password from
# a client that isn't itself locked always gets in, after at most _DELAY_MAX seconds.
_FAILS = {}                      # key -> [timestamps]
_FAIL_WINDOW = 15 * 60
_FAIL_MAX = 5                    # per client: lock after this many failures in the window
_USER_SLOW = _FAIL_MAX * 2       # per username: start slowing down after this many
_GLOBAL_SLOW = 30                # all clients together: start slowing down after this many
_DELAY_STEP = 0.5
_DELAY_MAX = 5.0
MAX_PASSWORD = 1024


def _prune(now):
    for k in list(_FAILS):
        _FAILS[k] = [t for t in _FAILS[k] if now - t < _FAIL_WINDOW]
        if not _FAILS[k]:
            del _FAILS[k]


def _locked(keys, now):
    worst = 0
    for k, limit in keys:
        ts = _FAILS.get(k) or []
        if len(ts) >= limit:
            worst = max(worst, int(ts[-limit] + _FAIL_WINDOW - now) + 1)
    return worst


def _delay(keys):
    """Seconds to wait before checking a password: grows with failures over the soft (username/global) limits."""
    over = max((len(_FAILS.get(k) or []) - limit + 1 for k, limit in keys), default=0)
    return min(_DELAY_MAX, _DELAY_STEP * over) if over > 0 else 0.0


def _fail(keys, now):
    for k, _ in keys:
        _FAILS.setdefault(k, []).append(now)


def _check_client(client, now, what="Too many attempts"):
    """Raise 429 while this client address is locked out; -> (hard keys, soft keys)."""
    hard = [("ip:" + (client or "?"), _FAIL_MAX)]
    with _LOCK:
        _prune(now)
        wait = _locked(hard, now)
    if wait:
        raise AuthError(429, "rate_limited", "%s. Try again in %d minutes." % (what, max(1, (wait + 59) // 60)),
                        retry_after=wait)
    return hard


def verify_login(username, password, client):
    """-> username, or AuthError (401 wrong, 429 locked out). Locks per client address; per username and globally it
    only adds a delay (never a lock), so the real admin can always sign in from their own device."""
    now = time.time()
    uname = str(username or "").strip().lower()[:128]
    hard = _check_client(client, now)
    soft = [("user:" + uname, _USER_SLOW), ("all", _GLOBAL_SLOW)]
    with _LOCK:
        wait = _delay(soft)
    if wait:
        time.sleep(wait)                              # outside the lock: slows this attempt only
    pw = password if isinstance(password, str) else ""
    ok = False
    with _LOCK:
        d = _load()
        h = d.get("password_hash") or ""
        if len(pw) > MAX_PASSWORD:
            pw = ""                                   # never hash a huge input; counts as a failure below
        try:
            if h and pw and _eq(uname, (d.get("username") or "").lower()):
                ok = _PH.verify(h, pw)
            else:
                _PH.verify(_DUMMY, pw or "x")         # same work either way (no username probing by timing)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            ok = False
        if not ok:
            _fail(hard + soft, now)
            log.warning("failed sign-in from %s", client or "?")
            raise AuthError(401, "unauthorized", "Wrong username or password.")
        _FAILS.pop(hard[0][0], None)
        _FAILS.pop(soft[0][0], None)
        if _PH.check_needs_rehash(h):
            dd = _data()
            dd["password_hash"] = _PH.hash(pw)
            _save(dd)
        return d["username"]


_DUMMY = _PH.hash(secrets.token_hex(16))


def verify_password(password):
    d = _load()
    if not isinstance(password, str) or not password or len(password) > MAX_PASSWORD:
        return False
    try:
        return bool(d.get("password_hash")) and _PH.verify(d["password_hash"], password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# ------------------------------------------------------------------ sessions
def _serializer():
    return URLSafeTimedSerializer(vault.derived_key("session"), salt="tubarr-session")


def new_session():
    """-> (cookie value, csrf token). Records the session in auth.json."""
    sid, csrf = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    with _LOCK:
        d = _data()
        ss = d.get("sessions") or {}
        now = int(time.time())
        ss = {k: v for k, v in ss.items() if now - int(v.get("created", 0)) < SESSION_DAYS * 86400}
        while len(ss) >= MAX_SESSIONS:
            ss.pop(min(ss, key=lambda k: ss[k].get("created", 0)))
        ss[hashlib.sha256(sid.encode()).hexdigest()] = {"created": now}
        d["sessions"] = ss
        _save(d)
    return _serializer().dumps({"s": sid, "c": csrf}), csrf


def read_session(cookie):
    """-> {"sid", "csrf", "user"} for a valid, still-listed session cookie, else None."""
    if not cookie:
        return None
    try:
        p = _serializer().loads(cookie, max_age=int(SESSION_DAYS * 86400))
    except BadSignature:
        return None
    if not isinstance(p, dict) or not p.get("s") or not p.get("c"):
        return None
    d = _load()
    if hashlib.sha256(p["s"].encode()).hexdigest() not in (d.get("sessions") or {}):
        return None
    return {"sid": p["s"], "csrf": p["c"], "user": d.get("username")}


def end_session(sid):
    with _LOCK:
        d = _data()
        (d.get("sessions") or {}).pop(hashlib.sha256((sid or "").encode()).hexdigest(), None)
        _save(d)


def change_password(current, new, client=None):
    """Same per-client lockout as sign-in, so a stolen session can't be used to guess the password."""
    now = time.time()
    hard = _check_client(client, now)
    if not verify_password(current):
        with _LOCK:
            _fail(hard, now)
        raise AuthError(403, "forbidden", "The current password is wrong.")
    set_password(new)                               # signs out every session; the caller issues a fresh one


# ------------------------------------------------------------------ API key
def api_key_info():
    d = _load()
    return {"exists": bool(d.get("api_key_hash")), "created_at": d.get("api_key_created"), "hint": d.get("api_key_hint")}


def new_api_key():
    key = secrets.token_urlsafe(32)
    with _LOCK:
        d = _data()
        d.update(api_key_hash=hashlib.sha256(key.encode()).hexdigest(),
                 api_key_created=time.strftime("%Y-%m-%dT%H:%M:%S%z"), api_key_hint="…" + key[-4:])
        _save(d)
    return dict(api_key_info(), api_key=key)


def revoke_api_key():
    with _LOCK:
        d = _data()
        for k in ("api_key_hash", "api_key_created", "api_key_hint"):
            d.pop(k, None)
        _save(d)


def check_api_key(key):
    h = _load().get("api_key_hash")
    if not h or not key or len(key) > 256:
        return False
    return _eq(hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest(), h)


def csrf_ok(session, header):
    return bool(session and header) and _eq(session["csrf"], header)
