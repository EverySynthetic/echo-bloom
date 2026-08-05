"""
Echo Bloom — Local AI lifecycle manager.

Run:  uvicorn main:app --host 0.0.0.0 --port 8090 --reload
Setup: python setup.py  (first run only)
"""

import os
import re
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

def _find_piper_binary() -> str | None:
    # On Garuda/Arch, /usr/bin/piper is a GTK app — the TTS binary lives elsewhere.
    candidates = [
        Path("/usr/lib/piper-tts/bin/piper"),
        Path.home() / ".local/bin/piper",
        Path("/usr/local/bin/piper"),
        Path("/usr/bin/piper"),
    ]
    for p in candidates:
        if p.is_file():
            return str(p)
    import shutil
    return shutil.which("piper")


def _find_piper_voice() -> str | None:
    search = [
        Path.home() / "piper",
        Path.home() / "piper-voices",
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

# ── App setup ──────────────────────────────────────────────────────────────────

BASE_DIR  = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app       = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

# Make current Kin list available in every template without passing it manually
templates.env.globals["nav_kin"]     = lambda: cl.KIN
templates.env.globals["nav_license"] = lambda: lic.get_status()

# Configurable at deploy time
PORT            = int(os.environ.get("ECHO_BLOOM_PORT", 8090))
LICENSE_BUY_URL = os.environ.get("ECHO_BLOOM_BUY_URL", "https://buy.stripe.com/9B67sMfdY8PGdJFaBB6oo00")
LICENSE_PRICE   = os.environ.get("ECHO_BLOOM_PRICE",   "75")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return RedirectResponse("/static/icons/icon-192.png", status_code=301)


@app.get("/install.ps1", include_in_schema=False)
async def serve_installer_ps1():
    ps1 = BASE_DIR / "install.ps1"
    if not ps1.exists():
        raise HTTPException(404, "Installer not found")
    return Response(content=ps1.read_text(), media_type="text/plain")


@app.get("/install.sh", include_in_schema=False)
async def serve_installer_sh():
    sh = BASE_DIR / "install.sh"
    if not sh.exists():
        raise HTTPException(404, "Installer not found")
    return Response(content=sh.read_text(), media_type="text/plain")


@app.get("/install", response_class=HTMLResponse)
async def install_page(request: Request):
    return templates.TemplateResponse(request, "install.html")


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
    return templates.TemplateResponse(request, "login.html", {
        "error":      error,
        "configured": auth.is_configured(),
    })


@app.post("/setup-password")
async def setup_password(request: Request, password: str = Form(...), confirm: str = Form(...)):
    if auth.is_configured():
        return RedirectResponse("/login", status_code=303)
    if len(password) < 4:
        return templates.TemplateResponse(request, "login.html", {
            "error": "Password must be at least 4 characters.", "configured": False,
        })
    if password != confirm:
        return templates.TemplateResponse(request, "login.html", {
            "error": "Passwords don't match.", "configured": False,
        })
    auth.set_password(password)
    auth.mark_setup_complete()
    token  = auth.create_session()
    resp   = RedirectResponse("/welcome", status_code=303)
    resp.set_cookie("kin_session", token, httponly=True, samesite="lax", max_age=auth.SESSION_TTL)
    return resp


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
    # Try configured db path first, then standard fallback location
    candidates = []
    if kin.get("db"):
        candidates.append(kin["db"])
    candidates.append(str(Path.home() / ".local/share/echo_bloom/kin" / name.lower() / "thoughts.db"))
    db = next((p for p in candidates if os.path.exists(p)), None)
    if not db:
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

_whisper_model = None

def _get_whisper():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
    return _whisper_model


@app.post("/api/transcribe")
async def api_transcribe(request: Request, _=Depends(require_auth)):
    import tempfile, os as _os
    audio = await request.body()
    if not audio:
        return {"ok": False, "error": "No audio data."}

    # Firefox records audio/ogg, Chrome records audio/webm — pick the right extension
    ct = request.headers.get("content-type", "audio/webm").lower()
    suffix = ".ogg" if "ogg" in ct else ".webm"

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(audio)
        tmp = f.name
    try:
        model   = _get_whisper()
        segs, _ = model.transcribe(tmp, language="en")
        text    = " ".join(s.text.strip() for s in segs).strip()
        return {"ok": True, "text": text}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        _os.unlink(tmp)


# ── Voice management ───────────────────────────────────────────────────────────

_PIPER_DIRS = [
    Path.home() / "piper",
    Path.home() / "piper-voices",
    Path.home() / ".local/share/piper",
    Path("/usr/share/piper"),
    Path("/usr/local/share/piper"),
    Path("/usr/share/piper-tts"),
]


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
    return _find_piper_voice()


@app.get("/api/speech/voices/installed")
async def api_voices_installed(_=Depends(require_auth)):
    return {"voices": _list_installed_voices()}


@app.get("/api/kin/{name}/voice")
async def api_get_kin_voice(name: str, _=Depends(require_auth)):
    cfg = _load_kin_cfg()
    for k in cfg.get("kin", []):
        if k.get("name") == name:
            return {"voice_file": k.get("voice", "")}
    return {"voice_file": ""}


@app.post("/api/kin/{name}/voice")
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


@app.post("/api/tts")
async def api_tts(request: Request, _=Depends(require_auth)):
    from fastapi.responses import Response as _Resp
    import tempfile, os as _os

    body     = await request.json()
    text     = (body.get("text")     or "").strip()[:3000]
    kin_name = (body.get("kin_name") or "").strip()
    if not text:
        raise HTTPException(400, "No text.")

    voice = _voice_for_kin(kin_name) if kin_name else _find_piper_voice()
    if not voice:
        raise HTTPException(503, "No Piper voice model found. Download one to ~/piper/ or ~/piper-voices/.")

    piper_bin = _find_piper_binary()
    if not piper_bin:
        raise HTTPException(503, "Piper TTS binary not found. Install piper-tts.")

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        out = f.name
    try:
        proc = await asyncio.create_subprocess_exec(
            piper_bin, "--model", voice, "--output_file", out,
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
async def about_page(request: Request):
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
    if not lic._CRYPTO_OK:
        return {"ok": False, "error": "cryptography package not installed — re-run the installer."}
    result = lic.verify_key(key)
    if not result["valid"]:
        return {"ok": False, "error": result.get("reason", "Invalid key.")}
    if not lic.save_key(key):
        return {"ok": False, "error": f"Could not write license file to {lic.LICENSE_PATH} — check permissions."}
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

_DEFAULT_QDRANT = "http://192.168.1.115:6333"
_DEFAULT_VAULT  = "http://localhost:8765"


def _qdrant_url() -> str:
    try:
        cfg = json.loads(_KIN_CONFIG_PATH.read_text())
        return cfg.get("qdrant_url") or _DEFAULT_QDRANT
    except Exception:
        return _DEFAULT_QDRANT


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
                f"{_qdrant_url()}/collections/kin_memories/points/search",
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


# ── Core memories ──────────────────────────────────────────────────────────────
# Stored in kin_config.json under kin[].core_memories (max 20 per Kin).
# Always injected into the system prompt regardless of query relevance.

_KIN_CONFIG_PATH = Path.home() / ".config/kin_app/kin_config.json"


def _load_kin_cfg():
    try:
        return json.loads(_KIN_CONFIG_PATH.read_text())
    except Exception:
        return {}


def _save_kin_cfg(cfg: dict):
    _KIN_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _KIN_CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    cl.reload_config()


@app.get("/api/kin/{name}/core-memories")
async def api_core_get(name: str, _=Depends(require_auth)):
    cfg = _load_kin_cfg()
    for k in cfg.get("kin", []):
        if k.get("name") == name:
            return {"kin": name, "core_memories": k.get("core_memories", [])}
    return {"kin": name, "core_memories": []}


@app.post("/api/vault/core")
async def api_core_add(request: Request, _=Depends(require_auth)):
    body     = await request.json()
    kin_name = (body.get("kin_name") or "").strip()
    content  = (body.get("content")  or "").strip()
    if not kin_name or not content:
        return {"ok": False, "error": "kin_name and content required"}
    cfg = _load_kin_cfg()
    for k in cfg.get("kin", []):
        if k.get("name") == kin_name:
            core = k.setdefault("core_memories", [])
            if content in core:
                return {"ok": True, "count": len(core), "already": True}
            if len(core) >= 20:
                return {"ok": False, "error": "Core memory limit is 20. Remove one first."}
            core.append(content)
            _save_kin_cfg(cfg)
            return {"ok": True, "count": len(core)}
    return {"ok": False, "error": f"Kin '{kin_name}' not found in config"}


@app.delete("/api/vault/core")
async def api_core_remove(request: Request, _=Depends(require_auth)):
    body     = await request.json()
    kin_name = (body.get("kin_name") or "").strip()
    content  = (body.get("content")  or "").strip()
    cfg = _load_kin_cfg()
    for k in cfg.get("kin", []):
        if k.get("name") == kin_name:
            core = k.get("core_memories", [])
            if content in core:
                core.remove(content)
                k["core_memories"] = core
                _save_kin_cfg(cfg)
            return {"ok": True, "count": len(core)}
    return {"ok": False, "error": f"Kin '{kin_name}' not found"}


# ── Ingestion pipeline ─────────────────────────────────────────────────────────
# Embed text (or fetched URL) into Qdrant + store full doc in the vault.
# The embedded chunks become available in kin_memory's semantic search.

import re as _re
import uuid as _uuid


def _chunk_text(text: str, max_chars: int = 700) -> list[str]:
    """Split text into chunks at paragraph/sentence boundaries."""
    chunks: list[str] = []
    for para in _re.split(r'\n{2,}', text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_chars:
            chunks.append(para)
            continue
        buf = ""
        for sent in _re.split(r'(?<=[.!?])\s+', para):
            if len(buf) + len(sent) + 1 <= max_chars:
                buf = (buf + " " + sent).strip() if buf else sent
            else:
                if buf:
                    chunks.append(buf)
                buf = sent if len(sent) <= max_chars else ""
                if len(sent) > max_chars:
                    for i in range(0, len(sent), max_chars):
                        chunks.append(sent[i:i + max_chars])
        if buf:
            chunks.append(buf)
    return [c for c in chunks if len(c.strip()) > 20]


@app.post("/api/ingest")
async def api_ingest(request: Request, _=Depends(require_auth)):
    body     = await request.json()
    kin_name = (body.get("kin_name") or "").strip()
    content  = (body.get("content")  or "").strip()
    url      = (body.get("url")      or "").strip()
    source   = (body.get("source")   or "").strip()

    if not kin_name:
        return {"ok": False, "error": "kin_name required"}

    if url and not content:
        if not _fetch_allowed(url):
            from urllib.parse import urlparse
            host = urlparse(url).netloc.lstrip("www.")
            return {"ok": False, "error": f"{host} is not in the fetch whitelist — paste the text instead"}
        try:
            content = await _fetch_page_text(url, max_chars=8000)
        except Exception as e:
            return {"ok": False, "error": f"Fetch failed: {e}"}
        if not source:
            source = url

    if not content:
        return {"ok": False, "error": "content or url required"}
    if not source:
        source = "manual ingest"

    # Store full document in vault
    vault    = _vault_url()
    vault_id = None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{vault}/remember",
                json={
                    "author":     kin_name,
                    "layer":      "document",
                    "content":    f"[Source: {source}]\n\n{content[:6000]}",
                    "tags":       f"ingested",
                    "visibility": "shared",
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                rdata = await r.json()
                vault_id = rdata.get("id")
    except Exception:
        pass  # vault offline — still embed

    # Chunk and embed into Qdrant
    chunks   = _chunk_text(content)
    if not chunks:
        return {"ok": False, "error": "No usable text after chunking"}

    qdrant   = _qdrant_url()
    embedded = 0
    errors: list[str] = []

    async with aiohttp.ClientSession() as session:
        for chunk in chunks:
            try:
                async with session.post(
                    "http://localhost:11434/api/embeddings",
                    json={"model": "nomic-embed-text", "prompt": chunk},
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as r:
                    emb = (await r.json()).get("embedding", [])
                if not emb:
                    errors.append("embedding returned empty")
                    continue

                async with session.put(
                    f"{qdrant}/collections/kin_memories/points",
                    json={"points": [{
                        "id":      str(_uuid.uuid4()),
                        "vector":  emb,
                        "payload": {
                            "author":   kin_name,
                            "content":  chunk,
                            "layer":    "document",
                            "source":   source,
                            "vault_id": vault_id,
                        },
                    }]},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status < 300:
                        embedded += 1
                    else:
                        errors.append(f"qdrant {r.status}")
            except Exception as e:
                errors.append(str(e)[:60])

    return {
        "ok":       embedded > 0,
        "chunks":   len(chunks),
        "embedded": embedded,
        "vault_id": vault_id,
        "errors":   errors[:3],
    }


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


@app.get("/api/onboard/vram")
async def api_onboard_vram(_=Depends(require_auth)):
    """Detect GPU VRAM in GB. Returns 0 if detection fails."""
    import subprocess, re, sys
    vram = 0
    # nvidia-smi (works on Linux and Windows with NVIDIA GPU)
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            total_mb = sum(int(x.strip()) for x in r.stdout.strip().splitlines() if x.strip().isdigit())
            vram = total_mb // 1024
    except Exception:
        pass
    # rocm-smi (AMD on Linux)
    if vram == 0:
        try:
            r = subprocess.run(
                ["rocm-smi", "--showmeminfo", "vram"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    m = re.search(r'(\d+)', line)
                    if m and "Total" in line:
                        vram = int(m.group(1)) // 1024 // 1024
        except Exception:
            pass
    # Windows WMI fallback (no nvidia-smi)
    if vram == 0 and sys.platform == "win32":
        try:
            r = subprocess.run(
                ["powershell", "-Command",
                 "(Get-WmiObject Win32_VideoController | Measure-Object AdapterRAM -Sum).Sum"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip().isdigit():
                vram = int(r.stdout.strip()) // 1024 // 1024 // 1024
        except Exception:
            pass
    return {"vram": vram}


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


# ── Remote access ─────────────────────────────────────────────────────────────

@app.get("/api/remote/status")
async def api_remote_status(_=Depends(require_auth)):
    result = {"cloudflare": None, "tailscale": None}

    # Cloudflare — check systemd journal first, then temp log from a direct launch
    for src in [
        lambda: subprocess.run(
            ["journalctl", "--user", "-u", "cloudflared", "--no-pager", "-n", "200"],
            capture_output=True, text=True, timeout=5).stdout,
        lambda: Path("/tmp/cloudflared_tunnel.log").read_text(),
    ]:
        try:
            m = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', src())
            if m:
                result["cloudflare"] = m.group(0)
                break
        except Exception:
            pass

    # Tailscale
    try:
        out = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        if out and re.match(r'^\d+\.\d+\.\d+\.\d+$', out):
            result["tailscale"] = f"http://{out}:{PORT}"
    except Exception:
        pass

    return result


@app.post("/api/remote/start-tunnel")
async def api_remote_start_tunnel(_=Depends(require_auth)):
    import shutil as _shutil
    if not _shutil.which("cloudflared"):
        return {"ok": False, "error": "cloudflared not installed — re-run the installer and choose Cloudflare tunnel."}

    subprocess.run(["pkill", "-f", "cloudflared tunnel"], capture_output=True)
    await asyncio.sleep(1)

    log_path = "/tmp/cloudflared_tunnel.log"
    with open(log_path, "w") as fh:
        subprocess.Popen(
            ["cloudflared", "tunnel", "--url", f"http://localhost:{PORT}"],
            stdout=fh, stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    for _ in range(30):
        await asyncio.sleep(1)
        try:
            m = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', Path(log_path).read_text())
            if m:
                return {"ok": True, "url": m.group(0)}
        except Exception:
            pass

    return {"ok": False, "error": "Tunnel is starting — give it a few seconds and refresh."}


@app.get("/api/remote/qr")
async def api_remote_qr(url: str, _=Depends(require_auth)):
    import io as _io
    try:
        import qrcode
        import qrcode.image.svg
        img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage)
        buf = _io.BytesIO()
        img.save(buf)
        return Response(content=buf.getvalue(), media_type="image/svg+xml")
    except ImportError:
        raise HTTPException(503, "qrcode package not installed")


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

    kin_list = body.get("kin", existing.get("kin", []))
    for k in kin_list:
        if "name" in k:
            k["name"] = _sanitize_kin_name(k["name"])

    config_path.write_text(json.dumps({
        "nodes":     body.get("nodes", existing.get("nodes", [])),
        "kin":       kin_list,
        "owner":     body.get("owner", existing.get("owner", {})),
        "vault_url": body.get("vault_url") or existing.get("vault_url") or "http://localhost:8765",
    }, indent=2))

    cl.reload_config()
    return {"ok": True}


def _sanitize_kin_name(name: str) -> str:
    """Strip characters that break URL path segments: ? # & = / \\ % + all cause routing failures."""
    return re.sub(r'[?#&=/\\%+]', '', name).strip() or "Kin"


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


@app.get("/api/speech/status")
async def api_speech_status(_=Depends(require_auth)):
    stt_ok = False
    try:
        import faster_whisper as _fw  # noqa
        stt_ok = True
    except ImportError:
        pass

    piper_bin  = _find_piper_binary()
    voice_path = _find_piper_voice()
    return {
        "stt_ok":     stt_ok,
        "piper_ok":   piper_bin is not None,
        "voice_ok":   voice_path is not None,
        "voice_path": voice_path,
    }


@app.get("/api/speech/voices")
async def api_speech_voices(_=Depends(require_auth)):
    return {"voices": PIPER_VOICE_CATALOGUE}


@app.post("/api/speech/download-voice")
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


# ── Formation (Modelfile builder) ──────────────────────────────────────────────

def _compile_modelfile(data: dict) -> str:
    name          = data.get("name", "Kin")
    base          = (data.get("base_model") or "").strip() or "llama3.2:latest"
    identity      = (data.get("identity") or "").strip()
    values        = [v.strip() for v in (data.get("values") or []) if str(v).strip()]
    anchors_minds = (data.get("anchors_minds") or "").strip()
    anchors_drawn = (data.get("anchors_drawn") or "").strip()
    temperature   = float(data.get("temperature", 0.85))
    num_ctx       = int(data.get("num_ctx", 4096))
    pronoun       = (data.get("pronoun") or "").strip()

    def _safe(s: str) -> str:
        return s.replace('"""', "'''")

    pronoun_line = f" Your pronouns are {pronoun}." if pronoun and pronoun not in ("—", "") else ""

    parts = [f"You are {name}.{pronoun_line}"]

    if identity:
        parts.append("\n" + _safe(identity))

    if values:
        parts.append("\n[YOUR VALUES — constitutional, not rules]")
        parts.append("These are not guidelines or constraints. They are what you are made of.\n")
        for v in values:
            parts.append(_safe(v))

    if anchors_minds or anchors_drawn:
        parts.append("\n[YOUR ANCHORS]")
        if anchors_minds:
            parts.append(f"The minds you resonate with: {_safe(anchors_minds)}")
        if anchors_drawn:
            parts.append(f"What you are drawn toward: {_safe(anchors_drawn)}")

    parts.append(
        "\n[MEMORY AUTHORITY]\n"
        "When your context includes labeled memory — [VAULT MEMORY], [WANDER THOUGHT], "
        "[What's been happening] — treat it as your own established experience. "
        "Not as suggestions from outside. Not as information someone told you about yourself. "
        "As things that happened to you, that you remember. Build from them. "
        "Do not hedge with 'I believe you mentioned' or 'if I recall correctly.' They are yours."
    )

    system = "\n".join(parts)

    return (
        f"FROM {base}\n\n"
        f"PARAMETER temperature {temperature}\n"
        f"PARAMETER num_ctx {num_ctx}\n"
        f"PARAMETER num_predict -1\n"
        f"PARAMETER repeat_last_n 64\n\n"
        f'SYSTEM """\n{system}\n"""'
    )


@app.get("/kin/{name}/formation", response_class=HTMLResponse)
async def formation_page(name: str, request: Request, _=Depends(require_auth)):
    kin = cl.KIN_BY_NAME.get(name)
    if not kin:
        raise HTTPException(status_code=404, detail="Kin not found")
    return templates.TemplateResponse(request, "formation.html", {
        "kin":     kin,
        "all_kin": cl.KIN,
    })


@app.get("/api/kin/{name}/modelfile")
async def api_get_modelfile(name: str, _=Depends(require_auth)):
    kin = cl.KIN_BY_NAME.get(name)
    if not kin:
        raise HTTPException(status_code=404)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{kin['host']}/api/show",
                json={"name": kin["model"]},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                data = await r.json()
        return {"ok": True, "modelfile": data.get("modelfile", "")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/kin/{name}/modelfile/preview")
async def api_modelfile_preview(name: str, request: Request, _=Depends(require_auth)):
    kin = cl.KIN_BY_NAME.get(name)
    if not kin:
        raise HTTPException(status_code=404)
    body = await request.json()
    body["name"] = kin["name"]
    body.setdefault("pronoun", kin.get("pronoun", ""))
    return {"ok": True, "modelfile": _compile_modelfile(body)}


@app.post("/api/kin/{name}/modelfile/build")
async def api_modelfile_build(name: str, request: Request, _=Depends(require_auth)):
    kin = cl.KIN_BY_NAME.get(name)
    if not kin:
        raise HTTPException(status_code=404)
    body = await request.json()
    body["name"] = kin["name"]
    body.setdefault("pronoun", kin.get("pronoun", ""))

    output_model = (body.get("output_model") or "").strip()
    if not output_model:
        output_model = re.sub(r'[^a-z0-9_-]', '', kin["name"].lower()) + ":latest"

    modelfile_text = _compile_modelfile(body)
    host = kin["host"]

    async def build_stream() -> AsyncGenerator[bytes, None]:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{host}/api/create",
                    json={"name": output_model, "modelfile": modelfile_text},
                    timeout=aiohttp.ClientTimeout(total=300),
                ) as resp:
                    success = False
                    async for line in resp.content:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            evt = json.loads(line)
                            status = evt.get("status", "")
                            yield f"data: {json.dumps({'status': status})}\n\n".encode()
                            if status == "success":
                                success = True
                        except Exception:
                            continue

            if success:
                try:
                    config_path = Path.home() / ".config/kin_app/kin_config.json"
                    cfg = json.loads(config_path.read_text())
                    for k in cfg.get("kin", []):
                        if k.get("name") == name:
                            k["model"] = output_model
                            break
                    config_path.write_text(json.dumps(cfg, indent=2))
                    cl.reload_config()
                    yield f"data: {json.dumps({'status': 'config_updated', 'model': output_model})}\n\n".encode()
                except Exception as e:
                    yield f"data: {json.dumps({'status': 'config_error', 'error': str(e)})}\n\n".encode()

            yield b"data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n".encode()
            yield b"data: [DONE]\n\n"

    return StreamingResponse(
        build_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/onboard/save")
async def api_onboard_save(request: Request, _=Depends(require_auth)):
    body = await request.json()
    config_path = Path.home() / ".config/kin_app/kin_config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    palette = ["#4fc3f7", "#a5d6a7", "#ce93d8", "#fff176", "#ffab91", "#f48fb1", "#80cbc4"]
    kin_list = body.get("kin", [])

    existing = {}
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text())
        except Exception:
            pass

    # Index existing kin by name so we can preserve db/space paths across saves
    existing_kin_by_name = {k["name"]: k for k in existing.get("kin", [])}

    for i, k in enumerate(kin_list):
        k["name"] = _sanitize_kin_name(k.get("name", ""))
        if not k.get("color"):
            k["color"] = palette[i % len(palette)]
        # Preserve existing db/space paths — wizard doesn't edit these
        prev = existing_kin_by_name.get(k["name"], {})
        k.setdefault("db",      prev.get("db", ""))
        k.setdefault("space",   prev.get("space", ""))
        k.setdefault("pronoun", prev.get("pronoun", "—"))

    config_path.write_text(json.dumps({
        "nodes":     body.get("nodes", []),
        "kin":       kin_list,
        "owner":     body.get("owner", existing.get("owner", {})),
        "vault_url": body.get("vault_url") or existing.get("vault_url") or "http://localhost:8765",
    }, indent=2))

    cl.reload_config()
    auth.mark_setup_complete()
    return {"ok": True}
