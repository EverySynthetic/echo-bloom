"""Photo API routes: auth, needs_models, describe with stubs."""
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main  # noqa: E402
import photo_store as ps  # noqa: E402
import vision_eyes as ve  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(main.app)

PHOTO_ROUTES = [
    ("GET", "/api/photo/settings"),
    ("POST", "/api/photo/settings"),
    ("POST", "/api/photo/purge"),
    ("POST", "/api/photo/describe"),
    ("POST", "/api/photo/models/pull"),
    ("GET", "/api/photo/p_doesnotexist"),
]


def _png_bytes() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


class EveryRouteRequiresAuth(unittest.TestCase):
    def setUp(self):
        self._prior = main.app.dependency_overrides.get(main.require_auth)
        main.app.dependency_overrides.pop(main.require_auth, None)

    def tearDown(self):
        if self._prior is None:
            main.app.dependency_overrides.pop(main.require_auth, None)
        else:
            main.app.dependency_overrides[main.require_auth] = self._prior

    def test_every_photo_route_returns_401_without_auth(self):
        for method, path in PHOTO_ROUTES:
            if method == "GET":
                r = client.get(path)
            elif path.endswith("/describe"):
                r = client.post(
                    path,
                    files={"image": ("x.png", _png_bytes(), "image/png")},
                    data={"kin": "Eli"},
                )
            elif path.endswith("/settings") and method == "POST":
                r = client.post(path, json={"retention": "none"})
            elif path.endswith("/pull"):
                r = client.post(path, json={"model": "gemma3:4b"})
            else:
                r = client.post(path)
            self.assertIn(r.status_code, (401, 403), f"{method} {path} -> {r.status_code}")


class NeedsModels(unittest.TestCase):
    def setUp(self):
        main.app.dependency_overrides[main.require_auth] = lambda: True
        self._prior_auth = True

    def tearDown(self):
        main.app.dependency_overrides[main.require_auth] = lambda: True

    def test_missing_models_produce_needs_models(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "photos"
            with patch.object(ps, "PHOTOS_ROOT", root), \
                 patch.object(ve, "installed_models", return_value=set()), \
                 patch("auth.load_config", return_value={"photo_retention": "none", "vision_models": ["gemma3:4b"]}):
                r = client.post(
                    "/api/photo/describe",
                    files={"image": ("x.png", _png_bytes(), "image/png")},
                    data={"kin": "Eli"},
                )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertFalse(body.get("ok"))
        self.assertIn("needs_models", body)
        self.assertTrue(body["needs_models"])
        self.assertEqual(body["needs_models"][0]["name"], "gemma3:4b")


class DescribeWithStubs(unittest.TestCase):
    def setUp(self):
        main.app.dependency_overrides[main.require_auth] = lambda: True

    def _stub_describe(self, retention, cameras=None):
        """Patch the describe path: real normalise/save, stubbed eyes."""
        cameras = cameras or ["gemma3:4b", "qwen2.5vl:3b"]
        cam_text = (
            "SCENE: a park\nPEOPLE: none\nTHINGS: a red ball\n"
            "TEXT: NONE\nDETAILS: bright sun\n"
        )
        real_look = ve.look

        def fake_look(image, cameras=None, host="http://localhost:11434", _ask_fn=None):
            return real_look(
                image,
                cameras=cameras or ["gemma3:4b"],
                _ask_fn=lambda m, h, i, timeout=300: cam_text,
            )

        root = Path(tempfile.mkdtemp()) / "photos"
        self.addCleanup(lambda: __import__("shutil").rmtree(root.parent, ignore_errors=True))
        cfg = {"photo_retention": retention, "vision_models": cameras}
        return root, [
            patch("routers.photos.ps.PHOTOS_ROOT", root),
            patch("routers.photos.ve.look", side_effect=fake_look),
            patch("routers.photos.ve.missing_cameras", return_value=[]),
            patch("routers.photos.auth.load_config", return_value=cfg),
            patch("auth.load_config", return_value=cfg),
        ]

    def test_describe_two_camera_contract_shape(self):
        root, patches = self._stub_describe("none")
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            r = client.post(
                "/api/photo/describe",
                files={"image": ("x.png", _png_bytes(), "image/png")},
                data={"kin": "Eli"},
            )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"], body)
        self.assertTrue(body["id"].startswith("p_"))
        self.assertIn("[/photo]", body["for_kin"])
        self.assertIn("SCENE:", body["for_kin"])
        self.assertFalse(body["kept"])
        self.assertIsNone(body["thumb_url"])
        self.assertEqual(list(root.rglob("*")) if root.exists() else [], [])
        self.assertIn("mode", body)
        self.assertIn("eyes", body)

    def test_describe_retention_forever_keeps_file(self):
        root, patches = self._stub_describe("forever", cameras=["gemma3:4b"])
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            r = client.post(
                "/api/photo/describe",
                files={"image": ("x.png", _png_bytes(), "image/png")},
                data={"kin": "Eli"},
            )
            body = r.json()
            self.assertTrue(body["ok"], body)
            self.assertTrue(body["kept"])
            self.assertTrue(body["thumb_url"].startswith("/api/photo/"))
            self.assertTrue(list(root.glob("*/*.jpg")))
            gr = client.get(body["thumb_url"])
            # find() uses ps.PHOTOS_ROOT — patch for GET too
            with patch("routers.photos.ps.PHOTOS_ROOT", root), \
                 patch.object(ps, "PHOTOS_ROOT", root):
                gr = client.get(body["thumb_url"])
            self.assertEqual(gr.status_code, 200)
            self.assertEqual(gr.headers["content-type"], "image/jpeg")


class SettingsAndPurge(unittest.TestCase):
    def setUp(self):
        main.app.dependency_overrides[main.require_auth] = lambda: True

    def test_settings_get_shape(self):
        with patch.object(ve, "installed_models", return_value={"gemma3:4b"}), \
             patch.object(ve, "missing_cameras", return_value=[]), \
             patch("auth.load_config", return_value={"photo_retention": "90d"}):
            r = client.get("/api/photo/settings")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn(body["retention"], ("none", "90d", "forever"))
        self.assertIn("models", body)
        self.assertIn("camera1", body["models"])


if __name__ == "__main__":
    unittest.main()
