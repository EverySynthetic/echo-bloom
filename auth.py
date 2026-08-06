"""
auth.py — Session auth for the Kin app.

Single-user (the shop owner). bcrypt password, random session tokens,
server-side session store with expiry. Rate-limited login.
"""

import os
import time
import secrets
import hashlib
from pathlib import Path
from collections import defaultdict

import bcrypt

CONFIG_FILE = Path.home() / ".config" / "kin_app" / "config.json"
SETUP_TOKEN_FILE = CONFIG_FILE.parent / "setup_token"

# Minimum password length. Raised from 4: this app can be reached over a LAN or
# a public tunnel, so a 4-character password was not defensible.
MIN_PASSWORD_LEN = 8

# In-memory session store: token → expiry timestamp
_sessions: dict[str, float] = {}
SESSION_TTL = 60 * 60 * 24 * 7  # 7 days

# Rate limiting: ip → list of attempt timestamps
_login_attempts: dict[str, list[float]] = defaultdict(list)
MAX_ATTEMPTS  = 5
WINDOW_SECS   = 300  # 5 minutes


def load_config() -> dict:
    import json
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


def save_config(cfg: dict):
    import json
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def is_configured() -> bool:
    cfg = load_config()
    return bool(cfg.get("password_hash"))


def set_password(password: str):
    cfg = load_config()
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
    cfg["password_hash"] = hashed
    save_config(cfg)


def verify_password(password: str) -> bool:
    cfg = load_config()
    hashed = cfg.get("password_hash", "")
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception:
        return False


# ── First-run setup token ─────────────────────────────────────────────────────
# Setting the password is the act that claims this install. From the machine
# itself that needs no proof. From anywhere else it needs this code, which only
# someone with access to the host can read.

def ensure_setup_token() -> str | None:
    """Create the setup token once, if no password is set yet."""
    if is_configured():
        return None
    existing = get_setup_token()
    if existing:
        return existing
    try:
        tok = secrets.token_hex(6).upper()
        SETUP_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETUP_TOKEN_FILE.write_text(tok)
        try:
            os.chmod(SETUP_TOKEN_FILE, 0o600)
        except Exception:
            pass          # best effort; Windows has no chmod equivalent here
        return tok
    except Exception:
        return None


def get_setup_token() -> str | None:
    try:
        if SETUP_TOKEN_FILE.exists():
            return SETUP_TOKEN_FILE.read_text().strip() or None
    except Exception:
        pass
    return None


def verify_setup_token(supplied: str) -> bool:
    tok = get_setup_token()
    if not tok or not supplied:
        return False
    return secrets.compare_digest(tok.strip().upper(), supplied.strip().upper())


def clear_setup_token():
    """Once a password exists the token is meaningless — remove it."""
    try:
        SETUP_TOKEN_FILE.unlink()
    except Exception:
        pass


def is_rate_limited(ip: str) -> bool:
    now = time.time()
    attempts = _login_attempts[ip]
    # Drop old attempts outside the window
    _login_attempts[ip] = [t for t in attempts if now - t < WINDOW_SECS]
    return len(_login_attempts[ip]) >= MAX_ATTEMPTS


def record_attempt(ip: str):
    _login_attempts[ip].append(time.time())


def create_session() -> str:
    token = secrets.token_hex(32)
    _sessions[token] = time.time() + SESSION_TTL
    return token


def validate_session(token: str) -> bool:
    if not token:
        return False
    expiry = _sessions.get(token)
    if expiry is None:
        return False
    if time.time() > expiry:
        _sessions.pop(token, None)
        return False
    return True


def revoke_session(token: str):
    _sessions.pop(token, None)


def get_session_from_request(request) -> str | None:
    return request.cookies.get("kin_session")


def is_first_run() -> bool:
    return not load_config().get("setup_complete", False)


def mark_setup_complete():
    cfg = load_config()
    cfg["setup_complete"] = True
    save_config(cfg)
