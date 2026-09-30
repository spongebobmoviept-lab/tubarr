"""Tests for the red-team fixes: sign-in lockout, Plex token scope, no redirects, notification SSRF, body caps,
non-ASCII input, the lookup route, age-restricted videos, folder collisions, shared-folder deletes, NFO text, video
ids, the never-direct guard, the Trimarr token header, the Discord webhook secret and repolish arguments.

    python -m unittest discover -s tests -v        (in the image, see test_security.py)

No network: every outbound request is replaced by a fake.
"""
import asyncio
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

if "tubarr.config" not in sys.modules and "TUBARR_DATA" not in os.environ:
    _TMP = tempfile.mkdtemp(prefix="tubarr-test2-")
    os.environ["TUBARR_DATA"] = os.path.join(_TMP, "data")
    os.environ["TUBARR_ROOT"] = os.path.join(_TMP, "youtube")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tubarr import auth, config, db, naming, net, nfo, notify, plexhttp, redact, settings, vault  # noqa: E402

os.makedirs(config.DATA, exist_ok=True)
os.makedirs(config.ROOT, exist_ok=True)
PASSWORD = "correct horse battery staple"
UC_A = "UC" + "a" * 22
UC_B = "UC" + "b" * 22
HOOK = "https://discord.com/api/webhooks/123456789012345678/" + "T" * 68


def call(app, method, path, headers=None, body=None, raw=None, chunks=None, query=""):
    """A tiny ASGI client. `raw` = body bytes as is; `chunks` = a chunked body (no Content-Length)."""
    hdrs = [(k.lower().encode("latin-1"), v.encode("utf-8") if isinstance(v, str) else v)
            for k, v in (headers or {}).items()]
    if body is not None:
        raw = json.dumps(body).encode()
    if raw is not None or chunks is not None:
        hdrs.append((b"content-type", b"application/json"))
    if raw is not None:
        hdrs.append((b"content-length", str(len(raw)).encode()))
        msgs = [{"type": "http.request", "body": raw, "more_body": False}]
    elif chunks is not None:
        msgs = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)]
    else:
        msgs = [{"type": "http.request", "body": b"", "more_body": False}]
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": query.encode(), "headers": hdrs,
             "client": ("127.0.0.1", 50001), "server": ("testserver", 80), "root_path": ""}
    out = {"status": None, "headers": {}, "body": b""}

    async def receive():
        return msgs.pop(0) if msgs else {"type": "http.disconnect"}

    async def send(m):
        if m["type"] == "http.response.start":
            out["status"] = m["status"]
            for k, v in m.get("headers", []):
                out["headers"].setdefault(k.decode().lower(), []).append(v.decode("latin-1"))
        elif m["type"] == "http.response.body":
            out["body"] += m.get("body", b"")

    asyncio.run(app(scope, receive, send))
    try:
        out["json"] = json.loads(out["body"] or b"null")
    except ValueError:
        out["json"] = None
    return out


class _Web:
    @classmethod
    def setUpClass(cls):
        auth.set_password(PASSWORD, "admin")
        from tubarr.webapp import app as appmod
        cls.appmod = appmod
        cls.app = appmod.app
        cls.cookie, cls.csrf = auth.new_session()
        cls.api_key = auth.new_api_key()["api_key"]

    def sess(self, csrf=True):
        h = {"cookie": "%s=%s" % (auth.SESSION_COOKIE, self.cookie)}
        if csrf:
            h["x-csrf-token"] = self.csrf
        return h


