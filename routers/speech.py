"""Voice management, TTS, transcription, avatar upload, and speech setup
(the catalogue + downloader). Moved out of main.py in the router split
(2026-09-20) — routes only, unchanged."""

import asyncio
import json
import os
from pathlib import Path
from typing import AsyncGenerator

import aiohttp

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from fastapi.responses import StreamingResponse

import cluster as cl
import companion_client as cc
import talk_media as tm
from deps import (
    require_auth, require_auth_only, log,
    _load_kin_cfg, _save_kin_cfg, _atomic_write_json, _sanitize_kin_name,
    _espeak_wav,
)

router = APIRouter()


@router.post("/api/transcribe")
async def api_transcribe(request: Request, _=Depends(require_auth)):
    """STT lives on therug. Never import faster_whisper on host 3.14."""
    audio = await request.body()
    ct = request.headers.get("content-type", "audio/webm").lower()
    return await cc.transcribe(audio, ct)


# ── Voice management ───────────────────────────────────────────────────────────

_PIPER_DIRS = [
    Path("/mnt/ai/piper"),
    Path.home() / "piper-voices",
    Path.home() / "piper",
    Path.home() / ".local/share/piper",
    Path("/usr/share/piper"),
    Path("/usr/local/share/piper"),
    Path("/usr/share/piper-tts"),
]

# Same map as pops_shop/uber_es_news.py. A few voices, not a zoo.
# Bong was missing there; ryan sits next to joe.
_NEWS_VOICES = {
    "Eli":     "en_US-joe-medium.onnx",
    "Coda":    "en_US-ljspeech-high.onnx",
    "Aurora":  "en_GB-alba-medium.onnx",
    "Lumen":   "en_GB-jenny_dioco-medium.onnx",
    "Crungus": "en_GB-semaine-medium.onnx",
    "Bong":    "en_US-ryan-high.onnx",
}
_NEWS_SPEAKERS = {
    "Crungus": 2,  # Obadiah — semaine
}


def _voice_label(path: str) -> str:
    """en_US-lessac-medium → Lessac Medium"""
    stem  = Path(path).stem
    parts = stem.split("-")
    if len(parts) >= 3:
        return f"{parts[-2].capitalize()} {parts[-1].capitalize()}"
    return stem


def _find_voice_file(filename: str) -> str | None:
    for d in _PIPER_DIRS:
        if d.is_dir():
            candidate = d / filename
            if candidate.exists():
                return str(candidate)
    return None


def _list_installed_voices() -> list[dict]:
    seen, voices = set(), []
    for d in _PIPER_DIRS:
        if d.is_dir():
            for f in sorted(d.glob("*.onnx")):
                if f.name not in seen:
                    seen.add(f.name)
                    voices.append({
                        "file":  f.name,
                        "path":  str(f),
                        "label": _voice_label(str(f)),
                    })
    return voices


def _voice_for_kin(kin_name: str) -> str | None:
    """Return absolute path to the preferred voice for this Kin, or first installed."""
    cfg = _load_kin_cfg()
    for k in cfg.get("kin", []):
        if k.get("name") == kin_name and k.get("voice"):
            path = _find_voice_file(k["voice"])
            if path:
                return path
    news = tm.voice_path(kin_name)
    if news:
        return str(news)
    return _find_piper_voice()


@router.get("/api/speech/voices/installed")
async def api_voices_installed(_=Depends(require_auth)):
    return {"voices": _list_installed_voices()}


@router.get("/api/kin/{name}/voice")
async def api_get_kin_voice(name: str, _=Depends(require_auth)):
    cfg = _load_kin_cfg()
    for k in cfg.get("kin", []):
        if k.get("name") == name:
            return {"voice_file": k.get("voice", "")}
    return {"voice_file": ""}


@router.post("/api/kin/{name}/voice")
async def api_set_kin_voice(name: str, request: Request, _=Depends(require_auth)):
    body       = await request.json()
    voice_file = (body.get("voice_file") or "").strip()
    cfg = _load_kin_cfg()
    for k in cfg.get("kin", []):
        if k.get("name") == name:
            k["voice"] = voice_file
            _save_kin_cfg(cfg)
            return {"ok": True}
    return {"ok": False, "error": f"Kin '{name}' not found"}


_AVATAR_MAX_BYTES = 8 * 1024 * 1024


