"""Security tests for Trimarr's API and planner. No network beyond 127.0.0.1, no Plex, no SponsorBlock.

Run inside the image (see README "Tests"):
  python -m unittest discover -s tests -v
"""
import http.client
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="trimarr-test-")
os.environ["TRIMARR_DATA"] = os.path.join(_TMP, "data")
os.environ["TRIMARR_ROOT"] = os.path.join(_TMP, "youtube")
os.environ["TRIMARR_LINK_DIR"] = os.path.join(_TMP, "link")      # the shared link volume (empty unless a test fills it)
os.makedirs(os.environ["TRIMARR_ROOT"], exist_ok=True)
os.environ.pop("TRIMARR_ALLOWED_HOSTS", None)

from http.server import ThreadingHTTPServer  # noqa: E402

from trimarr import api, config, library, planner, service, settings, sponsorblock, trimmer  # noqa: E402

TOKEN = "t0k3n-for-tests-0123456789abcdef"


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


class ApiSecurity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
        cls.srv.daemon_threads = True
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self._env = {k: os.environ.get(k) for k in ("TRIMARR_TOKEN", "TRIMARR_ALLOWED_HOSTS")}
        os.environ["TRIMARR_TOKEN"] = TOKEN
        os.environ.pop("TRIMARR_ALLOWED_HOSTS", None)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def req(self, method, path, token=None, host=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            if host is not False:
                c.putheader("Host", host or "127.0.0.1:%d" % self.port)
            if token is not None:
                c.putheader("X-Trimarr-Token", token)
            data = json.dumps(body).encode() if body is not None else b""
            if data:
                c.putheader("Content-Type", "application/json")
            c.putheader("Content-Length", str(len(data)))
            c.endheaders(data)
            r = c.getresponse()
            raw = r.read()
            try:
                out = json.loads(raw) if raw else None
            except ValueError:
                out = raw
            return r.status, {k.lower(): v for k, v in r.getheaders()}, out
        finally:
            c.close()

    # -- token
    def test_health_needs_no_token(self):
        st, _, body = self.req("GET", "/health")
        self.assertEqual(st, 200)
        self.assertTrue(body["ok"])

    def test_missing_token_rejected(self):
        st, _, body = self.req("GET", "/api/status")
        self.assertEqual(st, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")

    def test_wrong_token_rejected(self):
        for bad in ("nope", TOKEN[:-1] + "X", TOKEN + "x", TOKEN.upper(), ""):
            st, _, _ = self.req("GET", "/api/settings", token=bad)
            self.assertEqual(st, 401, bad)

    def test_wrong_token_rejected_on_writes(self):
        st, _, _ = self.req("POST", "/api/pause", token="wrong-token-wrong-token", body={})
        self.assertEqual(st, 401)
        st, _, _ = self.req("PATCH", "/api/settings", body={"enabled": True})
        self.assertEqual(st, 401)
        self.assertFalse(settings.load()["enabled"])

    def test_right_token_ok(self):
        st, _, body = self.req("GET", "/api/status", token=TOKEN)
        self.assertEqual(st, 200)
        self.assertIn("version", body)
        self.assertNotIn(TOKEN, json.dumps(body))
        st, _, body = self.req("GET", "/api/settings", token=TOKEN)
        self.assertEqual(st, 200)
        self.assertNotIn(TOKEN, json.dumps(body))

    def test_unset_token_refuses(self):
        os.environ.pop("TRIMARR_TOKEN", None)
        st, _, body = self.req("GET", "/api/status", token="anything-anything-anything")
        self.assertEqual(st, 503)
        self.assertEqual(body["error"]["code"], "not_configured")
        st, _, _ = self.req("GET", "/api/status", token="")
        self.assertEqual(st, 503)
        st, _, _ = self.req("GET", "/health")
        self.assertEqual(st, 200)

    def test_short_token_refuses(self):
        os.environ["TRIMARR_TOKEN"] = "short-token"
        st, _, _ = self.req("GET", "/api/status", token="short-token")
        self.assertEqual(st, 503)
        self.assertFalse(api.token_ok())

    # -- CORS
    def test_no_cors_headers(self):
        for args in (("GET", "/health", None), ("GET", "/api/status", TOKEN), ("GET", "/api/status", None),
                     ("GET", "/api/nope", TOKEN)):
            _, headers, _ = self.req(args[0], args[1], token=args[2])
            self.assertFalse([h for h in headers if h.startswith("access-control-")], args)

    def test_options_not_allowed(self):
        self.assertFalse(hasattr(api.Handler, "do_OPTIONS"))
        st, headers, _ = self.req("OPTIONS", "/api/status", token=TOKEN)
        self.assertGreaterEqual(st, 400)
        self.assertFalse([h for h in headers if h.startswith("access-control-")])

    # -- Host
    def test_bad_host_rejected(self):
        for host in ("evil.example", "evil.example:8791", "trimarr.evil.example", "10.0.0.5:8791", "[::1]:8791"):
            st, _, body = self.req("GET", "/api/status", token=TOKEN, host=host)
            self.assertEqual(st, 421, host)
            st, _, _ = self.req("GET", "/health", host=host)
            self.assertEqual(st, 421, host)

    def test_missing_host_rejected(self):
        st, _, _ = self.req("GET", "/api/status", token=TOKEN, host=False)
        self.assertIn(st, (400, 421))

    def test_allowed_hosts(self):
        for host in ("trimarr:8791", "TRIMARR", "localhost", "127.0.0.1:%d" % self.port):
            st, _, _ = self.req("GET", "/api/status", token=TOKEN, host=host)
            self.assertEqual(st, 200, host)
        os.environ["TRIMARR_ALLOWED_HOSTS"] = "trimarr-2, my.box "
        st, _, _ = self.req("GET", "/api/status", token=TOKEN, host="my.box:8791")
        self.assertEqual(st, 200)
        st, _, _ = self.req("GET", "/api/status", token=TOKEN, host="trimarr-2")
        self.assertEqual(st, 200)

    # -- ids through the API
    def test_bad_video_id_404(self):
        for p in ("/api/videos/..", "/api/videos/%2e%2e%2fetc", "/api/videos/abc", "/api/videos/abcdefghijk%0a"):
            st, _, _ = self.req("GET", p, token=TOKEN)
            self.assertEqual(st, 404, p)


def _s(**kw):
    s = dict(settings.DEFAULTS)
    s.update(kw)
    return s


def _seg(start, end, **kw):
    d = {"category": "sponsor", "actionType": "skip", "start": start, "end": end, "videoDuration": 600.0,
         "votes": 0, "locked": 0, "UUID": "u%d" % start}
    d.update(kw)
    return d


class SponsorBlockTrust(unittest.TestCase):
    NOW = 1_800_000_000.0
    OLD_MS = (NOW - 3 * 86400) * 1000
    NEW_MS = (NOW - 3600) * 1000

    def plan(self, segs, **kw):
        return planner.make_plan(segs, 600.0, _s(**kw), now=self.NOW)

    def test_defaults(self):
        self.assertEqual(settings.DEFAULTS["min_votes"], 1)
        self.assertEqual(settings.DEFAULTS["min_segment_age_hours"], 24)
        self.assertEqual(settings.DEFAULTS["keep_originals_days"], 7)

    def test_unvoted_unlocked_dropped(self):
        p = self.plan([_seg(100, 130, votes=0, timeSubmitted=self.OLD_MS)])
        self.assertEqual(p["cuts"], [])
        self.assertEqual(len(p["ignored"]), 1)
        self.assertIn("votes", p["ignored"][0]["why"])

    def test_downvoted_dropped(self):
        self.assertEqual(self.plan([_seg(100, 130, votes=-2, timeSubmitted=self.OLD_MS)])["cuts"], [])

    def test_missing_votes_dropped(self):
        self.assertEqual(self.plan([_seg(100, 130, votes=None, timeSubmitted=self.OLD_MS)])["cuts"], [])

    def test_locked_kept(self):
        p = self.plan([_seg(100, 130, votes=0, locked=1, timeSubmitted=self.NEW_MS)])
        self.assertEqual(len(p["cuts"]), 1)
        self.assertEqual(p["ignored"], [])

    def test_voted_old_kept(self):
        p = self.plan([_seg(100, 130, votes=1, timeSubmitted=self.OLD_MS)])
        self.assertEqual(len(p["cuts"]), 1)

    def test_too_new_dropped(self):
        p = self.plan([_seg(100, 130, votes=5, timeSubmitted=self.NEW_MS)])
        self.assertEqual(p["cuts"], [])
        self.assertIn("h ago", p["ignored"][0]["why"])

    def test_first_seen_fallback(self):
        self.assertEqual(self.plan([_seg(100, 130, votes=5, first_seen=self.NOW - 3600)])["cuts"], [])
        self.assertEqual(len(self.plan([_seg(100, 130, votes=5, first_seen=self.NOW - 2 * 86400)])["cuts"]), 1)
        self.assertEqual(self.plan([_seg(100, 130, votes=5)])["cuts"], [])      # age unknown: not used

    def test_min_votes_setting(self):
        self.assertEqual(self.plan([_seg(100, 130, votes=2, timeSubmitted=self.OLD_MS)], min_votes=3)["cuts"], [])
        self.assertEqual(len(self.plan([_seg(100, 130, votes=3, timeSubmitted=self.OLD_MS)], min_votes=3)["cuts"]), 1)
        # a stored value below the floor can't loosen the rule
        self.assertEqual(self.plan([_seg(100, 130, votes=0, timeSubmitted=self.OLD_MS)], min_votes=0)["cuts"], [])

    def test_age_setting_floor(self):
        seg = _seg(100, 130, votes=5, timeSubmitted=(self.NOW - 2 * 3600) * 1000)
        self.assertEqual(self.plan([seg], min_segment_age_hours=1)["cuts"], [])

    def test_settings_validation(self):
        with self.assertRaises(settings.SettingsError):
            settings.validate({"min_votes": 0})
        with self.assertRaises(settings.SettingsError):
            settings.validate({"min_segment_age_hours": 1})
        self.assertEqual(settings.validate({"min_votes": 3, "min_segment_age_hours": 48}),
                         {"min_votes": 3.0, "min_segment_age_hours": 48.0})

    def test_stamp_first_seen(self):
        prev = [{"UUID": "a", "first_seen": 111.0}]
        out = sponsorblock.stamp_first_seen([{"UUID": "a"}, {"UUID": "b"}, {}], prev, now=999.0)
        self.assertEqual([x["first_seen"] for x in out], [111.0, 999.0, 999.0])


class VideoIds(unittest.TestCase):
    def test_valid(self):
        for v in ("dQw4w9WgXcQ", "a-b_c-d_e-f", "___________"):
            self.assertTrue(library.valid_video_id(v), v)

    def test_invalid(self):
        for v in (None, "", "abc", "dQw4w9WgXcQx", "../../etc/p", "dQw4w9WgXc/", "dQw4w9WgXc\n", "dQw4w9WgXcQ\n",
                  "dQw4 9WgXcQ", "..........."):
            self.assertFalse(library.valid_video_id(v), repr(v))

    def test_nfo_uniqueid_validated(self):
        d = tempfile.mkdtemp(dir=_TMP)
        good = os.path.join(d, "good.nfo")
        bad = os.path.join(d, "bad.nfo")
        with open(good, "w") as f:
            f.write('<episodedetails><title>x</title><uniqueid type="youtube">dQw4w9WgXcQ</uniqueid></episodedetails>')
        with open(bad, "w") as f:
            f.write('<episodedetails><title>x</title><uniqueid type="youtube">../../../../tmp/pwn</uniqueid>'
                    '</episodedetails>')
        self.assertEqual(library.read_episode(good)["video_id"], "dQw4w9WgXcQ")
        self.assertIsNone(library.read_episode(bad)["video_id"])

    def test_trim_rejects_bad_id(self):
        before = os.path.exists(os.path.join(os.environ["TRIMARR_ROOT"], ".trim-work"))
        with self.assertRaises(trimmer.Skip):
            trimmer.trim("../../evil")
        self.assertEqual(os.path.exists(os.path.join(os.environ["TRIMARR_ROOT"], ".trim-work")), before)


# ------------------------------------------------------------------ the automatic link token (shared volume)
class LinkToken(unittest.TestCase):
    """Trimarr reads the token Tubarr created in /link when TRIMARR_TOKEN isn't set; TRIMARR_TOKEN wins."""
    LINK = "L1nk-" + "a" * 64
    setUpClass = classmethod(ApiSecurity.setUpClass.__func__)
    tearDownClass = classmethod(ApiSecurity.tearDownClass.__func__)
    req = ApiSecurity.req

    def setUp(self):
        ApiSecurity.setUp(self)
        os.environ.pop("TRIMARR_TOKEN", None)
        self.link = tempfile.mkdtemp(prefix="link-", dir=_TMP)
        self._p = mock.patch.object(config, "LINK_DIR", self.link)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        shutil.rmtree(self.link, ignore_errors=True)
        ApiSecurity.tearDown(self)

    def write(self, value):
        with open(os.path.join(self.link, config.LINK_TOKEN), "w") as f:
            f.write(value + "\n")

    def test_reads_the_token_from_the_link_file(self):
        self.write(self.LINK)
        self.assertEqual(config.api_token(), self.LINK)
        st, _, body = self.req("GET", "/api/status", token=self.LINK)
        self.assertEqual(st, 200)
        self.assertNotIn(self.LINK, json.dumps(body))
        st, _, _ = self.req("GET", "/api/status", token="wrong-" + "b" * 40)
        self.assertEqual(st, 401)

    def test_fails_closed_without_a_token(self):
        self.assertEqual(config.api_token(), "")
        st, _, body = self.req("GET", "/api/status", token="anything-anything-anything")
        self.assertEqual(st, 503)
        self.assertEqual(body["error"]["code"], "not_configured")
        st, _, _ = self.req("GET", "/api/status", token="")
        self.assertEqual(st, 503)
        self.assertEqual(self.req("GET", "/health")[0], 200)

    def test_unusable_link_files_fail_closed(self):
        for bad in ("", "short", "has spaces in the middle of it ok", "x" * 600):
            self.write(bad)
            self.assertEqual(config.api_token(), "", bad)
            self.assertEqual(self.req("GET", "/api/status", token=bad or "z" * 20)[0], 503)

    def test_symlinked_token_file_is_not_followed(self):
        other = os.path.join(self.link, "elsewhere")
        with open(other, "w") as f:
            f.write(self.LINK)
        os.symlink(other, os.path.join(self.link, config.LINK_TOKEN))
        self.assertEqual(config.api_token(), "")

    def test_env_override_wins(self):
        self.write(self.LINK)
        os.environ["TRIMARR_TOKEN"] = TOKEN
        self.assertEqual(config.api_token(), TOKEN)
        self.assertEqual(self.req("GET", "/api/status", token=TOKEN)[0], 200)
        self.assertEqual(self.req("GET", "/api/status", token=self.LINK)[0], 401)
        os.environ["TRIMARR_TOKEN"] = "short-token"      # a bad override is not silently replaced by the file
        self.assertEqual(self.req("GET", "/api/status", token=self.LINK)[0], 503)

    def test_token_is_read_live(self):
        self.assertEqual(self.req("GET", "/api/status", token=self.LINK)[0], 503)
        self.write(self.LINK)
        self.assertEqual(self.req("GET", "/api/status", token=self.LINK)[0], 200)

    def test_waits_for_the_token_at_start(self):
        threading.Timer(0.3, self.write, args=(self.LINK,)).start()
        t0 = time.monotonic()
        self.assertTrue(api.wait_for_token(timeout=10, poll=0.05))
        self.assertLess(time.monotonic() - t0, 5)

    def test_gives_up_waiting_and_stays_closed(self):
        with self.assertLogs("trimarr.api", "ERROR") as cm:
            self.assertFalse(api.wait_for_token(timeout=0.2, poll=0.05))
        self.assertFalse(api.token_ok())
        self.assertNotIn(self.LINK, " ".join(cm.output))

    def test_token_never_logged(self):
        self.write(self.LINK)
        with self.assertLogs("trimarr.api", "INFO") as cm:
            self.assertTrue(api.wait_for_token(timeout=1, poll=0.05))
        self.assertNotIn(self.LINK, " ".join(cm.output))


# ------------------------------------------------------------------ off = idle: nothing happens on its own
class IdleWhileOff(unittest.TestCase):
    def setUp(self):
        settings.update({"enabled": False, "paused": False})
        self.video = os.path.join(config.ROOT, "Some Channel", "Season 2026", "clip [abcdefghijk].mkv")
        os.makedirs(os.path.dirname(self.video), exist_ok=True)
        with open(self.video, "wb") as f:
            f.write(b"not really a video")
        self.before = self.snapshot()
        self.patches = [mock.patch.object(sponsorblock.requests, "get", side_effect=AssertionError("network")),
                        mock.patch.object(sponsorblock, "fetch", side_effect=AssertionError("SponsorBlock")),
                        mock.patch.object(trimmer, "sync_library", side_effect=AssertionError("scan")),
                        mock.patch.object(trimmer, "check", side_effect=AssertionError("check")),
                        mock.patch.object(trimmer, "housekeeping", side_effect=AssertionError("housekeeping")),
                        mock.patch.object(service.signal, "signal"),
                        mock.patch.object(service.api, "start"),
                        mock.patch.object(service.api, "wait_for_token", return_value=True)]
        self.mocks = [p.start() for p in self.patches]

    def tearDown(self):
        for p in self.patches:
            p.stop()
        settings.update({"enabled": False, "paused": False})

    @staticmethod
    def snapshot():
        out = {}
        for dp, dns, fns in os.walk(config.ROOT):
            for n in dns + fns:
                p = os.path.join(dp, n)
                st = os.stat(p)
                out[p] = (st.st_size, st.st_mtime_ns)
        return out

    def run_service(self):
        svc = service.Service()
        th = threading.Thread(target=svc.run_forever, daemon=True)
        th.start()
        return svc, th

    def stop(self, svc, th):
        svc.stop.set()
        svc.wake.set()
        th.join(5)
        self.assertFalse(th.is_alive())

    def test_idle_while_off_does_nothing(self):
        svc, th = self.run_service()
        with mock.patch.object(service.Service, "run_pass", side_effect=AssertionError("pass")) as rp:
            time.sleep(0.6)
            svc.wake.set()                               # a wake-up (e.g. an API call) still starts nothing
            time.sleep(0.3)
            self.assertEqual(svc.state, "off")
            self.assertIsNone(svc.next_pass_at)
            self.stop(svc, th)
            rp.assert_not_called()
        for m in self.mocks[:5]:
            m.assert_not_called()
        self.assertEqual(self.snapshot(), self.before)   # no file changed, no .trim-work / .trim-originals
        self.assertFalse(os.path.exists(config.WORK))
        self.assertFalse(os.path.exists(config.ORIGINALS))

    def test_resume_while_off_starts_nothing(self):
        svc = service.Service()
        api._SERVICE[0] = svc
        try:
            api._post_resume(None, {}, None)
        finally:
            api._SERVICE[0] = None
        self.assertFalse(svc.pass_requested)
        self.assertFalse(svc.active())

    def test_switching_on_starts_a_pass(self):
        started = threading.Event()
        svc, th = self.run_service()
        with mock.patch.object(service.Service, "run_pass", side_effect=lambda: started.set()):
            time.sleep(0.3)
            self.assertFalse(started.is_set())
            api._SERVICE[0] = svc
            try:
                api._patch_settings(None, {}, {"enabled": True})      # what Tubarr's switch sends
            finally:
                api._SERVICE[0] = None
            self.assertTrue(started.wait(5))
            self.stop(svc, th)


if __name__ == "__main__":
    unittest.main()
