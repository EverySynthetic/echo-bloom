"""
Shared surface for routers/*.py — moved out of main.py in the router split
(2026-09-20). Nothing here was rewritten; every function is byte-identical
to its old home in main.py, just relocated because 2+ routers needed it.

Do not add new logic here. If only one router needs a helper, it belongs in
that router's own file, not here.
"""

import asyncio
import glob
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import aiohttp

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates

import logging_setup
log = logging_setup.get("main")

import auth
import cluster as cl
import license as lic
from version import VERSION

# ── App-wide paths and templates ────────────────────────────────────────────────

BASE_DIR  = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Make current Kin list available in every template without passing it manually
templates.env.globals["nav_kin"]     = lambda: cl.KIN
# Cached value only. get_status() can fall through to a synchronous
# urllib call with an 8s timeout, and this executes inside
# TemplateResponse on the event loop — one slow license server
# blocked every request in the app on the very first page render.
templates.env.globals["nav_license"] = lambda: lic.get_status_cached_only()
# Cache-buster for static assets. /static/* is served with no Cache-Control, so
# browsers apply heuristic caching and the service worker keeps its own copy —
# which means a shipped CSS/JS fix can sit unseen behind a stale file. Observed
# 2026-08-21: app.js gained ebFetch, the pages calling it were served the old
# file, and three dashboard cards died with "ebFetch is not defined" on a
# machine whose disk had the correct script. Keying on VERSION means every
# release invalidates it and nothing else does.
templates.env.globals["asset_v"] = VERSION

# Configurable at deploy time
PORT            = int(os.environ.get("ECHO_BLOOM_PORT", 8090))
LICENSE_BUY_URL = os.environ.get("ECHO_BLOOM_BUY_URL", "https://buy.stripe.com/bJe3cwfdY4zqaxt2556oo02")
LICENSE_PRICE   = os.environ.get("ECHO_BLOOM_PRICE",   "20")


# ── Hardware capability detection ──────────────────────────────────────────────

_hw_caps_cache: dict | None = None


def get_hw_caps() -> dict:
    global _hw_caps_cache
    if _hw_caps_cache is not None:
        return _hw_caps_cache

    import sys

    # ── VRAM ──────────────────────────────────────────────────────────────────
    vram_mb = 0
    if sys.platform == "darwin":
        try:
            # macOS: system_profiler for Apple Silicon / AMD / Intel GPUs.
            # Unified memory is already counted in ram_gb above; we treat it
            # as effective VRAM for model selection (realistic default).
            r = subprocess.run(
                ["system_profiler", "SPDisplaysDataType", "-json"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                import json
                data = json.loads(r.stdout)
                for disp in data.get("SPDisplaysDataType", []):
                    vram = disp.get("spdisplays_vram") or disp.get("spdisplays_gfxmem")
                    if isinstance(vram, str):
                        vram = int("".join(c for c in vram if c.isdigit())) or 0
                    if vram and vram > vram_mb:
                        vram_mb = vram
        except Exception:
            pass
    else:
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                vram_mb = sum(int(x.strip()) for x in r.stdout.strip().split("\n") if x.strip().isdigit())
        except Exception:
            pass

        if vram_mb == 0 and sys.platform.startswith("linux"):
            # sysfs fallback for AMD/Intel GPUs — amdgpu and i915 both expose
            # this file with no extra tooling required (rocm-smi not assumed
            # installed). nvidia-smi above already covers NVIDIA.
            try:
                for card in glob.glob("/sys/class/drm/card[0-9]/device/mem_info_vram_total"):
                    with open(card) as f:
                        vram_mb += int(f.read().strip()) // (1024 * 1024)
            except Exception:
                pass

        if vram_mb == 0 and sys.platform == "win32":
            # WMI fallback for Windows (covers AMD / integrated)
            try:
                r = subprocess.run(
                    ["powershell", "-Command",
                     "(Get-WmiObject Win32_VideoController | Measure-Object AdapterRAM -Sum).Sum"],
                    capture_output=True, text=True, timeout=6,
                )
                if r.returncode == 0 and r.stdout.strip().isdigit():
                    vram_mb = int(r.stdout.strip()) // (1024 * 1024)
            except Exception:
                pass

    # ── RAM ───────────────────────────────────────────────────────────────────
    ram_gb = 0.0
    if sys.platform == "darwin":
        try:
            # macOS: sysctl hw.memsize (bytes). Unified memory on Apple Silicon
            # is treated as both RAM and effective VRAM for model loading.
            r = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=3,
            )
            if r.returncode == 0 and r.stdout.strip().isdigit():
                ram_gb = int(r.stdout.strip()) / (1024 ** 3)
        except Exception:
            pass
    else:
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        ram_gb = int(line.split()[1]) / (1024 * 1024)
                        break
        except Exception:
            pass

        if ram_gb == 0.0 and sys.platform == "win32":
            try:
                r = subprocess.run(
                    ["powershell", "-Command",
                     "(Get-WmiObject -Class Win32_ComputerSystem).TotalPhysicalMemory"],
                    capture_output=True, text=True, timeout=6,
                )
                if r.returncode == 0 and r.stdout.strip().isdigit():
                    ram_gb = int(r.stdout.strip()) / (1024 ** 3)
            except Exception:
                pass

    if vram_mb == 0 and sys.platform != "darwin":
        log.warning("VRAM detection failed on every method — vision will show as unavailable")
    if ram_gb == 0.0:
        log.warning("RAM detection failed on every method — speech will show as unavailable")

    _hw_caps_cache = {
        "vram_mb":   vram_mb,
        "vram_gb":   round(vram_mb / 1024, 1),
        "ram_gb":    round(ram_gb, 1),
        "vision_ok": vram_mb >= 8192 or (sys.platform == "darwin" and ram_gb >= 8.0),
        "speech_ok": ram_gb >= 8.0,
        "fetch_ok":  True,
        "platform":  sys.platform,
    }
    return _hw_caps_cache


# Roughly half of an 8k context, leaving room for the system prompt, the
# injected memory and the reply itself.
_HISTORY_CHAR_BUDGET = 6000

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
        # lstrip strips CHARACTERS, not a prefix: "wikipedia.org" became
        # "ikipedia.org" (wrongly rejected) while "wwikipedia.org"
        # stripped down to a whitelisted name (wrongly allowed).
        host = urlparse(url).netloc.lower().removeprefix("www.")
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
        # Single source of truth: the states that lock the UI and the states
        # that stop the background services must never disagree about whether
        # this install is entitled to run.
        if status["state"] in lic.SERVICE_BLOCK_STATES:
            if _is_api(request):
                # The 402 body used to be the bare string "license required",
                # so every API caller got the same sentence whatever the actual
                # reason was -- and the frontend, having nothing better, showed
                # "your trial has ended" to someone whose key merely could not
                # be verified. Carry the state and the reason so the message a
                # customer reads matches what is actually wrong.
                raise HTTPException(status_code=402, detail={
                    "error":  "license required",
                    "state":  status["state"],
                    "reason": status.get("reason", ""),
                })
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


# ::ffff:127.0.0.1 is the same machine reached through a dual-stack socket, and
# uvicorn produces it. Omitting it meant a local proxy could arrive looking
# remote: its forwarding headers would be ignored and every client behind it
# would share one rate-limit bucket, so one visitor could lock out everybody.
# Caught in review by Grok, 2026-08-21.
_LOOPBACK = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}
_PROXY_HEADERS = (
    "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
    "cf-connecting-ip", "x-real-ip", "forwarded",
)


