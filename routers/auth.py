"""Login, first-run password setup, session lifecycle. Moved out of main.py
in the router split (2026-09-20) — routes only, unchanged."""

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

import auth
from deps import templates, require_auth, get_client_ip, is_local_request

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = "", mode: str = ""):
    configured = auth.is_configured()
    if not configured:
        auth.ensure_setup_token()
    return templates.TemplateResponse(request, "login.html", {
        "error":       error,
        "configured":  configured,
        # mode=login lets someone who landed on the setup form by accident
        # ask for the login form instead (and vice versa via a plain /login).
        "show_login":  configured or mode == "login",
        "config_path": str(auth.CONFIG_FILE),
        "setup_local": is_local_request(request),
        "token_path":  str(auth.SETUP_TOKEN_FILE),
        "min_len":     auth.MIN_PASSWORD_LEN,
    })


@router.post("/setup-password")
async def setup_password(
    request: Request,
    password: str = Form(...),
    confirm: str = Form(...),
    setup_token: str = Form(""),
):
    if auth.is_configured():
        # Tell them why instead of bouncing silently to a form that looks identical.
        return RedirectResponse(
            "/login?error=A+password+is+already+set+on+this+machine.+Enter+it+below.",
            status_code=303,
        )

    ip    = get_client_ip(request)
    local = is_local_request(request)

    def _render(err: str, status: int = 200):
        return templates.TemplateResponse(request, "login.html", {
            "error":       err,
            "configured":  False,
            "show_login":  False,
            "config_path": str(auth.CONFIG_FILE),
            "setup_local": local,
            "token_path":  str(auth.SETUP_TOKEN_FILE),
            "min_len":     auth.MIN_PASSWORD_LEN,
        }, status_code=status)

    # Claiming this install from anywhere other than the machine itself needs
    # the setup code. Without this an app that is reachable before a password
    # exists — over the LAN, or the tunnel install.sh starts at step 6 — can be
    # taken over by whoever loads it first.
    if not local:
        if auth.is_rate_limited(ip):
            return _render("Too many attempts. Wait 5 minutes.", 429)
        auth.record_attempt(ip)
        auth.ensure_setup_token()
        if not auth.verify_setup_token(setup_token):
            return _render(
                "That setup code isn't right. Read it from the machine running Echo Bloom.",
                403,
            )

    if len(password) < auth.MIN_PASSWORD_LEN:
        return _render("Password must be at least %d characters." % auth.MIN_PASSWORD_LEN)
    if password != confirm:
        return _render("Passwords don't match.")

    auth.set_password(password)
    auth.mark_setup_complete()
    auth.clear_setup_token()

    token    = auth.create_session()
    resp     = RedirectResponse("/welcome", status_code=303)
    is_https = request.headers.get("x-forwarded-proto") == "https" \
               or request.url.scheme == "https"
    resp.set_cookie(
        "kin_session", token,
        httponly=True,
        samesite="strict",
        secure=is_https,
        max_age=auth.SESSION_TTL,
    )
    return resp


@router.post("/login")
async def login(
    request:  Request,
    response: Response,
    password: str = Form(...),
):
    ip = get_client_ip(request)

    if auth.is_rate_limited(ip):
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Too many attempts. Wait 5 minutes.",
             "configured": auth.is_configured(),
             "show_login": True,
             "config_path": str(auth.CONFIG_FILE)},
            status_code=429,
        )

    auth.record_attempt(ip)

    if not auth.verify_password(password):
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Wrong password.",
             "configured": auth.is_configured(),
             "show_login": True,
             "config_path": str(auth.CONFIG_FILE)},
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


@router.get("/logout")
async def logout(request: Request):
    token = auth.get_session_from_request(request)
    auth.revoke_session(token)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("kin_session")
    return resp


@router.get("/welcome", response_class=HTMLResponse)
async def welcome_page(request: Request, _=Depends(require_auth)):
    return templates.TemplateResponse(request, "welcome.html")


@router.post("/api/setup-complete")
async def api_setup_complete(_=Depends(require_auth)):
    auth.mark_setup_complete()
    return {"ok": True}