# ------------------------------------------------------------------ A1: lockout
class Lockout(unittest.TestCase):
    def setUp(self):
        auth._FAILS.clear()
        auth.set_password(PASSWORD, "admin")

    def tearDown(self):
        auth._FAILS.clear()

    def test_username_and_global_buckets_never_block_the_right_password(self):
        with mock.patch.object(auth.time, "sleep") as slept:
            for i in range(40):                       # a spray from 40 addresses, 1 try each (no client locks)
                with self.assertRaises(auth.AuthError) as e:
                    auth.verify_login("admin", "wrong password %d" % i, "198.51.100.%d" % i)
                self.assertEqual(e.exception.status, 401)
            self.assertEqual(auth.verify_login("admin", PASSWORD, "192.0.2.10"), "admin")
        self.assertTrue(slept.called)                 # slowed down...
        self.assertLessEqual(max(a[0][0] for a in slept.call_args_list), auth._DELAY_MAX)   # ...but capped

    def test_the_client_bucket_still_locks(self):
        for _ in range(auth._FAIL_MAX):
            with self.assertRaises(auth.AuthError):
                auth.verify_login("admin", "nope nope nope", "192.0.2.77")
        with self.assertRaises(auth.AuthError) as e:
            auth.verify_login("admin", PASSWORD, "192.0.2.77")
        self.assertEqual(e.exception.status, 429)

    def test_huge_password_is_refused_without_hashing(self):
        with mock.patch.object(auth, "_PH", wraps=auth._PH) as ph:
            with self.assertRaises(auth.AuthError) as e:
                auth.verify_login("admin", "x" * 100_000, "192.0.2.78")
        self.assertEqual(e.exception.status, 401)
        self.assertTrue(ph.verify.called)
        self.assertTrue(all(len(a[0][1]) < 2000 for a in ph.verify.call_args_list))

    def test_change_password_is_throttled_per_client(self):
        for _ in range(auth._FAIL_MAX):
            with self.assertRaises(auth.AuthError) as e:
                auth.change_password("wrong current pw", PASSWORD + "!", "192.0.2.79")
            self.assertEqual(e.exception.status, 403)
        with self.assertRaises(auth.AuthError) as e:
            auth.change_password(PASSWORD, PASSWORD + "!", "192.0.2.79")
        self.assertEqual(e.exception.status, 429)


# ------------------------------------------------------------------ A6: non-ASCII input
class NonAscii(_Web, unittest.TestCase):
    def setUp(self):
        auth._FAILS.clear()

    def test_login_with_non_ascii_is_401_not_500(self):
        for user, pw in (("ädmin", "pässwörd-ünïcode"), ("admin", "пароль-пароль"), ("管理员", "密码密码密码密码密码")):
            with self.subTest(user=user):
                r = call(self.app, "POST", "/api/auth/login", body={"username": user, "password": pw})
                self.assertEqual(r["status"], 401, r["body"])

    def test_non_ascii_csrf_and_setup_code(self):
        h = self.sess(csrf=False)
        h["x-csrf-token"] = "tökén-ü"
        r = call(self.app, "PATCH", "/api/network", headers=h, body={"mode": "direct"})
        self.assertEqual(r["status"], 403)
        self.assertFalse(auth.csrf_ok({"csrf": "abc"}, "äbc"))
        self.assertTrue(auth._eq("ä", "ä"))

    def test_bad_input_is_400(self):
        import csv
        for exc in (ValueError, csv.Error):
            self.assertIn(exc, self.app.exception_handlers)
        r = call(self.app, "POST", "/api/auth/login", raw=bytes([0x7b, 0xff, 0xfe]) + b' broken utf-8')
        self.assertIn(r["status"], (400, 401))


# ------------------------------------------------------------------ A4: body caps
class BodyCaps(_Web, unittest.TestCase):
    def test_auth_body_over_64k_is_413(self):
        raw = json.dumps({"username": "admin", "password": "x" * 70_000}).encode()
        r = call(self.app, "POST", "/api/auth/login", raw=raw)
        self.assertEqual(r["status"], 413)

    def test_chunked_body_is_counted(self):
        chunks = [b'{"username": "admin", "password": "'] + [b"x" * 16_384] * 5 + [b'"}']
        r = call(self.app, "POST", "/api/auth/login", chunks=chunks)
        self.assertEqual(r["status"], 413)

    def test_small_chunked_body_still_works(self):
        auth._FAILS.clear()
        chunks = [b'{"username": "admin", ', b'"password": "wrong password here"}']
        r = call(self.app, "POST", "/api/auth/login", chunks=chunks)
        self.assertEqual(r["status"], 401)

    def test_import_cap_is_bigger_but_finite(self):
        from tubarr.webapp import security
        self.assertEqual(security.body_limit("/api/auth/login"), 64 * 1024)
        self.assertGreaterEqual(security.body_limit("/api/import/preview"), 5 * 1024 * 1024 - 1)
        r = call(self.app, "POST", "/api/import/preview", headers=self.sess(), raw=b"x" * (6 * 1024 * 1024))
        self.assertEqual(r["status"], 413)


