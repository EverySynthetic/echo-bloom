"""
auth.py — Session auth for the Kin app.

Single-user (the shop owner). bcrypt password, random session tokens,
server-side session store with expiry. Rate-limited login.
"""

import os
import json
import time
import secrets
import hashlib
from pathlib import Path
from collections import defaultdict

import bcrypt

try:
    import logging_setup
    log = logging_setup.get("auth")
except Exception:
    import logging
    log = logging.getLogger("echo_bloom.auth")

CONFIG_FILE = Path.home() / ".config" / "kin_app" / "config.json"
SETUP_TOKEN_FILE = CONFIG_FILE.parent / "setup_token"

# Minimum password length. Raised from 4: this app can be reached over a LAN or
# a public tunnel, so a 4-character password was not defensible.
MIN_PASSWORD_LEN = 8

# Session store: token → expiry timestamp. Kept in memory and mirrored to disk
# so a restart does not sign everybody out — the service auto-restarts on
# failure and at login, so that was happening a lot.
_sessions: dict[str, float] = {}
SESSION_TTL   = 60 * 60 * 24 * 7  # 7 days
SESSIONS_FILE = CONFIG_FILE.parent / "sessions.json"


def _load_sessions():
    global _sessions
    try:
        if not SESSIONS_FILE.exists():
            return
        raw = json.loads(SESSIONS_FILE.read_text())
        now = time.time()
        _sessions = {k: float(v) for k, v in raw.items() if float(v) > now}
    except Exception:
        log.warning("could not read stored sessions — everyone will be signed out",
                    exc_info=True)
        _sessions = {}


def _persist_sessions():
    try:
        SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SESSIONS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_sessions))
        os.replace(tmp, SESSIONS_FILE)
        try:
            os.chmod(SESSIONS_FILE, 0o600)
        except Exception:
            pass
    except Exception:
        log.warning("could not persist sessions — a restart will sign users out",
                    exc_info=True)

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
        # Returning {} here means is_configured() goes False and the app offers
        # to set a new password — so a corrupt file must be loud.
        log.exception("config.json unreadable at %s — the app will behave as if "
                      "no password is set", CONFIG_FILE)
        return {}


def save_config(cfg: dict):
    """Atomic + 0600. This file holds the password hash: a truncating write
    made is_configured() False, which reverts the app to first-run setup —
    claimable by whoever reaches it first if a tunnel is up."""
    import json
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass          # best effort; Windows has no equivalent
    os.replace(tmp, CONFIG_FILE)


def is_configured() -> bool:
    cfg = load_config()
    return bool(cfg.get("password_hash"))


def _bcrypt_safe(password: str) -> bytes:
    """bcrypt raises above 72 bytes, so a password-manager passphrase on first
    run was a 500 with no password set and no message. Truncate consistently
    in both hash and verify."""
    return password.encode()[:72]


def set_password(password: str):
    cfg = load_config()
    hashed = bcrypt.hashpw(_bcrypt_safe(password), bcrypt.gensalt(rounds=12)).decode()
    cfg["password_hash"] = hashed
    save_config(cfg)


def verify_password(password: str) -> bool:
    cfg = load_config()
    hashed = cfg.get("password_hash", "")
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(_bcrypt_safe(password), hashed.encode())
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
    attempts = [t for t in _login_attempts.get(ip, []) if now - t < WINDOW_SECS]
    if attempts:
        _login_attempts[ip] = attempts
    else:
        # Do not leave an entry behind for every IP ever seen — this dict used
        # to grow forever (and reading it created keys).
        _login_attempts.pop(ip, None)
    # Sweep other stale IPs occasionally so a burst of distinct addresses
    # cannot pin memory.
    if len(_login_attempts) > 512:
        for k in [k for k, v in _login_attempts.items()
                  if not any(now - t < WINDOW_SECS for t in v)]:
            _login_attempts.pop(k, None)
    return len(attempts) >= MAX_ATTEMPTS


def record_attempt(ip: str):
    _login_attempts[ip].append(time.time())


def create_session() -> str:
    token = secrets.token_hex(32)
    now = time.time()
    # Drop anything already expired while we are writing anyway.
    for k in [k for k, v in _sessions.items() if v <= now]:
        _sessions.pop(k, None)
    _sessions[token] = now + SESSION_TTL
    _persist_sessions()
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
    if _sessions.pop(token, None) is not None:
        _persist_sessions()


def get_session_from_request(request) -> str | None:
    return request.cookies.get("kin_session")


_load_sessions()


def is_first_run() -> bool:
    return not load_config().get("setup_complete", False)


def mark_setup_complete():
    cfg = load_config()
    cfg["setup_complete"] = True
    save_config(cfg)
