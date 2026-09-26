"""Tests for photo sharing frontend in Echo Bloom.

Verifies:
1. Templates (templates/kin.html and templates/room.html) include the 📎 button,
   retention controls, and static/js/photo_share.js.
2. static/js/photo_share.js has no alert() or confirm() calls and never uses
   navigator.clipboard.read().
3. Headless browser test harness (tests/photo_share_harness.html) runs via
   `chromium --headless=new --dump-dom` and passes all synthetic event tests:
   - drop event with PNG file
   - paste event with image file
   - paste event with text only (untouched)
   - chip lifecycle (thumbnail, looking..., ready, remove ✕)
   - send button gating while looking
   - outgoing message payload assembly
   - max 4 photos constraint
   - retention settings and inline purge confirmation
   - missing models card and pull trigger
"""
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestPhotoShareFrontend(unittest.TestCase):
    def test_templates_have_photo_controls(self):
        kin_html = (ROOT / "templates" / "kin.html").read_text(encoding="utf-8")
        room_html = (ROOT / "templates" / "room.html").read_text(encoding="utf-8")

        # Both templates must load photo_share.js
        self.assertIn("photo_share.js", kin_html, "kin.html must include photo_share.js")
        self.assertIn("photo_share.js", room_html, "room.html must include photo_share.js")

        # 📎 button must exist with an accessible label on both pages
        self.assertRegex(kin_html, r'id="photo-attach-btn"[^>]*aria-label="Attach photo"',
                         "kin.html must have #photo-attach-btn with aria-label")
        self.assertRegex(room_html, r'id="photo-attach-btn"[^>]*aria-label="Attach photo"',
                         "room.html must have #photo-attach-btn with aria-label")

        # File picker accept="image/*"
        self.assertRegex(kin_html, r'id="photo-file-input"[^>]*accept="image/\*"',
                         "kin.html must have #photo-file-input accepting image/*")
        self.assertRegex(room_html, r'id="photo-file-input"[^>]*accept="image/\*"',
                         "room.html must have #photo-file-input accepting image/*")

        # Retention control next to 📎 on both pages
        self.assertIn('id="photo-retention-select"', kin_html,
                      "kin.html must have #photo-retention-select")
        self.assertIn('id="photo-retention-select"', room_html,
                      "room.html must have #photo-retention-select")

        # Purge button on both pages
        self.assertIn('id="photo-purge-btn"', kin_html,
                      "kin.html must have #photo-purge-btn")
        self.assertIn('id="photo-purge-btn"', room_html,
                      "room.html must have #photo-purge-btn")

        # Drop overlay
        self.assertIn('id="photo-drop-overlay"', kin_html,
                      "kin.html must have drop overlay")
        self.assertIn('id="photo-drop-overlay"', room_html,
                      "room.html must have drop overlay")

        # Chips container
        self.assertIn('id="photo-chips"', kin_html,
                      "kin.html must have #photo-chips container")
        self.assertIn('id="photo-chips"', room_html,
                      "room.html must have #photo-chips container")

    def test_no_forbidden_apis_in_photo_code(self):
        js_path = ROOT / "static" / "js" / "photo_share.js"
        self.assertTrue(js_path.exists(), "static/js/photo_share.js must exist")
        js_code = js_path.read_text(encoding="utf-8")

        # No alert(...) or confirm(...)
        self.assertNotRegex(js_code, r'\balert\s*\(', "photo_share.js must not call alert()")
        self.assertNotRegex(js_code, r'\bconfirm\s*\(', "photo_share.js must not call confirm()")

        # Never use navigator.clipboard.read()
        self.assertNotIn("clipboard.read(", js_code,
                         "photo_share.js must not use navigator.clipboard.read()")

    def test_no_new_alerts_in_templates(self):
        # Verify no alert( added in git diff for templates
        res = subprocess.run(["git", "diff", "HEAD", "--", "templates/"],
                             cwd=str(ROOT), capture_output=True, text=True)
        added_lines = [line for line in res.stdout.splitlines() if line.startswith("+") and not line.startswith("+++")]
        for line in added_lines:
            self.assertNotIn("alert(", line, f"Found alert() in added template line: {line}")
            self.assertNotIn("confirm(", line, f"Found confirm() in added template line: {line}")

    def test_headless_browser_harness(self):
        harness_path = ROOT / "tests" / "photo_share_harness.html"
        self.assertTrue(harness_path.exists(), "tests/photo_share_harness.html must exist")

        chromium = shutil.which("chromium") or shutil.which("google-chrome")
        self.assertIsNotNone(chromium, "chromium must be installed")

        cmd = [
            chromium,
            "--headless=new",
            "--virtual-time-budget=5000",
            "--dump-dom",
            f"file://{harness_path.resolve()}"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        self.assertEqual(res.returncode, 0, f"chromium exited with code {res.returncode}: {res.stderr}")

        dom = res.stdout
        # The harness records summary status in #test-results
        self.assertIn('id="test-results"', dom, "Harness must contain #test-results element")
        self.assertIn('data-status="PASS"', dom, f"Harness tests must pass. DOM output:\n{dom[:2000]}")
        self.assertNotIn('data-status="FAIL"', dom, f"Harness reported failures. DOM output:\n{dom}")


if __name__ == "__main__":
    unittest.main()
