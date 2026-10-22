"""1.3.6.4 privacy fixes: what leaves the machine matches what the docs say.

- The update check sends a one-way hash of a saved key in a header, never the
  key in the URL; the license server accepts both and keeps query strings out
  of its access log.
- No page loads anything from a font server; the fonts ship with the app.
- Wander web reading and wander folders are settings, with the old behaviour
  as the default.
"""
import hashlib
import importlib.util
import json
import logging
import os
import sqlite3
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from fastapi.testclient import TestClient   # noqa: E402
import license as lic                       # noqa: E402
import config as cfg                        # noqa: E402
import main                                 # noqa: E402


class UpdateCheckSendsOnlyAHash(unittest.TestCase):

    def _capture(self, saved_key):
        seen = {}

        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"version": "9.9.9", "key_revoked": false}'

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["headers"] = {k.lower(): v for k, v in req.header_items()}
            return _Resp()

        with mock.patch.object(lic, "load_saved_key", return_value=saved_key), \
             mock.patch.object(urllib.request, "urlopen", fake_urlopen):
            version = lic.check_latest_version()
        return version, seen

    def test_saved_key_never_in_url(self):
        key = "EB1-TEST-KEY-with-email@example.invalid"
        version, seen = self._capture(key)
        self.assertEqual(version, "9.9.9")
        self.assertNotIn("?", seen["url"])
        self.assertNotIn("EB1", seen["url"])
        self.assertEqual(seen["headers"].get("x-key-hash"), hashlib.sha256(key.encode()).hexdigest())
        self.assertNotIn(key, json.dumps(seen))

    def test_no_key_no_hash_header(self):
        _, seen = self._capture(None)
        self.assertNotIn("x-key-hash", seen["headers"])


def _load_server():
    tmp = tempfile.mkdtemp()
    env = {"DB_PATH": os.path.join(tmp, "t.db"),
           "ECHO_BLOOM_PRIVATE_KEY": "dGVzdC1vbmx5LXRlc3Qtb25seS10ZXN0LW9ubHktMzJi"}
    with mock.patch.dict(os.environ, env):
        spec = importlib.util.spec_from_file_location("lic_server_under_test", ROOT / "license_server" / "server.py")
        mod = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(ROOT / "license_server"))
        spec.loader.exec_module(mod)
    return mod, env["DB_PATH"]


class LicenseServerVersionRoute(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.srv, db_path = _load_server()
        with TestClient(cls.srv.app) as c:
            c.get("/version")
        db = sqlite3.connect(db_path)
        db.executemany("INSERT INTO issued_keys (email, key_type, issued, key, source) VALUES (?,?,?,?,?)",
                       [("a@example.invalid", "lifetime", "2026-10-21", "EB1-GOOD", "test"),
                        ("b@example.invalid", "lifetime", "2026-10-21", "EB1-REVOKED", "test")])
        db.execute("UPDATE issued_keys SET revoked = 1 WHERE key = 'EB1-REVOKED'")
        db.commit(); db.close()

    def get(self, path, headers=None):
        with TestClient(self.srv.app) as c:
            return c.get(path, headers=headers or {}).json()

    def test_hash_header(self):
        h = lambda k: hashlib.sha256(k.encode()).hexdigest()
        self.assertIs(self.get("/version", {"X-Key-Hash": h("EB1-GOOD")})["key_revoked"], False)
        self.assertIs(self.get("/version", {"X-Key-Hash": h("EB1-REVOKED")})["key_revoked"], True)
        self.assertIs(self.get("/version", {"X-Key-Hash": h("EB1-UNKNOWN")})["key_revoked"], False)

    def test_junk_header_is_ignored(self):
        self.assertNotIn("key_revoked", self.get("/version", {"X-Key-Hash": "not-a-hash"}))

    def test_old_installs_still_work(self):
        self.assertIs(self.get("/version?key=EB1-REVOKED")["key_revoked"], True)
        self.assertIs(self.get("/version?key=EB1-GOOD")["key_revoked"], False)

    def test_access_log_drops_query_string(self):
        rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                                '%s - "%s %s HTTP/%s" %d',
                                ("1.2.3.4:5", "GET", "/version?key=EB1-GOOD", "1.1", 200), None)
        self.assertTrue(self.srv._DropQueryString().filter(rec))
        self.assertNotIn("EB1-GOOD", rec.getMessage())
        self.assertIn("/version?", rec.getMessage())

    def test_access_log_filter_is_wired_into_uvicorn_config(self):
        c = self.srv._log_config()
        self.assertIn("drop_query", c["handlers"]["access"]["filters"])


