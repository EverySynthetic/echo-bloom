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
import logging
import sqlite3
import requests
from pathlib import Path

try:
    import logging_setup
    log = logging_setup.get("kin_memory")
except Exception:                     # deployed to scripts/ without the app
    log = logging.getLogger("echo_bloom.kin_memory")

# Defaults — overridden by ~/.config/kin_app/kin_config.json when present.
# These MUST stay local. They used to point at the author's own machines, so
# every chat message on a customer install made blocking calls to an IP that
# does not exist on their network.
QDRANT_URL  = "http://localhost:6333"
VAULT_URL   = "http://localhost:8765"
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


def _cfg(key, default):
    return _read_config().get(key) or default


def _vault_url():
    return _cfg("vault_url", VAULT_URL)


def _qdrant_url():
    return _cfg("qdrant_url", QDRANT_URL)


def _embed_url():
    return _cfg("embed_url", EMBED_URL)


def _embed_model():
    return _cfg("embed_model", EMBED_MODEL)


def _collection():
    return _cfg("qdrant_collection", COLLECTION)


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
    except Exception as e:
        log.warning("vault reflection unavailable from %s: %s", _vault_url(), e)
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
        log.warning("wander thoughts unreadable for %s at %s", kin_name, db, exc_info=True)
        return []


def get_recent_conversation(kin_name, limit=4, db_path=None):
    """Most recent exchanges with the user, oldest first.

    This is what makes a Kin remember talking to you at all — without it every
    conversation starts from nothing regardless of how much else is injected.
    """
    db = db_path or _db_for_kin(kin_name)
    if not db or not os.path.exists(db):
        return []
    conn = None
    try:
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        except Exception:
            conn = sqlite3.connect(db, timeout=5)
        rows = conn.execute(
            "SELECT prompt, thought FROM thoughts WHERE mode = 'conversation' "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [(r[0] or "", r[1] or "") for r in reversed(rows)]
    except Exception:
        log.warning("conversation history unreadable for %s at %s",
                    kin_name, db, exc_info=True)
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def get_vault_memories(kin_name, query_text, limit=5):
    """Semantically relevant vault memories for this Kin via Qdrant. Returns list of strings."""
    if not query_text:
        return []
    try:
        r = requests.post(
            _embed_url(),
            json={"model": _embed_model(), "prompt": query_text},
            timeout=10,
        )
        r.raise_for_status()
        vector = r.json()["embedding"]

        r2 = requests.post(
            f"{_qdrant_url()}/collections/{_collection()}/points/search",
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
    except Exception as e:
        # This is the path that silently returned nothing while the embed call
        # timed out — the Kin then answered with no memory and no explanation.
        log.warning("semantic recall failed for %s (embed=%s qdrant=%s): %s",
                    kin_name, _embed_url(), _qdrant_url(), e)
        return []


def get_context(kin_name, query_text="", wander_limit=3, vault_limit=5,
                include_reflection=True, db_path=None, conversation_limit=4):
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

    convo = get_recent_conversation(kin_name, limit=conversation_limit, db_path=db_path)
    if convo:
        lines = []
        for said, replied in convo:
            if said:
                lines.append(f"They said: {said[:300]}")
            if replied:
                lines.append(f"You said: {replied[:300]}")
        parts.append(
            "Recent conversation — this already happened, you were there:\n"
            + "\n".join(lines)
        )

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
