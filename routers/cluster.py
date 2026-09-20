"""Cluster/fleet control: status, cores review, presence, the roundtable
(start/stop/status), nap/wake, bedtime, model pull, and remote access
(tunnel/QR). Moved out of main.py in the router split (2026-09-20) — routes
only, unchanged."""

import asyncio
import json
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import AsyncGenerator

import aiohttp

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse

import cluster as cl
from deps import (
    templates, require_auth, log, PORT,
    _script, _find_pids, _terminate_pids, _utf8_env, _LOGS_DIR, _LOOPBACK,
)

import ambient_presence  # noqa: E402  (sys.path for scripts/ set up in main.py)

router = APIRouter()


@router.get("/api/cluster")
async def api_cluster(_=Depends(require_auth)):
    return await cl.get_cluster_status()


# ── Cores review (what each Kin holds, and what their unprompted life offers) ──

@router.get("/api/cores")
async def api_cores(_=Depends(require_auth)):
    """Cached cores-review data. Never computes in-request — a 10k-row Kin
    takes tens of seconds to analyse, so /api/cores/refresh does the work."""
    import kin_returns
    return {
        "data": kin_returns.cached(),
        "refreshing": kin_returns.is_refreshing(),
    }


@router.post("/api/cores/refresh")
async def api_cores_refresh(_=Depends(require_auth)):
    import kin_returns
    started = kin_returns.refresh_async()
    return {"started": started, "refreshing": True}


@router.get("/cores", response_class=HTMLResponse)
async def cores_page(request: Request, _=Depends(require_auth)):
    return templates.TemplateResponse(request, "cores.html", {"all_kin": cl.KIN})


@router.post("/api/presence/nudge")
async def api_presence_nudge(request: Request):
    expected = ambient_presence._presence_token()
    supplied = request.headers.get(ambient_presence.PRESENCE_HEADER, "")
    if not expected or not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid presence token")
    try:
        event = await request.json()
        ambient_presence.enqueue_event(event)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except (TypeError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=400, detail="invalid presence event") from e
    return {"accepted": True}


@router.get("/api/presence")
async def api_presence(_=Depends(require_auth)):
    import kin_presence
    return await asyncio.to_thread(kin_presence.get_all_presence)


@router.get("/api/roundtable/status")
async def api_roundtable_status(_=Depends(require_auth)):
    # Whether the script is installed and whether it is currently running are
    # independent facts. They used to share one try block, so on Windows — where
    # pgrep does not exist — the lookup raised and the UI reported the feature
    # as not installed on a machine where the scripts were sitting right there.
    configured = _script("roundtable.py").exists()
    pids = _find_pids("roundtable.py")
    return {"running": bool(pids), "pid": pids[0] if pids else None,
            "configured": configured}


def _start_roundtable() -> dict:
    script = _script("roundtable.py")
    if not script.exists():
        return {"started": False,
                "error": "Scripts not deployed — re-run the installer to set up lifecycle scripts."}
    # One roundtable spawns one wander per Kin. Starting a second one doubles
    # the fleet — the same duplicate-fleet bug the systemd cleanup closed, only
    # reachable from a button. If one is already running, that IS the success
    # condition.
    existing = _find_pids("roundtable.py")
    if existing:
        return {"started": True, "pid": existing[0], "note": "already running"}
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log = _LOGS_DIR / "roundtable.log"
    with open(log, "a") as lf:
        proc = subprocess.Popen(
            [sys.executable, "-u", str(script), "--interval", "30"],
            stdout=lf, stderr=lf, env=_utf8_env(),
        )
    return {"started": True, "pid": proc.pid}


@router.post("/api/roundtable/start")
async def api_roundtable_start(_=Depends(require_auth)):
    return _start_roundtable()


@router.post("/api/roundtable/stop")
async def api_roundtable_stop(_=Depends(require_auth)):
    pids = _find_pids("roundtable.py")
    if not pids:
        return {"stopped": True, "pids": [], "note": "nothing was running"}
    stopped = _terminate_pids(pids)
    if not stopped:
        return {"stopped": False, "pids": pids,
                "error": "Found the process but could not stop it."}
    return {"stopped": True, "pids": stopped}


