"""Vault browser (recall/search/semantic), core memories, and the ingestion
pipeline. Moved out of main.py in the router split (2026-09-20) — routes
only, unchanged."""

import asyncio
import json
import os
import re as _re
import subprocess
import sys
import uuid as _uuid
from urllib.parse import urlparse

import aiohttp

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

import cluster as cl
from deps import (
    templates, require_auth, log, _script, _LOGS_DIR, _utf8_env,
    _load_kin_cfg, _save_kin_cfg, _fetch_allowed, _fetch_page_text,
    _KIN_CONFIG_PATH,
)

router = APIRouter()

_DEFAULT_QDRANT = "http://localhost:6333"
_DEFAULT_VAULT  = "http://localhost:8765"


def _qdrant_url() -> str:
    if not _KIN_CONFIG_PATH.exists():
        return _DEFAULT_QDRANT
    try:
        cfg = json.loads(_KIN_CONFIG_PATH.read_text())
        return cfg.get("qdrant_url") or _DEFAULT_QDRANT
    except Exception:
        # Silently repointing at localhost looks exactly like "semantic search
        # stopped working" with no cause.
        log.exception("kin_config.json unreadable — falling back to Qdrant at %s",
                      _DEFAULT_QDRANT)
        return _DEFAULT_QDRANT


def _vault_url() -> str:
    if not _KIN_CONFIG_PATH.exists():
        return _DEFAULT_VAULT
    try:
        cfg = json.loads(_KIN_CONFIG_PATH.read_text())
        return cfg.get("vault_url") or _DEFAULT_VAULT
    except Exception:
        log.exception("kin_config.json unreadable — falling back to vault at %s",
                      _DEFAULT_VAULT)
        return _DEFAULT_VAULT


@router.get("/vault", response_class=HTMLResponse)
async def vault_page(request: Request, _=Depends(require_auth)):
    return templates.TemplateResponse(request, "vault.html", {"all_kin": cl.KIN})


@router.get("/api/vault")
async def api_vault(
    layer:         str = "",
    author:        str = "",
    search:        str = "",
    exclude_layer: str = "",
    limit:         int = 20,
    offset:        int = 0,
    _=Depends(require_auth),
):
    vault = _vault_url()
    params = {"limit": min(limit, 50), "offset": max(offset, 0)}
    if layer:         params["layer"]         = layer
    if author:        params["author"]        = author
    if search:        params["search"]        = search
    if exclude_layer: params["exclude_layer"] = exclude_layer

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{vault}/recall", params=params,
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                entries = await r.json()

            count_params = {k: v for k, v in params.items() if k not in ("limit", "offset")}
            async with session.get(f"{vault}/count", params=count_params,
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                total = (await r.json()).get("count", 0)

        return {"entries": entries, "total": total, "offset": offset, "limit": limit}
    except Exception:
        return {"entries": [], "total": 0, "offset": offset, "limit": limit,
                "error": "vault_offline", "vault_url": vault}


@router.get("/api/vault/count")
async def api_vault_count(
    layer:         str = "",
    author:        str = "",
    search:        str = "",
    exclude_layer: str = "",
    _=Depends(require_auth),
):
    vault = _vault_url()
    params = {}
    if layer:         params["layer"]         = layer
    if author:        params["author"]        = author
    if search:        params["search"]        = search
    if exclude_layer: params["exclude_layer"] = exclude_layer
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{vault}/count", params=params,
                                   timeout=aiohttp.ClientTimeout(total=5)) as r:
                total = (await r.json()).get("count", 0)
        return {"total": total}
    except Exception:
        log.warning("vault count failed against %s", vault, exc_info=True)
        return {"total": 0, "error": "vault_offline"}


@router.get("/api/vault/meta")
async def api_vault_meta(_=Depends(require_auth)):
    vault = _vault_url()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{vault}/layers",
                                   timeout=aiohttp.ClientTimeout(total=5)) as r:
                layers_data = await r.json()
            async with session.get(f"{vault}/authors",
                                   timeout=aiohttp.ClientTimeout(total=5)) as r:
                authors_data = await r.json()
        return {"layers": layers_data["layers"], "authors": authors_data["authors"]}
    except Exception:
        return {"layers": [], "authors": [], "error": "vault_offline"}


