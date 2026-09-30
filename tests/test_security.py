"""Security unit tests: proxy URL validation, the proxy test endpoint, sign-in/CSRF/API-key rules, secrets at rest,
log redaction. Standard library unittest only (no pytest needed):

    python -m unittest discover -s tests -v

They need Tubarr's Python dependencies (run them in the image:
    docker run --rm --network none -e TUBARR_DATA=/tmp/d -e TUBARR_ROOT=/tmp/y -v "$PWD/tests:/app/tests:ro"
        --entrypoint python tubarr:local -m unittest discover -s tests -v)
and never touch the network: every outbound request is replaced by a fake.
"""
import asyncio
import json
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="tubarr-test-")
os.environ["TUBARR_DATA"] = os.path.join(_TMP, "data")
os.environ["TUBARR_ROOT"] = os.path.join(_TMP, "youtube")
os.makedirs(os.environ["TUBARR_DATA"], exist_ok=True)
os.makedirs(os.environ["TUBARR_ROOT"], exist_ok=True)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tubarr import auth, net, redact, vault  # noqa: E402

PASSWORD = "correct horse battery staple"


# ------------------------------------------------------------------ a tiny ASGI client (no httpx needed)
def call(app, method, path, query="", headers=None, body=None):
    raw = json.dumps(body).encode() if body is not None else b""
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if body is not None:
        hdrs.append((b"content-type", b"application/json"))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": query.encode(), "headers": hdrs,
             "client": ("127.0.0.1", 50000), "server": ("testserver", 80), "root_path": ""}
    msgs = [{"type": "http.request", "body": raw, "more_body": False}]
    out = {"status": None, "headers": {}, "body": b""}

    async def receive():
        return msgs.pop(0) if msgs else {"type": "http.disconnect"}

    async def send(m):
        if m["type"] == "http.response.start":
            out["status"] = m["status"]
            for k, v in m.get("headers", []):
                out["headers"].setdefault(k.decode().lower(), []).append(v.decode())
        elif m["type"] == "http.response.body":
            out["body"] += m.get("body", b"")

    asyncio.run(app(scope, receive, send))
    try:
        out["json"] = json.loads(out["body"] or b"null")
    except ValueError:
        out["json"] = None
    return out


class FakeResponse:
    def __init__(self, status=200, body=b"203.0.113.9"):
        self.status_code = status
        self._body = body
        self.raw = self

    def read(self, n=-1, decode_content=True):
        return self._body[:n] if n and n > 0 else self._body

    def close(self):
        pass


class FakeSession:
    """Stands in for requests.Session inside net.py; records every request."""
    calls = []
    raise_exc = None
    response = None

    def __init__(self):
        self.trust_env = True
        self.proxies = {}
        self.max_redirects = 30
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, **kw):
        FakeSession.calls.append({"url": url, "kw": kw, "proxies": dict(self.proxies), "trust_env": self.trust_env,
                                  "max_redirects": self.max_redirects})
        if FakeSession.raise_exc:
            raise FakeSession.raise_exc
        return FakeSession.response or FakeResponse()


