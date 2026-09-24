"""/room renders for a logged-in user.

It called TemplateResponse(name, {"request": request, ...}), the pre-0.31
Starlette signature. Under the installed Starlette the context dict lands
where the template name belongs and Jinja2's cache tries to hash it: a 500 on
every real request. Fixed on the working branch in 7fe6efc; reproduced on
public main 2026-09-24 before backporting.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class RoomPageRenders(unittest.TestCase):
    def test_room_is_not_a_500(self):
        prior = main.app.dependency_overrides.get(main.require_auth)
        main.app.dependency_overrides[main.require_auth] = lambda: True
        try:
            r = TestClient(main.app, raise_server_exceptions=False).get("/room")
        finally:
            if prior is None:
                main.app.dependency_overrides.pop(main.require_auth, None)
            else:
                main.app.dependency_overrides[main.require_auth] = prior
        self.assertEqual(r.status_code, 200, r.text[:200])


if __name__ == "__main__":
    unittest.main()