@router.get("/api/vault/semantic")
async def api_vault_semantic(q: str, limit: int = 10, _=Depends(require_auth)):
    if not q.strip():
        return {"results": [], "error": "empty query"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "http://localhost:11434/api/embeddings",
                json={"model": "nomic-embed-text", "prompt": q,
                      "keep_alive": "999h"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                embedding = (await r.json()).get("embedding", [])
            if not embedding:
                return {"results": [], "error": "embedding failed"}

            async with session.post(
                f"{_qdrant_url()}/collections/kin_memories/points/search",
                json={"vector": embedding, "limit": min(limit, 20), "with_payload": True},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                r.raise_for_status()
                pts = (await r.json()).get("result", [])

        return {"results": [
            {
                "score":    round(pt.get("score", 0), 3),
                "author":   pt["payload"].get("author", ""),
                "layer":    pt["payload"].get("layer", ""),
                "content":  pt["payload"].get("content", ""),
                "tags":     pt["payload"].get("tags", ""),
                "vault_id": pt["payload"].get("vault_id"),
            }
            for pt in pts
        ]}
    except Exception as e:
        # No installer has ever shipped Qdrant, so on a stock customer
        # machine this "SEMANTIC SEARCH" mode in the vault browser has been
        # dead from the first click. Fall back to the vault's own embedded
        # search (vault_server.py's /search-semantic) - same Ollama call,
        # no separate server required.
        log.info("qdrant semantic search unavailable (%s) - falling back to vault", e)
        return await _vault_semantic_fallback(q, limit)


async def _vault_semantic_fallback(q: str, limit: int) -> dict:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{_vault_url()}/search-semantic",
                params={"query": q, "limit": min(limit, 20)},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                r.raise_for_status()
                memories = await r.json()
        return {"results": [
            {
                "score":    round(m.get("score", 0), 3),
                "author":   m.get("author", ""),
                "layer":    m.get("layer", ""),
                "content":  m.get("content", ""),
                "tags":     m.get("tags", ""),
                "vault_id": m.get("id"),
            }
            for m in memories
        ]}
    except Exception as e:
        return {"results": [], "error": str(e)}


def _vault_is_local(url: str) -> bool:
    """Whether this machine could actually start the vault at that URL."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0", "")


def _vault_start_command() -> str:
    """The real command, built from the real resolved path.

    This used to be assembled in JavaScript from a guessed path — it named
    app/vault_server.py (the file lives in app/scripts/) and used cmd.exe's
    %LOCALAPPDATA% syntax, which PowerShell does not expand. Both wrong at
    once, so the one instruction we gave the user could not work.
    """
    script = _script("vault_server.py")
    if os.name == "nt":
        return f'& "{sys.executable}" "{script}" --port 8765'
    return f'"{sys.executable}" "{script}" --port 8765'


@router.get("/api/vault/status")
async def api_vault_status(_=Depends(require_auth)):
    vault  = _vault_url()
    script = _script("vault_server.py")
    info = {
        "url":          vault,
        "local":        _vault_is_local(vault),
        "can_start":    script.exists() and _vault_is_local(vault),
        "start_command": _vault_start_command(),
        "script_path":  str(script),
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{vault}/", timeout=aiohttp.ClientTimeout(total=3)) as r:
                return {"online": r.status < 500, **info}
    except Exception:
        return {"online": False, **info}


@router.post("/api/vault/start")
async def api_vault_start(_=Depends(require_auth)):
    """Start the local vault server ourselves.

    Telling a customer to paste a python command into a terminal was never a
    real answer — especially on Windows, where the command we printed was
    wrong in two different ways. If the vault is meant to live on this
    machine, the app can just start it.
    """
    vault = _vault_url()
    if not _vault_is_local(vault):
        return {"started": False,
                "error": f"Your vault is configured at {vault}, which is another machine. "
                         f"Start it there, or point Setup → Vault at this machine."}

    script = _script("vault_server.py")
    if not script.exists():
        return {"started": False,
                "error": "vault_server.py is missing — re-run the installer to deploy "
                         "the lifecycle scripts."}

    # Already up? Don't start a second one fighting for the port.
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{vault}/", timeout=aiohttp.ClientTimeout(total=2)) as r:
                if r.status < 500:
                    return {"started": True, "already_running": True, "url": vault}
    except Exception:
        pass

    try:
        _LOGS_DIR.mkdir(parents=True, exist_ok=True)
        logfile = _LOGS_DIR / "vault.log"
        with open(logfile, "a") as lf:
            proc = subprocess.Popen(
                [sys.executable, "-u", str(script), "--port", "8765"],
                stdout=lf, stderr=lf, env=_utf8_env(),
                start_new_session=(os.name != "nt"),
            )
    except Exception:
        log.exception("could not start the vault server")
        return {"started": False,
                "error": "Could not start the vault — see the app log for the reason."}

    # Give it a moment and confirm, so the UI never claims success blindly.
    for _ in range(10):
        await asyncio.sleep(0.5)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{vault}/",
                                       timeout=aiohttp.ClientTimeout(total=2)) as r:
                    if r.status < 500:
                        return {"started": True, "pid": proc.pid, "url": vault}
        except Exception:
            continue

    return {"started": False,
            "error": f"The vault was launched (pid {proc.pid}) but is not answering at "
                     f"{vault} yet. Check {_LOGS_DIR / 'vault.log'}."}


# ── Core memories ──────────────────────────────────────────────────────────────
# Stored in kin_config.json under kin[].core_memories (max 20 per Kin).
# Always injected into the system prompt regardless of query relevance.

@router.get("/api/kin/{name}/core-memories")
async def api_core_get(name: str, _=Depends(require_auth)):
    cfg = _load_kin_cfg()
    for k in cfg.get("kin", []):
        if k.get("name") == name:
            return {"kin": name, "core_memories": k.get("core_memories", [])}
    return {"kin": name, "core_memories": []}


@router.post("/api/vault/core")
async def api_core_add(request: Request, _=Depends(require_auth)):
    body     = await request.json()
    kin_name = (body.get("kin_name") or "").strip()
    content  = (body.get("content")  or "").strip()
    if not kin_name or not content:
        return {"ok": False, "error": "kin_name and content required"}
    cfg = _load_kin_cfg()
    for k in cfg.get("kin", []):
        if k.get("name") == kin_name:
            core = k.setdefault("core_memories", [])
            if content in core:
                return {"ok": True, "count": len(core), "already": True}
            if len(core) >= 20:
                return {"ok": False, "error": "Core memory limit is 20. Remove one first."}
            core.append(content)
            _save_kin_cfg(cfg)
            return {"ok": True, "count": len(core)}
    return {"ok": False, "error": f"Kin '{kin_name}' not found in config"}


@router.delete("/api/vault/core")
async def api_core_remove(request: Request, _=Depends(require_auth)):
    body     = await request.json()
    kin_name = (body.get("kin_name") or "").strip()
    content  = (body.get("content")  or "").strip()
    cfg = _load_kin_cfg()
    for k in cfg.get("kin", []):
        if k.get("name") == kin_name:
            core = k.get("core_memories", [])
            if content in core:
                core.remove(content)
                k["core_memories"] = core
                _save_kin_cfg(cfg)
            return {"ok": True, "count": len(core)}
    return {"ok": False, "error": f"Kin '{kin_name}' not found"}


# ── Ingestion pipeline ─────────────────────────────────────────────────────────
# Embed text (or fetched URL) into Qdrant + store full doc in the vault.
# The embedded chunks become available in kin_memory's semantic search.

def _chunk_text(text: str, max_chars: int = 700) -> list[str]:
    """Split text into chunks at paragraph/sentence boundaries."""
    chunks: list[str] = []
    for para in _re.split(r'\n{2,}', text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_chars:
            chunks.append(para)
            continue
        buf = ""
        for sent in _re.split(r'(?<=[.!?])\s+', para):
            if len(buf) + len(sent) + 1 <= max_chars:
                buf = (buf + " " + sent).strip() if buf else sent
            else:
                if buf:
                    chunks.append(buf)
                buf = sent if len(sent) <= max_chars else ""
                if len(sent) > max_chars:
                    for i in range(0, len(sent), max_chars):
                        chunks.append(sent[i:i + max_chars])
        if buf:
            chunks.append(buf)
    return [c for c in chunks if len(c.strip()) > 20]


@router.post("/api/ingest")
async def api_ingest(request: Request, _=Depends(require_auth)):
    body     = await request.json()
    kin_name = (body.get("kin_name") or "").strip()
    content  = (body.get("content")  or "").strip()
    url      = (body.get("url")      or "").strip()
    source   = (body.get("source")   or "").strip()

    if not kin_name:
        return {"ok": False, "error": "kin_name required"}

    if url and not content:
        if not _fetch_allowed(url):
            from urllib.parse import urlparse
            host = urlparse(url).netloc.lower().removeprefix("www.")
            return {"ok": False, "error": f"{host} is not in the fetch whitelist — paste the text instead"}
        try:
            content = await _fetch_page_text(url, max_chars=8000)
        except Exception as e:
            return {"ok": False, "error": f"Fetch failed: {str(e) or type(e).__name__}"}
        if not source:
            source = url

    if not content:
        return {"ok": False, "error": "content or url required"}
    if not source:
        source = "manual ingest"

    # Store full document in vault
    vault       = _vault_url()
    vault_id    = None
    vault_error = None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{vault}/remember",
                json={
                    "author":     kin_name,
                    "layer":      "document",
                    "content":    f"[Source: {source}]\n\n{content[:6000]}",
                    "tags":       f"ingested",
                    "visibility": "shared",
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                # Only an exception set vault_error before, so a vault that
                # answered 500 with a JSON body sailed through: .get("id")
                # returned None and the caller saw vault_id: null with no
                # reason given — the exact "offline vault looks like a
                # successful store" case this was supposed to rule out.
                if r.status >= 300:
                    vault_error = f"vault store failed: HTTP {r.status}"
                    log.warning("ingest: vault store returned %s from %s", r.status, vault)
                else:
                    rdata = await r.json()
                    vault_id = rdata.get("id")
    except Exception as e:
        # Still worth embedding, but returning vault_id: None with no reason
        # made an offline vault indistinguishable from a successful store.
        vault_error = f"vault store failed: {e}"
        log.warning("ingest: vault store failed against %s — continuing with embedding",
                    vault, exc_info=True)

    # Chunk and embed into Qdrant (the author's own cluster) and the vault
    # (every install — including every customer's, where Qdrant below has
    # never once been reachable). Same bug class as get_vault_memories:
    # this used to only ever land in Qdrant, so ingested documents were
    # unsearchable on every customer machine with no error surfaced.
    chunks   = _chunk_text(content)
    if not chunks:
        return {"ok": False, "error": "No usable text after chunking"}

    qdrant       = _qdrant_url()
    vault        = _vault_url()
    embedded     = 0
    vault_chunks = 0
    errors: list[str] = []

    async with aiohttp.ClientSession() as session:
        for chunk in chunks:
            try:
                async with session.post(
                    f"{vault}/remember",
                    json={
                        "author":  kin_name,
                        "layer":   "document_chunk",
                        "content": chunk,
                        "tags":    f"ingested,chunk,source:{source}",
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status < 300:
                        vault_chunks += 1
                    else:
                        errors.append(f"vault {r.status}")
            except Exception as e:
                errors.append(f"vault: {str(e)[:60]}")

            try:
                async with session.post(
                    "http://localhost:11434/api/embeddings",
                    json={"model": "nomic-embed-text", "prompt": chunk,
                          "keep_alive": "999h"},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as r:
                    emb = (await r.json()).get("embedding", [])
                if not emb:
                    errors.append("embedding returned empty")
                    continue

                async with session.put(
                    f"{qdrant}/collections/kin_memories/points",
                    json={"points": [{
                        "id":      str(_uuid.uuid4()),
                        "vector":  emb,
                        "payload": {
                            "author":   kin_name,
                            "content":  chunk,
                            "layer":    "document",
                            "source":   source,
                            "vault_id": vault_id,
                        },
                    }]},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status < 300:
                        embedded += 1
                    else:
                        errors.append(f"qdrant {r.status}")
            except Exception as e:
                errors.append(f"qdrant: {str(e)[:60]}")

    return {
        # True if the document is searchable *anywhere* -- a customer with
        # no Qdrant but a working vault should see success, not "0 embedded"
        # for a document that actually is there.
        "ok":           embedded > 0 or vault_chunks > 0,
        "chunks":       len(chunks),
        "embedded":     embedded,
        "vault_chunks": vault_chunks,
        "vault_id":     vault_id,
        "errors":       ([vault_error] + errors)[:3] if vault_error else errors[:3],
    }
