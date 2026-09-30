"""Two-eyes / one-camera filter for shared photos.

Stub cameras only — no Ollama. Shows agreed, disputed, one-camera labelling,
and that raw eye text never reaches for_kin.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import vision_eyes as ve  # noqa: E402


def _cam(text: str):
    def _ask(model, host, image, timeout=300):
        return text
    return _ask


AGREE_A = """\
SCENE: a kitchen table at breakfast
PEOPLE: two people sitting, one in a blue shirt
THINGS: a cake with candles
TEXT: Happy Birthday
DETAILS: warm yellow light from a window
"""

AGREE_B = """\
SCENE: breakfast at a kitchen table
PEOPLE: two people, blue shirt on one
THINGS: birthday cake and candles
TEXT: Happy Birthday
DETAILS: yellow window light
"""

DISPUTE = """\
SCENE: a kitchen table at breakfast
PEOPLE: two people sitting
THINGS: a vase of flowers
TEXT: NONE
DETAILS: cold blue fluorescent light
"""

ONE_ONLY = """\
SCENE: a snowy path through pine trees
PEOPLE: NONE
THINGS: NONE
TEXT: NONE
DETAILS: green light in the sky
"""


class TwoCamerasAgree(unittest.TestCase):
    def test_agreed_slots_use_shorter_verbatim(self):
        # TEXT both say the same; shorter of SCENE picks the shorter string
        seen = ve.look(
            b"fake",
            cameras=["gemma3:4b", "qwen2.5vl:3b"],
            _ask_fn=lambda m, h, i, timeout=300: AGREE_A if "gemma" in m else AGREE_B,
        )
        self.assertEqual(seen.mode, "two")
        self.assertIn("SCENE", seen.agreed)
        self.assertIn("TEXT", seen.agreed)
        self.assertEqual(seen.agreed["TEXT"], "Happy Birthday")
        # shorter SCENE wins
        a = "a kitchen table at breakfast"
        b = "breakfast at a kitchen table"
        self.assertEqual(seen.agreed["SCENE"], min(a, b, key=len))


class TwoCamerasDispute(unittest.TestCase):
    def test_disputed_slots_are_named_not_averaged(self):
        seen = ve.look(
            b"fake",
            cameras=["gemma3:4b", "qwen2.5vl:3b"],
            _ask_fn=lambda m, h, i, timeout=300: AGREE_A if "gemma" in m else DISPUTE,
        )
        # cake vs vase — no shared content words → disputed, not blended
        self.assertIn("THINGS", seen.disputed)
        self.assertNotIn("THINGS", seen.agreed)
        # Happy Birthday vs NONE → disputed
        self.assertIn("TEXT", seen.disputed)
        block = seen.for_kin()
        self.assertIn("the two cameras did not agree", block)
        self.assertNotIn("vase", block)
        self.assertNotIn("cake", block)  # disputed content omitted


class OneCameraMode(unittest.TestCase):
    def test_one_camera_is_labelled_and_uses_its_slots(self):
        seen = ve.look(
            b"fake",
            cameras=["gemma3:4b"],
            _ask_fn=_cam(ONE_ONLY),
        )
        self.assertEqual(seen.mode, "one")
        self.assertIn("SCENE", seen.agreed)
        block = seen.for_kin()
        self.assertIn("One camera looked at it; nothing double-checked it", block)
        self.assertIn("snowy path", block)
        self.assertNotIn("Two cameras looked", block)


class RawNeverReachesKin(unittest.TestCase):
    def test_raw_eye_text_never_in_for_kin(self):
        marker = "SECRET_RAW_PHRASE_XYZ_SHOULD_NOT_LEAK"
        raw = AGREE_A + "\n" + marker
        seen = ve.look(
            b"fake",
            cameras=["gemma3:4b", "qwen2.5vl:3b"],
            _ask_fn=_cam(raw),
        )
        block = seen.for_kin()
        self.assertNotIn(marker, block)
        # Operator still has it
        self.assertTrue(any(marker in t for t in seen.raw.values()))
        for eye in seen.eyes_payload():
            self.assertIn(marker, eye["text"])


class HardwareTable(unittest.TestCase):
    def test_table_tiers(self):
        self.assertEqual(ve.default_cameras(16), ["gemma3:12b", "qwen2.5vl:7b"])
        self.assertEqual(ve.default_cameras(8), ["gemma3:4b", "qwen2.5vl:3b"])
        self.assertEqual(ve.default_cameras(4), ["gemma3:4b"])
        self.assertEqual(
            ve.default_cameras(0, ram_gb=16, platform="darwin"),
            ["gemma3:4b", "qwen2.5vl:3b"],
        )

    def test_vram_contention_warning_on_6gb_discrete_gpu(self):
        warn = ve.vram_contention_warning(6.0, platform="linux")
        self.assertIsNotNone(warn)
        self.assertIn("VRAM contention warning", warn)
        self.assertIn("6.0 GB", warn)
        self.assertIn("gemma3:4b", warn)

    def test_vram_contention_warning_none_on_8gb_or_above(self):
        self.assertIsNone(ve.vram_contention_warning(8.0, platform="linux"))
        self.assertIsNone(ve.vram_contention_warning(12.0, platform="linux"))

    def test_vram_contention_warning_none_below_6gb(self):
        # < 6GB defaults to single camera anyway
        self.assertIsNone(ve.vram_contention_warning(4.0, platform="linux"))

    def test_vram_contention_warning_none_on_darwin(self):
        # Apple Silicon unified memory does not experience discrete VRAM contention
        self.assertIsNone(ve.vram_contention_warning(6.0, platform="darwin"))

    def test_vram_contention_warning_none_with_single_camera_override(self):
        self.assertIsNone(
            ve.vram_contention_warning(6.0, platform="linux", cameras=["gemma3:4b"])
        )


if __name__ == "__main__":
    unittest.main()
