"""A customer's mic must work without Don's LAN.

companion_client used to default ECHO_BLOOM_COMPANION to therug's address on
Don's network. On any other install that address doesn't exist: the mic said
"companion unreachable", speech status read false, and the faster-whisper the
installer had just put on the box was never touched. The companion is now
opt-in, like ECHO_BLOOM_THERUG in talk_media.py. Unset means local.
"""
import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main  # noqa: E402
import companion_client as cc  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(main.app)


def _no_network(*a, **k):
    raise AssertionError("reached for the network with no companion configured")


class NoCompanionConfigured(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("ECHO_BLOOM_COMPANION", None)
        self._prior = main.app.dependency_overrides.get(main.require_auth)
        main.app.dependency_overrides[main.require_auth] = lambda: True

    def tearDown(self):
        self._env.stop()
        if self._prior is None:
            main.app.dependency_overrides.pop(main.require_auth, None)
        else:
            main.app.dependency_overrides[main.require_auth] = self._prior

    def test_there_is_no_default_address(self):
        self.assertEqual(cc.companion_url(), "")

    def test_transcribe_runs_locally(self):
        with patch.object(cc, "local_stt_available", return_value=True), \
             patch.object(cc, "_transcribe_local", return_value="hello there") as local, \
             patch("aiohttp.ClientSession", _no_network):
            out = asyncio.run(cc.transcribe(b"\x00" * 64, "audio/webm"))
        self.assertEqual(out, {"ok": True, "text": "hello there"})
        local.assert_called_once()

    def test_no_engine_says_what_to_do(self):
        with patch.object(cc, "local_stt_available", return_value=False), \
             patch("aiohttp.ClientSession", _no_network):
            out = asyncio.run(cc.transcribe(b"\x00" * 64, "audio/webm"))
        self.assertFalse(out["ok"])
        self.assertNotIn("unreachable", out["error"])
        self.assertIn("faster-whisper", out["error"])

    def test_a_local_failure_is_an_error_not_a_crash(self):
        def boom(*a, **k):
            raise RuntimeError("decoder fell over")
        with patch.object(cc, "local_stt_available", return_value=True), \
             patch.object(cc, "_transcribe_local", side_effect=boom):
            out = asyncio.run(cc.transcribe(b"\x00" * 64, "audio/webm"))
        self.assertFalse(out["ok"])

    def test_animate_does_not_dial_out(self):
        with patch("aiohttp.ClientSession", _no_network):
            out = asyncio.run(cc.animate("Eli", ROOT / "README.md", ROOT / "README.md"))
        self.assertIsNone(out)

    def test_speech_status_reports_local_engine(self):
        with patch.object(cc, "local_stt_available", return_value=True), \
             patch("aiohttp.ClientSession", _no_network):
            r = client.get("/api/speech/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["stt_ok"])
        self.assertNotIn("192.168.", str(body.get("companion")))


class CompanionConfigured(unittest.TestCase):
    """Don's boxes: set ECHO_BLOOM_COMPANION and the companion is used."""

    def test_env_wins(self):
        with patch.dict(os.environ, {"ECHO_BLOOM_COMPANION": "http://rug.lan:8092/"}):
            self.assertEqual(cc.companion_url(), "http://rug.lan:8092")


if __name__ == "__main__":
    unittest.main()
