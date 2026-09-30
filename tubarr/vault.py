"""Secrets at rest: a per-install random key (/data/secret.key, mode 600) and Fernet-encrypted credentials.

What lives here:
  * the install key: created on first use with O_EXCL (so the worker and the web app can never both create one),
    never logged, never sent anywhere;
  * /data/credentials.json (mode 600): named credentials (the Plex token, proxies, the Discord webhook) encrypted with Fernet
    (AES-128-CBC + HMAC-SHA256). Plain values only exist in memory while they're being used;
  * derived keys for other purposes (signing session cookies), so one key file covers everything.

Nothing in this module returns a secret to the web UI: callers get the value only to use it (a Plex request, a
yt-dlp run). The UI only ever learns whether a credential is set, or a masked form of it.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import threading

from cryptography.fernet import Fernet, InvalidToken

from . import config

KEY_PATH = os.path.join(config.DATA, "secret.key")
CREDS_PATH = os.path.join(config.DATA, "credentials.json")
_LOCK = threading.Lock()
_KEY = {"raw": None}
_CACHE = {"mtime": None, "data": {}, "plain": {}}
_WARNED = set()
log = logging.getLogger("tubarr.vault")


def _write_private(path, data):
    """Atomically write `data` (bytes) to `path` with mode 600."""
    tmp = "%s.tmp.%d.%d" % (path, os.getpid(), threading.get_ident())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def install_key():
    """The raw 32-byte install key, created on first use."""
    if _KEY["raw"] is not None:
        return _KEY["raw"]
    with _LOCK:
        if _KEY["raw"] is not None:
            return _KEY["raw"]
        os.makedirs(config.DATA, exist_ok=True)
        if not os.path.exists(KEY_PATH):
            key = Fernet.generate_key()
            tmp = "%s.new.%d" % (KEY_PATH, os.getpid())
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, key + b"\n")
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.link(tmp, KEY_PATH)            # atomic and fails if another process won the race
            except FileExistsError:
                pass
            finally:
                os.unlink(tmp)
        try:
            os.chmod(KEY_PATH, 0o600)
        except OSError:
            pass
        with open(KEY_PATH, "rb") as f:
            key = f.read().strip()
        raw = base64.urlsafe_b64decode(key)
        if len(raw) != 32:
            raise RuntimeError("%s is not a valid key file; move it away to create a new one "
                               "(saved credentials will then need to be entered again)" % KEY_PATH)
        _KEY["raw"] = raw
        return raw


def derived_key(purpose):
    """A 32-byte key for one purpose (e.g. 'session'), derived from the install key with HMAC-SHA256."""
    return hmac.new(install_key(), ("tubarr:" + purpose).encode(), hashlib.sha256).digest()


def _fernet():
    return Fernet(base64.urlsafe_b64encode(install_key()))


def _load():
    try:
        m = os.stat(CREDS_PATH).st_mtime_ns
    except OSError:
        _CACHE.update(mtime=None, data={}, plain={})
        return _CACHE["data"]
    if m != _CACHE["mtime"]:
        try:
            with open(CREDS_PATH, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        _CACHE.update(mtime=m, data=d if isinstance(d, dict) else {}, plain={})
    return _CACHE["data"]


def get(name):
    """The decrypted credential, or None if it isn't set (or can't be decrypted with this install's key)."""
    with _LOCK:
        data = _load()
        if name in _CACHE["plain"]:
            return _CACHE["plain"][name]
        tok = data.get(name)
        val = None
        if tok:
            try:
                val = _fernet().decrypt(tok.encode()).decode()
            except (InvalidToken, ValueError):
                val = None
                if "key" not in _WARNED:                    # once per process: the usual cause is a key mismatch
                    _WARNED.add("key")
                    log.warning("Saved credentials in %s can't be decrypted with %s (the key file was replaced or "
                                "restored from a different backup). Restore the matching secret.key, or enter the "
                                "Plex sign-in, proxies and webhook again.", CREDS_PATH, KEY_PATH)
        _CACHE["plain"][name] = val
        return val


def has(name):
    return bool(get(name))


def put(name, value):
    """Encrypt and store a credential; an empty value deletes it."""
    with _LOCK:
        data = dict(_load())
        if value:
            data[name] = _fernet().encrypt(value.encode()).decode()
        else:
            data.pop(name, None)
        os.makedirs(config.DATA, exist_ok=True)
        _write_private(CREDS_PATH, json.dumps(data, indent=1, sort_keys=True).encode())
        _CACHE["mtime"] = None
    from . import redact
    if value:
        redact.remember(value)


def all_plain_items():
    """Every stored (name, plain value) (for the log redaction filter only)."""
    out = []
    for name in list(_load().keys()):
        v = get(name)
        if v:
            out.append((name, v))
    return out


def all_plain_values():
    return [v for _, v in all_plain_items()]