# ------------------------------------------------------------------ proxy URL validation
class ProxyUrlValidation(unittest.TestCase):
    BAD = [
        "", " ", "socks5h://host:1080; rm -rf /", "socks5h://host:1080$(id)", "socks5h://$(id):1080",
        "socks5h://host`id`:1080", "socks5h://`whoami`@host:1080", "socks5h://host:1080\nX-Injected: 1",
        "socks5h://host:1080\r\n", "socks5h://ho\tst:1080", "socks5h://host :1080", "file:///etc/passwd",
        "gopher://host:70/_payload", "ftp://host:21", "javascript:alert(1)", "http://user@evil@host:8080",
        "http://user:pw@evil.com@host:8080", "http://host:8080/path", "http://host:8080?x=1", "http://host:8080#frag",
        "http://host", "http://host:0", "http://host:65536", "http://host:99999", "http://host:-1", "http://:8080",
        "socks5h://2001:db8::1:1080", "socks5h://[not-an-ip]:1080", "socks5h://[2001:db8::zz]:1080",
        "socks5h://" + "a" * 600 + ":1080", "http://" + "a." * 200 + "com:8080", "socks5h://host_name!:1080",
        "http://host:8080\\@evil", "http://<script>:8080", "socks5h://host:1080|nc", "socks4://host:1080",
        "HTTP://host:80 extra", "http://host:8080/../../", "\x00http://host:8080", "http://ho\x00st:8080",
        "http://user:p\x7fw@host:8080", "socks5h://host:1080'", 'socks5h://host:1080"', "socks5h://{host}:1080",
    ]
    GOOD = [
        ("socks5h://proxy.example:1080", ("socks5h", "proxy.example", 1080, None, None)),
        ("socks5://user:pass@198.51.100.7:1080", ("socks5", "198.51.100.7", 1080, "user", "pass")),
        ("http://proxy.example:3128/", ("http", "proxy.example", 3128, None, None)),
        ("https://u%40x:p%3Aw%2F@proxy.example:443", ("https", "proxy.example", 443, "u@x", "p:w/")),
        ("socks5h://[2001:db8::1]:1080", ("socks5h", "2001:db8::1", 1080, None, None)),
        ("SOCKS5H://Proxy.Example:1080", ("socks5h", "proxy.example", 1080, None, None)),
    ]

    def test_rejects_injection_and_tricks(self):
        for raw in self.BAD:
            with self.subTest(raw=raw[:60]):
                with self.assertRaises(ValueError):
                    net.parse_proxy_url(raw)

    def test_rejects_non_strings(self):
        for raw in (None, 123, b"socks5h://h:1", ["socks5h://h:1"]):
            with self.assertRaises(ValueError):
                net.parse_proxy_url(raw)

    def test_accepts_and_returns_components(self):
        for raw, (scheme, host, port, user, pw) in self.GOOD:
            with self.subTest(raw=raw):
                c = net.parse_proxy_url(raw)
                self.assertEqual((c["scheme"], c["host"], c["port"], c["username"], c["password"]),
                                 (scheme, host, port, user, pw))

    def test_rebuilt_url_is_canonical(self):
        c = net.parse_proxy_url("https://u%40x:p%3Aw%2F@proxy.example:443/")
        self.assertEqual(net._url(c), "https://u%40x:p%3Aw%2F@proxy.example:443")
        self.assertEqual(net._masked(c), "https://***@proxy.example:443")
        c6 = net.parse_proxy_url("socks5h://[2001:db8::1]:1080")
        self.assertEqual(net._url(c6), "socks5h://[2001:db8::1]:1080")


# ------------------------------------------------------------------ secrets at rest + redaction
class SecretsAtRest(unittest.TestCase):
    def test_vault_encrypts_and_key_is_private(self):
        vault.put("unit_test_secret", "s3cr3t-value-123")
        self.assertEqual(vault.get("unit_test_secret"), "s3cr3t-value-123")
        with open(vault.CREDS_PATH, encoding="utf-8") as f:
            self.assertNotIn("s3cr3t-value-123", f.read())
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(vault.KEY_PATH).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(vault.CREDS_PATH).st_mode), 0o600)
        vault.put("unit_test_secret", "")
        self.assertIsNone(vault.get("unit_test_secret"))

    def test_proxy_stored_as_encrypted_components(self):
        out = net.add_proxy("Unit", "socks5h://alice:hunter2pass@proxy.example:1080", "tester")
        pid = out["proxies"][-1]["id"]
        self.assertEqual(out["proxies"][-1]["masked"], "socks5h://***@proxy.example:1080")
        blob = json.dumps(out)
        self.assertNotIn("hunter2pass", blob)
        self.assertNotIn("alice", blob)
        for path in (vault.CREDS_PATH, os.path.join(os.environ["TUBARR_DATA"], "settings.json")):
            with open(path, encoding="utf-8") as f:
                self.assertNotIn("hunter2pass", f.read())
        net.delete_proxy(pid, "tester")

    def test_redaction(self):
        redact.remember("hunter2pass")
        s = redact.text("proxy socks5h://alice:hunter2pass@proxy.example:1080 failed; X-Plex-Token=abcdef123456 "
                        "token=zzz999 Cookie: SID=1 password=hunter2pass")
        for leak in ("hunter2pass", "abcdef123456", "zzz999", "SID=1", "alice:"):
            self.assertNotIn(leak, s)


