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


if __name__ == "__main__":
    unittest.main()
