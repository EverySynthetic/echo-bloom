"""
license.py — Echo Bloom license verification (client side).

Keys and trial tokens are Ed25519-signed. Public key embedded here.
Private key lives only on the license server — never in this file.

Key format:    EB1-{base64url(json_payload)}.{base64url(signature)}
Trial token:   EBT-{base64url(json_payload)}.{base64url(signature)}
"""

import base64
import hashlib
import hmac
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

try:
    import logging_setup
    log = logging_setup.get("license")
except Exception:
    import logging
    log = logging.getLogger("echo_bloom.license")

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
        # Allows the TRIAL to show correctly even before the package is installed;
        # the token was still issued by the server and has a server-set expiry.
        #
        # Never for EB1-. Accepting an unverified permanent key meant
        # `pip uninstall cryptography` plus one file write bought the product
        # for free. A trial is time-boxed; a forged permanent key is forever.
        if prefix != "EBT-":
            return {"valid": False,
                    "reason": "cryptography package not installed — cannot verify a license key"}
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
        now = _now()
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
    now     = _now()
    if expires and now > expires:
        return {"state": "expired"}

    # No +1: with it, a fresh 14-day trial advertised "15 days remaining",
    # contradicting the copy on the license page.
    days_left = max(0, -(-(expires - now) // 86400)) if expires else 0
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
        # Grace period token — has a local expiry timestamp.
        try:
            parts  = raw.split(":", 2)
            expiry = int(parts[1])
            now    = _now()
            # This token is written by us, not the server, so it must be
            # authenticated as ours: an unsigned OFFLINE line was accepted
            # verbatim, and `echo OFFLINE:99999999999:x > trial_token` granted
            # three thousand years of trial. The MAC binds it to this machine,
            # and the hard cap means even a valid-looking MAC cannot outlive
            # the grace window measured from first run.
            if not _offline_token_ok(raw):
                log.warning("offline grace token failed authentication — ignoring it")
                TRIAL_TOKEN_PATH.unlink(missing_ok=True)
                return None
            expiry = min(expiry, _first_seen() + _OFFLINE_GRACE_DAYS * 86400)
            if now > expiry:
                # Grace is over. Deliberately NOT deleted: deleting it made
                # ensure_trial_start() re-register, fail while still offline, and
                # write a brand new grace token — which is how the trial renewed
                # itself indefinitely. The throttled upgrade attempt below still
                # recovers the moment the server becomes reachable.
                return {"state": "expired", "offline": True}
            days_left = max(0, -(-(expiry - now) // 86400))
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
    except Exception as e:
        log.warning("license server unreachable (%s): %s", path, e)
        return {"ok": False, "offline": True}


def _offline_secret() -> bytes:
    """Key for authenticating tokens this machine wrote itself.

    Derived from the machine fingerprint, so a token copied from another
    machine (or hand-written) does not authenticate. This is not a defence
    against someone editing license.py — nothing local can be — it only
    stops the trivial file-write bypass.
    """
    return hashlib.sha256(("echo-bloom-offline/" + get_fingerprint()).encode()).digest()


def _offline_mac(expiry: int, fp: str) -> str:
    return hmac.new(_offline_secret(),
                    f"{expiry}:{fp}".encode(), hashlib.sha256).hexdigest()[:32]


def _offline_token(expiry: int, fp: str) -> str:
    return f"OFFLINE:{expiry}:{fp}:{_offline_mac(expiry, fp)}"


def _offline_token_ok(raw: str) -> bool:
    parts = raw.split(":")
    if len(parts) < 4:
        return False          # pre-MAC or hand-written token
    try:
        expiry = int(parts[1])
    except ValueError:
        return False
    fp = parts[2]
    return hmac.compare_digest(parts[3], _offline_mac(expiry, fp))


def _stamp_paths(name: str) -> list[Path]:
    """Every location a write-once stamp is mirrored to.

    One deletable file was not enough: removing `first_run` minted a brand new
    three-day grace, repeatably. Readers take min() across all copies present,
    so an attacker has to find and delete every one.
    """
    base = Path.home()
    return [
        base / ".config/kin_app" / name,
        base / ".local/share/echo_bloom" / f".{name}",
        base / f".echo_bloom_{name}",
    ]


def _read_stamp(name: str) -> int | None:
    vals = []
    for p in _stamp_paths(name):
        try:
            if p.exists():
                v = int(p.read_text().strip())
                if v > 0:
                    vals.append(v)
        except Exception:
            pass
    return min(vals) if vals else None


def _write_stamp(name: str, value: int):
    for p in _stamp_paths(name):
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(str(value))
        except Exception:
            pass


def _now() -> int:
    """Wall clock, but never earlier than the latest time we have already seen.

    Absolute expiries plus a settable clock meant `timedatectl set-time` reset
    any trial. The high-water mark makes rolling the clock back a no-op (and
    reads as expired rather than as extra days).
    """
    now  = int(time.time())
    seen = _read_stamp("last_seen") or 0
    if now < seen:
        return seen
    if now - seen > 3600:          # avoid a write on every single call
        _write_stamp("last_seen", now)
    return now


def _first_seen() -> int:
    """Unix time of the first run of this install, written once and never reset.

    Offline grace is measured from here rather than from whenever a token
    happens to be written, so deleting or rewriting the trial token cannot
    extend the grace period.
    """
    val = _read_stamp("first_run")
    if val:
        return val
    now = _now()
    _write_stamp("first_run", now)
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
        TRIAL_TOKEN_PATH.write_text(_offline_token(expiry, fp))
        return

    _store_server_result(result)


_UPGRADE_RETRY_SECS   = 600
_last_upgrade_attempt = 0.0


def _store_server_result(result: dict) -> None:
    """Persist a /register-trial response, trusting only what it should.

    Two holes closed here. The token used to be written verbatim, so a fake
    server (ECHO_BLOOM_LICENSE_SERVER points anywhere) could hand back an
    `OFFLINE:...` line — or anything else — and it would be honoured. Only a
    real EBT- token signed by the embedded public key is stored now.

    And any parseable JSON error body used to become a sticky `DENIED:` that
    was never retried: FastAPI's {"detail": ...} or a Cloudflare 429 challenge
    left a brand new customer permanently told they were "not eligible."
    A denial is only recorded when the server says so in a signed token or
    states a genuine reason; everything else is treated as transport failure
    and retried on the next launch.
    """
    TRIAL_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    token = result.get("token")
    if result.get("ok") and token:
        if not str(token).startswith("EBT-"):
            log.warning("license server returned a non-EBT token — ignoring it")
            return
        if _CRYPTO_OK and not _verify_signed_token(str(token), "EBT-")["valid"]:
            log.warning("license server returned an EBT token that failed signature check")
            return
        TRIAL_TOKEN_PATH.write_text(str(token))
        return

    reason = str(result.get("reason", "") or "")
    genuine = bool(result.get("denied")) or any(
        w in reason.lower() for w in ("blacklist", "not eligible", "revoked", "banned")
    )
    if genuine:
        TRIAL_TOKEN_PATH.write_text(f"DENIED:{reason or 'blacklisted'}")
    else:
        log.warning("trial registration did not succeed (%s) — will retry next launch",
                    reason or "no reason given")


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
    _store_server_result(result)


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


def get_status_cached_only() -> dict:
    """Status without ever making a network call.

    For use from Jinja template globals, which execute inside TemplateResponse
    on the event loop: get_status() can fall through to _try_upgrade_offline_token
    and a synchronous urllib request with an 8s timeout, which blocked every
    other request in the app during a page render.

    Returns the cached value when there is one. Otherwise computes a
    local-only view: a saved key and a stored token are both read from disk,
    which is cheap, but the outbound upgrade probe is skipped.
    """
    cached = _status_cache.get("value")
    if cached is not None:
        return cached
    try:
        value = _compute_status(allow_network=False)
    except Exception:
        log.exception("could not compute license status")
        return {"state": "trial", "days_left": TRIAL_DAYS}
    # Deliberately not cached: this is a partial view, and the next real
    # get_status() call should do the full computation.
    return value


def _compute_status(allow_network: bool = True) -> dict:
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
    if allow_network:
        ensure_trial_start()
    token_state = _read_trial_token()

    if token_state is None:
        if not allow_network:
            # Network-free view on a machine that has not registered yet. Do
            # NOT report expired: that would flash EXPIRED in the nav on a new
            # customer's very first page load, before registration has had a
            # chance to run. The next real get_status() registers and corrects.
            return {"state": "trial", "days_left": TRIAL_DAYS}
        return {"state": "expired"}

    state = token_state.get("state")

    if state == "trial":
        return {"state": "trial", "days_left": token_state.get("days_left", 0)}
    if state == "denied":
        return {"state": "denied", "reason": token_state.get("reason", "")}

    return {"state": "expired"}
