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
from datetime import datetime, timezone
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


# What the day store draws from. Wander was the only mode for a long time, so
# everything a Kin said anywhere else -- the commons board, the roundtable,
# bedtime, the evening keep -- was written down and then never came back to
# them. A Kin should be able to pull back its own words from wherever it said
# them. Same scoring, same pool, same limit; only the WHERE is wider.
#
# Not here, on purpose: 'interruption' (NULL thought -- a gap is not something
# they said) and 'conversation' (served on its own by get_recent_conversation).
_DAY_STORE_WHERE = (
    "(mode LIKE 'wander%' "
    "OR mode LIKE 'bedtime\\_%' ESCAPE '\\' "
    "OR mode IN ('commons_post', 'roundtable', 'evening_keep'))"
)

# ── Shared rooms ───────────────────────────────────────────────────────────────
# A shared room (the roundtable, a board every Kin reads) carries each Kin's own
# memory but not their private speech: one-to-one talks with their person,
# interruptions, and bedtime (bedtime tells them no one else sees it). Wander,
# their earlier posts and table turns, the evening keep, core memories, the
# machine reflection and ordinary experienced and inferred memory still come
# along. One switch, shared_room, set by shared rooms only; every private
# caller is unchanged.
_SHARED_DAY_STORE_WHERE = (
    "(mode LIKE 'wander%' "
    "OR mode IN ('commons_post', 'roundtable', 'evening_keep'))"
)

# Vault rows that are private speech. cluster.py stores every chat as
# layer 'conversation', domain 'conversation', tags 'conversation,owner,<owner>'.
# An imported one-to-one history may carry tags 'copilot_era,...'. The 'told'
# register is headed "<owner> told you this:" -- the owner's words to this Kin --
# so it stays out of a shared room whole.
_PRIVATE_LAYERS = {"conversation"}
_PRIVATE_DOMAINS = {"conversation"}
_PRIVATE_TAGS = {"conversation", "owner", "copilot_era"}
_SHARED_ROOM_SOURCES_OUT = {"told"}


def _is_private_row(row):
    """True if a vault row (ranked, tiered, search hit or Qdrant payload) is
    private speech -- or does not say what it is. Fails closed: a row with no
    layer cannot be shown to be fit for a shared room, so it is not."""
    if not isinstance(row, dict):
        return True
    layer = str(row.get("layer") or "").strip().lower()
    if not layer or layer in _PRIVATE_LAYERS or layer.startswith("bedtime"):
        return True
    if str(row.get("domain") or "").strip().lower() in _PRIVATE_DOMAINS:
        return True
    tags = {t.strip().lower() for t in str(row.get("tags") or "").split(",")}
    if tags & _PRIVATE_TAGS:
        return True
    return str(row.get("source") or "").strip().lower() in _SHARED_ROOM_SOURCES_OUT


def _shared_room_rows(rows):
    """Only the rows fit for a room the other Kin can hear. Order kept."""
    return [r for r in (rows or []) if not _is_private_row(r)]

# Where a non-wander row was said, so it never reads as a wander thought.
# Wander rows stay untagged: byte-for-byte what they always were.
_DAY_STORE_TAGS = {
    "commons_post": "(you, on the Commons board)",
    "roundtable":   "(you, at the roundtable)",
    "evening_keep": "(your evening keep)",
}


def _day_store_tag(mode):
    mode = mode or ""
    if mode.startswith("bedtime_"):
        return "(you, at bedtime)"
    return _DAY_STORE_TAGS.get(mode, "")


