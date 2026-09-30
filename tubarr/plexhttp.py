"""Every HTTP request to Plex (and plex.tv) goes through here.

  * Redirects are never followed: a redirect would carry the X-Plex-Token header to whatever host it points at.
    A 3xx answer raises requests.HTTPError instead.
  * Environment proxy variables are ignored (trust_env=False): local traffic is never proxied (net.py is only for
    YouTube traffic).
"""
import threading

import requests

_LOCAL = threading.local()


class Session(requests.Session):
    """A requests.Session that never follows redirects and ignores environment proxies (also given to plexapi)."""

    def __init__(self):
        super().__init__()
        self.trust_env = False
        self.max_redirects = 0

    def request(self, method, url, **kw):
        kw["allow_redirects"] = False
        r = super().request(method, url, **kw)
        if r.is_redirect or 300 <= r.status_code < 400:
            r.close()
            raise requests.HTTPError("Plex answered with a redirect (HTTP %d); not followed" % r.status_code,
                                     response=r)
        return r


def _session():
    s = getattr(_LOCAL, "s", None)
    if s is None:
        s = _LOCAL.s = Session()
    return s


def request(method, url, **kw):
    return _session().request(method, url, **kw)


def get(url, **kw):
    return request("GET", url, **kw)


def post(url, **kw):
    return request("POST", url, **kw)


def put(url, **kw):
    return request("PUT", url, **kw)


def delete(url, **kw):
    return request("DELETE", url, **kw)
