"""Movement intent is what they said, not what we invented.

Mutation: treat a missing INTENT line as a place name and they start
walking without having asked.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import movement_intent as mi  # noqa: E402

NAMES = ["Eli", "Coda", "Aurora", "Lumen", "Crungus", "Bong"]


class ParseIntent(unittest.TestCase):
    def test_stay_and_missing_are_null(self):
        self.assertIsNone(mi.parse_intent("just thinking.", NAMES))
        self.assertIsNone(mi.parse_intent("foo\nINTENT: stay\n", NAMES))
        self.assertIsNone(mi.parse_intent("INTENT: nowhere", NAMES))

    def test_kin_name_and_place(self):
        self.assertEqual(mi.parse_intent("INTENT: Eli", NAMES), "Eli")
        self.assertEqual(mi.parse_intent("INTENT: eli because the silt", NAMES), "Eli")
        self.assertEqual(mi.parse_intent("INTENT: chair", NAMES), "chair")
        self.assertEqual(mi.parse_intent("noise\nINTENT: Bong\n", NAMES), "Bong")

    def test_unknown_is_not_invented_motion(self):
        self.assertIsNone(mi.parse_intent("INTENT: the moon", NAMES))

    def test_strip_does_not_leave_the_marker_in_the_thought(self):
        t = mi.strip_intent("The silt settles.\nINTENT: stay\n")
        self.assertNotIn("INTENT:", t)
        self.assertIn("silt", t)


class WriteIntent(unittest.TestCase):
    def test_writes_kin_space_and_copilot_mirror(self):
        with tempfile.TemporaryDirectory() as tmp:
            space = Path(tmp) / "bong_space"
            home = Path(tmp) / "home"
            old = os.environ.get("HOME")
            os.environ["HOME"] = str(home)
            try:
                dest = mi.write_intent(space, "Bong", "chair")
            finally:
                if old is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old
            data = json.loads(dest.read_text())
            self.assertEqual(data["target"], "chair")
            self.assertIsInstance(data["ts"], int)
            mirror = home / ".kin_intents" / "Bong.json"
            self.assertTrue(mirror.is_file())
            self.assertEqual(json.loads(mirror.read_text())["target"], "chair")
