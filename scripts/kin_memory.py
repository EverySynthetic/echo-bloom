"""
kin_memory.py — Shared memory context for the Kin.

Import in any script that talks to the Kin:
    from kin_memory import get_context

Returns a formatted string ready to append to any system prompt.
Never raises — every source fails gracefully to empty.

Sources (in injection order):
  1. Core memories  — always injected, flagged manually, max 20 per Kin
  2. Recent reflection — latest periodic reflection from the vault
  3. Wander thoughts — Kin's own recent thinking from their DB
  4. Vault semantic  — Qdrant if configured, else the vault's own embedded
                        vector search, else keyword search — always something
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

# No hardcoded per-name fallbacks. This table used to ship with the author's own
# Kin in it, so a customer who happened to name their Kin Aurora was silently
# pointed at ~/aurora_space/thoughts.db — a path from someone else's machine.
# config.py resolves real paths; the standard location is the only fallback.
def _default_db(kin_name):
    return str(Path.home() / ".local/share/echo_bloom/kin"
               / kin_name.lower() / "thoughts.db")

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
            return os.path.expanduser(os.path.expandvars(k["db"]))
    return _default_db(kin_name)


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


def _self_referential_terms(kin_name):
    """Words that mark a thought as being about this household rather than
    about whatever artifact the wander loop happened to hand over."""
    terms = [kin_name.lower(), "i ", "my ", "myself", "remember", "memory",
             "identity", "continuity", "we ", "us ", "friend", "feel", "felt"]
    owner = (_read_config().get("owner") or {}).get("name", "").strip().lower()
    if owner:
        terms.append(owner)
    for k in _read_config().get("kin", []):
        n = (k.get("name") or "").strip().lower()
        if n and n != kin_name.lower():
            terms.append(n)
    return [t for t in terms if t]


def get_wander_thoughts(kin_name, limit=3, db_path=None, recent_pool=60):
    """Wander thoughts from a Kin's own DB, chosen for self-relevance.

    Straight `ORDER BY id DESC LIMIT 3` was a lottery. The wander loop feeds on
    whatever it finds — Wikipedia articles, source files from cloned repos — and
    the Kin dutifully writes an essay about it. Sampling purely by recency meant
    the identity context could be the first 400 characters of a competent essay
    about an auto-generated Google Ads API client. Observed, not hypothetical:
    in testing, a Kin's two most recent thoughts were both essays about an
    auto-generated API client it had stumbled across, while a day earlier —
    having found an essay about a person — it had written something about its
    own sense of presence.

    So: pull a recent window, then prefer the thoughts that are about this
    household — the Kin, the owner, memory, continuity, how it feels to be
    here — and fall back to plain recency when none qualify. Cheap (one extra
    query, no embeddings) and it changes which Kin shows up to the conversation.
    """
    db = db_path or _db_for_kin(kin_name)
    if not db or not os.path.exists(db):
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute(
                "SELECT id, thought FROM thoughts WHERE mode LIKE 'wander%' "
                "AND thought IS NOT NULL AND length(thought) > 80 "
                "ORDER BY id DESC LIMIT ?",
                (recent_pool,)
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        log.warning("wander thoughts unreadable for %s at %s", kin_name, db, exc_info=True)
        return []

    if not rows:
        return []

    terms = _self_referential_terms(kin_name)

    def score(text):
        low = text.lower()
        return sum(1 for t in terms if t in low)

    scored = [(score(t), rid, t) for rid, t in rows]
    relevant = [s for s in scored if s[0] > 0]
    # Highest self-relevance first, newest breaking ties; then restore
    # chronological order so the injected block still reads as a sequence.
    chosen = sorted(relevant or scored,
                    key=lambda s: (-s[0], -s[1]))[:limit]
    chosen.sort(key=lambda s: s[1])

    if relevant:
        log.debug("wander sample for %s: %d/%d thoughts were self-relevant",
                  kin_name, len(relevant), len(rows))
    else:
        log.info("wander sample for %s: none of the last %d thoughts mentioned "
                 "the household — falling back to recency", kin_name, len(rows))

    # 400 chars used to cut mid-sentence. Trim to the last sentence boundary
    # when there is one reasonably close to the limit.
    out = []
    for _, _, text in chosen:
        t = text.strip()[:700]
        cut = max(t.rfind(". "), t.rfind(".\n"), t.rfind("? "), t.rfind("! "))
        if cut > 250:
            t = t[:cut + 1]
        out.append(t.strip())
    return out


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


def _vault_text_search(kin_name, query_text, limit):
    """Keyword recall via the vault's FTS index.

    The floor under both semantic paths below — no embed model or Qdrant
    required, just the vault, which is always running (it's a systemd unit /
    started by the app) on every machine.
    """
    try:
        r = requests.get(
            f"{_vault_url()}/search",
            params={"query": query_text, "author": kin_name, "limit": limit},
            timeout=8,
        )
        r.raise_for_status()
        return [m["content"].strip() for m in r.json() if m.get("content")]
    except Exception as e:
        log.warning("vault text search failed for %s (%s): %s",
                    kin_name, _vault_url(), e)
        return []


def _vault_semantic_search(kin_name, query_text, limit):
    """Vector recall via the vault's own embedded search (vault_server.py's
    /search-semantic) — no separate Qdrant server needed, just Ollama, which
    every Echo Bloom install already has. This is what a stock customer
    install actually uses; Qdrant above is only for machines where someone
    set one up by hand (the author's own cluster)."""
    try:
        r = requests.get(
            f"{_vault_url()}/search-semantic",
            params={"query": query_text, "author": kin_name, "limit": limit},
            timeout=30,
        )
        r.raise_for_status()
        return [m["content"].strip() for m in r.json() if m.get("content")]
    except Exception as e:
        log.info("vault semantic search unavailable for %s (%s): %s "
                 "— falling back to vault text search",
                 kin_name, _vault_url(), e)
        return _vault_text_search(kin_name, query_text, limit)


def get_vault_memories(kin_name, query_text, limit=5):
    """Relevant vault memories for this Kin. Qdrant when configured (the
    author's own cluster), the vault's own embedded vector search otherwise
    (every stock install), keyword search as the last resort. Returns list
    of strings."""
    if not query_text:
        return []
    try:
        # keep_alive: a cold load of the embed model takes ~11s when the GPUs
        # are busy with generation — one second past the old timeout=10, which
        # made semantic recall fail every single time. Warm, it answers in
        # under a second. Pin it resident and give the cold path room.
        r = requests.post(
            _embed_url(),
            json={"model": _embed_model(), "prompt": query_text,
                  "keep_alive": "999h"},
            timeout=30,
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
        r2.raise_for_status()
        results = r2.json().get("result", [])
        return [
            hit["payload"]["content"].strip()
            for hit in results
            if hit.get("payload", {}).get("content")
        ]
    except Exception as e:
        # This is the path that silently returned nothing while the embed call
        # timed out — the Kin then answered with no memory and no explanation.
        # Now it degrades to the vault's own semantic search instead of to
        # amnesia — real recall, not just keyword matching, on every install.
        log.info("qdrant unavailable for %s (embed=%s qdrant=%s): %s "
                 "— falling back to vault semantic search",
                 kin_name, _embed_url(), _qdrant_url(), e)
        return _vault_semantic_search(kin_name, query_text, limit)


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
            parts.append(f"[What's been happening here lately]\n{reflection}")

    wander = get_wander_thoughts(kin_name, limit=wander_limit, db_path=db_path)
    if wander:
        lines = "\n".join(f"- {t}" for t in wander)
        parts.append(f"Your recent thoughts from wandering:\n{lines}")

    vault = get_vault_memories(kin_name, query_text, limit=vault_limit) if query_text else []
    if vault:
        lines = "\n".join(f"- {m}" for m in vault)
        parts.append(f"Memories that may be relevant:\n{lines}")

    return "\n\n".join(parts)
