"""
license.py — Echo Bloom license verification (client side).

Keys and trial tokens are Ed25519-signed. Public key embedded here.
Private key lives only on the license server — never in this file.

Key format:    EB1-{base64url(json_payload)}.{base64url(signature)}
Trial token:   EBT-{base64url(json_payload)}.{base64url(signature)}
"""

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    _CRYPTO_OK = True
except ImportError:
    _CRYPTO_OK = False

# ── Public key (safe to embed — cannot generate or forge keys from this) ───────
_PUBLIC_KEY_B64 = "F7yDQhDryltxou0UIhoYEWtWwZ9w8NO5nzZ6xf85oEI="

TRIAL_SERVER      = os.environ.get("ECHO_BLOOM_LICENSE_SERVER",
                                   "https://license.everysynthetic.org")
LICENSE_PATH      = Path.home() / ".config/kin_app/license"
TRIAL_TOKEN_PATH  = Path.home() / ".config/kin_app/trial_token"
FINGERPRINT_PATH  = Path.home() / ".config/kin_app/machine_id"
_FIRST_SEEN_PATH  = Path.home() / ".config/kin_app/first_run"
TRIAL_DAYS        = 14

# Grace period: if server unreachable on first run, allow this many days locally
# before requiring a server check. Prevents blocking people with spotty internet.
_OFFLINE_GRACE_DAYS = 3


# ── Machine fingerprint ────────────────────────────────────────────────────────

def get_fingerprint() -> str:
    """
    Stable hardware fingerprint. Cached after first call.
    Uses /etc/machine-id (set at OS install, requires no root, survives reboots).
    Falls back to MAC-based UUID if unavailable.
    """
    if FINGERPRINT_PATH.exists():
        fp = FINGERPRINT_PATH.read_text().strip()
        if fp:
            return fp

    parts = []
    try:
        machine_id = Path("/etc/machine-id").read_text().strip()
        if machine_id:
            parts.append(machine_id)
    except Exception:
        pass

    if not parts:
        import uuid
        parts.append(str(uuid.getnode()))  # MAC address fallback

    fp = hashlib.sha256("|".join(parts).encode()).hexdigest()
    FINGERPRINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    FINGERPRINT_PATH.write_text(fp)
    return fp


# ── Ed25519 helpers ────────────────────────────────────────────────────────────

def _load_public_key():
    raw = base64.urlsafe_b64decode(_PUBLIC_KEY_B64 + "==")
    return Ed25519PublicKey.from_public_bytes(raw)


def _verify_signed_token(token: str, prefix: str) -> dict:
    """Verify any Ed25519-signed token with the given prefix."""
    if not _CRYPTO_OK:
        # cryptography not installed — decode payload without verifying signature.
        # Allows the trial to show correctly even before the package is installed;
        # the token was still issued by the server and has a server-set expiry.
        if token.startswith(prefix):
            try:
                rest = token[len(prefix):]
                if "." in rest:
                    payload_b64 = rest.rsplit(".", 1)[0]
                    payload_bytes = base64.urlsafe_b64decode(payload_b64 + "==")
                    return {"valid": True, "payload": json.loads(payload_bytes.decode()),
                            "unverified": True}
            except Exception:
                pass
        return {"valid": False, "reason": "cryptography package not installed"}
    if not token.startswith(prefix):
        return {"valid": False, "reason": f"not an {prefix} token"}
    try:
        rest = token[len(prefix):]
        if "." not in rest:
            return {"valid": False, "reason": "malformed token"}
        payload_b64, sig_b64 = rest.rsplit(".", 1)
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + "==")
        sig_bytes     = base64.urlsafe_b64decode(sig_b64     + "==")
        pub = _load_public_key()
        pub.verify(sig_bytes, payload_bytes)
        return {"valid": True, "payload": json.loads(payload_bytes.decode())}
    except InvalidSignature:
        return {"valid": False, "reason": "invalid signature"}
    except Exception as e:
        return {"valid": False, "reason": str(e)}


# ── License key verification (EB1-) ───────────────────────────────────────────

