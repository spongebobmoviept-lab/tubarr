"""Keep secrets out of logs, error messages and anything written to /data.

`install()` puts a filter on every logging handler (the root's and any named logger's), so every record is
scrubbed right before it's written, whichever module logged it (Tubarr, yt-dlp through its logger, requests,
uvicorn). `text()` scrubs a single string; the DB layer uses it for error/reason fields that the UI shows.

Removed: credentials inside URLs (scheme://user:pass@host), Plex tokens (X-Plex-Token / token= / authToken),
Cookie / Authorization / X-Api-Key header values, password= pairs, and the exact values of every stored credential.
"""
import logging
import re
import threading

_KNOWN = set()
_LOCK = threading.Lock()
_PATTERNS = [
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/\s:@'\"]+:[^/\s@'\"]*@"), r"\1***:***@"),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/\s:@'\"]+@"), r"\1***@"),
    (re.compile(r"(?i)(x-plex-token[\"']?\s*[:=]\s*[\"']?)[^\s&\"',;}]+"), r"\1***"),
    (re.compile(r"(?i)\b((?:auth)?token[\"']?\s*[:=]\s*[\"']?)[^\s&\"',;}]+"), r"\1***"),
    (re.compile(r"(?i)\b(api[_-]?key[\"']?\s*[:=]\s*[\"']?)[^\s&\"',;}]+"), r"\1***"),
    (re.compile(r"(?i)\b(password[\"']?\s*[:=]\s*[\"']?)[^\s&\"',;}]+"), r"\1***"),
    (re.compile(r"(?i)\b((?:set-)?cookie\s*:\s*)[^\r\n]+"), r"\1***"),
    (re.compile(r"(?i)\b(authorization\s*:\s*)[^\r\n]+"), r"\1***"),
    (re.compile(r"(?i)(discord(?:app)?\.com/api/webhooks/\d+/)[\w-]+"), r"\1***"),
]


def remember(value):
    """Also scrub this exact value wherever it appears (a stored credential, or the password part of a proxy URL)."""
    if not value or len(value) < 4:
        return
    with _LOCK:
        _KNOWN.add(value)
        m = re.match(r"^[a-z][a-z0-9+.\-]*://([^/\s:@]+):([^/\s@]+)@", value, re.I)
        if m:
            for part in m.groups():
                if len(part) >= 4:
                    _KNOWN.add(part)


def remember_proxy(raw):
    """A stored proxy (JSON components, net.py): scrub its username, password and the full URL built from them."""
    try:
        import json
        c = json.loads(raw)
        from .net import _url
        for part in (c.get("username"), c.get("password"), _url(c)):
            remember(part)
    except Exception:
        pass


def text(s):
    if not isinstance(s, str) or not s:
        return s
    for pat, rep in _PATTERNS:
        s = pat.sub(rep, s)
    with _LOCK:
        known = sorted(_KNOWN, key=len, reverse=True)
    for k in known:
        if k in s:
            s = s.replace(k, "***")
    return s


class Filter(logging.Filter):
    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        clean = text(msg)
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = text(record.exc_text)
        if clean != msg or record.args:
            record.msg, record.args = clean, None
        return True


_FILTER = Filter()


def install():
    """Attach the filter to every handler that exists now (call again after adding handlers)."""
    try:
        from . import vault
        for name, v in vault.all_plain_items():
            remember(v)
            if name.startswith("proxy:"):                  # a JSON of components: also each part and the URL
                remember_proxy(v)
    except Exception:
        pass
    loggers = [logging.getLogger()] + [lg for lg in logging.root.manager.loggerDict.values()
                                      if isinstance(lg, logging.Logger)]
    for lg in loggers:
        for h in lg.handlers:
            if _FILTER not in h.filters:
                h.addFilter(_FILTER)
