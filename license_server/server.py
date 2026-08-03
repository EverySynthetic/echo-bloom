"""
Echo Bloom License Server

Runs on Don's VPS. NEVER commit the private key.
Set ECHO_BLOOM_PRIVATE_KEY in environment (or .env file).

Endpoints:
  GET  /health                     — liveness check
  POST /register-trial             — client: register machine fingerprint for trial
  POST /stripe                     — Stripe webhook (auto-fires on payment)
  POST /admin/generate             — admin: issue a key manually
  POST /admin/blacklist            — admin: ban a fingerprint
  DELETE /admin/blacklist/{fp}     — admin: unban a fingerprint
  GET  /admin/blacklist            — admin: list all banned fingerprints
  GET  /admin/trials               — admin: list all trial registrations
  GET  /admin/keys                 — admin: list all issued keys
"""

import os
import json
import base64
import time
import hmac
import hashlib
import smtplib
import sqlite3
from email.mime.text import MIMEText
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager

from fastapi import FastAPI, Request, HTTPException, Header
import uvicorn

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError:
    raise SystemExit("pip install cryptography")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Config ─────────────────────────────────────────────────────────────────────

PRIVATE_KEY_B64  = os.environ.get("ECHO_BLOOM_PRIVATE_KEY", "")
ADMIN_TOKEN      = os.environ.get("ADMIN_TOKEN", "")
STRIPE_SECRET    = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
SMTP_HOST        = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT        = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER        = os.environ.get("SMTP_USER", "")
SMTP_PASS        = os.environ.get("SMTP_PASS", "")
FROM_EMAIL       = os.environ.get("FROM_EMAIL", SMTP_USER)
DB_PATH          = Path(os.environ.get("DB_PATH", "echo_bloom_licenses.db"))
TRIAL_DAYS       = int(os.environ.get("TRIAL_DAYS", "14"))

if not PRIVATE_KEY_B64:
    raise SystemExit("ECHO_BLOOM_PRIVATE_KEY not set")


# ── Database ───────────────────────────────────────────────────────────────────