# ------------------------------------------------------------------ web app: sign-in, CSRF, API key, test endpoint
class WebSecurity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        auth.set_password(PASSWORD, "admin")
        from tubarr.webapp import app as appmod
        cls.app = appmod.app
        cookie, csrf = auth.new_session()
        cls.cookie, cls.csrf = cookie, csrf
        cls.api_key = auth.new_api_key()["api_key"]

    def session_headers(self, csrf=True):
        h = {"cookie": "%s=%s" % (auth.SESSION_COOKIE, self.cookie)}
        if csrf:
            h["x-csrf-token"] = self.csrf
        return h

    def setUp(self):
        FakeSession.calls, FakeSession.raise_exc, FakeSession.response = [], None, None
        net._TESTS.clear()

    def test_api_needs_auth(self):
        r = call(self.app, "GET", "/api/network")
        self.assertEqual(r["status"], 401)
        r = call(self.app, "GET", "/api/network", query="api_key=" + self.api_key)   # never via the URL
        self.assertEqual(r["status"], 401)
        self.assertEqual(call(self.app, "GET", "/health")["status"], 200)

    def test_csrf_required_for_changes(self):
        r = call(self.app, "PATCH", "/api/network", headers=self.session_headers(csrf=False), body={"mode": "direct"})
        self.assertEqual(r["status"], 403)
        self.assertEqual(r["json"]["error"]["code"], "csrf")
        h = self.session_headers(csrf=False)
        h["x-csrf-token"] = "wrong"
        self.assertEqual(call(self.app, "PATCH", "/api/network", headers=h, body={"mode": "direct"})["status"], 403)

    def test_api_key_is_read_only_and_sees_no_proxy_details(self):
        h = {"x-api-key": self.api_key}
        r = call(self.app, "GET", "/api/network", headers=h)
        self.assertEqual(r["status"], 200)
        self.assertEqual(set(r["json"]), {"status"})
        for method, path, body in (("POST", "/api/network/proxies", {"name": "x", "url": "socks5h://h:1"}),
                                   ("PATCH", "/api/network", {"mode": "direct"}),
                                   ("POST", "/api/network/proxies/direct/test", {}),
                                   ("DELETE", "/api/network/proxies/p_1", None)):
            with self.subTest(path=path, method=method):
                self.assertEqual(call(self.app, method, path, headers=h, body=body)["status"], 403)
        self.assertEqual(call(self.app, "GET", "/api/audit", headers=h)["status"], 403)
        self.assertEqual(call(self.app, "GET", "/api/auth/apikey", headers=h)["status"], 403)

    def test_proxy_test_endpoint_ignores_any_target(self):
        with mock.patch.object(net.requests, "Session", FakeSession):
            r = call(self.app, "POST", "/api/network/proxies/direct/test",
                     query="url=http://169.254.169.254/latest&target=gopher://evil:70&host=10.0.0.1",
                     headers=self.session_headers(),
                     body={"url": "http://169.254.169.254/", "target": "http://evil.example/", "host": "10.0.0.1",
                           "port": 22})
        self.assertEqual(r["status"], 200, r["body"])
        self.assertEqual(set(r["json"]), {"ok", "ip", "latency_ms"})
        self.assertEqual(r["json"]["ip"], "203.0.113.9")
        self.assertEqual(len(FakeSession.calls), 1)
        c = FakeSession.calls[0]
        self.assertEqual(c["url"], net._TEST_URL)
        self.assertEqual(c["url"], "https://api.ipify.org")
        self.assertIs(c["kw"].get("allow_redirects"), False)
        self.assertEqual(c["kw"].get("timeout"), 10)
        self.assertFalse(c["trust_env"])
        self.assertEqual(c["max_redirects"], 0)

    def test_proxy_test_errors_are_generic(self):
        import requests
        FakeSession.raise_exc = requests.exceptions.ConnectionError("Failed to connect to 10.9.8.7:1080 internal-host")
        with mock.patch.object(net.requests, "Session", FakeSession):
            r = call(self.app, "POST", "/api/network/proxies/direct/test", headers=self.session_headers(), body={})
        self.assertEqual(r["status"], 502)
        msg = r["json"]["error"]["message"]
        self.assertNotIn("10.9.8.7", msg)
        self.assertNotIn("internal-host", msg)
        FakeSession.raise_exc = None
        FakeSession.response = FakeResponse(200, b"<html>not an ip</html>")
        with mock.patch.object(net.requests, "Session", FakeSession):
            r = call(self.app, "POST", "/api/network/proxies/direct/test", headers=self.session_headers(), body={})
        self.assertEqual(r["status"], 502)
        self.assertNotIn("html", r["body"].decode())

    def test_proxy_test_is_rate_limited(self):
        with mock.patch.object(net.requests, "Session", FakeSession):
            codes = [call(self.app, "POST", "/api/network/proxies/direct/test", headers=self.session_headers(),
                          body={})["status"] for _ in range(7)]
        self.assertEqual(codes[:5], [200] * 5)
        self.assertEqual(codes[5:], [429, 429])

    def test_unknown_proxy_id_is_not_fetched(self):
        with mock.patch.object(net.requests, "Session", FakeSession):
            r = call(self.app, "POST", "/api/network/proxies/..%2F..%2Fetc/test", headers=self.session_headers(), body={})
        self.assertIn(r["status"], (404, 502))
        self.assertEqual(FakeSession.calls, [])

    def test_security_headers(self):
        r = call(self.app, "GET", "/health")
        self.assertIn("default-src 'self'", r["headers"]["content-security-policy"][0])
        self.assertEqual(r["headers"]["x-frame-options"][0], "DENY")


