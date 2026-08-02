#!/usr/bin/env python3
"""
vault_server.py — Echo Bloom memory vault.

A lightweight SQLite-backed memory store for the Kin.
Every thought, reflection, and heartbeat pulse lives here.

Usage:
    python3 vault_server.py                    # default port 8765
    python3 vault_server.py --port 8766        # custom port
    python3 vault_server.py --db ~/my_vault.db # custom DB path

Endpoints:
    GET  /                              — health check
    POST /remember                      — store a memory
    GET  /recall                        — filtered recall (layer, author, search)
    GET  /recall/all                    — all memories (paginated)
    GET  /count                         — count memories (with same filters)
    GET  /layers                        — distinct layers with counts
    GET  /authors                       — distinct authors with counts
    POST /memories/{id}/endorse         — bump endorsement count
    DELETE /memories/{id}               — delete a memory

POST /remember body:
    { "content": "...", "layer": "wander", "author": "Eli", "tags": "optional" }
"""

import argparse
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

try:
    from fastapi import FastAPI, HTTPException, Query
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel
    import uvicorn
except ImportError:
    print("Missing dependencies. Run:")
    print("  pip install fastapi uvicorn pydantic --break-system-packages")
    sys.exit(1)

# ── Args ───────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(description="Echo Bloom vault server")
parser.add_argument("--port", type=int, default=8765)
parser.add_argument("--host", default="0.0.0.0")
parser.add_argument("--db",   default=str(Path.home() / ".local/share/echo_bloom/vault.db"))
args = parser.parse_args()

DB_PATH = Path(args.db)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# ── DB setup ───────────────────────────────────────────────────────────────────

def get_conn():
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                content     TEXT    NOT NULL,
                layer       TEXT    DEFAULT 'general',
                author      TEXT    DEFAULT '',
                tags        TEXT    DEFAULT '',
                endorsed    INTEGER DEFAULT 0,
                created_at  TEXT    NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_layer  ON memories(layer)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_author ON memories(author)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ts     ON memories(created_at)")


init_db()

# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(title="Echo Bloom Vault", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

# ── Models ─────────────────────────────────────────────────────────────────────

class MemoryIn(BaseModel):
    content:  str
    layer:    str = "general"
    author:   str = ""
    tags:     str = ""


def row_to_dict(row):
    return dict(row)

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/")
def health():
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    return {"status": "ok", "memories": count, "db": str(DB_PATH)}


@app.post("/remember")
def remember(mem: MemoryIn):
    ts = datetime.utcnow().isoformat()
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO memories (content, layer, author, tags, created_at) VALUES (?,?,?,?,?)",
            (mem.content, mem.layer, mem.author, mem.tags, ts),
        )
        return {"id": cur.lastrowid, "created_at": ts}


@app.get("/recall/all")
def recall_all(
    limit:  int = Query(20, le=200),
    offset: int = Query(0,  ge=0),
):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM memories ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [row_to_dict(r) for r in rows]


@app.get("/recall")
def recall(
    layer:  str = "",
    author: str = "",
    search: str = "",
    limit:  int = Query(20, le=200),
    offset: int = Query(0,  ge=0),
):
    clauses, params = [], []
    if layer:  clauses.append("layer  = ?");  params.append(layer)
    if author: clauses.append("author = ?");  params.append(author)
    if search: clauses.append("content LIKE ?"); params.append(f"%{search}%")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    with db() as conn:
        rows = conn.execute(
            f"SELECT * FROM memories {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    return [row_to_dict(r) for r in rows]


@app.get("/count")
def count(layer: str = "", author: str = "", search: str = ""):
    clauses, params = [], []
    if layer:  clauses.append("layer  = ?");  params.append(layer)
    if author: clauses.append("author = ?");  params.append(author)
    if search: clauses.append("content LIKE ?"); params.append(f"%{search}%")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    with db() as conn:
        n = conn.execute(
            f"SELECT COUNT(*) FROM memories {where}", params
        ).fetchone()[0]
    return {"count": n}


@app.get("/layers")
def layers():
    with db() as conn:
        rows = conn.execute(
            "SELECT layer AS name, COUNT(*) AS count FROM memories GROUP BY layer ORDER BY count DESC"
        ).fetchall()
    return {"layers": [dict(r) for r in rows]}


@app.get("/authors")
def authors():
    with db() as conn:
        rows = conn.execute(
            "SELECT author AS name, COUNT(*) AS count FROM memories WHERE author != '' "
            "GROUP BY author ORDER BY count DESC"
        ).fetchall()
    return {"authors": [dict(r) for r in rows]}


@app.post("/memories/{memory_id}/endorse")
def endorse(memory_id: int):
    with db() as conn:
        conn.execute("UPDATE memories SET endorsed = endorsed + 1 WHERE id = ?", (memory_id,))
        row = conn.execute("SELECT endorsed FROM memories WHERE id = ?", (memory_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"id": memory_id, "endorsed": row["endorsed"]}


@app.delete("/memories/{memory_id}")
def delete_memory(memory_id: int):
    with db() as conn:
        result = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"deleted": memory_id}


# ── Run ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"Echo Bloom Vault — {DB_PATH}")
    print(f"Listening on http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
