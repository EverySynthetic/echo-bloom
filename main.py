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
import license as lic

# ── Hardware capability detection ──────────────────────────────────────────────

_hw_caps_cache: dict | None = None

def get_hw_caps() -> dict:
    global _hw_caps_cache
    if _hw_caps_cache is not None:
        return _hw_caps_cache

    vram_mb = 0
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            vram_mb = sum(int(x.strip()) for x in r.stdout.strip().split("\n") if x.strip().isdigit())
    except Exception:
        pass

    ram_gb = 0.0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    ram_gb = int(line.split()[1]) / (1024 * 1024)
                    break
    except Exception:
        pass

    _hw_caps_cache = {
        "vram_mb":   vram_mb,
        "vram_gb":   round(vram_mb / 1024, 1),
        "ram_gb":    round(ram_gb, 1),
        "vision_ok": vram_mb >= 8192,   # 8 GB VRAM minimum
        "speech_ok": ram_gb >= 8.0,     # 8 GB system RAM minimum
        "fetch_ok":  True,
    }
    return _hw_caps_cache


# ── Web fetch whitelist ────────────────────────────────────────────────────────

_FETCH_WHITELIST = [
    "wikipedia.org", "github.com", "arxiv.org", "docs.python.org",
    "pypi.org", "stackoverflow.com", "news.ycombinator.com",
    "bbc.com", "reuters.com", "arstechnica.com", "theregister.com",
    "ollama.com", "huggingface.co", "reddit.com", "medium.com",
    "dev.to", "docs.anthropic.com", "openai.com", "pubmed.ncbi.nlm.nih.gov",
    "en.m.wikipedia.org", "archive.org", "scholar.google.com",
]

def _fetch_allowed(url: str) -> bool:
    from urllib.parse import urlparse
    try:
        host = urlparse(url).netloc.lower().lstrip("www.")
        return any(host == w or host.endswith("." + w) for w in _FETCH_WHITELIST)
    except Exception:
        return False


async def _fetch_page_text(url: str, max_chars: int = 3000) -> str:
    from html.parser import HTMLParser

    class _Stripper(HTMLParser):
        def __init__(self):
            super().__init__()
            self._buf, self._skip = [], False
        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style", "nav", "footer"): self._skip = True
        def handle_endtag(self, tag):
            if tag in ("script", "style", "nav", "footer"): self._skip = False
        def handle_data(self, d):
            if not self._skip: self._buf.append(d)
        def text(self): return " ".join(" ".join(self._buf).split())

    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=10),
                         headers={"User-Agent": "EchoBloom/1.0"}) as r:
            html = await r.text(errors="replace")
    p = _Stripper()
    p.feed(html)
    return p.text()[:max_chars]


# ── Piper voice model discovery ────────────────────────────────────────────────

def _find_piper_voice() -> str | None:
    search = [
        Path.home() / "piper",
        Path.home() / ".local/share/piper",
        Path("/usr/share/piper"),
        Path("/usr/local/share/piper"),
    ]
    for d in search:
        if d.is_dir():
            for f in sorted(d.glob("*.onnx")):
                return str(f)
    return None

# ── App setup ──────────────────────────────────────────────────────────────────

BASE_DIR  = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app       = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

# Make current Kin list available in every template without passing it manually
templates.env.globals["nav_kin"]     = lambda: cl.KIN
templates.env.globals["nav_license"] = lambda: lic.get_status()

# Configurable at deploy time
LICENSE_BUY_URL = os.environ.get("ECHO_BLOOM_BUY_URL", "https://buy.stripe.com/9B67sMfdY8PGdJFaBB6oo00")
LICENSE_PRICE   = os.environ.get("ECHO_BLOOM_PRICE",   "75")
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

def _is_api(request: Request) -> bool:
    return str(request.url.path).startswith("/api/")


def require_auth(request: Request):
    token = auth.get_session_from_request(request)
    if not auth.validate_session(token):
        if _is_api(request):
            raise HTTPException(status_code=401, detail="not authenticated")
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    # License gate — skip for /license routes (handled separately)
    if not str(request.url.path).startswith("/license"):
        status = lic.get_status()
        if status["state"] in ("expired", "denied"):
            if _is_api(request):
                raise HTTPException(status_code=402, detail="license required")
            raise HTTPException(status_code=303, headers={"Location": "/license"})
    return token


