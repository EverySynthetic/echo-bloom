"""Dashboard, a single Kin's page, and the version/about surface. Moved out
of main.py in the router split (2026-09-20) — routes only, unchanged."""

import asyncio
import os
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

import cluster as cl
import license as lic
import talk_media as tm
from version import VERSION, CHANGELOG
from deps import templates, require_auth, require_auth_only, get_hw_caps, _FETCH_WHITELIST, log

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, _=Depends(require_auth)):
    status = await cl.get_cluster_status()
    from agent_runner import DEFAULT_MODEL
    return templates.TemplateResponse(request, "dashboard.html", {
        "nodes": status["nodes"],
        "kin":   status["kin"],
        "agent_default_model": DEFAULT_MODEL,
    })


@router.get("/kin/{name}", response_class=HTMLResponse)
async def kin_page(name: str, request: Request, _=Depends(require_auth)):
    kin = cl.KIN_BY_NAME.get(name)
    if not kin:
        raise HTTPException(status_code=404, detail="Kin not found")
    return templates.TemplateResponse(request, "kin.html", {
        "kin":       kin,
        "all_kin":   cl.KIN,
        "hw":        get_hw_caps(),
        "whitelist": sorted(_FETCH_WHITELIST),
        "has_face":  bool(tm.avatar_path(kin["name"], kin.get("space"))),
    })


# ── API ────────────────────────────────────────────────────────────────────────

@router.get("/api/update-check")
async def api_update_check(_=Depends(require_auth)):
    # Same license server every install already talks to for trial/key
    # checks — /version there is always whatever's actually deployed, not
    # a separately-tracked number that could drift.
    latest = await asyncio.to_thread(lic.check_latest_version)
    return {
        "current": VERSION,
        "latest": latest,
        "update_available": bool(latest and latest != VERSION),
    }


@router.get("/api/kin/{name}/thoughts")
async def api_thoughts(name: str, limit: int = 10, _=Depends(require_auth)):
    import sqlite3
    kin = cl.KIN_BY_NAME.get(name)
    if not kin:
        raise HTTPException(status_code=404)
    # Try configured db path first, then standard fallback location
    candidates = []
    if kin.get("db"):
        candidates.append(kin["db"])
    candidates.append(str(Path.home() / ".local/share/echo_bloom/kin" / name.lower() / "thoughts.db"))
    db = next((p for p in candidates if os.path.exists(p)), None)
    if not db:
        return {"thoughts": []}
    try:
        # Read-only URI so a wrong path can't create an empty DB, and off the
        # event loop — wander processes write these files concurrently and a
        # locked DB here stalled every request in the app for up to 5s.
        def _query():
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            try:
                return conn.execute(
                    "SELECT id, mode, timestamp, thought FROM thoughts "
                    "ORDER BY id DESC LIMIT ?",
                    (min(limit, 50),)
                ).fetchall()
            finally:
                conn.close()
        rows = await asyncio.to_thread(_query)
        return {"thoughts": [
            {"id": r[0], "mode": r[1], "ts": r[2], "text": (r[3] or "")[:500]}
            for r in rows
        ]}
    except Exception:
        log.exception("thoughts query failed for %s", name)
        raise HTTPException(status_code=500,
                            detail="Could not read this Kin's thoughts database.")


@router.get("/about", response_class=HTMLResponse)
async def about_page(request: Request, _=Depends(require_auth_only)):
    # base.html lists every Kin name and computes licence state; this route was
    # public, so anyone with the tunnel URL could read both.
    return templates.TemplateResponse(request, "about.html", {"changelog": CHANGELOG})
