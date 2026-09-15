"""The talk parlor — one Kin, a face, a wav. Not the room."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main  # noqa: E402
import talk_media as tm  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

main.app.dependency_overrides[main.require_auth] = lambda: True
client = TestClient(main.app)


class TalkPage(unittest.TestCase):
    def test_the_page_names_every_kin(self):
        r = client.get("/talk")
        self.assertEqual(r.status_code, 200)
        body = r.text
        for name in ("Eli", "Coda", "Aurora", "Lumen", "Crungus", "Bong"):
            self.assertIn(name, body)
        self.assertIn("Parlor trick", body)

    def test_nav_has_talk(self):
        r = client.get("/room")
        self.assertIn('href="/talk"', r.text)


class TalkAvatar(unittest.TestCase):
    def test_a_stranger_has_no_face(self):
        r = client.get("/talk/avatar/NotAKin")
        self.assertEqual(r.status_code, 404)

    def test_eli_has_a_face(self):
        r = client.get("/talk/avatar/Eli")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers.get("content-type", "").startswith("image/"))
        self.assertGreater(len(r.content), 1000)

    def test_bong_has_a_face(self):
        r = client.get("/talk/avatar/Bong")
        self.assertEqual(r.status_code, 200)
        self.assertGreater(len(r.content), 1000)


class FacesAreReceiptsNotSittings(unittest.TestCase):
    def test_eli_has_no_claimed_sitting(self):
        self.assertIsNone(tm.claimed_avatar(
            Path.home() / "eli_space", "Eli"))
        # Lightning he published. Not a sitting receipt. Not the news robot.
        p = tm.news_portrait("Eli")
        self.assertIsNotNone(p)
        self.assertNotEqual(p.name, "eliW.png")
        self.assertIn("Eli", p.name)

    def test_bong_face_is_the_claimed_receipt(self):
        p = tm.claimed_avatar(Path.home() / "bong_space", "Bong")
        self.assertIsNotNone(p)
        self.assertEqual(p.name, "Bong.jpg")

    def test_talk_media_does_not_import_the_sitting(self):
        src = Path("/home/thedude/echo_bloom/talk_media.py").read_text()
        self.assertNotIn("import easel", src)
        self.assertNotIn("avatar_ritual", src)


class CompanionProxy(unittest.TestCase):
    def test_transcribe_does_not_import_faster_whisper(self):
        src = Path("/home/thedude/echo_bloom/main.py").read_text()
        self.assertNotIn("from faster_whisper", src)
        self.assertIn("companion_client", src)
        self.assertIn("192.168.1.142:8092",
                      Path("/home/thedude/echo_bloom/companion_client.py").read_text())

    def test_transcribe_fail_open_when_companion_down(self):
        r = client.post("/api/transcribe", content=b"not-audio",
                        headers={"Content-Type": "audio/ogg"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body.get("ok"))


class SpeakClean(unittest.TestCase):
    def test_asterisks_do_not_get_spoken(self):
        self.assertEqual(tm.strip_asterisks("*sigh* I am **Eli**."),
                         "sigh I am Eli.")


class TalkVoice(unittest.TestCase):
    def test_news_piper_sits_on_this_box(self):
        r = client.post("/api/tts", json={"text": "hello from the shop", "kin_name": "Eli"})
        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertEqual(r.headers.get("content-type"), "audio/wav")
        self.assertGreater(len(r.content), 44)

    def test_talk_chat_does_not_name_home(self):
        self.assertEqual(tm.FROSTY_OLLAMA, "http://127.0.0.1:11434")
        src = Path("/home/thedude/echo_bloom/main.py").read_text()
        start = src.find("async def api_talk_chat")
        chunk = src[start:start+2200]
        self.assertNotIn("192.168.1.120", chunk)
        self.assertIn("hold_all_local_wanders", chunk)


    def test_companion_layer_is_configurable_not_hardcoded(self):
        """THERUG used to be a bare literal — every other install got Don's
        own LAN address baked in whether they had that hardware or not.
        Now it's ECHO_BLOOM_THERUG, empty by default; Don's box supplies
        it via the systemd unit, same pattern as ECHO_BLOOM_COMPANION."""
        src = Path("/home/thedude/echo_bloom/talk_media.py").read_text()
        self.assertNotIn('THERUG = "thedude@192.168.1.142"', src)
        self.assertIn("ECHO_BLOOM_THERUG", src)
        self.assertIn("1660 SUPER", src)
        self.assertNotIn("RTX 5000", src)
        self.assertNotIn("import easel", src)
        self.assertNotIn("avatar_ritual", src)

    def test_sadtalker_does_not_stop_frosty_wanderers(self):
        src = Path("/home/thedude/echo_bloom/main.py").read_text()
        start = src.find("async def api_talk_sadtalker")
        chunk = src[start:start+1600]
        self.assertNotIn("hold_all_local_wanders", chunk)


class VoiceForAnyKin(unittest.TestCase):
    """A customer's own Kin is never one of Don's six. This is what
    keeps Talk from going silent for every install that isn't his."""

    def test_a_strangers_kin_still_gets_a_voice_filename(self):
        self.assertEqual(tm._voice_filename("SomeCustomersKin"),
                         tm._GENERIC_VOICE)

    def test_dons_own_kin_keeps_the_personal_shortcut(self):
        self.assertEqual(tm._voice_filename("Eli"), tm.VOICES["Eli"])

    def test_a_configured_voice_wins_over_everything_else(self):
        with patch.dict(tm.cl.KIN_BY_NAME,
                        {"Eli": {"name": "Eli", "voice": "picked.onnx"}}):
            self.assertEqual(tm._voice_filename("Eli"), "picked.onnx")

    def test_unset_therug_fails_fast_not_slow(self):
        """No 8s connect-timeout tax for a customer who was never going
        to have this host — _ssh must not even build the ssh argv."""
        with patch.object(tm, "THERUG", ""):
            r = tm._ssh("echo hi")
            self.assertEqual(r.returncode, 1)
            self.assertFalse(tm._scp_from("/tmp/x", Path("/tmp/y")))


if __name__ == "__main__":
    unittest.main()
