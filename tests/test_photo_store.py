"""Photo normalise, retention, refuse bad input.

Each case was first run against a deliberately broken store (see
test_photo_mutations.py / the report) so the break is shown, then the fix.
"""
import io
import struct
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import photo_store as ps  # noqa: E402


def _jpeg_with_exif_gps() -> bytes:
    """Minimal JPEG that carries an EXIF APP1 with a GPS IFD marker.

    Built without relying on piexif: SOI + APP1(Exif) + a tiny baseline scan.
    Pillow must strip the APP1 on normalise.
    """
    # Tiny 2x2 RGB JPEG via Pillow, then inject EXIF APP1 before SOS.
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (20, 40, 60)).save(buf, format="JPEG", quality=90)
    bare = buf.getvalue()
    # Fake EXIF payload: "Exif\0\0" + TIFF header + GPS IFD tag id 0x8825
    # Enough for jpeg_has_exif / our strip check; not a valid full EXIF parse.
    exif_payload = (
        b"Exif\x00\x00"
        b"MM\x00*\x00\x00\x00\x08"  # big-endian TIFF
        b"\x00\x01"                  # 1 entry
        b"\x88\x25\x00\x04\x00\x00\x00\x01\x00\x00\x00\x01"  # GPSInfo tag
        b"\x00\x00\x00\x00"
    )
    app1 = b"\xff\xe1" + struct.pack(">H", len(exif_payload) + 2) + exif_payload
    # Insert APP1 after SOI (first 2 bytes)
    assert bare[:2] == b"\xff\xd8"
    return bare[:2] + app1 + bare[2:]


def _plain_png() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 100, 50)).save(buf, format="PNG")
    return buf.getvalue()


class NormaliseStripsMetadata(unittest.TestCase):
    def test_gps_tagged_jpeg_comes_out_with_no_exif(self):
        tagged = _jpeg_with_exif_gps()
        self.assertTrue(ps.jpeg_has_exif(tagged), "fixture must carry EXIF before fix")
        out = ps.normalize(tagged)
        self.assertFalse(ps.jpeg_has_exif(out), "normalised JPEG still has EXIF/GPS")
        # Still a decodable JPEG
        from PIL import Image
        img = Image.open(io.BytesIO(out))
        img.load()
        self.assertEqual(img.format, "JPEG")


class RetentionNoneWritesNothing(unittest.TestCase):
    def test_retention_none_leaves_zero_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "photos"
            with patch.object(ps, "PHOTOS_ROOT", root):
                jpeg = ps.normalize(_plain_png())
                info = ps.save(jpeg, kin="Eli", retention="none")
                self.assertFalse(info["kept"])
                self.assertIsNone(info["thumb_url"])
                self.assertEqual(list(root.rglob("*")), [],
                                 "retention none must not create any file")


class RetentionNinetyDaySweep(unittest.TestCase):
    def test_old_90d_files_are_swept_newer_kept(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "photos"
            with patch.object(ps, "PHOTOS_ROOT", root):
                jpeg = ps.normalize(_plain_png())
                old = ps.save(jpeg, kin="Eli", retention="90d", photo_id="p_oldold01")
                new = ps.save(jpeg, kin="Eli", retention="90d", photo_id="p_newnew01")
                # Backdate the old meta to 91 days ago
                old_path = Path(old["path"])
                meta_path = old_path.with_suffix(".meta.json")
                import json
                meta = json.loads(meta_path.read_text())
                past = datetime.now(timezone.utc) - timedelta(days=91)
                meta["expires_at"] = past.isoformat()
                meta["created_at"] = (past - timedelta(days=1)).isoformat()
                meta_path.write_text(json.dumps(meta))

                n = ps.sweep(now=datetime.now(timezone.utc))
                self.assertEqual(n, 1)
                self.assertFalse(old_path.exists())
                self.assertTrue(Path(new["path"]).exists())


class RefuseBadInput(unittest.TestCase):
    def test_non_image_renamed_jpg_is_refused(self):
        with self.assertRaises(ps.PhotoError) as cm:
            ps.normalize(b"this is not an image at all, just text named.jpg")
        self.assertIn("not a photo", str(cm.exception).lower())

    def test_over_20mb_is_refused(self):
        huge = b"\xff\xd8\xff" + b"\x00" * (ps.MAX_BYTES + 10)
        with self.assertRaises(ps.PhotoError) as cm:
            ps.normalize(huge)
        self.assertIn("too large", str(cm.exception).lower())


class ForeverKeepsWithoutExpiry(unittest.TestCase):
    def test_forever_has_no_expires_and_survives_sweep(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "photos"
            with patch.object(ps, "PHOTOS_ROOT", root):
                jpeg = ps.normalize(_plain_png())
                info = ps.save(jpeg, kin="room", retention="forever")
                self.assertTrue(info["kept"])
                self.assertIsNone(info["expires_at"])
                self.assertEqual(ps.sweep(), 0)
                self.assertTrue(Path(info["path"]).exists())


if __name__ == "__main__":
    unittest.main()
