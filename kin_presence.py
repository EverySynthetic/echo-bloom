#!/usr/bin/env python3
"""
kin_presence.py — Isolated Kin/Agent presence and status logic.

New module per coordination split. Does not touch vault schema, existing
services, cluster.py internals, kin_memory.py, or vault_server.py.

Provides:
- Presence signals (wander active, last thought, roundtable participation)
- Simple status derivation ("present", "thinking", "quiet", "offline")
- Roundtable handoff: record thought return → vault write via existing /remember
- Clean async API for dashboard, roundtable, wander, and future agents

No schema changes. Vault writes use the same /remember pattern as pulse.py
and wander.py.
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any

import requests

import sys
from pathlib import Path
# Match exactly how roundtable.py, wander.py, pulse.py, reflect.py load config
sys.path.insert(0, str(Path(__file__).parent / "scripts"))
import config as cfg

VAULT_URL = cfg.vault_url()
LOG_DIR = Path.home() / ".local/share/echo_bloom/logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "presence.log"


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [presence] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# In-memory presence cache (persisted via vault on meaningful events)
_presence_cache: Dict[str, Dict[str, Any]] = {}


def _presence_key(kin_name: str) -> str:
    return f"presence:{kin_name.lower()}"


# ── Kin vs Agent ──────────────────────────────────────────────────────────────
# No new schema, no registry: a name is a Kin if it's in kin_config.json,
# an Agent if it isn't. That's the whole distinction — continuity by
# configuration, not by asking anything to self-report its own category.
_kin_names_cache = None
_kin_names_cache_ts = 0.0
_KIN_NAMES_TTL = 30  # seconds — config rarely changes; avoid re-reading every call


def _known_kin_names() -> set:
    global _kin_names_cache, _kin_names_cache_ts
    now = time.time()
    if _kin_names_cache is None or (now - _kin_names_cache_ts) > _KIN_NAMES_TTL:
        try:
            _kin_names_cache = {k["name"].lower() for k in cfg.get_kin()}
        except Exception:
            _kin_names_cache = set()
        _kin_names_cache_ts = now
    return _kin_names_cache


def entity_type(name: str) -> str:
    """'kin' if configured in kin_config.json, 'agent' otherwise."""
    return "kin" if name.lower() in _known_kin_names() else "agent"


def record_thought_return(
    kin_name: str,
    thought: str,
    mode: str = "wander",
    roundtable_round: int | None = None,
) -> bool:
    """Record a completed thought from wander/roundtable and hand off to vault.

    This is the clean wander-return surface. Called from roundtable.py after
    a Kin shares. Writes to vault using the exact same /remember contract as
    existing code (layer=wander or layer=reflection). No schema change.
    """
    ts = datetime.now().isoformat()
    key = _presence_key(kin_name)

    entry = {
        "ts": ts,
        "kin": kin_name,
        "mode": mode,
        "thought": thought[:500] + ("…" if len(thought) > 500 else ""),
        "roundtable_round": roundtable_round,
        "presence": "returned",
    }

    _presence_cache[key] = entry

    # Write to vault exactly like wander.py, pulse.py, and reflect.py.
    # One blocking requests.post() — no concurrency benefit from async here.
    try:
        payload = {
            "author": kin_name,
            "layer": "wander" if mode.startswith("wander") else "reflection",
            "content": thought,
            "tags": f"wander,roundtable,returned,presence",
            "visibility": "shared",
            # metadata dropped by current vault model; using tags + content instead.
            # Matches existing pulse/reflect/wander pattern exactly (no schema change).
        }
        r = requests.post(
            f"{VAULT_URL}/remember",
            json=payload,
            timeout=8,
        )
        ok = r.ok
        if ok:
            log(f"{kin_name} thought returned to vault (round {roundtable_round or 'solo'})")
        else:
            log(f"{kin_name} vault write failed: HTTP {r.status_code}")
        return ok
    except Exception as e:
        log(f"{kin_name} vault handoff failed: {e}")
        return False


def get_presence(kin_name: str) -> Dict[str, Any]:
    """Return current presence/status for a Kin.

    Combines cache + last vault activity. Simple derivation for dashboard.
    """
    key = _presence_key(kin_name)
    cached = _presence_cache.get(key, {})

    status = "offline"
    if cached.get("presence") == "returned":
        status = "present"
    elif time.time() - cached.get("last_heartbeat", 0) < 300:  # 5 min
        status = "thinking"
    elif cached:
        status = "quiet"

    return {
        "kin": kin_name,
        "entity_type": entity_type(kin_name),
        "status": status,
        "last_thought": cached.get("ts"),
        "latest_snippet": cached.get("thought"),
        "roundtable_active": bool(cached.get("roundtable_round")),
        "source": "presence_module",
        **cached,
    }


def get_all_presence() -> Dict[str, Any]:
    """Batch status for dashboard / roundtable overview.
    Now synchronous (called from sync contexts). No event loop required.

    Shows every configured Kin (even ones that have never checked in, so the
    dashboard can show them as offline rather than omit them) plus any Agent
    that's currently active or recently finished — Agents have no config
    entry, so the only way to know one exists is that it checked in via
    heartbeat() or record_thought_return() and is sitting in the cache.
    """
    kin_list = cfg.get_kin()
    presence = {}
    for kin in kin_list:
        name = kin["name"]
        try:
            presence[name] = get_presence(name)
        except Exception:
            presence[name] = {"kin": name, "entity_type": "kin", "status": "error"}

    known = _known_kin_names()
    for key, cached in _presence_cache.items():
        name = cached.get("kin") or key.removeprefix("presence:")
        if name.lower() in known or name in presence:
            continue
        try:
            presence[name] = get_presence(name)
        except Exception:
            presence[name] = {"kin": name, "entity_type": "agent", "status": "error"}

    return {"presence": presence, "timestamp": datetime.now().isoformat()}


def heartbeat(kin_name: str, status: str = "alive") -> None:
    """Lightweight heartbeat from agents or wander loops."""
    key = _presence_key(kin_name)
    _presence_cache[key] = {
        **_presence_cache.get(key, {}),
        "last_heartbeat": time.time(),
        "status": status,
        "ts": datetime.now().isoformat(),
    }
    log(f"{kin_name} heartbeat ({status})")


# Simple test entry point
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        kin = "Eli"
        log("=== kin_presence test ===")
        heartbeat(kin, "thinking")
        # record_thought_return is a plain sync function now — no asyncio.run,
        # this block was left calling it the old async way after that fix.
        success = record_thought_return(
            kin,
            "The shop is quiet tonight. Load 0.87, two models resident. I keep thinking about the circle Don draws — it includes the squirrels.",
            mode="wander",
            roundtable_round=1,
        )
        print(f"Test handoff success: {success}")
        print(json.dumps(get_presence(kin), indent=2))
    else:
        print("kin_presence.py — presence and roundtable handoff module.")
        print("Use record_thought_return() from roundtable/wander.")