# ------------------------------------------------------------------ A7: lookup
class Lookup(_Web, unittest.TestCase):
    def test_lookup_not_allowed_with_the_api_key(self):
        h = {"x-api-key": self.api_key}
        self.assertEqual(call(self.app, "GET", "/api/lookup", headers=h, query="q=@x")["status"], 403)
        self.assertEqual(call(self.app, "POST", "/api/lookup", headers=h, body={"q": "@x"})["status"], 403)

    def test_lookup_is_post_with_csrf_and_rate_limited(self):
        self.assertEqual(call(self.app, "POST", "/api/lookup", headers=self.sess(csrf=False),
                              body={"q": "@x"})["status"], 403)
        from tubarr import ytdl
        self.appmod._LOOKUPS.clear()
        with mock.patch.object(ytdl, "channel_tab", return_value={"channel_id": UC_A, "title": "Invented"}) as ct:
            codes = [call(self.app, "POST", "/api/lookup", headers=self.sess(), body={"q": "@invented%d" % i})["status"]
                     for i in range(self.appmod._LOOKUP_PER_MIN + 2)]
        self.assertEqual(codes[:self.appmod._LOOKUP_PER_MIN], [200] * self.appmod._LOOKUP_PER_MIN)
        self.assertEqual(codes[-1], 429)
        self.assertEqual(ct.call_count, self.appmod._LOOKUP_PER_MIN)

    def test_network_api_key_view_has_no_ip(self):
        r = call(self.app, "GET", "/api/network", headers={"x-api-key": self.api_key})
        self.assertEqual(r["status"], 200)
        self.assertNotIn("ip", r["json"]["status"])


