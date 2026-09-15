"""Echo Bloom → companion on therug.

Both Frosty (dev) and Themess (product) import this. One URL.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import aiohttp

log = logging.getLogger("echo_bloom.companion")

COMPANION_URL = os.environ.get(
    "ECHO_BLOOM_COMPANION", "http://192.168.1.142:8092"
).rstrip("/")


async def transcribe(audio: bytes, content_type: str = "audio/webm") -> dict:
    if not audio:
        return {"ok": False, "error": "No audio data."}
    name = "clip.ogg" if "ogg" in content_type else "clip.webm"
    data = aiohttp.FormData()
    data.add_field("audio", audio, filename=name, content_type=content_type)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{COMPANION_URL}/transcribe",
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
    if not wav.is_file() or not image.is_file():
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
                f"{COMPANION_URL}/animate",
                data=data,
                timeout=aiohttp.ClientTimeout(total=300),
            ) as r:
                if r.status == 200 and "video" in (r.content_type or ""):
                    return await r.read()
                return None
    except Exception as e:
        log.warning("companion animate unreachable: %s", e)
        return None