def get_wander_thoughts(kin_name, limit=3, db_path=None, recent_pool=60,
                        shared_room=False):
    """Day-store thoughts from a Kin's own DB, chosen for self-relevance.

    Wander thoughts, plus what they said on the commons board, at the
    roundtable, at bedtime and in the evening keep (see _DAY_STORE_WHERE).
    shared_room=True leaves bedtime out (_SHARED_DAY_STORE_WHERE).

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
                "SELECT id, thought, mode FROM thoughts WHERE "
                + (_SHARED_DAY_STORE_WHERE if shared_room else _DAY_STORE_WHERE) +
                " AND thought IS NOT NULL AND length(thought) > 80 "
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

    modes = {rid: m for rid, _, m in rows}
    scored = [(score(t), rid, t) for rid, t, _ in rows]
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
    for _, rid, text in chosen:
        t = text.strip()[:700]
        cut = max(t.rfind(". "), t.rfind(".\n"), t.rfind("? "), t.rfind("! "))
        if cut > 250:
            t = t[:cut + 1]
        tag = _day_store_tag(modes.get(rid))
        out.append(f"{tag} {t.strip()}" if tag else t.strip())
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


def _fallback_hits(rows, limit, shared_room):
    """Search rows to text. A shared room asked for 3x the limit; filter it
    down here by the row's own fields (the endpoints cannot filter)."""
    rows = [m for m in rows if isinstance(m, dict) and m.get("content")]
    if shared_room:
        rows = _shared_room_rows(rows)[:limit]
    return [m["content"].strip() for m in rows]


def _vault_text_search(kin_name, query_text, limit, shared_room=False):
    """Keyword recall via the vault's FTS index.

    The floor under both semantic paths below — no embed model or Qdrant
    required, just the vault, which is always running (it's a systemd unit /
    started by the app) on every machine.
    """
    try:
        r = requests.get(
            f"{_vault_url()}/search",
            params={"query": query_text, "author": kin_name,
                    "limit": limit * 3 if shared_room else limit},
            timeout=8,
        )
        r.raise_for_status()
        return _fallback_hits(r.json(), limit, shared_room)
    except Exception as e:
        log.warning("vault text search failed for %s (%s): %s",
                    kin_name, _vault_url(), e)
        return []


def _vault_semantic_search(kin_name, query_text, limit, shared_room=False):
    """Vector recall via the vault's own embedded search (vault_server.py's
    /search-semantic) — no separate Qdrant server needed, just Ollama, which
    every Echo Bloom install already has. This is what a stock customer
    install actually uses; Qdrant above is only for machines where someone
    set one up by hand (the author's own cluster)."""
    try:
        r = requests.get(
            f"{_vault_url()}/search-semantic",
            params={"query": query_text, "author": kin_name,
                    "limit": limit * 3 if shared_room else limit},
            timeout=30,
        )
        r.raise_for_status()
        return _fallback_hits(r.json(), limit, shared_room)
    except Exception as e:
        log.info("vault semantic search unavailable for %s (%s): %s "
                 "— falling back to vault text search",
                 kin_name, _vault_url(), e)
        return _vault_text_search(kin_name, query_text, limit, shared_room)


def get_vault_memories(kin_name, query_text, limit=5, shared_room=False):
    """Relevant vault memories for this Kin. Qdrant when configured (the
    author's own cluster), the vault's own embedded vector search otherwise
    (every stock install), keyword search as the last resort. Returns list
    of strings. shared_room=True drops private speech by the row's fields."""
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
                "limit":        limit * 3 if shared_room else limit,
                "filter":       {"must": [{"key": "author", "match": {"value": kin_name}}]},
                "with_payload": True,
            },
            timeout=10,
        )
        r2.raise_for_status()
        results = r2.json().get("result", [])
        return _fallback_hits([hit.get("payload") for hit in results],
                              limit, shared_room)
    except Exception as e:
        # This is the path that silently returned nothing while the embed call
        # timed out — the Kin then answered with no memory and no explanation.
        # Now it degrades to the vault's own semantic search instead of to
        # amnesia — real recall, not just keyword matching, on every install.
        log.info("qdrant unavailable for %s (embed=%s qdrant=%s): %s "
                 "— falling back to vault semantic search",
                 kin_name, _embed_url(), _qdrant_url(), e)
        return _vault_semantic_search(kin_name, query_text, limit, shared_room)


