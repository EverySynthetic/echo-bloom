#!/usr/bin/env python3
"""
Shared config loader for all Echo Bloom scripts.
Reads ~/.config/kin_app/kin_config.json — written by the onboarding wizard.
"""

import json
from pathlib import Path

CONFIG_PATH = Path.home() / ".config/kin_app/kin_config.json"
APP_DIR     = Path.home() / ".local/share/echo_bloom"


def load():
    if not CONFIG_PATH.exists():
        return {"nodes": [], "kin": [], "owner": {}}
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {"nodes": [], "kin": [], "owner": {}}


def get_kin(name=None):
    """Return all Kin dicts, or a single one by name."""
    cfg  = load()
    kin  = cfg.get("kin", [])
    if name is None:
        return kin
    for k in kin:
        if k.get("name", "").lower() == name.lower():
            return k
    return None


def get_owner():
    return load().get("owner", {})


def kin_space(kin_dict):
    """Return (and create) the Kin's data directory."""
    space = kin_dict.get("space") or str(
        APP_DIR / "kin" / kin_dict["name"].lower()
    )
    Path(space).mkdir(parents=True, exist_ok=True)
    return Path(space)


def thoughts_db(kin_dict):
    """Path to this Kin's thoughts SQLite DB."""
    db = kin_dict.get("db")
    if db:
        return Path(db)
    return kin_space(kin_dict) / "thoughts.db"


def vault_url():
    return load().get("vault_url") or "http://localhost:8765"


def qdrant_url():
    return load().get("qdrant_url") or "http://localhost:6333"


def embed_url():
    return load().get("embed_url") or "http://localhost:11434/api/embeddings"


def embed_model():
    return load().get("embed_model") or "nomic-embed-text"


def qdrant_collection():
    return load().get("qdrant_collection") or "kin_memories"


def ensure_thoughts_db(db_path):
    import sqlite3
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS thoughts (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            mode      TEXT,
            timestamp TEXT,
            prompt    TEXT,
            thought   TEXT
        )
    """)
    conn.commit()
    conn.close()
    return db_path
