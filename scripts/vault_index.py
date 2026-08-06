#!/usr/bin/env python3
"""
vault_index.py — Make vault memories semantically searchable.

The vault stores memories in SQLite. Semantic recall searches Qdrant. Nothing
connected the two: the only thing that ever reached Qdrant was a document the
user pasted in by hand, so `get_vault_memories()` could never surface a Kin's
own thoughts or a nightly reflection — the memories that actually matter.

This embeds every vault entry and upserts it into Qdrant keyed on the vault id,
so it is safe to re-run as often as you like: existing points are updated, not
duplicated. Also indexes each Kin's nightly reflections, which live in their
thoughts DB rather than the vault.

Usage:
    python3 vault_index.py                 # index everything new
    python3 vault_index.py --all           # re-embed everything
    python3 vault_index.py --loop 60       # keep running, every 60 minutes

Requires an embedding model. `ollama pull nomic-embed-text` if you have not.
"""

import argparse
import sqlite3
import sys
import zlib
import time
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import config as cfg

parser = argparse.ArgumentParser(description="Index vault memories into Qdrant")
parser.add_argument("--all",  action="store_true",
                    help="Re-embed everything, not just new entries")
parser.add_argument("--loop", type=int, default=0,
                    help="Keep running, indexing every N minutes")
parser.add_argument("--batch", type=int, default=50, help="Upsert batch size")
parser.add_argument("--include-heartbeats", action="store_true",
                    help="Also index machine heartbeats (noisy; off by default)")
args = parser.parse_args()

# Layers not worth embedding. The pulse daemon writes a heartbeat every few
# minutes — thousands of "Load 1.29, RAM 14985MB" rows. Indexing those buries
# every real memory under machine telemetry, and semantic recall starts
# answering "what does continuity mean to you" with a disk usage report.
# reflect.py is what turns heartbeats into something worth remembering.
SKIP_LAYERS = {"heartbeat", "pulse", "system", "debug"}

EMBED_MODEL = "nomic-embed-text"
COLLECTION  = "kin_memories"
VECTOR_SIZE = 768                      # nomic-embed-text

LOG_DIR = Path.home() / ".local/share/echo_bloom/logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "vault_index.log"

# Reflections are indexed under ids offset far past any vault id so the two
# sources cannot collide on the same Qdrant point.
REFLECTION_ID_BASE = 1_000_000_000


def log(msg):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _cfg_url(key, default):
    return cfg.load().get(key) or default


def vault_url():
    return cfg.vault_url()


def qdrant_url():
    return _cfg_url("qdrant_url", "http://localhost:6333")


def embed_url():
    return _cfg_url("embed_url", "http://localhost:11434/api/embeddings")