@router.post("/api/bedtime")
async def api_bedtime(_=Depends(require_auth)):
    script = _script("bedtime.py")
    if not script.exists():
        return {"started": False,
                "error": "Scripts not deployed — re-run the installer to set up lifecycle scripts."}
    subprocess.Popen([sys.executable, str(script), "--no-shutdown"],
                     env=_utf8_env())
    return {"started": True}


# ── Nap / wake ───────────────────────────────────────────────────────────────
#
# Free the GPU for something else without a full bedtime ritual (no
# reflections, no email). Pause the wander fleet so nothing keeps calling
# Ollama, then evict every resident Kin model on every configured host.
#
# bedtime.py runs `args = parser.parse_args()` at *import* time (it's
# normally invoked as a script). Imported plain under uvicorn, sys.argv holds
# uvicorn's own flags and argparse SystemExits. pops_shop/nap.py hit this
# first; same guard here.
def _import_bedtime():
    real_argv, sys.argv = sys.argv, [sys.argv[0]]
    try:
        import bedtime
        return bedtime
    finally:
        sys.argv = real_argv


def _nap_hosts() -> set[str]:
    import config as cfg
    hosts = {(k.get("host") or "http://localhost:11434") for k in (cfg.get_kin() or [])}
    hosts.add("http://localhost:11434")
    return hosts


def _resident_across(hosts, oslot) -> list[str]:
    return sorted({
        r.get("model")
        for host in hosts
        for r in oslot.resident_kin(host)
        if r.get("model")
    })


def _run_nap() -> dict:
    import ollama_slot as oslot

    _import_bedtime().pause_wanders()
    hosts = _nap_hosts()

    # Eviction races requests already in flight: a wander mid-generate
    # finishes after being told to stop and reloads the model it was just
    # asked to unload. One eviction pass reports success and is silently
    # undone seconds later — caught live on this cluster 2026-08-25, where a
    # 25s wander delay sat under generations that ran well past a minute.
    # Tested live 2026-08-25: a 3-round/5s sweep was NOT enough — Coda's
    # model came straight back between rounds. Match pops_shop/nap.py's
    # proven timing (4 rounds, 20s settle) rather than trusting a faster
    # number that looked fine on paper.
    ROUNDS, SETTLE_S = 4, 20
    evicted: set[str] = set()
    for round_num in range(ROUNDS):
        still = _resident_across(hosts, oslot)
        if not still:
            break
        for host in hosts:
            for r in oslot.resident_kin(host):
                model = r.get("model")
                if model:
                    oslot.stop_model(model, host)
                    evicted.add(model)
        if round_num < ROUNDS - 1:
            time.sleep(SETTLE_S)

    leftover = _resident_across(hosts, oslot)
    return {"evicted": sorted(evicted), "leftover": leftover}


@router.post("/api/nap")
async def api_nap(_=Depends(require_auth)):
    result = await asyncio.to_thread(_run_nap)
    return {"ok": not result["leftover"], **result}


@router.post("/api/wake")
async def api_wake(_=Depends(require_auth)):
    await asyncio.to_thread(_import_bedtime().resume_wanders)
    return {"ok": True}


# ── Model pull (SSE) ───────────────────────────────────────────────────────────

