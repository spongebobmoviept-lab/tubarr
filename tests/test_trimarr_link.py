"""Tubarr <-> Trimarr ship together: the link token is created automatically (tubarr/trimlink.py) and used by the
proxy (tubarr/webapp/addons.py) unless TRIMARR_TOKEN overrides it.

    python -m unittest discover -s tests -v        (in the image, see test_security.py)

No network: Trimarr is replaced by a fake.
"""
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock

if "tubarr.config" not in sys.modules and "TUBARR_DATA" not in os.environ:
    _TMP = tempfile.mkdtemp(prefix="tubarr-test3-")
    os.environ["TUBARR_DATA"] = os.path.join(_TMP, "data")
    os.environ["TUBARR_ROOT"] = os.path.join(_TMP, "youtube")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tubarr import redact, trimlink  # noqa: E402


class LinkDir(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="link-")
        os.chmod(self.dir, 0o750)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def path(self):
        return os.path.join(self.dir, trimlink.TOKEN_NAME)


class TokenFile(LinkDir):
    def test_created_once_and_reused(self):
        tok = trimlink.ensure_token(self.dir)
        self.assertRegex(tok, r"^[0-9a-f]{64}$")                     # secrets.token_hex(32)
        self.assertEqual(stat.S_IMODE(os.stat(self.path()).st_mode), 0o640)
        mtime = os.stat(self.path()).st_mtime_ns
        for _ in range(3):
            self.assertEqual(trimlink.ensure_token(self.dir), tok)
        self.assertEqual(os.stat(self.path()).st_mtime_ns, mtime)   # never rewritten
        self.assertEqual(trimlink.read_token(self.dir), tok)
        self.assertEqual(os.listdir(self.dir), [trimlink.TOKEN_NAME])   # no temporary files left behind

    def test_concurrent_starts_agree_on_one_token(self):
        out = []
        start = threading.Barrier(8)

        def go():
            start.wait()
            out.append(trimlink.ensure_token(self.dir))
        ths = [threading.Thread(target=go) for _ in range(8)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        self.assertEqual(len(set(out)), 1)
        self.assertEqual(out[0], trimlink.read_token(self.dir))
        self.assertEqual(os.listdir(self.dir), [trimlink.TOKEN_NAME])

    def test_damaged_file_is_replaced(self):
        with open(self.path(), "w") as f:
            f.write("short\n")
        tok = trimlink.ensure_token(self.dir)
        self.assertRegex(tok, r"^[0-9a-f]{64}$")
        self.assertEqual(trimlink.read_token(self.dir), tok)

    def test_symlink_is_not_followed(self):
        other = os.path.join(self.dir, "elsewhere")
        with open(other, "w") as f:
            f.write("x" * 64)
        os.symlink(other, self.path())
        self.assertEqual(trimlink.read_token(self.dir), "")

    def test_no_link_folder_means_no_token(self):
        self.assertEqual(trimlink.ensure_token(os.path.join(self.dir, "missing")), "")

    def test_read_only_link_folder_is_not_written(self):
        if os.getuid() == 0:
            self.skipTest("root ignores directory permissions")
        os.chmod(self.dir, 0o550)
        try:
            self.assertEqual(trimlink.ensure_token(self.dir), "")
            self.assertEqual(os.listdir(self.dir), [])
        finally:
            os.chmod(self.dir, 0o750)


class Proxy(LinkDir):
    """The proxy sends the link token, TRIMARR_TOKEN wins, and without either it fails closed."""

    def setUp(self):
        super().setUp()
        from tubarr.webapp import addons
        self.addons = addons
        self.seen = {}
        seen = self.seen

        class Resp:
            status_code = 200

            def json(self):
                return {"enabled": False, "seconds": {}, "worker": {}, "version": "x"}

        def fake(method, url, **kw):
            seen.update(kw, url=url)
            return Resp()
        self.patches = [mock.patch.object(trimlink, "LINK_DIR", self.dir),
                        mock.patch.object(addons, "TRIMARR_URL", "http://trimarr:8791"),
                        mock.patch.object(addons._SESSION, "request", side_effect=fake)]
        self.req = [p.start() for p in self.patches][-1]

    def tearDown(self):
        for p in self.patches:
            p.stop()
        super().tearDown()

    def test_default_url_and_off_switch(self):
        u = self.addons._trimarr_url
        self.assertEqual(u({}), "http://trimarr:8791")
        for off in ("", "off", "OFF", "none", "0"):
            self.assertEqual(u({"TRIMARR_URL": off}), "")
        self.assertEqual(u({"TRIMARR_URL": "http://trimarr.lan:8791/"}), "http://trimarr.lan:8791")

    def test_link_token_created_and_sent(self):
        with mock.patch.object(self.addons, "TRIMARR_TOKEN", ""):
            self.addons._t("GET", "/api/status")
        tok = trimlink.read_token(self.dir)
        self.assertRegex(tok, r"^[0-9a-f]{64}$")
        self.assertEqual(self.seen["headers"]["X-Trimarr-Token"], tok)
        self.assertIs(self.seen["allow_redirects"], False)
        self.assertNotIn(tok, redact.text("sent %s to trimarr" % tok))      # scrubbed from logs

    def test_env_override_wins(self):
        trimlink.ensure_token(self.dir)
        with mock.patch.object(self.addons, "TRIMARR_TOKEN", "E" * 40):
            self.addons._t("GET", "/api/status")
        self.assertEqual(self.seen["headers"]["X-Trimarr-Token"], "E" * 40)

    def test_fails_closed_without_a_link(self):
        with mock.patch.object(trimlink, "LINK_DIR", os.path.join(self.dir, "missing")), \
                mock.patch.object(self.addons, "TRIMARR_TOKEN", ""):
            with self.assertRaises(self.addons.ProxyError) as e:
                self.addons._t("GET", "/api/status")
        self.assertEqual(e.exception.status, 503)
        self.assertEqual(e.exception.code, "not_configured")
        self.req.assert_not_called()

    def test_token_never_in_status(self):
        with mock.patch.object(self.addons, "TRIMARR_TOKEN", ""):
            out = self.addons.trimarr("GET", "status", None, {})
        tok = trimlink.read_token(self.dir)
        self.assertTrue(tok)
        self.assertNotIn(tok, repr(out))


if __name__ == "__main__":
    unittest.main()
