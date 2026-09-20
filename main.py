"""
Echo Bloom — Local AI lifecycle manager.

Run:  uvicorn main:app --host 0.0.0.0 --port 8090 --reload
Setup: python setup.py  (first run only)

2026-09-20: split into routers/*.py. This file keeps only app creation,
middleware, startup, and include_router calls — every route moved out with
no behavior change. See ~/claude_home/REPORT_sonnet_routers.md.
"""

import os
import sys
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

import logging_setup
logging_setup.setup()
log = logging_setup.get("main")

import auth
import cluster as cl

# scripts/naming_ritual.py, naming_common.py and ambient_presence.py all live
# in scripts/, not on the default path. Done here, once, before any router is
# imported — routers/setup.py and routers/cluster.py both import names from
# there at module load time, and sys.path is process-global, so this one
# insert covers both.
sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))

from deps import BASE_DIR, templates  # noqa: E402  (after sys.path insert)
# Re-exported so tests/*.py can keep overriding main.require_auth /
# main.require_auth_only for TestClient — same function objects the routers
# bind with Depends(), just also reachable from here after the move. cl/lic
# are the same module objects every router imports, so patching
# main.lic.get_status or patch.dict(main.cl.KIN_BY_NAME, ...) still reaches
# every consumer. LICENSE_PRICE is a plain constant read, not mutated.
from deps import require_auth, require_auth_only, LICENSE_PRICE  # noqa: E402,F401
import license as lic  # noqa: E402,F401

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ── Security headers ───────────────────────────────────────────────────────────

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
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


# ── Setup check ────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def check_setup():
    from deps import get_hw_caps, _find_pids
    from routers.cluster import _start_roundtable

    if not auth.is_configured():
        tok = auth.ensure_setup_token()
        print("\n" + "="*60)
        print("  First run: no password set.")
        print("  Open http://localhost:8090 on THIS machine to set one.")
        if tok:
            print("")
            print("  Setting up from another device? Setup code:")
            print("      " + tok)
            print("  (also saved at " + str(auth.SETUP_TOKEN_FILE) + ")")
        print("="*60 + "\n")
    elif auth.load_config().get("setup_complete") is None:
        # Existing install without the flag — mark done so tour doesn't fire
        auth.mark_setup_complete()

    # Warm the hardware-caps cache in the background so the first page load
    # isn't blocked by WMI / nvidia-smi subprocess calls (can be 10+ s on Windows).
    import threading
    threading.Thread(target=get_hw_caps, daemon=True).start()

    # On Linux, systemd owns the wander/roundtable lifecycle. Windows has no
    # equivalent — the scheduled task starts only this app, so on every Windows
    # install the Kin sat idle until someone found the START button on the
    # dashboard. Start it here instead, once Kin exist. _start_roundtable()
    # is a no-op if one is already running.
    if os.name == "nt" and cl.KIN and auth.is_configured():
        def _autostart():
            time.sleep(10)  # let uvicorn finish binding; nothing depends on this
            res = _start_roundtable()
            print(f"[startup] roundtable autostart: {res}")
            # A pid alone is not proof of life — the roundtable once died in
            # under a second and the pid we'd just printed was already a ghost.
            time.sleep(5)
            if res.get("started") and not _find_pids("roundtable.py"):
                print("[startup] WARNING: roundtable exited within seconds of "
                      "starting — read logs/roundtable.log for the traceback.")
        threading.Thread(target=_autostart, daemon=True).start()


# ── Routers ────────────────────────────────────────────────────────────────────
# Moved out of this file 2026-09-20 (path move only — no prefixes, so every
# route keeps the exact path it had as an @app.* decorator). No two routes in
# the original file shared a method+path or an ambiguous path template
# (verified before the split), so include_router order below does not affect
# request dispatch.

from routers import (  # noqa: E402
    agent, agora, auth as auth_router, chat, cluster as cluster_router,
    dashboard, formation, license as license_router, setup, speech, talk,
    vault,
)

app.include_router(setup.router)
app.include_router(auth_router.router)
app.include_router(dashboard.router)
app.include_router(chat.router)
app.include_router(talk.router)
app.include_router(speech.router)
app.include_router(cluster_router.router)
app.include_router(agent.router)
app.include_router(license_router.router)
app.include_router(vault.router)
app.include_router(agora.router)
app.include_router(formation.router)