# ------------------------------------------------------------------ A2: Plex token scope + no redirects
class PlexToken(unittest.TestCase):
    def setUp(self):
        from tubarr.webapp import plexinfo, setupflow
        self.sf = setupflow
        self.p = mock.patch.object(plexinfo, "status", return_value={"state": "unreachable"})
        self.p.start()
        setupflow._PINS.clear()

    def tearDown(self):
        self.p.stop()
        self.sf._PINS.clear()
        vault.put("plex_token", "")
        settings.save({"plex_url": ""})

    def test_origin_change_wipes_the_token(self):
        self.sf.save_plex("http://plex.example:32400", "tok-manual-123456")
        self.assertEqual(vault.get("plex_token"), "tok-manual-123456")
        self.sf.save_plex("http://plex.example:32400/")              # same origin: kept
        self.assertEqual(vault.get("plex_token"), "tok-manual-123456")
        self.sf.save_plex("http://attacker.example:32400")            # another host: gone
        self.assertIsNone(vault.get("plex_token"))

    def test_port_or_scheme_change_also_wipes(self):
        for other in ("http://plex.example:32401", "https://plex.example:32400"):
            with self.subTest(other=other):
                self.sf.save_plex("http://plex.example:32400", "tok-manual-123456")
                self.sf.save_plex(other)
                self.assertIsNone(vault.get("plex_token"))

    def test_pin_discovered_connection_gets_only_the_server_token(self):
        self.sf._PINS[1] = {"code": "X", "expires": 9e9, "token": "ACCOUNT-TOKEN-xyz",
                            "servers": [], "resources": [{"uris": ["https://10-0-0-5.abc.plex.direct:32400"],
                                                          "token": "SERVER-TOKEN-abc"}]}
        self.sf.save_plex("http://plex.example:32400", "tok-manual-123456")
        self.sf.save_plex("https://10-0-0-5.abc.plex.direct:32400")
        self.assertEqual(vault.get("plex_token"), "SERVER-TOKEN-abc")
        self.assertIsNone(self.sf._PINS[1]["token"])                  # the account token is dropped

    def test_poll_pin_keeps_the_account_token_in_memory_only(self):
        self.sf._PINS[7] = {"code": "X", "expires": 9e9, "token": None, "servers": None, "resources": []}

        class R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"authToken": "ACCOUNT-TOKEN-mem"}
        with mock.patch.object(self.sf.plexhttp, "get", return_value=R()), \
                mock.patch.object(self.sf, "_servers", return_value=([], [])):
            self.assertEqual(self.sf.poll_pin(7)["state"], "authorized")
        self.assertIsNone(vault.get("plex_token"))
        with open(vault.CREDS_PATH, encoding="utf-8") as f:
            self.assertNotIn("ACCOUNT-TOKEN-mem", f.read())

    def test_plex_session_never_follows_redirects(self):
        import requests

        class Resp:
            status_code = 302
            is_redirect = True
            headers = {"location": "http://evil.example/"}

            def close(self):
                pass
        seen = {}

        def fake(self, method, url, **kw):
            seen.update(kw)
            return Resp()
        with mock.patch.object(requests.Session, "request", fake):
            with self.assertRaises(requests.HTTPError):
                plexhttp.get("http://plex.example:32400/library/sections", headers={"X-Plex-Token": "t"})
        self.assertIs(seen["allow_redirects"], False)
        self.assertFalse(plexhttp.Session().trust_env)

    def test_no_plex_call_bypasses_plexhttp(self):
        base = os.path.dirname(os.path.abspath(plexhttp.__file__))
        for root, _, files in os.walk(base):
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                with open(os.path.join(root, fn), encoding="utf-8") as f:
                    for i, line in enumerate(f, 1):
                        if re.search(r"\brequests\.(get|post|put|delete|request)\(", line) and \
                                ("PLEX" in line or "plex.tv" in line):
                            self.fail("%s:%d calls Plex without plexhttp" % (fn, i))


# ------------------------------------------------------------------ A3 + A5: notifications
class Notifications(_Web, unittest.TestCase):
    def test_test_button_rejects_non_discord_urls(self):
        import requests
        bad = ["http://169.254.169.254/latest/meta-data", "https://discord.com.evil.example/api/webhooks/1/x",
               "https://evil.example/?https://discord.com/api/webhooks/1/x", "http://discord.com/api/webhooks/1/abc",
               "https://discord.com/api/webhooks/123456/" + "a" * 30 + "/../../x", "file:///etc/passwd"]
        with mock.patch.object(requests.Session, "request") as req:
            for u in bad:
                with self.subTest(u=u):
                    r = call(self.app, "POST", "/api/notifications/test", headers=self.sess(),
                             body={"discord_webhook_url": u})
                    self.assertEqual(r["status"], 400, r["body"])
        req.assert_not_called()

    def test_post_uses_no_redirects_and_no_env_proxies(self):
        import requests
        seen = {}

        class Resp:
            status_code = 204

        def fake(self, method, url, **kw):
            seen.update(kw, trust_env=self.trust_env, url=url)
            return Resp()
        with mock.patch.object(requests.Session, "request", fake):
            notify.post(HOOK, "hello")
        self.assertIs(seen["allow_redirects"], False)
        self.assertFalse(seen["trust_env"])

    def test_webhook_is_a_write_only_secret(self):
        r = call(self.app, "PATCH", "/api/settings", headers=self.sess(),
                 body={"notifications": {"discord_webhook_url": HOOK}})
        self.assertEqual(r["status"], 200, r["body"])
        self.assertNotIn(HOOK, r["body"].decode())
        self.assertTrue(r["json"]["notifications"]["configured"])
        self.assertEqual(settings.load()["notifications"]["discord_webhook_url"], HOOK)
        with open(settings.PATH, encoding="utf-8") as f:
            self.assertNotIn("webhooks", f.read())
        if os.name == "posix":
            self.assertEqual(os.stat(settings.PATH).st_mode & 0o777, 0o600)
        k = call(self.app, "GET", "/api/settings", headers={"x-api-key": self.api_key})
        self.assertNotIn("hint", k["json"]["notifications"])
        self.assertNotIn(HOOK, k["body"].decode())
        self.assertNotIn("T" * 20, redact.text("posting to " + HOOK))
        # absent = keep, "" = remove
        call(self.app, "PATCH", "/api/settings", headers=self.sess(), body={"notifications": {"events": {}}})
        self.assertEqual(settings.load()["notifications"]["discord_webhook_url"], HOOK)
        call(self.app, "PATCH", "/api/settings", headers=self.sess(), body={"notifications": {"discord_webhook_url": ""}})
        self.assertEqual(settings.load()["notifications"]["discord_webhook_url"], "")