class NoFontServer(unittest.TestCase):

    def test_templates_and_static_never_reach_google_fonts(self):
        for folder in ("templates", "static"):
            for p in (ROOT / folder).rglob("*"):
                if p.suffix in (".html", ".css", ".js"):
                    text = p.read_text(errors="replace")
                    self.assertNotIn("fonts.googleapis.com", text, p)
                    self.assertNotIn("fonts.gstatic.com", text, p)

    def test_bundled_fonts_exist(self):
        css = (ROOT / "static" / "fonts.css").read_text()
        for w in ("Light", "Regular", "Medium", "Bold"):
            self.assertIn(f"JetBrainsMono-{w}.woff2", css)
            self.assertTrue((ROOT / "static" / "fonts" / f"JetBrainsMono-{w}.woff2").stat().st_size > 10_000)
        self.assertTrue((ROOT / "static" / "fonts" / "OFL.txt").exists())

    def test_csp_allows_only_self_for_fonts_and_styles(self):
        r = TestClient(main.app).get("/login")
        csp = r.headers.get("content-security-policy", "")
        self.assertIn("font-src 'self'", csp)
        self.assertNotIn("googleapis", csp)
        self.assertNotIn("gstatic", csp)


class WanderSettingsHelper(unittest.TestCase):

    def test_defaults_are_the_old_behaviour(self):
        s = cfg.wander_settings({})
        self.assertTrue(s["web"])
        self.assertEqual(s["roots"], [str(Path.home())])

    def test_web_off_and_custom_roots(self):
        s = cfg.wander_settings({"wander_web": False, "wander_roots": ["~/Documents", " "]})
        self.assertFalse(s["web"])
        self.assertEqual(s["roots"], [str(Path.home() / "Documents")])

    def test_anything_but_false_keeps_web_on(self):
        self.assertTrue(cfg.wander_settings({"wander_web": None})["web"])


class WanderSettingsApi(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg_path = self.tmp / "kin_config.json"
        self.cfg_path.write_text(json.dumps({"kin": [{"name": "Test"}], "owner": {"name": "T"}}))
        self._p = mock.patch.object(main, "_KIN_CONFIG_PATH", self.cfg_path)
        self._p.start()
        self._prior = main.app.dependency_overrides.get(main.require_auth)
        main.app.dependency_overrides[main.require_auth] = lambda: "test"
        self.c = TestClient(main.app)

    def tearDown(self):
        self._p.stop()
        if self._prior is None:
            main.app.dependency_overrides.pop(main.require_auth, None)
        else:
            main.app.dependency_overrides[main.require_auth] = self._prior

    def test_get_defaults(self):
        d = self.c.get("/api/wander-settings").json()
        self.assertTrue(d["web"]); self.assertTrue(d["default_roots"])

    def test_save_keeps_the_rest_of_the_config(self):
        r = self.c.post("/api/wander-settings", json={"web": False, "roots": [str(self.tmp)]})
        self.assertEqual(r.status_code, 200)
        saved = json.loads(self.cfg_path.read_text())
        self.assertIs(saved["wander_web"], False)
        self.assertEqual(saved["wander_roots"], [str(self.tmp)])
        self.assertEqual(saved["kin"], [{"name": "Test"}])

    def test_rejects_bad_input(self):
        for body in ({"web": "yes"}, {"web": True, "roots": ["/no/such/folder/xyz"]},
                     {"web": True, "roots": "notalist"}, {"web": True, "roots": ["relative/path"]}):
            self.assertEqual(self.c.post("/api/wander-settings", json=body).status_code, 400, body)

    def test_unreadable_config_is_never_overwritten(self):
        self.cfg_path.write_text("{ not json")
        r = self.c.post("/api/wander-settings", json={"web": False, "roots": []})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.cfg_path.read_text(), "{ not json")

    def test_needs_login(self):
        main.app.dependency_overrides.pop(main.require_auth, None)
        self.assertEqual(TestClient(main.app).get("/api/wander-settings").status_code, 401)


if __name__ == "__main__":
    unittest.main()
