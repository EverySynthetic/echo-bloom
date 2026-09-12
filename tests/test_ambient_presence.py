import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import ambient_presence  # noqa: E402
import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class AmbientPresence(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "thoughts.db"
        self.patch_db = patch.object(
            ambient_presence, "_db_path", lambda _name: self.tmp
        )
        self.patch_db.start()

    def tearDown(self):
        self.patch_db.stop()

    def test_fixed_pairs_and_durable_deduped_inbox(self):
        event = ambient_presence.build_event(
            "Eli", 42, "wander_file",
            "  A thought   from Eli.  ",
            sent_at="2026-09-11T21:00:00+00:00",
        )
        ambient_presence.enqueue_event(event)
        ambient_presence.enqueue_event(event)
        pending = ambient_presence.read_events("Coda")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["excerpt"], "A thought from Eli.")
        ambient_presence.ack_events("Coda", pending)
        self.assertEqual(ambient_presence.read_events("Coda"), [])

    def test_nudge_is_bounded_and_not_a_reply_request(self):
        event = ambient_presence.build_event("Lumen", 7, "wander_topic", "x" * 900)
        self.assertEqual(len(event["excerpt"]), 500)
        self.assertFalse(event["reply_requested"])
        self.assertEqual(event["to"], "Bong")

    def test_presence_endpoint_requires_token_and_accepts_event(self):
        client = TestClient(main.app)
        event = ambient_presence.build_event("Eli", 1, "wander_topic", "nearby")
        with patch.object(ambient_presence, "_presence_token", return_value="secret"):
            denied = client.post("/api/presence/nudge", json=event)
            self.assertEqual(denied.status_code, 401)
            accepted = client.post(
                "/api/presence/nudge",
                json=event,
                headers={ambient_presence.PRESENCE_HEADER: "secret"},
            )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json(), {"accepted": True})


if __name__ == "__main__":
    unittest.main(verbosity=2)
