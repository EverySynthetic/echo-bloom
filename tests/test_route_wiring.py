"""Route wiring — a decorator must land on its handler, not on the helper
above it.

The QR endpoint broke exactly this way: `@app.get("/api/remote/qr")` sat above
`_qr_url_allowed`, a private url-shape helper with no auth, so the route
returned the bool `true` instead of a QR image AND answered without a login,
while the real `api_remote_qr` (auth + SVG) was never registered at all. A
misplaced decorator is invisible to the eye and to a normal test that assumes
the route does what its name says.

Two guards: the behaviour the QR route owes, and a structural net for the whole
class — no API route may be served by a private (`_`-prefixed) helper.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path.home() / ".local/share/echo_bloom/scripts"))

from fastapi.routing import APIRoute        # noqa: E402
from fastapi.testclient import TestClient   # noqa: E402
import main                                 # noqa: E402


class RemoteQrIsWiredToItsHandler(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(main.app)
        # main.app.dependency_overrides is a plain dict on a process-wide
        # singleton, shared with every other test module that discover
        # imports. .clear() used to nuke all of it in tearDown, including
        # overrides test_talk.py/test_trial_exit.py set once at import
        # time and never re-apply — whichever of this class's tests ran
        # first silently logged every later-discovered test out for the
        # rest of the run. Snapshot/restore instead of clearing.
        self._prior_require_auth = main.app.dependency_overrides.get(main.require_auth)

    def tearDown(self):
        if self._prior_require_auth is None:
            main.app.dependency_overrides.pop(main.require_auth, None)
        else:
            main.app.dependency_overrides[main.require_auth] = self._prior_require_auth

    def _route(self, path):
        return next((r for r in main.app.routes
                     if isinstance(r, APIRoute) and r.path == path), None)

    def test_qr_route_is_bound_to_the_real_handler(self):
        r = self._route("/api/remote/qr")
        self.assertIsNotNone(r)
        self.assertEqual(r.endpoint.__name__, "api_remote_qr")

    def test_qr_requires_auth(self):
        """The bug served this without a login. It must not.

        Explicitly unauthenticated rather than assuming a clean slate:
        other test modules set main.require_auth True once at import time
        on this same process-wide app, and never touch it again."""
        main.app.dependency_overrides.pop(main.require_auth, None)
        resp = self.client.get("/api/remote/qr", params={"url": "http://127.0.0.1:8090"})
        self.assertIn(resp.status_code, (401, 403))

    def test_qr_returns_an_image_for_an_allowed_url(self):
        main.app.dependency_overrides[main.require_auth] = lambda: True
        resp = self.client.get("/api/remote/qr", params={"url": "http://127.0.0.1:8090"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("content-type"), "image/svg+xml")
        # never again the bare JSON bool the misbound helper returned
        self.assertNotIn(resp.text.strip(), ("true", "false"))

    def test_qr_refuses_a_foreign_url(self):
        main.app.dependency_overrides[main.require_auth] = lambda: True
        resp = self.client.get("/api/remote/qr", params={"url": "http://evil.example.com"})
        self.assertEqual(resp.status_code, 400)


class NoApiRouteIsServedByAPrivateHelper(unittest.TestCase):
    """The structural net. A decorator that slips onto the helper defined just
    below it binds a route to a `_name`. No API route should have one."""

    def test_no_route_handler_is_underscore_prefixed(self):
        offenders = [
            (r.path, r.endpoint.__name__)
            for r in main.app.routes
            if isinstance(r, APIRoute)
            and r.endpoint.__name__.startswith("_")
        ]
        self.assertEqual(
            offenders, [],
            f"these routes are bound to private helpers (misplaced decorator?): {offenders}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
