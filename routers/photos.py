"""Photo share API — describe, keep, purge, pull vision models.

Contract: ~/claude_home/DESIGN_echo_photo_share.md (fixed; Gem builds the page).
All routes require auth.
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncGenerator

import aiohttp
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response, StreamingResponse

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
    return ve.resolve_cameras(
        vram_gb=float(hw.get("vram_gb") or 0),
        ram_gb=float(hw.get("ram_gb") or 0),
        platform=hw.get("platform"),
        override=override if isinstance(override, list) else None,
    )


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
async def api_photo_describe(
    request: Request,
    image: UploadFile = File(...),
    kin: str = Form("room"),
    _=Depends(require_auth),
):
    raw = await image.read()
    # Run the blocking vision calls off the event loop.
    result = await asyncio.to_thread(describe_image_bytes, raw, kin or "room")
    return result


@router.get("/api/photo/settings")
async def api_photo_settings(_=Depends(require_auth)):
    cfg = auth.load_config()
    cameras = _cameras(cfg)
    host = _ollama_host(cfg)
    installed = sorted(ve.installed_models(host))
    missing = ve.missing_cameras(cameras, host=host)
    cam1 = cameras[0] if cameras else None
    cam2 = cameras[1] if len(cameras) > 1 else None
    return {
        "retention": ps.get_retention(cfg),
        "kept_count": ps.kept_count(),
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
    """Stream Ollama pull progress as data: {...} lines (chat-stream style)."""
    body = await request.json()
    model = str(body.get("model") or body.get("name") or "").strip()
    if not model:
        return {"ok": False, "error": "model required"}
    host = _ollama_host()

    async def pull_stream() -> AsyncGenerator[bytes, None]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{host}/api/pull",
                    json={"name": model, "stream": True},
                    timeout=aiohttp.ClientTimeout(total=None, sock_read=120),
                ) as resp:
                    async for line in resp.content:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            evt = json.loads(line.decode() if isinstance(line, bytes) else line)
                        except Exception:
                            yield f"data: {json.dumps({'model': model, 'raw': line.decode(errors='replace')})}\n\n".encode()
                            continue
                        # Normalise a percent when Ollama sends completed/total.
                        percent = None
                        completed = evt.get("completed")
                        total = evt.get("total")
                        if isinstance(completed, int) and isinstance(total, int) and total > 0:
                            percent = round(100.0 * completed / total, 1)
                        payload = {"model": model, "status": evt.get("status"), "percent": percent}
                        if evt.get("error"):
                            payload["error"] = evt["error"]
                        yield f"data: {json.dumps(payload)}\n\n".encode()
            yield f"data: {json.dumps({'model': model, 'status': 'done', 'percent': 100})}\n\n".encode()
        except Exception as e:
            msg = str(e) or f"{type(e).__name__} (no detail)"
            yield f"data: {json.dumps({'model': model, 'error': msg})}\n\n".encode()

    return StreamingResponse(
        pull_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
