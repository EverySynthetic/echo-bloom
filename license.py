"""
license.py — Echo Bloom license verification (client side).

Keys are Ed25519-signed payloads. The public key is embedded here.
The private key lives only on the license server — never in this file.

Key format:  EB1-{base64url(json_payload)}.{base64url(signature)}
"""

import base64
import json
import time
from pathlib import Path

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from cryptography.exceptions import InvalidSignature
    _CRYPTO_OK = True
except ImportError:
    _CRYPTO_OK = False

# ── Public key (safe to embed — cannot generate keys from this) ────────────────
_PUBLIC_KEY_B64 = "F7yDQhDryltxou0UIhoYEWtWwZ9w8NO5nzZ6xf85oEI="

LICENSE_PATH    = Path.home() / ".config/kin_app/license"
TRIAL_PATH      = Path.home() / ".config/kin_app/trial_start"
TRIAL_DAYS      = 14


# ── Trial tracking ─────────────────────────────────────────────────────────────

def ensure_trial_start():
    """Record first-run timestamp if not already set."""
    if not TRIAL_PATH.exists():
        TRIAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        TRIAL_PATH.write_text(str(int(time.time())))


def trial_days_remaining() -> int:
    """How many full days are left in the trial. 0 = expired."""
    if not TRIAL_PATH.exists():
        return TRIAL_DAYS
    try:
        start    = int(TRIAL_PATH.read_text().strip())
        elapsed  = time.time() - start
        remaining = TRIAL_DAYS - int(elapsed / 86400)
        return max(0, remaining)
    except Exception:
        return 0


# ── Key verification ──────────────────────────────────────────────────────────

def _load_public_key():
    raw = base64.urlsafe_b64decode(_PUBLIC_KEY_B64 + "==")
    return Ed25519PublicKey.from_public_bytes(raw)


def verify_key(key: str) -> dict:
    """
    Verify a license key. Returns:
      {"valid": True,  "type": "permanent", "email": "...", "days_left": None}
      {"valid": True,  "type": "trial",     "email": "...", "days_left": N}
      {"valid": False, "reason": "..."}
    """
    if not _CRYPTO_OK:
        return {"valid": False, "reason": "cryptography package not installed"}

    key = key.strip()
    if not key.startswith("EB1-"):
        return {"valid": False, "reason": "not an Echo Bloom key"}

    try:
        rest = key[4:]  # strip "EB1-"
        if "." not in rest:
            return {"valid": False, "reason": "malformed key"}
        payload_b64, sig_b64 = rest.rsplit(".", 1)

        payload_bytes = base64.urlsafe_b64decode(payload_b64 + "==")
        sig_bytes     = base64.urlsafe_b64decode(sig_b64     + "==")

        pub = _load_public_key()
        pub.verify(sig_bytes, payload_bytes)   # raises InvalidSignature if bad

        payload = json.loads(payload_bytes.decode())
        ktype   = payload.get("type", "permanent")
        email   = payload.get("email", "")
        expires = payload.get("expires", 0)

        if ktype == "trial" and expires:
            now = int(time.time())
            if now > expires:
                return {"valid": False, "reason": "trial key expired",
                        "type": "trial", "email": email}
            days_left = max(1, (expires - now) // 86400 + 1)
            return {"valid": True, "type": "trial", "email": email,
                    "days_left": days_left}

        return {"valid": True, "type": "permanent", "email": email,
                "days_left": None}

    except InvalidSignature:
        return {"valid": False, "reason": "invalid signature"}
    except Exception as e:
        return {"valid": False, "reason": str(e)}


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

def get_status() -> dict:
    """
    Returns one of:
      {"state": "licensed",  "type": "permanent", "email": "..."}
      {"state": "trial",     "days_left": N}
      {"state": "expired"}
    """
    # Check saved key first
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

    # Fall back to local trial
    ensure_trial_start()
    days = trial_days_remaining()
    if days > 0:
        return {"state": "trial", "days_left": days}

    return {"state": "expired"}