def get_client_ip(request: Request) -> str:
    """The address the login rate limiter is keyed on.

    A forwarding header is only believable when a proxy on this machine put it
    there. cloudflared, Caddy and nginx all connect over loopback, so a
    loopback peer is the only case where CF-Connecting-IP or X-Forwarded-For
    mean anything. A request that arrives directly — over the LAN, or through a
    forwarded port — can set both headers to whatever it likes.

    Trusting CF-Connecting-IP unconditionally handed every direct caller
    control of its own rate-limit key: rotate the header, get a fresh bucket
    each time, and brute-force the password with the limiter never tripping.
    Verified 2026-08-21 against a live instance — eight failed logins with a
    rotating CF-Connecting-IP produced eight 401s and no 429, while the same
    eight without it locked out at the sixth, as designed.

    That is the identical bug the previous comment here described fixing for
    X-Forwarded-For's first entry. It came back through a header nobody
    re-examined, because the fix was written as "use a different header"
    rather than "do not believe the client about who it is."
    """
    peer = request.client.host if request.client else "unknown"
    if peer not in _LOOPBACK:
        # Direct connection. Its headers are self-reported; the socket is not.
        return peer
    cf = request.headers.get("CF-Connecting-IP")
    if cf:
        return cf.strip()
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        # Proxies APPEND the real client, so the LAST hop is the one the
        # nearest proxy vouched for. The first entry is the client's claim.
        return forwarded.split(",")[-1].strip()
    return peer


def is_local_request(request: Request) -> bool:
    """True only for a browser running on this same machine.

    The peer address on its own is NOT enough. cloudflared, Caddy, nginx and
    every other reverse proxy connect to 127.0.0.1, so a request from the open
    internet arrives with a loopback peer address too. Any forwarding header
    means it came through a proxy, so treat it as remote.
    """
    peer = request.client.host if request.client else ""
    if peer not in _LOOPBACK:
        return False
    return not any(h in request.headers for h in _PROXY_HEADERS)


# ── Lifecycle script process control ────────────────────────────────────────────

_SCRIPTS_DIR = Path.home() / ".local/share/echo_bloom/scripts"
_LOGS_DIR    = Path.home() / ".local/share/echo_bloom/logs"