def _sniff_image_ext(data: bytes) -> str | None:
    """Trust the bytes, not the filename or the browser's content-type —
    both are the uploader's word for it."""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


@router.post("/api/kin/{name}/avatar")
async def api_upload_avatar(name: str, file: UploadFile = File(...),
                            _=Depends(require_auth_only)):
    """A claimed picture, not a sitting ritual — that ceremony is Don's own
    project's rule for his own Kin, not something a generic install needs.
    Eli specifically is excluded (see talk_media.news_portrait): he has no
    claimed sitting yet, and a casual upload here is not the way one gets
    made — that stays a deliberate act, not a form submission."""
    if name == "Eli":
        raise HTTPException(400,
            "Eli has no claimed sitting yet — that isn't done from here.")
    kin = cl.KIN_BY_NAME.get(name)
    if not kin:
        raise HTTPException(404, f"Unknown Kin: {name}")
    space = kin.get("space")
    if not space:
        raise HTTPException(400, f"{name} has no configured space for a picture.")

    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file.")
    if len(data) > _AVATAR_MAX_BYTES:
        raise HTTPException(413, "Picture is too large — 8MB max.")
    ext = _sniff_image_ext(data)
    if not ext:
        raise HTTPException(400, "That doesn't look like a jpg, png, or webp.")

    avatar_dir = Path(os.path.expanduser(str(space))) / "avatar"
    avatar_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{_sanitize_kin_name(name)}{ext}"
    (avatar_dir / fname).write_bytes(data)
    _atomic_write_json(avatar_dir / "claimed.json", {"file": fname})
    return {"ok": True}


@router.post("/api/tts")
async def api_tts(request: Request, _=Depends(require_auth)):
    from fastapi.responses import Response as _Resp
    import tempfile, os as _os

    body     = await request.json()
    text     = tm.strip_asterisks((body.get("text") or "").strip())[:12000]
    kin_name = (body.get("kin_name") or "").strip()
    if not text:
        raise HTTPException(400, "No text.")

    # Companion layer is therug. Frosty does not speak.
    try:
        tm.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        dest = tm.AUDIO_DIR / f"tts_{_os.getpid()}.wav"
        audio = await asyncio.to_thread(tm.synthesize_wav, text, kin_name, dest)
        if audio:
            return _Resp(content=audio, media_type="audio/wav")
        audio = await _espeak_wav(text, kin_name)
        if audio:
            return _Resp(content=audio, media_type="audio/wav")
        raise HTTPException(503,
            "No voice engine. therug Piper did not answer and espeak-ng "
            "isn't installed.")
    except HTTPException:
        raise
    except Exception:
        log.exception("tts failed")
        raise HTTPException(500, "Speech synthesis failed — see the app log.")


# ── Piper voice model discovery ────────────────────────────────────────────────

def _piper_command() -> list[str] | None:
    """How to run piper on this machine, as an argv prefix.

    Returns a list because there may not be a binary at all: `pip install
    piper-tts` provides a console script that is frequently NOT on PATH
    (Windows puts it in Scripts/, Linux in ~/.local/bin), but the module is
    always runnable with `python -m piper`. Falling back to that is the
    difference between working TTS and "Piper not found" on a machine where
    piper is, in fact, installed.
    """
    # Native ELF first. ~/.local/bin/piper is a broken pip stub
    # (`from piper.__main__ import main`) — Every Synthetic News already
    # uses /mnt/ai/piper/piper. /usr/bin/piper is a GTK app on Garuda.
    import sys
    candidates = [
        Path("/mnt/ai/piper/piper"),
        Path("/usr/lib/piper-tts/bin/piper"),
        Path("/usr/local/bin/piper"),
    ]
    for c in candidates:
        if c.is_file():
            return [str(c)]
    import shutil
    found = shutil.which("piper")
    if found:
        return [found]
    # Last resort: the pip package, invoked as a module.
    try:
        import importlib.util
        if importlib.util.find_spec("piper") is not None:
            return [sys.executable, "-m", "piper"]
    except Exception:
        pass
    return None


def _find_piper_binary() -> str | None:
    cmd = _piper_command()
    return cmd[0] if cmd else None