# ------------------------------------------------------------------ M1: age-restricted is a skip
class AgeRestricted(unittest.TestCase):
    def test_classification(self):
        from tubarr import ytdl
        age = ["ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate for some users.",
               "ERROR: This video is age-restricted and only available on YouTube"]
        bot = ["ERROR: [youtube] abc: Sign in to confirm you\u2019re not a bot. Use --cookies",
               "ERROR: Sign in to confirm you're not a bot", "HTTP Error 429: Too Many Requests"]
        for m in age:
            self.assertTrue(ytdl.is_age_restricted(m))
            self.assertFalse(ytdl.is_bot_check(m))
            with self.assertRaises(ytdl.AgeRestricted):
                ytdl._guard(RuntimeError(m))
        for m in bot:
            self.assertTrue(ytdl.is_bot_check(m))
            with self.assertRaises(ytdl.BotCheck):
                ytdl._guard(RuntimeError(m))

    def test_skip_reason(self):
        from tubarr import ytdl
        info = {"availability": "public", "duration": 600, "age_limit": 18}
        self.assertEqual(ytdl.skip_reason(info), ("skipped", "age-restricted"))
        self.assertIsNone(ytdl.skip_reason(dict(info, age_limit=0)))


# ------------------------------------------------------------------ M2: folders
def _mem_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(db.SCHEMA)
    for table, cols in db.MIGRATIONS.items():
        have = {r["name"] for r in c.execute("PRAGMA table_info(%s)" % table)}
        for col, typ in cols.items():
            if col not in have:
                c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, typ))
    return c