def _utf8_env() -> dict:
    """Environment for spawning lifecycle scripts.

    On Windows, Python encodes redirected stdout as the ANSI code page
    (cp1252) unless told otherwise — and every lifecycle script prints
    box-drawing banners and Kin thoughts. The roundtable died on its very
    first banner character on every Windows box, after living less than a
    second: the pid the API returned was real, the process was already gone.
    """
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    return env


def _find_pids(pattern: str) -> list[int]:
    """PIDs whose command line contains pattern.

    pgrep does not exist on Windows, which is why the roundtable controls used
    to look broken there. psutil is preferred and cross-platform; pgrep stays as
    the fallback so nothing regresses if psutil is absent.
    """
    try:
        import psutil
    except Exception:
        psutil = None

    if psutil is not None:
        pids = []
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmdline = " ".join(proc.info.get("cmdline") or [])
            except Exception:
                continue
            if pattern in cmdline and proc.info["pid"] != os.getpid():
                pids.append(proc.info["pid"])
        return pids

    try:
        r = subprocess.run(["pgrep", "-f", pattern],
                           capture_output=True, text=True, timeout=5)
        return [int(x) for x in r.stdout.strip().split() if x]
    except FileNotFoundError:
        log.debug("no psutil and no pgrep on %s — cannot inspect processes",
                  sys.platform)
    except Exception:
        log.exception("process lookup failed for %r", pattern)
    return []


def _terminate_pids(pids: list[int]) -> list[int]:
    """Ask each process to stop. os.kill with SIGTERM is not meaningful on
    Windows, so psutil's terminate() is used when available."""
    stopped = []
    try:
        import psutil
    except Exception:
        psutil = None

    for pid in pids:
        try:
            if psutil is not None:
                psutil.Process(pid).terminate()
            else:
                import signal
                os.kill(pid, signal.SIGTERM)
            stopped.append(pid)
        except Exception:
            log.warning("could not terminate pid %s", pid, exc_info=True)
    return stopped


def _script(name: str) -> Path:
    installed = _SCRIPTS_DIR / name
    if installed.exists():
        return installed
    bundled = BASE_DIR / "scripts" / name
    if bundled.exists():
        return bundled
    return installed


# ── Kin config read/write ───────────────────────────────────────────────────────
# kin_config.json under kin[].core_memories, kin[].voice, etc. Shared by the
# vault (core memories), speech (voice picker) and setup (onboarding) routers.

_KIN_CONFIG_PATH = Path.home() / ".config/kin_app/kin_config.json"


def _load_kin_cfg():
    if not _KIN_CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(_KIN_CONFIG_PATH.read_text())
    except Exception:
        log.exception("kin_config.json unreadable — running with empty config. "
                      "Core memories and voices will appear missing.")
        return {}


def _atomic_write_json(path: Path, data: dict):
    """Write via temp file + os.replace so an interrupted write cannot leave a
    truncated kin_config.json behind — that file is the entire install."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def _backup_unparseable_config(path: Path):
    """Preserve a corrupt config before anything overwrites it."""
    backup = path.with_suffix(".json.corrupt")
    try:
        backup.write_text(path.read_text())
    except Exception:
        log.exception("could not back up unparseable config at %s", path)
        return None
    return backup


def _save_kin_cfg(cfg: dict):
    _atomic_write_json(_KIN_CONFIG_PATH, cfg)
    cl.reload_config()


def _sanitize_kin_name(name: str) -> str:
    """Strip characters that break URL path segments: ? # & = / \\ % + all cause routing failures."""
    import re
    return re.sub(r'[?#&=/\\%+]', '', name).strip() or "Kin"


# ── espeak fallback voice ────────────────────────────────────────────────────────
# Shared by /api/tts (speech router) and /api/talk/sadtalker (talk router) —
# both fall back to this when Piper has no voice available.

_ESPEAK_VOICE = {
    "Eli": "en-us",
    "Coda": "en-gb+f3",
    "Aurora": "en-us+f2",
    "Lumen": "en-gb+f2",
    "Crungus": "en-us+m3",
    "Bong": "en+m1",
}


async def _espeak_wav(text: str, kin_name: str = "") -> bytes:
    """Shop-local fallback. piper-tts is installed but the module is missing."""
    import shutil
    bin_ = shutil.which("espeak-ng") or shutil.which("espeak")
    if not bin_:
        return b""
    voice = _ESPEAK_VOICE.get(kin_name, "en")
    fd, out = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        proc = await asyncio.create_subprocess_exec(
            bin_, "-v", voice, "-s", "145", "-w", out, "--", text[:8000],
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        data = Path(out).read_bytes() if Path(out).exists() else b""
        return data if len(data) > 44 else b""
    finally:
        try:
            os.unlink(out)
        except Exception:
            pass