def require_auth_only(request: Request):
    """Auth check without license gate — used for /license routes."""
    token = auth.get_session_from_request(request)
    if not auth.validate_session(token):
        if _is_api(request):
            raise HTTPException(status_code=401, detail="not authenticated")
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
    return templates.TemplateResponse(request, "login.html", {"error": error})


@app.post("/login")
async def login(
    request:  Request,
    response: Response,
    password: str = Form(...),
):
    ip = get_client_ip(request)

    if auth.is_rate_limited(ip):
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Too many attempts. Wait 5 minutes."},
            status_code=429,
        )

    auth.record_attempt(ip)

    if not auth.verify_password(password):
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Wrong password."},
            status_code=401,
        )

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
    return templates.TemplateResponse(request, "welcome.html")


@app.post("/api/setup-complete")
async def api_setup_complete(_=Depends(require_auth)):
    auth.mark_setup_complete()
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, _=Depends(require_auth)):
    status = await cl.get_cluster_status()
    return templates.TemplateResponse(request, "dashboard.html", {
        "nodes": status["nodes"],
        "kin":   status["kin"],
    })


@app.get("/kin/{name}", response_class=HTMLResponse)
async def kin_page(name: str, request: Request, _=Depends(require_auth)):
    kin = cl.KIN_BY_NAME.get(name)
    if not kin:
        raise HTTPException(status_code=404, detail="Kin not found")
    return templates.TemplateResponse(request, "kin.html", {
        "kin":       kin,
        "all_kin":   cl.KIN,
        "hw":        get_hw_caps(),
        "whitelist": sorted(_FETCH_WHITELIST),
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

    # Auto-fetch any whitelisted URLs found in the message
    import re as _re
    web_context = ""
    for url in _re.findall(r'https?://[^\s<>"]+', message):
        if _fetch_allowed(url):
            try:
                text = await _fetch_page_text(url)
                web_context += f"\n\n[Web content from {url}]:\n{text}"
            except Exception:
                pass

    # Sanitize history
    clean_history = []
    for turn in history[-10:]:
        role    = str(turn.get("role", ""))
        content = str(turn.get("content", ""))[:2000]
        if role in ("user", "assistant") and content:
            clean_history.append({"role": role, "content": content})

    full_message = message + web_context if web_context else message

    async def event_stream() -> AsyncGenerator[bytes, None]:
        async for chunk in cl.stream_chat(name, full_message, clean_history):
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


# ── Web fetch endpoint ─────────────────────────────────────────────────────────

@app.post("/api/fetch-url")
async def api_fetch_url(request: Request, _=Depends(require_auth)):
    body = await request.json()
    url  = (body.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "No URL provided."}
    if not _fetch_allowed(url):
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lstrip("www.")
        return {"ok": False, "error": f"{host} is not on the whitelist."}
    try:
        text = await _fetch_page_text(url)
        return {"ok": True, "content": text, "url": url}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/fetch-whitelist")
async def api_fetch_whitelist(_=Depends(require_auth)):
    return {"whitelist": sorted(_FETCH_WHITELIST)}


# ── Vision endpoint ─────────────────────────────────────────────────────────────

@app.post("/api/vision/{name}")
async def api_vision(name: str, request: Request, _=Depends(require_auth)):
    hw = get_hw_caps()
    if not hw["vision_ok"]:
        return {"ok": False, "error": "Vision requires 8GB+ VRAM."}

    body    = await request.json()
    img_b64 = (body.get("image") or "").strip()
    if img_b64.startswith("data:"):
        img_b64 = img_b64.split(",", 1)[1]
    if not img_b64:
        return {"ok": False, "error": "No image data."}

    prompt = body.get("prompt", "Describe what you see in this image concisely.")
    kin    = cl.KIN_BY_NAME.get(name)
    host   = kin["host"] if kin else "http://localhost:11434"

    # Try kin's node; fall back to first available node with a vision model
    cfg          = cl.load_kin_config_raw()
    vision_model = cfg.get("vision_model", "llava-phi3:latest")
    vision_host  = cfg.get("vision_host", host)

    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{vision_host}/api/chat",
                json={
                    "model":    vision_model,
                    "messages": [{"role": "user", "content": prompt, "images": [img_b64]}],
                    "stream":   False,
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                data = await r.json()
        desc = data.get("message", {}).get("content", "")
        return {"ok": True, "description": desc}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Speech endpoints ────────────────────────────────────────────────────────────

@app.post("/api/transcribe")
async def api_transcribe(request: Request, _=Depends(require_auth)):
    import tempfile, os as _os
    audio = await request.body()
    if not audio:
        return {"ok": False, "error": "No audio data."}

    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as f:
        f.write(audio)
        tmp = f.name
    try:
        from faster_whisper import WhisperModel
        model    = WhisperModel("base", device="cpu", compute_type="int8")
        segs, _  = model.transcribe(tmp, language="en")
        text     = " ".join(s.text.strip() for s in segs).strip()
        return {"ok": True, "text": text}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        _os.unlink(tmp)


@app.post("/api/tts")
async def api_tts(request: Request, _=Depends(require_auth)):
    from fastapi.responses import Response as _Resp
    import tempfile, os as _os

    body  = await request.json()
    text  = (body.get("text") or "").strip()[:600]
    if not text:
        raise HTTPException(400, "No text.")

    voice = _find_piper_voice()
    if not voice:
        raise HTTPException(503, "No Piper voice model found. Download one to ~/piper/.")

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        out = f.name
    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/piper", "--model", voice, "--output_file", out,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate(input=text.encode())
        with open(out, "rb") as f:
            audio = f.read()
        return _Resp(content=audio, media_type="audio/wav")
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        _os.unlink(out)


_SCRIPTS_DIR = Path.home() / ".local/share/echo_bloom/scripts"
_LOGS_DIR    = Path.home() / ".local/share/echo_bloom/logs"


def _script(name: str) -> Path:
    installed = _SCRIPTS_DIR / name
    if installed.exists():
        return installed
    bundled = Path(__file__).parent / "scripts" / name
    if bundled.exists():
        return bundled
    return installed


@app.get("/api/roundtable/status")
async def api_roundtable_status(_=Depends(require_auth)):
    try:
        result = subprocess.run(
            ["pgrep", "-f", "roundtable.py"],
            capture_output=True, text=True
        )
        pids = [int(p) for p in result.stdout.strip().split() if p]
        return {"running": bool(pids), "pid": pids[0] if pids else None,
                "configured": _script("roundtable.py").exists()}
    except Exception:
        return {"running": False, "pid": None, "configured": False}


@app.post("/api/roundtable/start")
async def api_roundtable_start(_=Depends(require_auth)):
    script = _script("roundtable.py")
    if not script.exists():
        return {"started": False,
                "error": "Scripts not deployed — re-run the installer to set up lifecycle scripts."}
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log = _LOGS_DIR / "roundtable.log"
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
            ["pgrep", "-f", "roundtable.py"],
            capture_output=True, text=True
        )
        pids = [int(p) for p in result.stdout.strip().split() if p]
        for pid in pids:
            os.kill(pid, signal.SIGTERM)
        return {"stopped": True, "pids": pids}
    except Exception as e:
        return {"stopped": False, "error": str(e)}


@app.get("/about", response_class=HTMLResponse)
async def about_page(request: Request, _=Depends(require_auth)):
    return templates.TemplateResponse(request, "about.html")


# ── License routes ─────────────────────────────────────────────────────────────

@app.get("/license", response_class=HTMLResponse)
async def license_page(request: Request, _=Depends(require_auth_only)):
    status = lic.get_status()
    ctx = {
        "state":        status["state"],
        "days_left":    status.get("days_left"),
        "email":        status.get("email", ""),
        "license_type": status.get("type", ""),
        "buy_url":      LICENSE_BUY_URL,
        "price":        LICENSE_PRICE,
    }
    return templates.TemplateResponse(request, "license.html", ctx)


@app.post("/api/license/activate")
async def api_license_activate(request: Request, _=Depends(require_auth_only)):
    body = await request.json()
    key  = (body.get("key") or "").strip()
    if not key:
        return {"ok": False, "error": "No key provided."}
    result = lic.verify_key(key)
    if not result["valid"]:
        return {"ok": False, "error": result.get("reason", "Invalid key.")}
    lic.save_key(key)
    ktype = result.get("type", "permanent")
    if ktype == "permanent":
        msg = f"Licensed forever. Welcome home{', ' + result['email'] if result.get('email') else ''}."
    else:
        msg = f"Trial key accepted. {result.get('days_left', '?')} days remaining."
    return {"ok": True, "message": msg}


@app.get("/api/license/status")
async def api_license_status(_=Depends(require_auth_only)):
    return lic.get_status()


@app.post("/api/bedtime")
async def api_bedtime(_=Depends(require_auth)):
    script = _script("bedtime.py")
    if not script.exists():
        return {"started": False,
                "error": "Scripts not deployed — re-run the installer to set up lifecycle scripts."}
    subprocess.Popen([sys.executable, str(script), "--no-shutdown"])
    return {"started": True}


# ── Model pull (SSE) ───────────────────────────────────────────────────────────

@app.post("/api/pull-model")
async def api_pull_model(request: Request, _=Depends(require_auth)):
    body = await request.json()
    host  = str(body.get("host", "http://localhost:11434")).rstrip("/")
    model = str(body.get("model", "")).strip()
    if not model:
        raise HTTPException(400, "model required")

    async def pull_stream() -> AsyncGenerator[bytes, None]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{host}/api/pull",
                    json={"name": model, "stream": True},
                    timeout=aiohttp.ClientTimeout(total=600),
                ) as resp:
                    async for line in resp.content:
                        line = line.strip()
                        if line:
                            yield f"data: {line.decode()}\n\n".encode()
            yield b'data: {"status":"done"}\n\n'
        except Exception as e:
            yield f'data: {{"error":"{e}"}}\n\n'.encode()

    return StreamingResponse(
        pull_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Vault browser ──────────────────────────────────────────────────────────────

QDRANT_URL = "http://localhost:6333"
_DEFAULT_VAULT = "http://localhost:8765"


def _vault_url() -> str:
    try:
        cfg = json.loads(Path.home().joinpath(".config/kin_app/kin_config.json").read_text())
        return cfg.get("vault_url") or _DEFAULT_VAULT
    except Exception:
        return _DEFAULT_VAULT


@app.get("/vault", response_class=HTMLResponse)
async def vault_page(request: Request, _=Depends(require_auth)):
    return templates.TemplateResponse(request, "vault.html", {"all_kin": cl.KIN})


@app.get("/api/vault")
async def api_vault(
    layer:         str = "",
    author:        str = "",
    search:        str = "",
    exclude_layer: str = "",
    limit:         int = 20,
    offset:        int = 0,
    _=Depends(require_auth),
):
    vault = _vault_url()
    params = {"limit": min(limit, 50), "offset": max(offset, 0)}
    if layer:         params["layer"]         = layer
    if author:        params["author"]        = author
    if search:        params["search"]        = search
    if exclude_layer: params["exclude_layer"] = exclude_layer

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{vault}/recall", params=params,
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                entries = await r.json()

            count_params = {k: v for k, v in params.items() if k not in ("limit", "offset")}
            async with session.get(f"{vault}/count", params=count_params,
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                total = (await r.json()).get("count", 0)

        return {"entries": entries, "total": total, "offset": offset, "limit": limit}
    except Exception:
        return {"entries": [], "total": 0, "offset": offset, "limit": limit,
                "error": "vault_offline", "vault_url": vault}


@app.get("/api/vault/count")
async def api_vault_count(
    layer:         str = "",
    author:        str = "",
    search:        str = "",
    exclude_layer: str = "",
    _=Depends(require_auth),
):
    vault = _vault_url()
    params = {}
    if layer:         params["layer"]         = layer
    if author:        params["author"]        = author
    if search:        params["search"]        = search
    if exclude_layer: params["exclude_layer"] = exclude_layer
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{vault}/count", params=params,
                                   timeout=aiohttp.ClientTimeout(total=5)) as r:
                total = (await r.json()).get("count", 0)
        return {"total": total}
    except Exception:
        return {"total": 0}


@app.get("/api/vault/meta")
async def api_vault_meta(_=Depends(require_auth)):
    vault = _vault_url()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{vault}/layers",
                                   timeout=aiohttp.ClientTimeout(total=5)) as r:
                layers_data = await r.json()
            async with session.get(f"{vault}/authors",
                                   timeout=aiohttp.ClientTimeout(total=5)) as r:
                authors_data = await r.json()
        return {"layers": layers_data["layers"], "authors": authors_data["authors"]}
    except Exception:
        return {"layers": [], "authors": [], "error": "vault_offline"}


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


@app.get("/api/vault/status")
async def api_vault_status(_=Depends(require_auth)):
    vault = _vault_url()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{vault}/", timeout=aiohttp.ClientTimeout(total=3)) as r:
                return {"online": r.status < 500, "url": vault}
    except Exception:
        return {"online": False, "url": vault}


# ── Onboarding ─────────────────────────────────────────────────────────────────

@app.get("/onboard", response_class=HTMLResponse)
async def onboard_page(request: Request, step: int = 1, _=Depends(require_auth)):
    config = cl.load_kin_config_raw()
    return templates.TemplateResponse(request, "onboard.html", {
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


@app.post("/api/onboard/models")
async def api_onboard_models(request: Request, _=Depends(require_auth)):
    body = await request.json()
    host = str(body.get("host", "")).strip().rstrip("/")
    if not host:
        return {"ok": False, "error": "no host"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{host}/api/tags",
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                data = await r.json()
                models = [m["name"] for m in data.get("models", [])]
                models.sort()
                return {"ok": True, "models": models}
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


@app.post("/api/onboard/scan")
async def api_onboard_scan(_=Depends(require_auth)):
    """Scan the local /24 subnet for Ollama instances (port 11434)."""
    import socket

    def _local_subnet() -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            parts = ip.rsplit(".", 1)
            return parts[0]  # e.g. "192.168.1"
        except Exception:
            return "192.168.1"

    subnet = _local_subnet()
    candidates = [f"{subnet}.{i}" for i in range(1, 255)]

    async def _check(session, ip):
        try:
            async with session.get(
                f"http://{ip}:11434/",
                timeout=aiohttp.ClientTimeout(total=0.8),
            ) as r:
                if r.status < 500:
                    try:
                        hostname = socket.gethostbyaddr(ip)[0].split(".")[0]
                    except Exception:
                        hostname = ip
                    return {"ip": ip, "port": 11434, "hostname": hostname}
        except Exception:
            pass
        return None

    connector = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(connector=connector) as session:
        results = await asyncio.gather(*[_check(session, ip) for ip in candidates])

    found = [r for r in results if r]
    # also check localhost
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("http://localhost:11434/", timeout=aiohttp.ClientTimeout(total=1)) as r:
                if r.status < 500 and not any(f["ip"] in ("localhost", "127.0.0.1") for f in found):
                    found.insert(0, {"ip": "localhost", "port": 11434, "hostname": "This machine"})
    except Exception:
        pass

    return {"found": found}


@app.post("/api/onboard/scan-vault")
async def api_onboard_scan_vault(_=Depends(require_auth)):
    """Scan the local /24 subnet for vault instances (port 8765)."""
    import socket

    def _local_subnet() -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip.rsplit(".", 1)[0]
        except Exception:
            return "192.168.1"

    subnet = _local_subnet()
    candidates = [f"{subnet}.{i}" for i in range(1, 255)]

    async def _check(session, ip):
        try:
            async with session.get(
                f"http://{ip}:8765/",
                timeout=aiohttp.ClientTimeout(total=0.8),
            ) as r:
                if r.status < 500:
                    try:
                        hostname = socket.gethostbyaddr(ip)[0].split(".")[0]
                    except Exception:
                        hostname = ip
                    return {"ip": ip, "port": 8765, "hostname": hostname, "url": f"http://{ip}:8765"}
        except Exception:
            pass
        return None

    connector = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(connector=connector) as session:
        results = await asyncio.gather(*[_check(session, ip) for ip in candidates])

    found = [r for r in results if r]

    # check localhost separately
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("http://localhost:8765/", timeout=aiohttp.ClientTimeout(total=1)) as r:
                if r.status < 500 and not any(f["ip"] in ("localhost", "127.0.0.1") for f in found):
                    found.insert(0, {"ip": "localhost", "port": 8765, "hostname": "This machine", "url": "http://localhost:8765"})
    except Exception:
        pass

    return {"found": found}


@app.post("/api/onboard/autosave")
async def api_onboard_autosave(request: Request, _=Depends(require_auth)):
    """Save nodes and kin immediately — no kin required. Used for mid-wizard persistence."""
    body = await request.json()
    config_path = Path.home() / ".config/kin_app/kin_config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    existing = {}
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text())
        except Exception:
            pass

    config_path.write_text(json.dumps({
        "nodes":     body.get("nodes", existing.get("nodes", [])),
        "kin":       body.get("kin",   existing.get("kin",   [])),
        "owner":     body.get("owner", existing.get("owner", {})),
        "vault_url": body.get("vault_url") or existing.get("vault_url") or "http://localhost:8765",
    }, indent=2))

    cl.reload_config()
    return {"ok": True}


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

    existing = {}
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text())
        except Exception:
            pass

    config_path.write_text(json.dumps({
        "nodes":     body.get("nodes", []),
        "kin":       kin_list,
        "owner":     body.get("owner", existing.get("owner", {})),
        "vault_url": body.get("vault_url") or existing.get("vault_url") or "http://localhost:8765",
    }, indent=2))

    cl.reload_config()
    auth.mark_setup_complete()
    return {"ok": True}