# ------------------------------------------------------------------ passwords + sessions
class Passwords(unittest.TestCase):
    def test_argon2id_and_lockout(self):
        auth.set_password(PASSWORD, "admin")
        with open(auth.PATH, encoding="utf-8") as f:
            d = json.load(f)
        self.assertTrue(d["password_hash"].startswith("$argon2id$"))
        self.assertNotIn(PASSWORD, json.dumps(d))
        self.assertEqual(auth.verify_login("admin", PASSWORD, "192.0.2.50"), "admin")
        for _ in range(5):
            with self.assertRaises(auth.AuthError) as e:
                auth.verify_login("admin", "wrong password!", "192.0.2.51")
            self.assertEqual(e.exception.status, 401)
        with self.assertRaises(auth.AuthError) as e:
            auth.verify_login("admin", PASSWORD, "192.0.2.51")      # right password, but locked out
        self.assertEqual(e.exception.status, 429)

    def test_password_change_signs_out_everyone(self):
        auth.set_password(PASSWORD, "admin")
        cookie, _ = auth.new_session()
        self.assertIsNotNone(auth.read_session(cookie))
        auth.change_password(PASSWORD, PASSWORD + "!")
        self.assertIsNone(auth.read_session(cookie))
        self.assertIsNone(auth.read_session(cookie[:-2] + "xx"))
        auth.set_password(PASSWORD, "admin")

    def test_api_key_stored_as_hash(self):
        k = auth.new_api_key()["api_key"]
        with open(auth.PATH, encoding="utf-8") as f:
            self.assertNotIn(k, f.read())
        self.assertTrue(auth.check_api_key(k))
        self.assertFalse(auth.check_api_key(k + "x"))


if __name__ == "__main__":
    unittest.main()
