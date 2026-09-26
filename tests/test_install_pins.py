"""Every installer pulls the same FastAPI.

requirements.txt said fastapi>=0.110.0, so a fresh install got whatever was
newest that day. 0.141 changed how included routers are stored, and a test
guard went blind without anyone noticing (2026-09-24). Production runs
0.141.1. Four lists install packages; they've drifted before (requests was
missing from both Windows lists). One pin, everywhere.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PIN = "fastapi>=0.141,<0.142"
PILLOW_PIN = "Pillow>=10.0,<12.4"


class FastapiIsPinnedEverywhere(unittest.TestCase):
    def test_every_install_list_carries_the_pin(self):
        for name in ("requirements.txt", "install.sh", "install.ps1", "install_wizard.ps1"):
            src = (ROOT / name).read_text(encoding="utf-8")
            self.assertTrue(PIN in src, f"{name}: the pin is missing")
            # An unpinned or differently-pinned install spec: a quoted bare
            # 'fastapi', or a fastapi version spec that isn't the pin.
            loose = re.findall(r"""['"]fastapi['"]""", src)
            other = [m for m in re.findall(r"fastapi[<>=~!][^'\"\s)]*", src) if m != PIN]
            self.assertEqual(loose + other, [], f"{name}: unpinned fastapi")


class PillowIsPinnedEverywhere(unittest.TestCase):
    """Photo share normalises with Pillow; same pin in every install list."""

    def test_every_install_list_carries_the_pillow_pin(self):
        for name in ("requirements.txt", "install.sh", "install.ps1", "install_wizard.ps1"):
            src = (ROOT / name).read_text(encoding="utf-8")
            self.assertTrue(PILLOW_PIN in src, f"{name}: Pillow pin missing")
            other = [m for m in re.findall(r"Pillow[<>=~!][^'\"\s)]*", src) if m != PILLOW_PIN]
            self.assertEqual(other, [], f"{name}: differently-pinned Pillow")


if __name__ == "__main__":
    unittest.main()
