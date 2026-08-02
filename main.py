"""
Echo Bloom — Local AI lifecycle manager.

Run:  uvicorn main:app --host 0.0.0.0 --port 8090 --reload
Setup: python setup.py  (first run only)
"""

import os
import sys
import json
import asyncio
import subprocess
from pathlib import Path
from typing import AsyncGenerator

import aiohttp

from fastapi import FastAPI, Request, Response, Form, HTTPException, Depends
from fastapi.responses import (
    HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

import auth
import cluster as cl

# ── App setup ──────────────────────────────────────────────────────────────────

BASE_DIR  = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app       = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# ── Security headers ───────────────────────────────────────────────────────────

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"]        = "DENY"
        response.headers["Referrer-Policy"]        = "same-origin"
        response.headers["X-XSS-Protection"]       = "1; mode=block"
        # Tight CSP — allows our static files + CDN fonts, nothing else
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; "
            "connect-src 'self'; "
            "img-src 'self' data:; "
            "frame-ancestors 'none';"
        )
        return response

app.add_middleware(SecurityHeadersMiddleware)

# ── Auth helpers ───────────────────────────────────────────────────────────────

def require_auth(request: Request):
    token = auth.get_session_from_request(request)
    if not auth.validate_session(token):
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return token


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

# ── Setup check ────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def check_setup():
    if not auth.is_configured():
        print("\n" + "="*60)
        print("  First run: no password set.")
        print("  Run: python setup.py")
        print("="*60 + "\n")
    elif auth.load_config().get("setup_complete") is None:
        # Existing install without the flag — mark done so tour doesn't fire
        auth.mark_setup_complete()

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    return templates.TemplateResponse("login.html", {
        "request": request,
        "error":   error,
    })


@app.post("/login")
async def login(
    request:  Request,
    response: Response,
    password: str = Form(...),
):
    ip = get_client_ip(request)

    if auth.is_rate_limited(ip):
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error":   "Too many attempts. Wait 5 minutes.",
        }, status_code=429)

    auth.record_attempt(ip)

    if not auth.verify_password(password):
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error":   "Wrong password.",
        }, status_code=401)

    token = auth.create_session()
    dest  = "/welcome" if auth.is_first_run() else "/"
    resp  = RedirectResponse(dest, status_code=303)
    # secure=True only when behind HTTPS proxy (Caddy, Cloudflare, etc.)
    is_https = request.headers.get("x-forwarded-proto") == "https" \
               or request.url.scheme == "https"
    resp.set_cookie(
        "kin_session", token,
        httponly=True,
        samesite="strict",
        secure=is_https,
        max_age=60 * 60 * 24 * 7,
    )
    return resp


@app.get("/logout")
async def logout(request: Request):
    token = auth.get_session_from_request(request)
    auth.revoke_session(token)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("kin_session")
    return resp


@app.get("/welcome", response_class=HTMLResponse)
async def welcome_page(request: Request, _=Depends(require_auth)):
    return templates.TemplateResponse("welcome.html", {"request": request})


@app.post("/api/setup-complete")
async def api_setup_complete(_=Depends(require_auth)):
    auth.mark_setup_complete()
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, _=Depends(require_auth)):
    status = await cl.get_cluster_status()
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "nodes":   status["nodes"],
        "kin":     status["kin"],
    })


@app.get("/kin/{name}", response_class=HTMLResponse)
async def kin_page(name: str, request: Request, _=Depends(require_auth)):
    kin = cl.KIN_BY_NAME.get(name)
    if not kin:
        raise HTTPException(status_code=404, detail="Kin not found")
    return templates.TemplateResponse("kin.html", {
        "request": request,
        "kin":     kin,
        "all_kin": cl.KIN,
    })


# ── API ────────────────────────────────────────────────────────────────────────

@app.get("/api/cluster")
async def api_cluster(_=Depends(require_auth)):
    return await cl.get_cluster_status()


