"""Show the break, then the fix — deliberate wrong versions fail these asserts.

Run as part of the suite: each test builds a broken stand-in, asserts it would
fail the real contract, then asserts the real module passes. Don's rule.
"""
import io
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import photo_store as ps  # noqa: E402
import vision_eyes as ve  # noqa: E402


def _jpeg_with_exif() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (1, 2, 3)).save(buf, format="JPEG")
    bare = buf.getvalue()
    exif = b"Exif\x00\x00" + b"GPS" + b"\x00" * 20
    app1 = b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif
    return bare[:2] + app1 + bare[2:]


class BrokenNormaliseKeepsExif(unittest.TestCase):
    def test_broken_passthrough_fails_the_exif_contract(self):
        tagged = _jpeg_with_exif()
        # Broken: return input unchanged
        broken_out = tagged
        self.assertTrue(ps.jpeg_has_exif(broken_out), "broken path still has EXIF")
        # Fixed:
        fixed = ps.normalize(tagged)
        self.assertFalse(ps.jpeg_has_exif(fixed))


class BrokenRetentionNoneWrites(unittest.TestCase):
    def test_broken_always_write_fails_none_contract(self):
        from unittest.mock import patch
        from PIL import Image

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "photos"
            root.mkdir()
            # Broken behaviour: write even when retention is none
            p = root / "Eli" / "p_broken.jpg"
            p.parent.mkdir()
            p.write_bytes(b"jpeg")
            self.assertTrue(list(root.rglob("*.jpg")), "broken left files")
            # Fixed path with real save(none):
            buf = io.BytesIO()
            Image.new("RGB", (8, 8)).save(buf, format="PNG")
            with patch.object(ps, "PHOTOS_ROOT", root / "fixed"):
                jpeg = ps.normalize(buf.getvalue())
                info = ps.save(jpeg, kin="Eli", retention="none")
                self.assertFalse(info["kept"])
                fixed_root = root / "fixed"
                self.assertEqual(
                    list(fixed_root.rglob("*")) if fixed_root.exists() else [], []
                )


class BrokenOneCameraSilent(unittest.TestCase):
    def test_broken_silent_one_eye_fails_label_contract(self):
        text = "SCENE: park\nPEOPLE: NONE\nTHINGS: NONE\nTEXT: NONE\nDETAILS: NONE\n"
        # Broken: pretend two-camera intro with one camera's data
        broken_intro = "[I'm sharing a photo. Two cameras looked; this is only what both saw:]"
        seen = ve.look(b"x", cameras=["gemma3:4b"],
                       _ask_fn=lambda m, h, i, timeout=300: text)
        self.assertEqual(seen.mode, "one")
        block = seen.for_kin()
        self.assertNotIn("Two cameras looked", block)
        self.assertIn("One camera looked", block)
        # And the broken intro would have lied:
        self.assertIn("Two cameras", broken_intro)


if __name__ == "__main__":
    unittest.main()