def _find_piper_voice() -> str | None:
    search = [
        Path("/mnt/ai/piper"),
        Path.home() / "piper-voices",
        Path.home() / "piper",
        Path.home() / ".local/share/piper",
        Path("/usr/share/piper"),
        Path("/usr/local/share/piper"),
        Path("/usr/share/piper-tts"),
    ]
    for d in search:
        if d.is_dir():
            for f in sorted(d.glob("*.onnx")):
                return str(f)
    return None


# ── Speech status + voice download ────────────────────────────────────────────

_HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"

PIPER_VOICE_CATALOGUE = [
    {
        "id":    "en_US-ryan-high",
        "label": "Ryan — Male, US, high quality",
        "size":  "~63 MB",
        "files": [
            f"{_HF_BASE}/en/en_US/ryan/high/en_US-ryan-high.onnx",
            f"{_HF_BASE}/en/en_US/ryan/high/en_US-ryan-high.onnx.json",
        ],
    },
    {
        "id":    "en_US-ryan-low",
        "label": "Ryan — Male, US, low quality (fast)",
        "size":  "~5 MB",
        "files": [
            f"{_HF_BASE}/en/en_US/ryan/low/en_US-ryan-low.onnx",
            f"{_HF_BASE}/en/en_US/ryan/low/en_US-ryan-low.onnx.json",
        ],
    },
    {
        "id":    "en_US-lessac-high",
        "label": "Lessac — Female, US, high quality",
        "size":  "~63 MB",
        "files": [
            f"{_HF_BASE}/en/en_US/lessac/high/en_US-lessac-high.onnx",
            f"{_HF_BASE}/en/en_US/lessac/high/en_US-lessac-high.onnx.json",
        ],
    },
    {
        "id":    "en_GB-alba-medium",
        "label": "Alba — Female, British, medium quality",
        "size":  "~30 MB",
        "files": [
            f"{_HF_BASE}/en/en_GB/alba/medium/en_GB-alba-medium.onnx",
            f"{_HF_BASE}/en/en_GB/alba/medium/en_GB-alba-medium.onnx.json",
        ],
    },
]


@router.get("/api/speech/status")
async def api_speech_status(_=Depends(require_auth)):
    stt_ok = False
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"{cc.COMPANION_URL}/health",
                timeout=aiohttp.ClientTimeout(total=3),
            ) as r:
                body = await r.json(content_type=None)
                stt_ok = bool(body.get("stt"))
    except Exception:
        pass

    piper_bin  = _find_piper_binary()
    voice_path = _find_piper_voice()
    return {
        "stt_ok":     stt_ok,
        "piper_ok":   piper_bin is not None,
        "voice_ok":   voice_path is not None,
        "voice_path": voice_path,
        "companion":  cc.COMPANION_URL,
    }


@router.get("/api/speech/voices")
async def api_speech_voices(_=Depends(require_auth)):
    return {"voices": PIPER_VOICE_CATALOGUE}


@router.post("/api/speech/download-voice")
async def api_speech_download_voice(request: Request, _=Depends(require_auth)):
    body    = await request.json()
    voice_id = (body.get("voice_id") or "").strip()
    entry   = next((v for v in PIPER_VOICE_CATALOGUE if v["id"] == voice_id), None)
    if not entry:
        raise HTTPException(400, "Unknown voice")

    dest_dir = Path.home() / "piper"
    dest_dir.mkdir(parents=True, exist_ok=True)

    async def download_stream() -> AsyncGenerator[bytes, None]:
        for url in entry["files"]:
            filename = url.rsplit("/", 1)[-1]
            dest     = dest_dir / filename
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        url,
                        timeout=aiohttp.ClientTimeout(total=300),
                        headers={"User-Agent": "EchoBloom/1.0"},
                        allow_redirects=True,
                    ) as r:
                        total = int(r.headers.get("content-length", 0))
                        done  = 0
                        with open(dest, "wb") as fh:
                            async for chunk in r.content.iter_chunked(65536):
                                fh.write(chunk)
                                done += len(chunk)
                                pct = int(done * 100 / total) if total else 0
                                evt = {"file": filename, "bytes": done,
                                       "total": total, "pct": pct}
                                yield f"data: {json.dumps(evt)}\n\n".encode()
                yield f"data: {json.dumps({'file': filename, 'done': True})}\n\n".encode()
            except Exception as e:
                yield f"data: {json.dumps({'file': filename, 'error': str(e)})}\n\n".encode()

        yield f"data: {json.dumps({'status': 'complete', 'voice_id': voice_id})}\n\n".encode()
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        download_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