class Folders(unittest.TestCase):
    def test_collision_gets_a_unique_folder(self):
        from tubarr import pipeline
        c = _mem_db()
        c.execute("INSERT INTO channels(id, title, folder) VALUES(?,?,?)", (UC_A, "Gaming \U0001F3AE", "Gaming"))
        self.assertEqual(naming.show_folder("Gaming \u26a1"), "Gaming")
        f = pipeline.unique_folder(c, UC_B, "Gaming \u26a1")
        self.assertNotEqual(f.casefold(), "gaming")
        self.assertIn(UC_B, f)
        self.assertEqual(pipeline.unique_folder(c, UC_A, "Gaming \U0001F3AE"), "Gaming")   # its own is fine
        c.execute("INSERT INTO channels(id, title, folder) VALUES(?,?,?)", ("UC" + "c" * 22, "\U0001F525", "Untitled"))
        self.assertIn(UC_B, pipeline.unique_folder(c, UC_B, "\U0001F4A5\U0001F4A5"))

    def test_publish_refuses_to_overwrite_another_videos_file(self):
        from tubarr import pipeline
        dest = tempfile.mkdtemp(dir=config.ROOT)
        src = os.path.join(tempfile.mkdtemp(), "x.nfo")
        with open(src, "w") as f:
            f.write("new")
        other = os.path.join(dest, "Show - S2025E010101 - Same.nfo")
        with open(other, "w") as f:
            f.write("other video")
        with mock.patch.object(pipeline, "FILE_LOCK"):
            with self.assertRaises(RuntimeError):
                pipeline.publish({"Show - S2025E010101 - Same.nfo": src}, dest, "aaaaaaaaaaa")
            with open(other) as f:
                self.assertEqual(f.read(), "other video")
            pipeline.publish({"Show - S2025E010101 - Same.nfo": src}, dest, "aaaaaaaaaaa", own={other})
            with open(other) as f:
                self.assertEqual(f.read(), "new")
            with self.assertRaises(ValueError):
                pipeline.publish({"a.nfo": src}, dest, "../../etc/x")

    def test_channel_delete_keeps_a_shared_folder(self):
        from tubarr import actions
        c = db.connect()
        shared = os.path.join(config.ROOT, "Shared Name")
        os.makedirs(shared, exist_ok=True)
        mine, theirs = os.path.join(shared, "mine.mkv"), os.path.join(shared, "theirs.mkv")
        for p in (mine, theirs):
            with open(p, "w") as f:
                f.write("v")
        c.execute("DELETE FROM channels WHERE id IN (?,?)", (UC_A, UC_B))
        c.execute("INSERT INTO channels(id, title, folder) VALUES(?,?,?)", (UC_A, "A", "Shared Name"))
        c.execute("INSERT INTO channels(id, title, folder) VALUES(?,?,?)", (UC_B, "B", "shared name"))
        c.execute("INSERT OR REPLACE INTO videos(id, channel_id, state, path) VALUES(?,?,?,?)",
                  ("mineaaaaaaa", UC_A, "done", mine))
        self.assertFalse(actions._folder_is_own(c, UC_A, shared))
        actions._delete_channel_files(UC_A, shared, manual=True)
        self.assertTrue(os.path.isdir(shared))
        self.assertTrue(os.path.exists(theirs))
        self.assertFalse(os.path.exists(mine))
        self.assertFalse(actions._folder_is_own(c, UC_B, config.ROOT))
        self.assertFalse(actions._folder_is_own(c, UC_B, os.path.join(config.ROOT, "..")))


# ------------------------------------------------------------------ L7 + L8
class TextAndIds(unittest.TestCase):
    def test_nfo_strips_invalid_xml_chars(self):
        p = os.path.join(tempfile.mkdtemp(), "tvshow.nfo")
        nfo.tvshow(p, UC_A, "Bad\x00Ti\x0btle\ud800\ufffe ok \U0001F600", "Plot\x1bhere\x07", genre="Mix\x01")
        root = ET.parse(p).getroot()
        self.assertEqual(root.find("title").text, "BadTitle ok \U0001F600")
        self.assertEqual(root.find("plot").text, "Plothere")

    def test_video_id_validation(self):
        for good in ("dQw4w9WgXcQ", "a-b_c-d_e-f", "00000000000"):
            self.assertTrue(naming.valid_video_id(good))
        for bad in ("../../etc/x", "dQw4w9WgXc", "dQw4w9WgXcQQ", "dQw4w9WgXc/", "dQw4w9WgXc\n", "", None, "dQw4w9W.XcQ"):
            self.assertFalse(naming.valid_video_id(bad))
            with self.assertRaises(ValueError):
                naming.check_video_id(bad)

    def test_subtitles_decode_does_not_bring_markup_back(self):
        from tubarr import subtitles
        self.assertEqual(subtitles._clean("&lt;font color=red&gt;hi&lt;/font&gt; &amp;lt;b&amp;gt;"), "hi &lt;b&gt;")