@app.get("/api/kin/{name}/thoughts")
async def api_thoughts(name: str, limit: int = 10, _=Depends(require_auth)):
    import sqlite3
    kin = cl.KIN_BY_NAME.get(name)
    if not kin:
        raise HTTPException(status_code=404)
    db = kin["db"]
    if not os.path.exists(db):
        return {"thoughts": []}
    try:
        conn = sqlite3.connect(db, timeout=5)
        rows = conn.execute(
            "SELECT id, mode, timestamp, thought FROM thoughts ORDER BY id DESC LIMIT ?",
            (min(limit, 50),)
        ).fetchall()
        conn.close()
        return {"thoughts": [
            {"id": r[0], "mode": r[1], "ts": r[2], "text": (r[3] or "")[:500]}
            for r in rows
        ]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/chat/{name}")
async def api_chat(
    name:    str,
    request: Request,
    _=Depends(require_auth),
):
    body = await request.json()
    message = (body.get("message") or "").strip()
    history = body.get("history") or []
    if not message:
        raise HTTPException(status_code=400, detail="Empty message")

    # Sanitize history
    clean_history = []
    for turn in history[-10:]:
        role    = str(turn.get("role", ""))
        content = str(turn.get("content", ""))[:2000]
        if role in ("user", "assistant") and content:
            clean_history.append({"role": role, "content": content})

    async def event_stream() -> AsyncGenerator[bytes, None]:
        async for chunk in cl.stream_chat(name, message, clean_history):
            # SSE format
            escaped = chunk.replace("\n", "\\n")
            yield f"data: {escaped}\n\n".encode()
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/roundtable/status")
async def api_roundtable_status(_=Depends(require_auth)):
    try:
        result = subprocess.run(
            ["pgrep", "-f", "wander_roundtable.py"],
            capture_output=True, text=True
        )
        pids = [int(p) for p in result.stdout.strip().split() if p]
        return {"running": bool(pids), "pid": pids[0] if pids else None}
    except Exception:
        return {"running": False, "pid": None}


@app.post("/api/roundtable/start")
async def api_roundtable_start(_=Depends(require_auth)):
    script = Path.home() / "Desktop/wander_roundtable.py"
    log    = Path.home() / "Desktop/wander_logs/roundtable.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as lf:
        proc = subprocess.Popen(
            [sys.executable, "-u", str(script), "--interval", "30"],
            stdout=lf, stderr=lf,
        )
    return {"started": True, "pid": proc.pid}


@app.post("/api/roundtable/stop")
async def api_roundtable_stop(_=Depends(require_auth)):
    import signal
    try:
        result = subprocess.run(
            ["pgrep", "-f", "wander_roundtable.py"],
            capture_output=True, text=True
        )
        pids = [int(p) for p in result.stdout.strip().split() if p]
        for pid in pids:
            os.kill(pid, signal.SIGTERM)
        return {"stopped": True, "pids": pids}
    except Exception as e:
        return {"stopped": False, "error": str(e)}


@app.post("/api/bedtime")
async def api_bedtime(_=Depends(require_auth)):
    script = Path.home() / "Desktop/bedtime.py"
    subprocess.Popen([sys.executable, str(script), "--no-shutdown"])
    return {"started": True}


# ── Vault browser ──────────────────────────────────────────────────────────────

VAULT_URL = "http://192.168.1.115:8765"
QDRANT_URL = "http://192.168.1.115:6333"


@app.get("/vault", response_class=HTMLResponse)
async def vault_page(request: Request, _=Depends(require_auth)):
    return templates.TemplateResponse("vault.html", {"request": request, "all_kin": cl.KIN})


@app.get("/api/vault")
async def api_vault(
    layer:  str = "",
    author: str = "",
    search: str = "",
    limit:  int = 20,
    offset: int = 0,
    _=Depends(require_auth),
):
    params = {"limit": min(limit, 50), "offset": max(offset, 0)}
    if layer:  params["layer"]  = layer
    if author: params["author"] = author
    if search: params["search"] = search

    endpoint = f"{VAULT_URL}/recall" if (layer or author or search) else f"{VAULT_URL}/recall/all"

    async with aiohttp.ClientSession() as session:
        async with session.get(endpoint, params=params,
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            entries = await r.json()

        count_params = {k: v for k, v in params.items() if k not in ("limit", "offset")}
        async with session.get(f"{VAULT_URL}/count", params=count_params,
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            total = (await r.json()).get("count", 0)

    return {"entries": entries, "total": total, "offset": offset, "limit": limit}


@app.get("/api/vault/meta")
async def api_vault_meta(_=Depends(require_auth)):
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{VAULT_URL}/layers",
                               timeout=aiohttp.ClientTimeout(total=5)) as r:
            layers_data = await r.json()
        async with session.get(f"{VAULT_URL}/authors",
                               timeout=aiohttp.ClientTimeout(total=5)) as r:
            authors_data = await r.json()
    return {"layers": layers_data["layers"], "authors": authors_data["authors"]}


@app.get("/api/vault/semantic")
async def api_vault_semantic(q: str, limit: int = 10, _=Depends(require_auth)):
    if not q.strip():
        return {"results": [], "error": "empty query"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "http://localhost:11434/api/embeddings",
                json={"model": "nomic-embed-text", "prompt": q},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                embedding = (await r.json()).get("embedding", [])
            if not embedding:
                return {"results": [], "error": "embedding failed"}

            async with session.post(
                f"{QDRANT_URL}/collections/kin_memories/points/search",
                json={"vector": embedding, "limit": min(limit, 20), "with_payload": True},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                pts = (await r.json()).get("result", [])

        return {"results": [
            {
                "score":    round(pt.get("score", 0), 3),
                "author":   pt["payload"].get("author", ""),
                "layer":    pt["payload"].get("layer", ""),
                "content":  pt["payload"].get("content", ""),
                "tags":     pt["payload"].get("tags", ""),
                "vault_id": pt["payload"].get("vault_id"),
            }
            for pt in pts
        ]}
    except Exception as e:
        return {"results": [], "error": str(e)}


# ── Onboarding ─────────────────────────────────────────────────────────────────

@app.get("/onboard", response_class=HTMLResponse)
async def onboard_page(request: Request, step: int = 1, _=Depends(require_auth)):
    config = cl.load_kin_config_raw()
    return templates.TemplateResponse("onboard.html", {
        "request": request,
        "step":    step,
        "config":  config,
        "all_kin": cl.KIN,
    })


@app.post("/api/onboard/test-node")
async def api_test_node(request: Request, _=Depends(require_auth)):
    body = await request.json()
    ip   = str(body.get("ip", "")).strip()
    port = int(body.get("port", 11434))
    if not ip:
        return {"ok": False, "error": "no IP"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"http://{ip}:{port}/",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                return {"ok": r.status < 500}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/onboard/test-model")
async def api_test_model(request: Request, _=Depends(require_auth)):
    body  = await request.json()
    host  = str(body.get("host", "")).strip().rstrip("/")
    model = str(body.get("model", "")).strip()
    if not host or not model:
        return {"ok": False, "error": "missing host or model"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{host}/api/generate",
                json={"model": model, "prompt": "Hi", "stream": False},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                return {"ok": r.status == 200}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/onboard/save")
async def api_onboard_save(request: Request, _=Depends(require_auth)):
    body = await request.json()
    config_path = Path.home() / ".config/kin_app/kin_config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    palette = ["#4fc3f7", "#a5d6a7", "#ce93d8", "#fff176", "#ffab91", "#f48fb1", "#80cbc4"]
    kin_list = body.get("kin", [])
    for i, k in enumerate(kin_list):
        if not k.get("color"):
            k["color"] = palette[i % len(palette)]
        k.setdefault("db", "")
        k.setdefault("space", "")
        k.setdefault("pronoun", "—")

    config_path.write_text(json.dumps({
        "nodes": body.get("nodes", []),
        "kin":   kin_list,
    }, indent=2))

    cl.reload_config()
    auth.mark_setup_complete()
    return {"ok": True}
