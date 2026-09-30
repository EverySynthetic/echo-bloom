"""Photo share API — describe, keep, purge, pull vision models.

Contract: ~/claude_home/DESIGN_echo_photo_share.md (fixed; Gem builds the page).
All routes require auth.
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncGenerator

import aiohttp
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

import auth
import photo_store as ps
import vision_eyes as ve
from deps import get_hw_caps, log, require_auth

router = APIRouter()


def _ollama_host(cfg: dict | None = None) -> str:
    cfg = cfg or auth.load_config()
    return str(cfg.get("vision_host") or cfg.get("ollama_host") or "http://localhost:11434").rstrip("/")


def _cameras(cfg: dict | None = None) -> list[str]:
    cfg = cfg or auth.load_config()
    hw = get_hw_caps()
    override = cfg.get("vision_models")
    if isinstance(override, str):
        override = [x.strip() for x in override.split(",") if x.strip()]
    cams = ve.resolve_cameras(
        vram_gb=float(hw.get("vram_gb") or 0),
        ram_gb=float(hw.get("ram_gb") or 0),
        platform=hw.get("platform"),
        override=override if isinstance(override, list) else None,
    )
    warn = ve.vram_contention_warning(
        vram_gb=float(hw.get("vram_gb") or 0),
        platform=hw.get("platform"),
        cameras=cams,
    )
    if warn:
        log.warning("%s", warn)
    return cams


def describe_image_bytes(raw: bytes, kin: str = "room", host: str | None = None) -> dict:
    """Shared path for POST /api/photo/describe and the webcam /api/vision/{name}.

    Returns the contract shape (ok/error). Never writes the original bytes.
    """
    cfg = auth.load_config()
    host = (host or _ollama_host(cfg)).rstrip("/")
    cameras = _cameras(cfg)
    retention = ps.get_retention(cfg)

    try:
        jpeg = ps.normalize(raw)
    except ps.PhotoError as e:
        return {"ok": False, "error": str(e)}

    missing = ve.missing_cameras(cameras, host=host)
    if missing:
        # Prefer reporting every missing model the table wants.
        names = ", ".join(m["name"] for m in missing)
        total = sum(m["size_gb"] for m in missing)
        return {
            "ok": False,
            "error": (
                f"Photos need a vision model ({names}, about {total:.1f} GB). "
                "Download now?"
            ),
            "needs_models": missing,
        }

    try:
        seen = ve.look(jpeg, cameras=cameras, host=host)
    except Exception as e:
        log.warning("photo describe failed", exc_info=True)
        return {
            "ok": False,
            "error": str(e) or f"{type(e).__name__} — the vision model may still be loading.",
        }

    if seen.errors and not seen.raw:
        err = "; ".join(f"{m}: {t}" for m, t in seen.errors.items())
        return {"ok": False, "error": f"The cameras could not look: {err}"}

    pid = ps.new_id()
    kept_info = ps.save(jpeg, kin=kin or "room", retention=retention, photo_id=pid)

    return {
        "ok": True,
        "id": kept_info["id"],
        "for_kin": seen.for_kin(),
        "mode": seen.mode,
        "agreed": list(seen.agreed.keys()),
        "disputed": list(seen.disputed),
        "eyes": seen.eyes_payload(),
        "kept": kept_info["kept"],
        "expires_at": kept_info["expires_at"],
        "thumb_url": kept_info["thumb_url"],
    }


@router.post("/api/photo/describe")
async def api_photo_describe(request: Request, kin: str = "room", _=Depends(require_auth)):
    """The photo arrives as the raw request body (the page sends the File as is).

    Not multipart: Starlette spools any multipart file over 1 MB to a temp file
    on disk, which breaks "Don't keep" means nothing touches the disk. And the
    body is read in chunks against the cap, so an oversized upload is refused
    at MAX_BYTES + 1 instead of being read whole into memory first.
    """
    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > ps.MAX_BYTES:
            return JSONResponse(status_code=413, content={
                "ok": False,
                "error": f"That photo is too large (max {ps.MAX_BYTES // (1024 * 1024)} MB). "
                         "Try a smaller copy or resize it first."})
    # Run the blocking vision calls off the event loop.
    return await asyncio.to_thread(describe_image_bytes, bytes(buf), kin or "room")


@router.get("/api/photo/settings")
async def api_photo_settings(_=Depends(require_auth)):
    cfg = auth.load_config()
    cameras = _cameras(cfg)
    host = _ollama_host(cfg)
    installed = sorted(ve.installed_models(host))
    missing = ve.missing_cameras(cameras, host=host)
    cam1 = cameras[0] if cameras else None
    cam2 = cameras[1] if len(cameras) > 1 else None
    hw = get_hw_caps()
    vram_warn = ve.vram_contention_warning(
        vram_gb=float(hw.get("vram_gb") or 0),
        platform=hw.get("platform"),
        cameras=cameras,
    )
    return {
        "retention": ps.get_retention(cfg),
        "kept_count": ps.kept_count(),
        "warning": vram_warn,
        "models": {
            "camera1": cam1,
            "camera2": cam2,
            "installed": installed,
            "missing": [m["name"] for m in missing],
        },
    }


@router.post("/api/photo/settings")
async def api_photo_settings_set(request: Request, _=Depends(require_auth)):
    body = await request.json()
    retention = str(body.get("retention", "")).strip()
    try:
        value = ps.set_retention(retention)
    except ps.PhotoError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "retention": value}


@router.post("/api/photo/purge")
async def api_photo_purge(_=Depends(require_auth)):
    n = await asyncio.to_thread(ps.purge_all)
    return {"deleted": n}


@router.get("/api/photo/{photo_id}")
async def api_photo_get(photo_id: str, _=Depends(require_auth)):
    path = ps.find(photo_id)
    if not path or not path.is_file():
        return Response(
            content=json.dumps({"error": "not kept"}),
            status_code=404,
            media_type="application/json",
        )
    data = path.read_bytes()
    return Response(content=data, media_type="image/jpeg")


@router.post("/api/photo/models/pull")
async def api_photo_models_pull(request: Request, _=Depends(require_auth)):
    """Stream Ollama pull progress as data: {...} lines (chat-stream style).

    No body (what the page's "Download now" sends): pull every missing camera,
    one after another. A named model must be one of this install's cameras;
    this route is not a general "pull anything" door.
    """
    raw = await request.body()
    try:
        body = json.loads(raw) if raw.strip() else {}
    except Exception:
        body = {}
    cfg = auth.load_config()
    host = _ollama_host(cfg)
    cameras = _cameras(cfg)
    named = str(body.get("model") or body.get("name") or "").strip()
    if named:
        if named not in cameras:
            return JSONResponse(status_code=400, content={
                "ok": False, "error": f"{named} isn't one of this install's photo cameras."})
        models = [named]
    else:
        models = [m["name"] for m in await asyncio.to_thread(ve.missing_cameras, cameras, host)]

    def line(payload: dict) -> bytes:
        return f"data: {json.dumps(payload)}\n\n".encode()

    async def pull_one(session, model):
        failed = None
        async with session.post(
            f"{host}/api/pull",
            json={"name": model, "stream": True},
            timeout=aiohttp.ClientTimeout(total=None, sock_read=120),
        ) as resp:
            async for chunk in resp.content:
                chunk = chunk.strip()
                if not chunk:
                    continue
                try:
                    evt = json.loads(chunk.decode(errors="replace"))
                except Exception:
                    continue
                percent = None
                completed, total = evt.get("completed"), evt.get("total")
                if isinstance(completed, int) and isinstance(total, int) and total > 0:
                    percent = round(100.0 * completed / total, 1)
                payload = {"model": model, "status": evt.get("status"), "percent": percent,
                           "step": models.index(model) + 1, "steps": len(models)}
                if evt.get("error"):
                    failed = payload["error"] = str(evt["error"])
                yield payload
        # Only say done when Ollama didn't report an error: a failed pull used
        # to be followed by "done" and a green bar.
        if not failed:
            yield {"model": model, "status": "done", "percent": 100,
                   "step": models.index(model) + 1, "steps": len(models)}

    async def pull_stream() -> AsyncGenerator[bytes, None]:
        if not models:
            yield line({"status": "done", "percent": 100, "note": "nothing missing"})
            return
        try:
            async with aiohttp.ClientSession() as session:
                for model in models:
                    async for payload in pull_one(session, model):
                        yield line(payload)
                        if payload.get("error"):
                            return
        except Exception as e:
            yield line({"error": str(e) or f"{type(e).__name__} (no detail)"})

    return StreamingResponse(
        pull_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
