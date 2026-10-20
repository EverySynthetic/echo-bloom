"""A stock install's voice is Piper, not espeak.

The installer puts piper-tts in as a pip package (often only runnable as
`python -m piper`) and a voice in ~/piper (en_US-lessac-medium). Talk's
synthesis (talk_media.synthesize_wav) knows a companion host and a few fixed
binary paths, and Don's own voice names; on a customer's machine it finds
none of them. When the server line's /api/tts met the public line in 1.3.6,
that alone would have sent every stock install to espeak, or to a 503 where
espeak isn't installed. The public line's own Piper path sits between them.
"""
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main  # noqa: E402
import talk_media as tm  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(main.app)

_FAKE_WAV = b"RIFF" + b"\x00" * 200

# Stands in for `python -m piper`: reads text on stdin, writes --output_file.
_FAKE_PIPER = textwrap.dedent("""
    import sys
    sys.stdin.read()
    out = sys.argv[sys.argv.index("--output_file") + 1]
    open(out, "wb").write(%r)
""" % _FAKE_WAV)


class StockInstallSpeaksWithPiper(unittest.TestCase):
    def setUp(self):
        self._prior = main.app.dependency_overrides.get(main.require_auth)
        main.app.dependency_overrides[main.require_auth] = lambda: True
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.piper = tmp / "fake_piper.py"
        self.piper.write_text(_FAKE_PIPER)
        self.voice = tmp / "en_US-lessac-medium.onnx"
        self.voice.write_bytes(b"onnx")
        self._patches = [
            # Nothing of Don's: no companion host, no fixed piper binary.
            patch.object(tm, "synthesize_wav", return_value=b""),
            patch.object(tm, "AUDIO_DIR", tmp / "audio"),
            patch.object(main, "_espeak_wav", AsyncMock(return_value=b"")),
            patch.object(main, "_piper_command",
                         return_value=[sys.executable, str(self.piper)]),
            patch.object(main, "_voice_for_kin", return_value=str(self.voice)),
            patch.object(main, "_find_piper_voice", return_value=str(self.voice)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()
        if self._prior is None:
            main.app.dependency_overrides.pop(main.require_auth, None)
        else:
            main.app.dependency_overrides[main.require_auth] = self._prior

    def test_pip_piper_and_installed_voice_answer(self):
        r = client.post("/api/tts", json={"text": "hello", "kin_name": "Nova"})
        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertEqual(r.content, _FAKE_WAV)

    def test_no_kin_name_uses_any_installed_voice(self):
        r = client.post("/api/tts", json={"text": "hello"})
        self.assertEqual(r.status_code, 200, r.text[:300])

    def test_a_voice_path_that_is_not_on_this_machine_is_skipped(self):
        """talk_media.voice_path can name a file on the companion host."""
        with patch.object(main, "_voice_for_kin",
                          return_value="~/piper/not-here.onnx"):
            r = client.post("/api/tts", json={"text": "hi", "kin_name": "Nova"})
        self.assertEqual(r.status_code, 503)

    def test_nothing_at_all_says_what_to_install(self):
        with patch.object(main, "_piper_command", return_value=None):
            r = client.post("/api/tts", json={"text": "hi", "kin_name": "Nova"})
        self.assertEqual(r.status_code, 503)
        self.assertIn("piper-tts", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
