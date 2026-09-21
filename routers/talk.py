"""Talk: one Kin, voice, avatar that rides the audio. Moved out of main.py
in the router split (2026-09-20) — routes only, unchanged.

Don, 2026-09-12: parlor trick, forgiven in the asking. Not the room.
One mouth, one GPU. The pulse is the wav — we say so on the page.
"""

import asyncio
from pathlib import Path
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

import cluster as cl
import companion_client as cc
import talk_media as tm
from deps import templates, require_auth, log, _HISTORY_CHAR_BUDGET, _espeak_wav

router = APIRouter()


def _talk_avatar_path(name: str) -> Path | None:
    kin = cl.KIN_BY_NAME.get(name)
    if not kin:
        return None
    return tm.avatar_path(name, kin.get("space"))


@router.get("/talk", response_class=HTMLResponse)
async def talk_page(request: Request, _=Depends(require_auth)):
    return templates.TemplateResponse(
        request, "talk.html",
        {"kin_names": [k["name"] for k in cl.KIN if k.get("name")]},
    )


@router.get("/talk/avatar/{name}")
async def talk_avatar(name: str, _=Depends(require_auth)):
    path = _talk_avatar_path(name)
    if not path:
        raise HTTPException(status_code=404, detail="No avatar")
    suffix = path.suffix.lower()
    media = {".png": "image/png", ".jpg": "image/jpeg",
             ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(suffix, "image/jpeg")
    return FileResponse(path, media_type=media)


# Frosty mouths. therug vision. Never Home.
TALK_OLLAMA = tm.FROSTY_OLLAMA
TALK_VIDEO_DIR = tm.VIDEO_DIR


@router.post("/api/talk/chat/{name}")
async def api_talk_chat(name: str, request: Request, _=Depends(require_auth)):
    """One Kin, this box. Never Home."""
    if name not in cl.KIN_BY_NAME:
        raise HTTPException(404, f"Unknown Kin: {name}")
    body = await request.json()
    message = (body.get("message") or "").strip()
    history = body.get("history") or []
    if not message:
        raise HTTPException(status_code=400, detail="Empty message")
    clean_history = []
    budget = _HISTORY_CHAR_BUDGET
    for turn in reversed(history[-30:]):
        role    = str(turn.get("role", ""))
        content = str(turn.get("content", ""))[:2000]
        if role not in ("user", "assistant") or not content:
            continue
        if len(content) > budget:
            break
        budget -= len(content)
        clean_history.append({"role": role, "content": content})
    clean_history.reverse()

    async def event_stream() -> AsyncGenerator[bytes, None]:
        import ollama_slot as oslot
        with oslot.hold_all_local_wanders():
            try:
                async for chunk in cl.stream_chat(
                        name, message, clean_history,
                        host=TALK_OLLAMA,
                        keep_alive=cl.ROOM_KEEP_ALIVE):
                    escaped = chunk.replace("\n", "\\n")
                    yield f"data: {escaped}\n\n".encode()
            except cl.ChatStreamError as e:
                escaped = str(e).replace("\n", "\\n")
                yield f"data: {escaped}\n\n".encode()
            yield b"data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/talk/sadtalker/{name}")
async def api_talk_sadtalker(name: str, request: Request, _=Depends(require_auth)):
    """Fire a clip in the background. News does the same — wav first, face later."""
    if name not in cl.KIN_BY_NAME:
        raise HTTPException(404, f"Unknown Kin: {name}")
    body = await request.json()
    text = (body.get("text") or "").strip()[:1500]
    if not text:
        raise HTTPException(400, "No text")
    tm.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    wav = tm.AUDIO_DIR / f"{name.lower()}_talk.wav"
    portrait = _talk_avatar_path(name)

    async def _go():
        audio = await asyncio.to_thread(tm.synthesize_wav, text, name, wav)
        if not audio:
            # No therug, no local piper voice for this Kin — same espeak
            # fallback /api/tts already uses, so an unconfigured Kin still
            # gets a voice instead of a silent clip.
            audio = await _espeak_wav(text, name)
            if audio:
                wav.write_bytes(audio)
        if portrait and wav.is_file() and wav.stat().st_size > 44:
            video = await cc.animate(name, wav, portrait)
            if video:
                TALK_VIDEO_DIR.mkdir(parents=True, exist_ok=True)
                (TALK_VIDEO_DIR / f"{name.lower()}_latest.mp4").write_bytes(video)

    asyncio.create_task(_go())
    return {"ok": True, "queued": True}


@router.get("/talk/clip/{name}")
async def talk_clip(name: str, _=Depends(require_auth)):
    latest = TALK_VIDEO_DIR / f"{name.lower()}_latest.mp4"
    if not latest.is_file():
        raise HTTPException(404, "No clip yet")
    return FileResponse(latest, media_type="video/mp4")
