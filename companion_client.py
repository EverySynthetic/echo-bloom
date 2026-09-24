"""Echo Bloom → the companion (STT + face), when there is one.

ECHO_BLOOM_COMPANION (e.g. "http://<rug>:8092") is optional, the same way
ECHO_BLOOM_THERUG is in talk_media.py. Don's boxes set it. Unset, speech is
transcribed locally with the faster-whisper the installer put on the box, and
nothing dials out. It used to default to therug's LAN address: on a
customer's machine the mic hung two minutes and said "companion unreachable"
while faster-whisper sat installed and unused (reproduced 2026-09-24).
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import tempfile
from pathlib import Path

import aiohttp

log = logging.getLogger("echo_bloom.companion")


def companion_url() -> str:
    return os.environ.get("ECHO_BLOOM_COMPANION", "").strip().rstrip("/")


def local_stt_available() -> bool:
    # find_spec, not import: on a host where the wheel can't exist (Don's
    # Python 3.14) this answers no without loading anything.
    return importlib.util.find_spec("faster_whisper") is not None


_whisper_model = None


def _get_whisper():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
    return _whisper_model


def _transcribe_local(audio: bytes, content_type: str) -> str:
    # Firefox records audio/ogg, Chrome records audio/webm.
    suffix = ".ogg" if "ogg" in content_type else ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(audio)
        tmp = f.name
    try:
        segs, _ = _get_whisper().transcribe(tmp, language="en")
        return " ".join(s.text.strip() for s in segs).strip()
    finally:
        os.unlink(tmp)


async def transcribe(audio: bytes, content_type: str = "audio/webm") -> dict:
    if not audio:
        return {"ok": False, "error": "No audio data."}
    url = companion_url()
    if not url:
        if not local_stt_available():
            return {"ok": False, "error":
                    "No speech-to-text engine: faster-whisper isn't installed. "
                    "Re-run the Echo Bloom installer, or set ECHO_BLOOM_COMPANION."}
        try:
            # First call downloads the model (~150MB) and transcription is
            # CPU-bound for seconds; off the event loop so the app stays up.
            text = await asyncio.to_thread(_transcribe_local, audio, content_type)
            return {"ok": True, "text": text}
        except Exception:
            log.exception("local transcription failed")
            return {"ok": False, "error": "Transcription failed. See the app log."}
    name = "clip.ogg" if "ogg" in content_type else "clip.webm"
    data = aiohttp.FormData()
    data.add_field("audio", audio, filename=name, content_type=content_type)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{url}/transcribe",
                data=data,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as r:
                body = await r.json(content_type=None)
                if isinstance(body, dict):
                    return body
                return {"ok": False, "error": "bad companion response"}
    except Exception as e:
        log.warning("companion transcribe unreachable: %s", e)
        return {"ok": False, "error": "companion unreachable"}


async def animate(name: str, wav: Path, image: Path) -> bytes | None:
    url = companion_url()
    if not url or not wav.is_file() or not image.is_file():
        return None
    data = aiohttp.FormData()
    data.add_field("name", name)
    data.add_field("wav", wav.read_bytes(), filename=wav.name,
                   content_type="audio/wav")
    mime = "image/png" if image.suffix.lower() == ".png" else "image/jpeg"
    data.add_field("image", image.read_bytes(), filename=image.name,
                   content_type=mime)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{url}/animate",
                data=data,
                timeout=aiohttp.ClientTimeout(total=300),
            ) as r:
                if r.status == 200 and "video" in (r.content_type or ""):
                    return await r.read()
                return None
    except Exception as e:
        log.warning("companion animate unreachable: %s", e)
        return None