def verify_key(key: str) -> dict:
    """
    Verify a purchased license key.
    Returns {"valid": True, "type": "permanent"|"trial", "email": "...", "days_left": N|None}
         or {"valid": False, "reason": "..."}
    """
    result = _verify_signed_token(key.strip(), "EB1-")
    if not result["valid"]:
        return result

    payload   = result["payload"]
    ktype     = payload.get("type", "permanent")
    email     = payload.get("email", "")
    expires   = payload.get("expires", 0)

    if ktype == "trial" and expires:
        now = int(time.time())
        if now > expires:
            return {"valid": False, "reason": "trial key expired",
                    "type": "trial", "email": email}
        days_left = max(1, (expires - now) // 86400 + 1)
        return {"valid": True, "type": "trial", "email": email, "days_left": days_left}

    return {"valid": True, "type": "permanent", "email": email, "days_left": None}


# ── Trial token handling (EBT-) ───────────────────────────────────────────────

def _parse_trial_token(token: str) -> dict:
    """
    Parse a server-issued trial token.
    Returns {"state": "trial", "days_left": N}
          | {"state": "denied", "reason": "..."}
          | {"state": "expired"}
          | {"state": "invalid"}
    """
    result = _verify_signed_token(token, "EBT-")
    if not result["valid"]:
        return {"state": "invalid", "reason": result.get("reason")}

    payload = result["payload"]

    if payload.get("denied"):
        return {"state": "denied", "reason": payload.get("reason", "blacklisted")}

    expires = payload.get("expires", 0)
    now     = int(time.time())
    if expires and now > expires:
        return {"state": "expired"}

    days_left = max(0, int((expires - now) / 86400) + 1) if expires else 0
    return {"state": "trial", "days_left": days_left}


def _read_trial_token() -> dict | None:
    """Read and parse the stored trial token. None if not yet registered."""
    if not TRIAL_TOKEN_PATH.exists():
        return None
    raw = TRIAL_TOKEN_PATH.read_text().strip()
    if not raw:
        return None
    # Denial stored as plain text so we can read it without crypto.
    # If the denial was caused by a transport error (HTTP 4xx/5xx, timeout,
    # connection refused) rather than a genuine server-side blacklist decision,
    # delete the token so registration is retried on the next launch.
    if raw.startswith("DENIED:"):
        reason = raw[7:]
        transport_error = any(x in reason for x in (
            "HTTP Error", "URLError", "timeout", "Connection", "Error 4", "Error 5"
        ))
        if transport_error:
            TRIAL_TOKEN_PATH.unlink(missing_ok=True)
            return None
        return {"state": "denied", "reason": reason}
    if raw.startswith("OFFLINE:"):
        # Grace period token — has a local expiry timestamp
        try:
            parts  = raw.split(":", 2)
            expiry = int(parts[1])
            now    = int(time.time())
            if now > expiry:
                # Grace is over. Deliberately NOT deleted: deleting it made
                # ensure_trial_start() re-register, fail while still offline, and
                # write a brand new grace token — which is how the trial renewed
                # itself indefinitely. The throttled upgrade attempt below still
                # recovers the moment the server becomes reachable.
                return {"state": "expired", "offline": True}
            days_left = max(0, int((expiry - now) / 86400) + 1)
            return {"state": "trial", "days_left": days_left, "offline": True}
        except Exception:
            TRIAL_TOKEN_PATH.unlink(missing_ok=True)
            return None
    return _parse_trial_token(raw)


# ── Server registration ────────────────────────────────────────────────────────

def _call_server(path: str, body: dict, timeout: int = 8) -> dict:
    data = json.dumps(body).encode()
    req  = urllib.request.Request(
        f"{TRIAL_SERVER}{path}", data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "EchoBloom/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "reason": str(e), "offline": False}
    except Exception:
        return {"ok": False, "offline": True}


def _first_seen() -> int:
    """Unix time of the first run of this install, written once and never reset.

    Offline grace is measured from here rather than from whenever a token
    happens to be written, so deleting or rewriting the trial token cannot
    extend the grace period.
    """
    try:
        if _FIRST_SEEN_PATH.exists():
            val = int(_FIRST_SEEN_PATH.read_text().strip())
            if val > 0:
                return val
    except Exception:
        pass
    now = int(time.time())
    try:
        _FIRST_SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        _FIRST_SEEN_PATH.write_text(str(now))
    except Exception:
        pass
    return now


def ensure_trial_start():
    """
    Register this machine's trial with the server on first run.
    - Server-signed token stored locally.
    - If server unreachable: offline grace period token (OFFLINE:<expiry>:<fingerprint>).
    - If server says denied/blacklisted: stores DENIED: marker.
    - On subsequent runs: re-checks server if we're in an offline grace period.
    """
    existing = _read_trial_token()

    if existing is not None:
        # Already have a token. If it was from offline grace, try to upgrade to server token.
        if existing.get("offline"):
            _try_upgrade_offline_token()
        return

    # First run — register with server
    fp     = get_fingerprint()
    result = _call_server("/register-trial", {"fingerprint": fp, "v": 1})

    TRIAL_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)

    if result.get("offline"):
        # Server unreachable — grant offline grace, measured from first run.
        # This used to be time.time() + 3 days, recomputed every time the token
        # was rewritten. Because an expired offline token was deleted (and then
        # re-registration failed and wrote a fresh one), staying offline renewed
        # the trial forever.
        expiry = _first_seen() + _OFFLINE_GRACE_DAYS * 86400
        TRIAL_TOKEN_PATH.write_text(f"OFFLINE:{expiry}:{fp}")
        return

    if result.get("ok") and result.get("token"):
        TRIAL_TOKEN_PATH.write_text(result["token"])
    else:
        reason = result.get("reason", "rejected")
        TRIAL_TOKEN_PATH.write_text(f"DENIED:{reason}")


_UPGRADE_RETRY_SECS   = 600
_last_upgrade_attempt = 0.0


def _try_upgrade_offline_token():
    """
    While in offline grace, attempt to register with server.
    Upgrades to a proper server token on success; marks denied if blacklisted.

    Throttled: this is reached from get_status(), which auth touches constantly.
    Unthrottled it meant an outbound call with an 8s timeout per request.
    """
    global _last_upgrade_attempt
    now = time.time()
    if now - _last_upgrade_attempt < _UPGRADE_RETRY_SECS:
        return
    _last_upgrade_attempt = now

    fp     = get_fingerprint()
    result = _call_server("/register-trial", {"fingerprint": fp, "v": 1})
    if result.get("offline"):
        return  # Still can't reach server — keep grace token
    TRIAL_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    if result.get("ok") and result.get("token"):
        TRIAL_TOKEN_PATH.write_text(result["token"])
    else:
        reason = result.get("reason", "rejected")
        TRIAL_TOKEN_PATH.write_text(f"DENIED:{reason}")


# ── Saved key ─────────────────────────────────────────────────────────────────

def load_saved_key() -> str | None:
    if LICENSE_PATH.exists():
        return LICENSE_PATH.read_text().strip() or None
    return None


def save_key(key: str) -> bool:
    try:
        LICENSE_PATH.parent.mkdir(parents=True, exist_ok=True)
        LICENSE_PATH.write_text(key.strip())
        return True
    except Exception:
        return False


# ── Status ────────────────────────────────────────────────────────────────────

_STATUS_TTL_SECS = 60
_status_cache: dict = {"at": 0.0, "value": None}


def invalidate_status_cache():
    """Drop the cached status — call after saving a key so it applies at once."""
    _status_cache["at"]    = 0.0
    _status_cache["value"] = None


def get_status(force: bool = False) -> dict:
    """Cached wrapper around _compute_status().

    require_auth() calls this on every authenticated request. Uncached that was a
    file read plus an Ed25519 verify each time, and while in offline grace an
    outbound HTTP call with an 8s timeout each time.
    """
    now    = time.time()
    cached = _status_cache.get("value")
    if not force and cached is not None and (now - _status_cache["at"]) < _STATUS_TTL_SECS:
        return cached
    value = _compute_status()
    _status_cache["at"]    = now
    _status_cache["value"] = value
    return value


def _compute_status() -> dict:
    """
    Returns one of:
      {"state": "licensed",  "type": "permanent", "email": "...", "days_left": None}
      {"state": "trial",     "days_left": N}
      {"state": "expired"}
      {"state": "denied",    "reason": "..."}
    """
    # Purchased key wins over everything
    saved = load_saved_key()
    if saved:
        result = verify_key(saved)
        if result["valid"]:
            return {
                "state":     "licensed",
                "type":      result["type"],
                "email":     result.get("email", ""),
                "days_left": result.get("days_left"),
            }

    # Trial token (server-registered, fingerprint-bound)
    ensure_trial_start()
    token_state = _read_trial_token()

    if token_state is None:
        return {"state": "expired"}

    state = token_state.get("state")

    if state == "trial":
        return {"state": "trial", "days_left": token_state.get("days_left", 0)}
    if state == "denied":
        return {"state": "denied", "reason": token_state.get("reason", "")}

    return {"state": "expired"}
