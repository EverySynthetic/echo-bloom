"""Expired trial: the price on the banner, and a way out."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

main.app.dependency_overrides[main.require_auth] = lambda: True
main.app.dependency_overrides[main.require_auth_only] = lambda: True
client = TestClient(main.app)


def _expired(**extra):
    d = {"state": "expired", "days_left": 0, "email": "", "type": "", "reason": ""}
    d.update(extra)
    return d


class ExpiredBannerPrice(unittest.TestCase):
    def test_the_ended_trial_banner_is_not_fifty_dollars(self):
        with patch("main.lic.get_status", return_value=_expired()):
            r = client.get("/license")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("$50", r.text)
        self.assertIn("YOUR TRIAL HAS ENDED", r.text)
        self.assertIn(f"${main.LICENSE_PRICE}", r.text)

    def test_install_page_is_not_fifty_dollars(self):
        r = client.get("/install")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("$50", r.text)
        self.assertNotIn("founding rate", r.text)
        self.assertIn(f"${main.LICENSE_PRICE}", r.text)
        self.assertIn("BUY ECHO BLOOM — $", r.text)
        self.assertIn("uninstall.sh", r.text)
        self.assertIn("uninstall.ps1", r.text)


class AWayOutWhenTheTrialEnds(unittest.TestCase):
    def test_expired_license_page_has_uninstall(self):
        with patch("main.lic.get_status", return_value=_expired()):
            r = client.get("/license")
        self.assertIn("LEAVE THIS MACHINE", r.text)
        self.assertIn("/uninstall.ps1", r.text)
        self.assertIn("/uninstall.sh", r.text)

    def test_uninstall_scripts_are_served(self):
        ps1 = client.get("/uninstall.ps1")
        sh = client.get("/uninstall.sh")
        self.assertEqual(ps1.status_code, 200, ps1.text[:200])
        self.assertEqual(sh.status_code, 200, sh.text[:200])
        self.assertIn("Echo Bloom", ps1.text)
        self.assertIn("echo_bloom", sh.text)
        self.assertNotIn("themess", sh.text.lower())


if __name__ == "__main__":
    unittest.main()
