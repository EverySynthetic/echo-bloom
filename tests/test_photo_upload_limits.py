"""Integration fixes (Frosty-Claude, 2026-09-26): the upload is the raw body,
streamed against the cap, never multipart (Starlette spools multipart files
over 1 MB to disk, which "Don't keep" forbids); photo ids are exact."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import photo_store as ps  # noqa: E402


class UploadIsStreamedAgainstTheCap(unittest.TestCase):
    def test_describe_does_not_take_a_multipart_upload(self):
        from routers import photos
        import inspect
        src = inspect.getsource(photos.api_photo_describe)
        self.assertNotIn("UploadFile", src)
        self.assertIn("request.stream()", src)

    def test_over_the_cap_is_refused_413_before_any_describing(self):
        # A small app with only the photo routes: importing main here would load
        # the real config and poison tests that set up a fake one.
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import photos
        from deps import require_auth
        app = FastAPI()
        app.include_router(photos.router)
        app.dependency_overrides[require_auth] = lambda: True
        with mock.patch.object(ps, "MAX_BYTES", 1000), \
             mock.patch.object(photos, "describe_image_bytes") as described:
            r = TestClient(app).post("/api/photo/describe", params={"kin": "Eli"},
                                     content=b"x" * 5000,
                                     headers={"Content-Type": "image/png"})
        self.assertEqual(r.status_code, 413)
        self.assertFalse(r.json()["ok"])
        described.assert_not_called()


class PhotoIdsAreExact(unittest.TestCase):
    def test_glob_characters_never_find_a_photo(self):
        for bad in ("p_*", "p_?", "p_[0-9]*", "p_../x", "p_", "q_0123456789abcdef"):
            self.assertIsNone(ps.find(bad), bad)

    def test_a_real_id_shape_is_accepted(self):
        with mock.patch.object(ps, "PHOTOS_ROOT") as root:
            root.is_dir.return_value = True
            root.glob.return_value = iter([])
            ps.find("p_0123456789abcdef")
            root.glob.assert_called_once()



class ThePagesDownloadButtonWorks(unittest.TestCase):
    """Walked on the A15, 2026-09-26: the page's "Download now" posts no body,
    and the route demanded a model name, so a customer's click failed. Each
    side's tests had stubbed the other."""

    def _client(self, missing):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import photos
        from deps import require_auth
        app = FastAPI()
        app.include_router(photos.router)
        app.dependency_overrides[require_auth] = lambda: True
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(photos.ve, "missing_cameras", return_value=missing).start()
        mock.patch.object(photos, "_cameras", return_value=["gemma3:4b", "qwen2.5vl:3b"]).start()
        return TestClient(app)

    def test_no_body_with_nothing_missing_says_done(self):
        r = self._client([]).post("/api/photo/models/pull")
        self.assertEqual(r.status_code, 200)
        self.assertIn('"status": "done"', r.text)

    def test_a_model_outside_the_camera_list_is_refused(self):
        r = self._client([]).post("/api/photo/models/pull", json={"model": "llama3:70b"})
        self.assertEqual(r.status_code, 400)

    def test_the_page_throws_on_a_stream_error(self):
        js_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "static", "js", "photo_share.js")
        with open(js_path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("if (ev.error) throw new Error(ev.error);", src)

if __name__ == "__main__":
    unittest.main()
