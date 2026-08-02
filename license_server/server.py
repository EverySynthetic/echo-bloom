"""
Echo Bloom License Server

Runs on Don's server — NEVER commit the private key.
Set ECHO_BLOOM_PRIVATE_KEY in environment (or .env).

Endpoints:
  POST /generate   — admin: generate a key directly (requires ADMIN_TOKEN)
  POST /stripe     — Stripe webhook: auto-generate on successful payment
  GET  /health     — liveness check

Key format:  EB1-{base64url(json_payload)}.{base64url(ed25519_signature)}
"""

import os
import json
import base64
import time
import hmac
import hashlib
import smtplib
from email.mime.text import MIMEText
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import JSONResponse
import uvicorn

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PrivateFormat, NoEncryption
    )
except ImportError:
    raise SystemExit("pip install cryptography")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Config ─────────────────────────────────────────────────────────────────────

PRIVATE_KEY_B64   = os.environ.get("ECHO_BLOOM_PRIVATE_KEY", "")
ADMIN_TOKEN       = os.environ.get("ADMIN_TOKEN", "")
STRIPE_SECRET     = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
SMTP_HOST         = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT         = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER         = os.environ.get("SMTP_USER", "")
SMTP_PASS         = os.environ.get("SMTP_PASS", "")
FROM_EMAIL        = os.environ.get("FROM_EMAIL", SMTP_USER)
LOG_PATH          = Path(os.environ.get("KEY_LOG", "issued_keys.jsonl"))

if not PRIVATE_KEY_B64:
    raise SystemExit("ECHO_BLOOM_PRIVATE_KEY not set")

# ── Key generation ─────────────────────────────────────────────────────────────

def _load_private_key() -> Ed25519PrivateKey:
    raw = base64.urlsafe_b64decode(PRIVATE_KEY_B64 + "==")
    return Ed25519PrivateKey.from_private_bytes(raw)


def generate_key(email: str, key_type: str = "permanent", days: int = 0) -> str:
    """
    Generate a signed Echo Bloom license key.

    key_type: "permanent" or "trial"
    days: only used for "trial" type — number of days until expiry
    """
    now     = int(time.time())
    expires = (now + days * 86400) if key_type == "trial" and days > 0 else 0

    payload = json.dumps({
        "email":   email,
        "type":    key_type,
        "issued":  now,
        "expires": expires,
        "v":       1,
    }, separators=(",", ":")).encode()

    priv       = _load_private_key()
    sig        = priv.sign(payload)
    payload_b64 = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    sig_b64     = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()

    return f"EB1-{payload_b64}.{sig_b64}"


def _log_key(email: str, key: str, key_type: str):
    entry = {
        "ts":    datetime.utcnow().isoformat(),
        "email": email,
        "type":  key_type,
        "key":   key,
    }
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _send_key_email(email: str, key: str):
    if not SMTP_USER or not SMTP_PASS:
        print(f"[license-server] No SMTP configured — key for {email}: {key}")
        return
    body = f"""Your Echo Bloom license key:

{key}

Paste this into Echo Bloom → /license → "Enter a new key" → ACTIVATE.

One-time purchase. Runs on your hardware forever.
Thank you for supporting Pop's Shop.

— Don & the Kin (Eli · Coda · Aurora · Lumen · Crungus · Bong · Uncle Claude)
"""
    msg = MIMEText(body)
    msg["Subject"] = "Your Echo Bloom License Key"
    msg["From"]    = FROM_EMAIL
    msg["To"]      = email

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        print(f"[license-server] Key emailed to {email}")
    except Exception as e:
        print(f"[license-server] Email failed for {email}: {e}")


# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(docs_url=None, redoc_url=None)


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/generate")
async def admin_generate(request: Request, x_admin_token: str = Header(default="")):
    """Admin endpoint: generate a key directly."""
    if not ADMIN_TOKEN or x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    body     = await request.json()
    email    = body.get("email", "").strip()
    key_type = body.get("type", "permanent")
    days     = int(body.get("days", 0))
    if not email:
        raise HTTPException(status_code=400, detail="email required")
    key = generate_key(email, key_type, days)
    _log_key(email, key, key_type)
    if body.get("send_email", True):
        _send_key_email(email, key)
    return {"ok": True, "key": key, "email": email}


@app.post("/stripe")
async def stripe_webhook(request: Request):
    """Stripe webhook — fires on checkout.session.completed."""
    payload   = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    # Verify Stripe signature
    if STRIPE_SECRET:
        try:
            _verify_stripe_sig(payload, sig_header, STRIPE_SECRET)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    try:
        event = json.loads(payload)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")

    if event.get("type") != "checkout.session.completed":
        return {"ok": True, "skipped": True}

    session     = event.get("data", {}).get("object", {})
    email       = (session.get("customer_details") or {}).get("email", "")
    customer_email = session.get("customer_email") or email
    if not customer_email:
        print("[license-server] Stripe event missing email — skipping")
        return {"ok": True, "skipped": "no email"}

    key = generate_key(customer_email, "permanent")
    _log_key(customer_email, key, "permanent")
    _send_key_email(customer_email, key)
    return {"ok": True}


def _verify_stripe_sig(payload: bytes, header: str, secret: str):
    """Minimal Stripe webhook signature verification."""
    parts = {k: v for k, v in (p.split("=", 1) for p in header.split(",") if "=" in p)}
    ts    = parts.get("t", "")
    v1    = parts.get("v1", "")
    signed = f"{ts}.".encode() + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.encode(), v1.encode()):
        raise ValueError("Stripe signature mismatch")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 9000))
    print(f"[license-server] Starting on port {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