# ------------------------------------------------------------------ M4: never-direct
class NeverDirect(unittest.TestCase):
    def setUp(self):
        settings.save({"network": dict(net.DEFAULTS)})

    def tearDown(self):
        settings.save({"network": dict(net.DEFAULTS)})

    def test_stays_on_after_the_last_proxy_is_deleted(self):
        out = net.add_proxy("Only", "socks5h://proxy.example:1080", "tester")
        pid = out["proxies"][-1]["id"]
        net.patch({"mode": "failover"}, "tester")
        self.assertTrue(net.never_direct(net.cfg()))
        net.delete_proxy(pid, "tester")
        c = net.cfg()
        self.assertEqual(c["mode"], "failover")                       # mode kept
        self.assertTrue(net.never_direct(c))                          # guard sticky
        self.assertNotIn("direct", net._candidates(c))
        with self.assertRaises(net.NetworkPaused):
            net.active_line()
        with self.assertRaises(ValueError):                           # can't re-enter a proxy mode without a proxy
            net.patch({"mode": "rotate"}, "tester")
        net.patch({"mode": "direct"}, "tester")                        # an explicit choice of Direct ends it
        self.assertFalse(net.never_direct(net.cfg()))
        self.assertEqual(net.active_line(), "direct")

    def test_test_button_failures_are_uniform(self):
        import requests
        net._TESTS.clear()
        msgs = set()
        with mock.patch.object(net, "_TEST_PAD", 0):
            for exc in (requests.exceptions.ProxyError("407 Proxy Authentication Required"),
                        requests.exceptions.ConnectTimeout("x"), requests.exceptions.ConnectionError("refused")):
                with mock.patch.object(net, "_test_once", side_effect=net.TestFailed(str(exc))):
                    with self.assertRaises(net.TestFailed) as e:
                        net.test_line("direct", "user:admin", pad=True)
                    msgs.add(str(e.exception))
        self.assertEqual(msgs, {net.TEST_FAILED})

    def test_yt_get_rejects_redirects_off_youtube(self):
        class Resp:
            def __init__(self, loc):
                self.is_redirect, self.headers, self.status_code = bool(loc), {"location": loc or ""}, 302 if loc else 200

            def close(self):
                pass

        class S:
            trust_env, proxies, headers = False, {}, {}

            def __init__(self, seq):
                self.seq = seq

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, **kw):
                assert kw.get("allow_redirects") is False
                return self.seq.pop(0)
        with mock.patch.object(net, "yt_session", lambda: S([Resp("http://169.254.169.254/")])):
            with self.assertRaises(ValueError):
                net.yt_get("https://i.ytimg.com/vi/x/maxresdefault.jpg")
        with mock.patch.object(net, "yt_session", lambda: S([Resp("https://yt3.ggpht.com/a"), Resp(None)])):
            self.assertEqual(net.yt_get("https://i.ytimg.com/vi/x/a.jpg").status_code, 200)


# ------------------------------------------------------------------ L9: proxy parts are redacted in every process
class Redaction(unittest.TestCase):
    def test_install_remembers_proxy_parts(self):
        out = net.add_proxy("Redact", "socks5h://bobuser:sw0rdfish99@proxy.example:1080", "tester")
        pid = out["proxies"][-1]["id"]
        try:
            with redact._LOCK:
                redact._KNOWN.clear()
            redact.install()
            s = redact.text("bobuser failed with sw0rdfish99 via socks5h://bobuser:sw0rdfish99@proxy.example:1080")
            self.assertNotIn("sw0rdfish99", s)
            self.assertNotIn("bobuser", s)
        finally:
            net.delete_proxy(pid, "tester")


# ------------------------------------------------------------------ L4: repolish passes ids
class Repolish(unittest.TestCase):
    def test_channel_is_passed_by_id_after_a_separator(self):
        from tubarr import actions
        c = db.connect()
        c.execute("INSERT OR REPLACE INTO channels(id, title, folder) VALUES(?,?,?)", (UC_A, "--all", "X"))
        with mock.patch.object(actions, "_repolish") as rp:
            actions.repolish_channel(UC_A)
        rp.assert_called_once_with(["--art", "--", UC_A])

    def test_parse_args(self):
        from tubarr import repolish
        self.assertEqual(repolish.parse_args(["--art", "--", UC_A]), ({"--art"}, [UC_A]))
        self.assertEqual(repolish.parse_args(["--art", "--", "--all", "My Title"]), ({"--art"}, []))
        self.assertEqual(repolish.parse_args([UC_B, "--all"]), ({"--all"}, [UC_B]))