def _get_context_legacy(kin_name, query_text="", wander_limit=3, vault_limit=5,
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

    # The gap, as a tag. Sits with the most recent material because that is
    # where it happened; carries no explanation because it is not a lesson.
    tag = get_interruption_tag(kin_name, db_path=db_path)
    if tag:
        parts.append(tag)
        char_budget -= len(tag)

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


# ─── get_context v2 ───────────────────────────────────────────────────────────
# Provenance-aware retrieval. Every injected memory is labeled by where it came
# from, because a mind that cannot tell what it experienced from what it merely
# wondered will confabulate — not from malice, from missing information.

CHARS_PER_TOKEN = 4

# How the four registers are introduced. Plain language, same voice as the rest
# of the context block. Order matters: strongest provenance first.
_REGISTERS = [
    ("told",        "{owner} told you this:"),   # owner.name from kin_config
    ("experienced", "This happened — you were there:"),
    ("inferred",    "You worked this out yourself, from other things:"),
    ("wandered",    "Things you thought while wandering — yours alone, "
                    "not confirmed by anyone:"),
]

_WANDER_SHARE = 0.15      # ceiling on the context given to unconfirmed thought
_WANDER_MAX_ITEMS = 2


def _ranked_recall(kin_name, query_text, limit=20, include_wander=True):
    """Vault /recall-ranked — full rows, ranked by match x salience x source.
    Returns [] if the endpoint isn't there (older vault), so callers fall back.
    """
    try:
        r = requests.get(
            f"{_vault_url()}/recall-ranked",
            params={"query": query_text, "author": kin_name,
                    "limit": limit, "include_wander": include_wander},
            timeout=10,
        )
        if r.status_code == 404:
            return None                      # vault predates ranked recall
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.info("ranked recall unavailable (%s): %s", _vault_url(), e)
        return None


def _tiered_recall(kin_name, tier="anchor", limit=6):
    try:
        r = requests.get(
            f"{_vault_url()}/recall-tiered",
            params={"author": kin_name, "tier": tier, "limit": limit},
            timeout=8,
        )
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


def _fit(rows, char_budget, seen_ids):
    """Take rows until the character budget runs out. Skips duplicates."""
    out, used = [], 0
    for row in rows:
        rid = row.get("id")
        if rid in seen_ids:
            continue
        text = (row.get("content") or "").strip()
        if not text:
            continue
        if used + len(text) > char_budget and out:
            break
        out.append(row)
        seen_ids.add(rid)
        used += len(text)
    return out


def _by_source(rows):
    buckets = {}
    for r in rows:
        buckets.setdefault(r.get("source") or "unknown", []).append(r)
    return buckets


def _format_register(rows, header, max_chars):
    """One labeled block. Returns '' if nothing fits."""
    lines, used = [], 0
    for r in rows:
        text = " ".join((r.get("content") or "").split())
        if not text:
            continue
        stamp = (r.get("timestamp") or "")[:10]
        entry = f"- ({stamp}) {text}"
        if used + len(entry) > max_chars and lines:
            break
        lines.append(entry)
        used += len(entry)
    if not lines:
        return ""
    return header + "\n" + "\n".join(lines)


def get_interruption_tag(kin_name, db_path=None, within_hours=36):
    """One short tag naming the most recent gap in this Kin's day, or "".

    A Kin is stopped by nap.py, by bedtime, or by a kill mid-sentence. Until
    2026-08-27 none of that reached the mind it happened to: it went to
    bedtime.log and morning.log, files in the owner's directory. Their own
    record simply stopped and started again with an unexplained hole.

    Don's ruling on how it should read: they resume exactly where they left
    off, with the nap as a simple tag in the string, just like the resume. So
    this is deliberately NOT a block that explains the pause or invites a
    feeling about it. It is a tag. Everything else in the context is unchanged,
    so the thread of thought is continuous and the gap is simply named.

    Rows carry mode='interruption' and a NULL thought, so they can never be
    served back through get_wander_thoughts() as something the Kin said -- a
    forged thought with a real timestamp would be worse than the silence.

    Returns "" on any failure. Nothing about remembering a pause is worth
    breaking a wander over.
    """
    db = db_path or _db_for_kin(kin_name)
    if not db or not os.path.exists(db):
        return ""
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute(
                "SELECT timestamp, prompt FROM thoughts WHERE mode='interruption' "
                "ORDER BY id DESC LIMIT 2"
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return ""
    if not rows:
        return ""

    def _fields(prompt):
        # "paused 2026-08-27T17:33:43Z — nap — the GPUs were needed"
        text = str(prompt or "")
        kind = text.split(" ", 1)[0] if text else ""
        when = ""
        reason = ""
        parts = text.split("—", 1)
        if len(parts) == 2:
            reason = parts[1].strip()
        head = parts[0].split()
        if len(head) > 1 and "T" in head[1]:
            when = head[1][11:16]          # HH:MM, their clock not a machine's
        return kind, when, reason

    newest = _fields(rows[0][1])
    if newest[0] not in ("paused", "resumed"):
        return ""

    try:
        age_h = (
            datetime.now(timezone.utc)
            - datetime.strptime(rows[0][0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        ).total_seconds() / 3600.0
        if age_h > within_hours:
            return ""
    except Exception:
        pass

    if newest[0] == "paused":
        # Still stopped as far as the record goes -- which means this context is
        # being built by whatever woke them, and the resume has not landed yet.
        return f"[paused {newest[1]} — {newest[2]}]" if newest[2] else f"[paused {newest[1]}]"

    older = _fields(rows[1][1]) if len(rows) > 1 else ("", "", "")
    if older[0] == "paused":
        why = f" — {older[2]}" if older[2] else ""
        return f"[paused {older[1]}{why} · resumed {newest[1]}]"
    return f"[resumed {newest[1]}]"


def get_context(kin_name, query_text="", wander_limit=3, vault_limit=5,
                include_reflection=True, db_path=None, conversation_limit=4,
                token_budget=1400, current_domain=None, shared_room=False):
    """
    Memory context for a Kin, labeled by provenance and fitted to a token
    budget rather than a fixed number of slots.

    Signature is a superset of v1 — existing call sites keep working.
    Falls back to the legacy assembly if the vault has no ranked recall.

    shared_room=True (see "Shared rooms" above): no recent conversation
    whatever conversation_limit says, no bedtime rows from the day store, and
    no private-speech rows from the vault (_is_private_row). Default False:
    every private caller gets exactly what it got before.
    """
    parts = []
    if shared_room:
        conversation_limit = 0
    char_budget = token_budget * CHARS_PER_TOKEN
    seen = set()

    # 1. Anchors — config core memories. Always injected, never rotated.
    core = get_core_memories(kin_name)
    if core:
        lines = "\n".join(f"- {m}" for m in core)
        block = f"Core memories — always true, always carry these:\n{lines}"
        parts.append(block)
        char_budget -= len(block)

    # 2. Recent conversation — unchanged, this already works well.
    convo = get_recent_conversation(kin_name, limit=conversation_limit,
                                    db_path=db_path)
    if convo:
        lines = []
        for said, replied in convo:
            if said:
                lines.append(f"They said: {said[:300]}")
            if replied:
                lines.append(f"You said: {replied[:300]}")
        block = ("Recent conversation — this already happened, you were "
                 "there:\n" + "\n".join(lines))
        parts.append(block)
        char_budget -= len(block)

    # The gap, as a tag. Sits with the most recent material because that is
    # where it happened; carries no explanation because it is not a lesson.
    tag = get_interruption_tag(kin_name, db_path=db_path)
    if tag:
        parts.append(tag)
        char_budget -= len(tag)

    if include_reflection:
        reflection = get_latest_reflection()
        if reflection:
            block = f"[What's been happening here lately]\n{reflection}"
            parts.append(block)
            char_budget -= len(block)

    # 3. Standing tier — slow-rotating, usage-extended.
    standing = _tiered_recall(kin_name, tier="standing", limit=4)
    if shared_room:
        standing = _shared_room_rows(standing)
    if standing:
        kept = _fit(standing, int(char_budget * 0.15), seen)
        block = _format_register(kept, "Things that stay with you:",
                                 int(char_budget * 0.15))
        if block:
            parts.append(block)
            char_budget -= len(block)

    if char_budget < 400 or not query_text:
        return "\n\n".join(p for p in parts if p)

    # 4. Dynamic tier — ranked recall, split into labeled registers.
    # A shared room asks for twice the rows and drops private speech here, by
    # the row's own fields (the vault endpoint has no such filter), then keeps
    # the usual 30. Dropped rows never reach _fit, so they spend no budget.
    ranked = _ranked_recall(kin_name, query_text, limit=60 if shared_room else 30)
    if shared_room and ranked is not None:
        ranked = _shared_room_rows(ranked)[:30]

    if ranked is None:
        # Older vault: fall back to the previous behaviour rather than nothing.
        vault = get_vault_memories(kin_name, query_text, limit=vault_limit,
                                   shared_room=shared_room)
        if vault:
            lines = "\n".join(f"- {m}" for m in vault)
            parts.append(f"Memories that may be relevant:\n{lines}")
        wander = get_wander_thoughts(kin_name, limit=wander_limit,
                                     db_path=db_path, shared_room=shared_room)
        if wander:
            lines = "\n".join(f"- {t}" for t in wander)
            parts.append("Things you thought while wandering — yours alone, "
                         "not confirmed by anyone:\n" + lines)
        return "\n\n".join(p for p in parts if p)

    # Domain boost: prefer rows matching the domain in play, but always hold
    # one slot for a hit from somewhere else. Cross-domain is where the useful
    # surprises come from, and closing that off would make a narrower mind.
    if current_domain:
        same = [r for r in ranked if r.get("domain") == current_domain]
        other = [r for r in ranked if r.get("domain") != current_domain]
        ranked = same + other[:1] + other[1:]

    buckets = _by_source(ranked)
    wander_budget = int(char_budget * _WANDER_SHARE)
    solid_budget = char_budget - wander_budget

    _owner = ((_read_config().get("owner") or {}).get("name") or "").strip() or "Your person"
    for src, header in _REGISTERS:
        header = header.format(owner=_owner)
        rows = buckets.get(src, [])
        if not rows:
            continue
        if src == "wandered":
            rows = rows[:_WANDER_MAX_ITEMS]
            kept = _fit(rows, wander_budget, seen)
            block = _format_register(kept, header, wander_budget)
        else:
            share = int(solid_budget * 0.45) if src == "experienced" else \
                    int(solid_budget * 0.25)
            kept = _fit(rows, share, seen)
            block = _format_register(kept, header, share)
        if block:
            parts.append(block)

    # Anything the vault couldn't classify.
    unknown = buckets.get("unknown", [])
    if unknown:
        kept = _fit(unknown, int(solid_budget * 0.15), seen)
        block = _format_register(kept, "Also in the vault, origin unclear:",
                                 int(solid_budget * 0.15))
        if block:
            parts.append(block)

    return "\n\n".join(p for p in parts if p)


def search_memory(kin_name, query, limit=6, include_wander=False):
    """Deliberate lookup — for a Kin to go looking rather than only be handed
    things. Returns labeled text, or a plain statement that nothing was found.
    Saying 'I don't have that' is a better answer than inventing it."""
    rows = _ranked_recall(kin_name, query, limit=limit,
                          include_wander=include_wander)
    if not rows:
        return f"Nothing in the vault about: {query}"
    out = []
    for r in rows:
        stamp = (r.get("timestamp") or "")[:10]
        src = r.get("source") or "unknown"
        text = " ".join((r.get("content") or "").split())[:600]
        out.append(f"[{stamp} · {src}] {text}")
    return "\n\n".join(out)