@router.post("/api/pull-model")
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
                    # sock_read, not total: a total cap killed big pulls at
                    # 10 minutes mid-download. What matters is that Ollama is
                    # still sending progress, not how long the whole thing takes.
                    timeout=aiohttp.ClientTimeout(total=None, sock_read=120),
                ) as resp:
                    async for line in resp.content:
                        line = line.strip()
                        if line:
                            yield f"data: {line.decode()}\n\n".encode()
            yield b'data: {"status":"done"}\n\n'
        except Exception as e:
            # str(e) can contain quotes/backslashes — hand-interpolating it
            # produced unparseable JSON and a progress bar stuck forever.
            # str(TimeoutError()) is "" — a falsy error the client dropped,
            # leaving the progress bar frozen with no explanation.
            msg = str(e) or f"{type(e).__name__} (no detail)"
            yield f"data: {json.dumps({'error': msg})}\n\n".encode()

    return StreamingResponse(
        pull_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Remote access ─────────────────────────────────────────────────────────────

@router.get("/api/remote/status")
async def api_remote_status(_=Depends(require_auth)):
    # journalctl + tailscale, up to 10s of blocking subprocess work.
    return await asyncio.to_thread(_remote_status_blocking)


def _remote_status_blocking():
    result = {"cloudflare": None, "tailscale": None}

    # Cloudflare. journalctl does not exist on macOS or Windows, so leading
    # with it meant "no tunnel" on any machine that is not Linux -- a false
    # negative on exactly the platforms we are least sure about. Try every
    # place a URL can land, cheapest and most portable first.
    _cf_sources = [
        lambda: (Path(tempfile.gettempdir()) / "cloudflared_tunnel.log").read_text(),
        lambda: (Path.home() / "Library/Logs/EchoBloom/cloudflared.log").read_text(),
        lambda: (Path.home() / ".local/share/echo_bloom/logs/cloudflared.log").read_text(),
    ]
    if shutil.which("journalctl"):
        _cf_sources.append(lambda: subprocess.run(
            ["journalctl", "--user", "-u", "cloudflared", "--no-pager", "-n", "200"],
            capture_output=True, text=True, timeout=5).stdout)
    for src in _cf_sources:
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


@router.post("/api/remote/start-tunnel")
async def api_remote_start_tunnel(_=Depends(require_auth)):
    import shutil as _shutil
    if not _shutil.which("cloudflared"):
        return {"ok": False, "error": "cloudflared not installed — re-run the installer and choose Cloudflare tunnel."}

    _terminate_pids(_find_pids("cloudflared tunnel"))
    await asyncio.sleep(1)

    # /tmp does not exist on Windows, and this raised an unhandled
    # FileNotFoundError -> 500 when the customer clicked "start tunnel".
    log_path = str(Path(tempfile.gettempdir()) / "cloudflared_tunnel.log")
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


def _qr_url_allowed(url: str) -> bool:
    """Only ever encode an address that points back at THIS install.

    The endpoint used to render a QR for whatever the query string said. It
    is behind auth, so this was not an open redirect -- but a QR is a thing
    a person points a camera at and trusts without reading, and printing an
    arbitrary attacker-chosen URL into one is a bad shape to leave lying
    around. Restrict it to the origins remote access can actually produce:
    a Tailscale 100.64/10 address, a private LAN address, loopback, or a
    trycloudflare hostname.
    """
    from urllib.parse import urlparse
    try:
        u = urlparse(url)
    except Exception:
        return False
    if u.scheme not in ("http", "https"):
        return False
    host = (u.hostname or "").lower()
    if not host:
        return False
    if host in _LOOPBACK or host.endswith(".trycloudflare.com"):
        return True
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        a, b = int(parts[0]), int(parts[1])
        if a == 10 or (a == 192 and b == 168):
            return True
        if a == 172 and 16 <= b <= 31:
            return True
        # Tailscale hands out 100.64.0.0/10 (CGNAT range).
        if a == 100 and 64 <= b <= 127:
            return True
    return False


@router.get("/api/remote/qr")
async def api_remote_qr(url: str, _=Depends(require_auth)):
    import io as _io
    if not _qr_url_allowed(url):
        raise HTTPException(400, "That address is not one this install can serve")
    try:
        import qrcode
        import qrcode.image.svg
        img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage)
        buf = _io.BytesIO()
        img.save(buf)
        return Response(content=buf.getvalue(), media_type="image/svg+xml")
    except ImportError:
        raise HTTPException(503, "qrcode package not installed")