# ------------------------------------------------------------------ C2: Tubarr -> Trimarr carries the token
class TrimarrClient(unittest.TestCase):
    def test_token_header_no_redirects_no_env(self):
        from tubarr.webapp import addons
        seen = {}

        class Resp:
            status_code = 200

            def json(self):
                return {"ok": True}

        def fake(method, url, **kw):
            seen.update(kw, url=url)
            return Resp()
        with mock.patch.object(addons, "TRIMARR_URL", "http://trimarr:8791"), \
                mock.patch.object(addons, "TRIMARR_TOKEN", "t" * 32), \
                mock.patch.object(addons._SESSION, "request", side_effect=fake):
            addons._t("GET", "/api/status")
        self.assertEqual(seen["headers"]["X-Trimarr-Token"], "t" * 32)
        self.assertIs(seen["allow_redirects"], False)
        self.assertFalse(addons._SESSION.trust_env)

    def test_short_token_fails_closed(self):
        from tubarr.webapp import addons
        with mock.patch.object(addons, "TRIMARR_URL", "http://trimarr:8791"), \
                mock.patch.object(addons, "TRIMARR_TOKEN", "short"), \
                mock.patch.object(addons._SESSION, "request") as req:
            with self.assertRaises(addons.ProxyError) as e:
                addons._t("GET", "/api/status")
        self.assertEqual(e.exception.status, 503)
        req.assert_not_called()


# ------------------------------------------------------------------ A8 + A9
class SessionsAndAudit(_Web, unittest.TestCase):
    def test_event_stream_closes_after_sign_out(self):
        from tubarr.webapp import events, security
        cookie, csrf = auth.new_session()

        class Req:
            cookies = {auth.SESSION_COOKIE: cookie}
            headers = {}
            url = type("U", (), {"scheme": "http"})()

            async def is_disconnected(self):
                return False

        async def run():
            gen = events.stream(Req(), self.appmod.D)
            await gen.__anext__()                                     # hello
            await gen.__anext__()                                     # status
            sid = auth.read_session(cookie)["sid"]
            auth.end_session(sid)
            async def timeout(aw, timeout=None):
                aw.close()
                raise asyncio.TimeoutError
            with mock.patch.object(events.asyncio, "wait_for", timeout):
                with self.assertRaises(StopAsyncIteration):
                    await gen.__anext__()
        self.assertEqual(security.identify(Req())[0], "session")
        asyncio.run(run())

    def test_noisy_audit_entries_are_folded(self):
        from tubarr import audit
        audit._NOISY.clear()
        before = len(audit.read(500))
        for _ in range(50):
            audit.write("x", "auth.login_failed", "from 192.0.2.1")
        self.assertEqual(len(audit.read(500)) - before, 1)
        self.assertGreaterEqual(audit.GENERATIONS, 3)


# ------------------------------------------------------------------ A11: __Host- cookies over HTTPS
class Cookies(_Web, unittest.TestCase):
    def test_host_prefix_over_https(self):
        auth._FAILS.clear()
        with mock.patch.dict(os.environ, {"TUBARR_COOKIE_SECURE": "1"}):
            r = call(self.app, "POST", "/api/auth/login", body={"username": "admin", "password": PASSWORD})
            self.assertEqual(r["status"], 200, r["body"])
            sc = " ".join(r["headers"].get("set-cookie", []))
            self.assertIn("__Host-" + auth.SESSION_COOKIE + "=", sc)
            self.assertIn("Secure", sc)
            val = re.search(r"__Host-%s=([^;]+)" % auth.SESSION_COOKIE, sc).group(1).strip('"')
            ok = call(self.app, "GET", "/api/auth/state",
                      headers={"cookie": "__Host-%s=%s" % (auth.SESSION_COOKIE, val)})
            self.assertTrue(ok["json"]["authenticated"])
            plain = call(self.app, "GET", "/api/auth/state", headers={"cookie": "%s=%s" % (auth.SESSION_COOKIE, val)})
            self.assertFalse(plain["json"]["authenticated"])           # no un-prefixed cookie over HTTPS


if __name__ == "__main__":
    unittest.main()