def _init_db():
    with _db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trials (
                fingerprint  TEXT PRIMARY KEY,
                registered   INTEGER NOT NULL,
                expires      INTEGER NOT NULL,
                token        TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS blacklist (
                fingerprint  TEXT PRIMARY KEY,
                reason       TEXT,
                blacklisted  INTEGER NOT NULL,
                by           TEXT
            );
            CREATE TABLE IF NOT EXISTS issued_keys (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                email        TEXT NOT NULL,
                key_type     TEXT NOT NULL,
                issued       INTEGER NOT NULL,
                key          TEXT NOT NULL,
                source       TEXT
            );
        """)


@contextmanager
def _db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── Ed25519 signing ────────────────────────────────────────────────────────────

def _load_private_key() -> Ed25519PrivateKey:
    raw = base64.urlsafe_b64decode(PRIVATE_KEY_B64 + "==")
    return Ed25519PrivateKey.from_private_bytes(raw)


def _sign(payload: dict, prefix: str) -> str:
    """Sign a payload dict and return a prefixed token string."""
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode()
    priv          = _load_private_key()
    sig           = priv.sign(payload_bytes)
    p_b64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
    s_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    return f"{prefix}{p_b64}.{s_b64}"


def _sign_trial(fingerprint: str, expires: int) -> str:
    return _sign({
        "fingerprint": fingerprint,
        "expires":     expires,
        "issued":      int(time.time()),
        "v":           1,
    }, "EBT-")


def _sign_denial(fingerprint: str, reason: str) -> str:
    return _sign({
        "fingerprint": fingerprint,
        "denied":      True,
        "reason":      reason,
        "issued":      int(time.time()),
        "v":           1,
    }, "EBT-")


def generate_license_key(email: str, key_type: str = "permanent", days: int = 0) -> str:
    now     = int(time.time())
    expires = (now + days * 86400) if key_type == "trial" and days > 0 else 0
    return _sign({
        "email":   email,
        "type":    key_type,
        "issued":  now,
        "expires": expires,
        "v":       1,
    }, "EB1-")


# ── Email ──────────────────────────────────────────────────────────────────────

def _send_key_email(email: str, key: str):
    if not SMTP_USER or not SMTP_PASS:
        print(f"[license-server] No SMTP configured — key for {email}: {key}")
        return
    plain = f"""Your Echo Bloom license key:

{key}

To activate, open this link and paste your key:
https://everysynthetic.org/license

Or manually:
  1. Open Echo Bloom in your browser
  2. Go to the License page (the "EXPIRED" badge in the top nav, or /license)
  3. Paste the key above and click ACTIVATE

One-time purchase. Runs on your hardware forever.
No subscription. No cloud dependency. Your Kin, your machine.

Thank you for supporting Pop's Shop.

— Don & the Kin (Eli · Coda · Aurora · Lumen · Crungus · Bong · Uncle Claude)
"""
    html = f"""<html><body style="font-family:monospace;background:#111;color:#eee;padding:2em;">
<h2 style="color:#7ecfff;">Your Echo Bloom License Key</h2>
<p style="background:#1a1a1a;padding:1em;border-left:3px solid #7ecfff;word-break:break-all;font-size:0.9em;">{key}</p>
<p><a href="https://everysynthetic.org/license"
   style="display:inline-block;background:#7ecfff;color:#111;padding:0.6em 1.4em;text-decoration:none;font-weight:bold;border-radius:4px;">
   ACTIVATE ECHO BLOOM →
</a></p>
<p style="color:#aaa;font-size:0.85em;">
One-time purchase. Runs on your hardware forever.<br>
No subscription. No cloud dependency. Your Kin, your machine.
</p>
<p style="color:#666;font-size:0.8em;">
— Don &amp; the Kin (Eli · Coda · Aurora · Lumen · Crungus · Bong · Uncle Claude)
</p>
</body></html>"""
    from email.mime.multipart import MIMEMultipart
    msg_root = MIMEMultipart("alternative")
    msg_root.attach(MIMEText(plain, "plain"))
    msg_root.attach(MIMEText(html, "html"))
    msg = msg_root
    msg["Subject"] = "Your Echo Bloom License Key"
    msg["From"]    = FROM_EMAIL
    msg["To"]      = email
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.ehlo()
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        print(f"[license-server] Key emailed to {email}")
    except Exception as e:
        print(f"[license-server] Email FAILED for {email}: {e}")


# ── Stripe ─────────────────────────────────────────────────────────────────────

def _verify_stripe_sig(payload: bytes, header: str, secret: str):
    parts    = {k: v for k, v in (p.split("=", 1) for p in header.split(",") if "=" in p)}
    ts       = parts.get("t", "")
    v1       = parts.get("v1", "")
    signed   = f"{ts}.".encode() + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.encode(), v1.encode()):
        raise ValueError("Stripe signature mismatch")


# ── Auth helper ────────────────────────────────────────────────────────────────

def _require_admin(token: str):
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")


# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(docs_url=None, redoc_url=None)


@app.on_event("startup")
async def startup():
    _init_db()
    print(f"[license-server] DB: {DB_PATH.resolve()}")


@app.get("/health")
async def health():
    with _db() as conn:
        trials    = conn.execute("SELECT COUNT(*) FROM trials").fetchone()[0]
        blacklist = conn.execute("SELECT COUNT(*) FROM blacklist").fetchone()[0]
        keys      = conn.execute("SELECT COUNT(*) FROM issued_keys").fetchone()[0]
    return {"ok": True, "trials": trials, "blacklisted": blacklist, "keys_issued": keys}


# ── Trial registration ─────────────────────────────────────────────────────────

@app.post("/register-trial")
async def register_trial(request: Request):
    """
    Called by Echo Bloom on first run.
    Checks blacklist → checks if already registered → issues signed trial token.
    Idempotent: same fingerprint always returns the same token (or denial).
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON body required with fingerprint field")
    fp   = (body.get("fingerprint") or "").strip()
    if not fp or len(fp) > 128:
        raise HTTPException(status_code=400, detail="fingerprint required")

    now = int(time.time())

    with _db() as conn:
        # Check blacklist first
        bl = conn.execute(
            "SELECT reason FROM blacklist WHERE fingerprint = ?", (fp,)
        ).fetchone()
        if bl:
            reason = bl["reason"] or "blacklisted"
            token  = _sign_denial(fp, reason)
            return {"ok": False, "reason": reason, "token": token}

        # Already registered — return existing token
        existing = conn.execute(
            "SELECT token, expires FROM trials WHERE fingerprint = ?", (fp,)
        ).fetchone()
        if existing:
            # If the trial has expired server-side, return expired denial
            if existing["expires"] < now:
                token = _sign_denial(fp, "trial expired")
                return {"ok": False, "reason": "trial expired", "token": token}
            return {"ok": True, "token": existing["token"]}

        # New registration
        expires = now + TRIAL_DAYS * 86400
        token   = _sign_trial(fp, expires)
        conn.execute(
            "INSERT INTO trials (fingerprint, registered, expires, token) VALUES (?,?,?,?)",
            (fp, now, expires, token),
        )

    print(f"[license-server] Trial registered: {fp[:16]}… expires {datetime.utcfromtimestamp(expires).date()}")
    return {"ok": True, "token": token}


# ── Stripe webhook ─────────────────────────────────────────────────────────────

@app.post("/stripe")
async def stripe_webhook(request: Request):
    """Fires on checkout.session.completed — fully automated key delivery."""
    payload    = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    if STRIPE_SECRET:
        try:
            _verify_stripe_sig(payload, sig_header, STRIPE_SECRET)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    try:
        event = json.loads(payload)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")

    etype = event.get("type", "")
    if etype not in ("checkout.session.completed", "payment_intent.succeeded"):
        return {"ok": True, "skipped": etype}

    session = event.get("data", {}).get("object", {})
    email   = (
        (session.get("customer_details") or {}).get("email")
        or session.get("customer_email")
        or (session.get("billing_details") or {}).get("email")
        or ""
    ).strip()

    if not email:
        print(f"[license-server] Stripe event {etype} missing email")
        return {"ok": True, "skipped": "no email"}

    key = generate_license_key(email, "permanent")
    with _db() as conn:
        conn.execute(
            "INSERT INTO issued_keys (email, key_type, issued, key, source) VALUES (?,?,?,?,?)",
            (email, "permanent", int(time.time()), key, "stripe"),
        )
    _send_key_email(email, key)
    print(f"[license-server] Key issued via Stripe for {email}")
    return {"ok": True}


# ── Admin: generate key ────────────────────────────────────────────────────────

@app.post("/admin/generate")
async def admin_generate(request: Request, x_admin_token: str = Header(default="")):
    _require_admin(x_admin_token)
    body     = await request.json()
    email    = body.get("email", "").strip()
    key_type = body.get("type", "permanent")
    days     = int(body.get("days", 0))
    if not email:
        raise HTTPException(status_code=400, detail="email required")

    key = generate_license_key(email, key_type, days)
    with _db() as conn:
        conn.execute(
            "INSERT INTO issued_keys (email, key_type, issued, key, source) VALUES (?,?,?,?,?)",
            (email, key_type, int(time.time()), key, "admin"),
        )
    if body.get("send_email", True):
        _send_key_email(email, key)
    return {"ok": True, "key": key, "email": email}


# ── Admin: blacklist ───────────────────────────────────────────────────────────

@app.post("/admin/blacklist")
async def admin_blacklist_add(request: Request, x_admin_token: str = Header(default="")):
    _require_admin(x_admin_token)
    body   = await request.json()
    fp     = (body.get("fingerprint") or "").strip()
    reason = body.get("reason", "").strip() or "manual blacklist"
    by     = body.get("by", "admin").strip()
    if not fp:
        raise HTTPException(status_code=400, detail="fingerprint required")

    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO blacklist (fingerprint, reason, blacklisted, by) VALUES (?,?,?,?)",
            (fp, reason, int(time.time()), by),
        )
    print(f"[license-server] Blacklisted: {fp[:16]}… reason={reason}")
    return {"ok": True, "fingerprint": fp}


