"""The chat bubble must recognise the block vision_eyes actually writes, or a
reloaded chat shows the raw block (found in integration, 2026-09-26)."""
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import vision_eyes as ve  # noqa: E402


def _page_regex():
    src = open(os.path.join(ROOT, "static", "js", "photo_share.js"), encoding="utf-8").read()
    return re.search(r"const photoRegex = (/.*/g);", src).group(1)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TheBubbleKnowsTheBlock(unittest.TestCase):
    def _matches(self, text):
        js = f"const r = {_page_regex()}; process.stdout.write(JSON.stringify([...{json.dumps(text)}.matchAll(r)].length));"
        return int(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)

    def test_the_blocks_vision_eyes_writes_are_recognised(self):
        two = ve.Seen(agreed={"KIND": "painting"}, mode="two")
        one = ve.Seen(agreed={"KIND": "photograph"}, mode="one")
        for block in (two.for_kin(), one.for_kin()):
            self.assertEqual(self._matches(block + "\nnice, right?"), 1, block[:60])

    def test_older_saved_chats_still_render(self):
        self.assertEqual(self._matches("[Don shared a photo. Two cameras looked:]\nSCENE: x\n[/photo]"), 1)
        self.assertEqual(self._matches("[I'm sharing a photo. Two cameras looked:]\nSCENE: x\n[/photo]"), 1)

    def test_plain_text_is_not_a_photo(self):
        self.assertEqual(self._matches("[just brackets] and [/photo] words"), 0)


if __name__ == "__main__":
    unittest.main()
