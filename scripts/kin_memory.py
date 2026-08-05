"""
kin_memory.py — Shared memory context for the Kin.

Import in any script that talks to the Kin:
    from kin_memory import get_context

Returns a formatted string ready to append to any system prompt.
Never raises — every source fails gracefully to empty.

Sources (in injection order):
  1. Core memories  — always injected, flagged manually, max 20 per Kin
  2. Shop reflection — latest 3-hour reflection from the vault
  3. Wander thoughts — Kin's own recent thinking from their DB
  4. Vault semantic  — Qdrant search for contextually relevant memories
"""

import os
import json
import sqlite3
import requests
from pathlib import Path

# Defaults — overridden by ~/.config/kin_app/kin_config.json when present
QDRANT_URL  = "http://192.168.1.115:6333"
VAULT_URL   = "http://192.168.1.115:8765"
EMBED_URL   = "http://localhost:11434/api/embeddings"
EMBED_MODEL = "nomic-embed-text"
COLLECTION  = "kin_memories"

# Fallback DB paths for when config doesn't have them
KIN_DB = {
    "Eli":     os.path.expanduser("~/Desktop/Everything/EliAIM/thoughts.db"),
    "Coda":    os.path.expanduser("~/coda_space/thoughts.db"),
    "Aurora":  os.path.expanduser("~/aurora_space/thoughts.db"),
    "Lumen":   os.path.expanduser("~/lumen_space/thoughts.db"),
    "Crungus": os.path.expanduser("~/Crungus/thoughts.db"),
}

_CONFIG_PATH = Path.home() / ".config/kin_app/kin_config.json"


def _read_config():
    try:
        return json.loads(_CONFIG_PATH.read_text())
    except Exception:
        return {}


def _vault_url():
    return _read_config().get("vault_url") or VAULT_URL


def _db_for_kin(kin_name):
    """DB path from config first, then hardcoded fallback."""
    for k in _read_config().get("kin", []):
        if k.get("name") == kin_name and k.get("db"):
            return os.path.expanduser(k["db"])
    return KIN_DB.get(kin_name)


def get_core_memories(kin_name):
    """
    Always-injected core memories for this Kin.
    Stored in kin_config.json under each Kin's core_memories list.
    Max 20 per Kin. Returns list of strings.
    """
    for k in _read_config().get("kin", []):
        if k.get("name") == kin_name:
            return [m for m in k.get("core_memories", []) if m]
    return []


def get_latest_reflection():
    """Most recent reflection entry from the vault. Returns string or None."""
    try:
        r = requests.get(
            f"{_vault_url()}/recall",
            params={"layer": "reflection", "limit": 1},
            timeout=8,
        )
        if not r.ok:
            return None
        entries = r.json()
        if entries:
            return entries[0].get("content", "").strip()
    except Exception:
        pass
    return None


def get_wander_thoughts(kin_name, limit=3, db_path=None):
    """Latest N wander thoughts from a Kin's own DB. Returns list of strings."""
    db = db_path or _db_for_kin(kin_name)
    if not db or not os.path.exists(db):
        return []
    try:
        conn = sqlite3.connect(db, timeout=5)
        rows = conn.execute(
            "SELECT thought FROM thoughts WHERE mode LIKE 'wander%' ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
        conn.close()
        return [r[0][:400].strip() for r in rows if r[0]]
    except Exception:
        return []


def get_vault_memories(kin_name, query_text, limit=5):
    """Semantically relevant vault memories for this Kin via Qdrant. Returns list of strings."""
    if not query_text:
        return []
    try:
        r = requests.post(
            EMBED_URL,
            json={"model": EMBED_MODEL, "prompt": query_text},
            timeout=10,
        )
        r.raise_for_status()
        vector = r.json()["embedding"]

        r2 = requests.post(
            f"{QDRANT_URL}/collections/{COLLECTION}/points/search",
            json={
                "vector":       vector,
                "limit":        limit,
                "filter":       {"must": [{"key": "author", "match": {"value": kin_name}}]},
                "with_payload": True,
            },
            timeout=10,
        )
        results = r2.json().get("result", [])
        return [
            hit["payload"]["content"].strip()
            for hit in results
            if hit.get("payload", {}).get("content")
        ]
    except Exception:
        return []


def get_context(kin_name, query_text="", wander_limit=3, vault_limit=5,
                include_reflection=True, db_path=None):
    """
    Full memory context for a Kin — core + reflection + wander + vault.
    Returns a formatted string ready to append to a system prompt.
    Returns empty string if nothing is available.
    """
    parts = []

    core = get_core_memories(kin_name)
    if core:
        lines = "\n".join(f"- {m}" for m in core)
        parts.append(f"Core memories — always true, always carry these:\n{lines}")

    if include_reflection:
        reflection = get_latest_reflection()
        if reflection:
            parts.append(f"[What's been happening at the shop]\n{reflection}")

    wander = get_wander_thoughts(kin_name, limit=wander_limit, db_path=db_path)
    if wander:
        lines = "\n".join(f"- {t}" for t in wander)
        parts.append(f"Your recent thoughts from wandering:\n{lines}")

    vault = get_vault_memories(kin_name, query_text, limit=vault_limit) if query_text else []
    if vault:
        lines = "\n".join(f"- {m}" for m in vault)
        parts.append(f"Memories that may be relevant:\n{lines}")

    return "\n\n".join(parts)