@app.delete("/admin/blacklist/{fp}")
async def admin_blacklist_remove(fp: str, x_admin_token: str = Header(default="")):
    _require_admin(x_admin_token)
    with _db() as conn:
        conn.execute("DELETE FROM blacklist WHERE fingerprint = ?", (fp,))
    print(f"[license-server] Un-blacklisted: {fp[:16]}…")
    return {"ok": True}


@app.get("/admin/blacklist")
async def admin_blacklist_list(x_admin_token: str = Header(default="")):
    _require_admin(x_admin_token)
    with _db() as conn:
        rows = conn.execute(
            "SELECT fingerprint, reason, blacklisted, by FROM blacklist ORDER BY blacklisted DESC"
        ).fetchall()
    return {"blacklist": [dict(r) for r in rows]}


# ── Admin: trials & keys ───────────────────────────────────────────────────────

@app.get("/admin/trials")
async def admin_trials(x_admin_token: str = Header(default=""), limit: int = 100):
    _require_admin(x_admin_token)
    with _db() as conn:
        rows = conn.execute(
            "SELECT fingerprint, registered, expires FROM trials ORDER BY registered DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return {"trials": [dict(r) for r in rows]}


@app.get("/admin/keys")
async def admin_keys(x_admin_token: str = Header(default=""), limit: int = 100):
    _require_admin(x_admin_token)
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, email, key_type, issued, source FROM issued_keys ORDER BY issued DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return {"keys": [dict(r) for r in rows]}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 9000))
    print(f"[license-server] Starting on :{port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