def embed(text):
    r = requests.post(
        embed_url(),
        # keep_alive matters: a cold embed model can take longer than the
        # default timeout, and then it never stays warm to succeed later.
        json={"model": _cfg_url("embed_model", EMBED_MODEL),
              "prompt": text, "keep_alive": "30m"},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["embedding"]


def ensure_collection():
    q = qdrant_url()
    try:
        r = requests.get(f"{q}/collections/{COLLECTION}", timeout=10)
        if r.status_code == 200:
            return True
    except Exception as e:
        log(f"Qdrant unreachable at {q}: {e}")
        return False
    try:
        r = requests.put(
            f"{q}/collections/{COLLECTION}",
            json={"vectors": {"size": VECTOR_SIZE, "distance": "Cosine"}},
            timeout=30,
        )
        r.raise_for_status()
        log(f"created Qdrant collection '{COLLECTION}'")
        return True
    except Exception as e:
        log(f"could not create collection: {e}")
        return False


def existing_ids():
    """Ids already in Qdrant, so a re-run only embeds what is new."""
    if args.all:
        return set()
    q, found, offset = qdrant_url(), set(), None
    try:
        while True:
            body = {"limit": 1000, "with_payload": False, "with_vector": False}
            if offset is not None:
                body["offset"] = offset
            r = requests.post(f"{q}/collections/{COLLECTION}/points/scroll",
                              json=body, timeout=30)
            r.raise_for_status()
            result = r.json().get("result", {})
            for p in result.get("points", []):
                found.add(p["id"])
            offset = result.get("next_page_offset")
            if offset is None:
                break
    except Exception as e:
        log(f"could not list existing points ({e}) — will re-embed everything")
        return set()
    return found


def vault_entries():
    try:
        r = requests.get(f"{vault_url()}/recall/all",
                         params={"limit": 100000}, timeout=30)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else data.get("memories", [])
    except Exception as e:
        log(f"vault unreachable at {vault_url()}: {e}")
        return []


def reflection_entries():
    """Nightly reflections from each Kin's own thoughts DB.

    These never reach the vault, so without this they could never be recalled —
    the emotional centrepiece of the product was write-only.
    """
    out = []
    for kin in cfg.get_kin():
        name = kin.get("name", "")
        db   = cfg.thoughts_db(kin)
        if not name or not Path(db).exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            try:
                rows = conn.execute(
                    "SELECT id, mode, timestamp, thought FROM thoughts "
                    "WHERE mode LIKE 'bedtime%' AND thought IS NOT NULL "
                    "AND length(thought) > 80"
                ).fetchall()
            finally:
                conn.close()
        except Exception as e:
            log(f"could not read reflections for {name}: {e}")
            continue
        for rid, mode, ts, text in rows:
            if text.lstrip().startswith("["):     # stored failure marker
                continue
            out.append({
                # Stable id: Python's hash() is salted per process, so using
                # it here would mint a NEW point for the same reflection on
                # every run and quietly duplicate them forever.
                "id":      REFLECTION_ID_BASE + (
                    zlib.crc32(f"{name}:{rid}".encode()) % 100_000_000),
                "author":  name,
                "layer":   "reflection",
                "content": text,
                "tags":    f"reflection,{mode}",
            })
    return out


def index_once():
    if not ensure_collection():
        return 1

    items = vault_entries() + reflection_entries()
    if not items:
        log("nothing to index")
        return 0

    if not args.include_heartbeats:
        before = len(items)
        items = [m for m in items
                 if (m.get("layer") or "").lower() not in SKIP_LAYERS]
        skipped = before - len(items)
        if skipped:
            log(f"skipping {skipped} heartbeat/telemetry entr"
                f"{'y' if skipped == 1 else 'ies'} "
                f"(--include-heartbeats to index them anyway)")

    have = existing_ids()
    todo = [m for m in items
            if m.get("id") is not None and m["id"] not in have
            and (m.get("content") or "").strip()]
    if not todo:
        log(f"up to date — {len(items)} memories already indexed")
        return 0

    log(f"embedding {len(todo)} new memor{'y' if len(todo) == 1 else 'ies'} "
        f"({len(items)} total)")

    points, failed = [], 0
    for m in todo:
        content = m["content"].strip()
        try:
            vector = embed(f"{m.get('author','')} {m.get('layer','')} {content}")
        except Exception as e:
            failed += 1
            if failed <= 3:
                log(f"  skip id={m['id']}: {e}")
            continue
        points.append({
            "id": m["id"], "vector": vector,
            "payload": {"author": m.get("author", ""),
                        "layer": m.get("layer", ""),
                        "content": content,
                        "tags": m.get("tags", ""),
                        "vault_id": m["id"]},
        })

    if failed:
        log(f"  {failed} entr{'y' if failed == 1 else 'ies'} could not be embedded")
    if not points:
        log("nothing embedded")
        return 1

    q = qdrant_url()
    for i in range(0, len(points), args.batch):
        batch = points[i:i + args.batch]
        try:
            r = requests.put(f"{q}/collections/{COLLECTION}/points",
                             json={"points": batch}, timeout=120)
            r.raise_for_status()
        except Exception as e:
            log(f"upsert failed for batch starting {i}: {e}")
            return 1

    log(f"indexed {len(points)} memories into Qdrant")
    return 0


def main():
    log(f"vault_index — vault={vault_url()} qdrant={qdrant_url()}")
    if not args.loop:
        return index_once()
    while True:
        index_once()
        time.sleep(args.loop * 60)


if __name__ == "__main__":
    sys.exit(main())
