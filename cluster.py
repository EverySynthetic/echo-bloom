"""
cluster.py — Live status of all nodes and Kin.
"""

import os
import json
import logging
import sqlite3
import asyncio
import aiohttp
from datetime import datetime
from pathlib import Path

try:
    import logging_setup
    log = logging_setup.get("cluster")
except Exception:                     # importable standalone from scripts/
    log = logging.getLogger("echo_bloom.cluster")

CONFIG_PATH = Path.home() / ".config/kin_app/kin_config.json"

NODES_DEFAULT = [
    {"name": "Local", "ip": "localhost", "ollama_port": 11434, "role": "primary"},
]

KIN_DEFAULT = []


def _expand_paths(kin_list):
    for k in kin_list:
        if k.get("db"):
            k["db"] = os.path.expanduser(k["db"])
        if k.get("space"):
            k["space"] = os.path.expanduser(k["space"])
    return kin_list


def load_kin_config_raw():
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except Exception:
            pass
    return {"nodes": NODES_DEFAULT, "kin": KIN_DEFAULT}


def _load():
    cfg = load_kin_config_raw()
    nodes = cfg.get("nodes", NODES_DEFAULT)
    kin   = _expand_paths(cfg.get("kin", KIN_DEFAULT))
    return nodes, kin


NODES, KIN = _load()
KIN_BY_NAME = {k["name"]: k for k in KIN}


def reload_config():
    global NODES, KIN, KIN_BY_NAME
    NODES, KIN = _load()
    KIN_BY_NAME = {k["name"]: k for k in KIN}


async def _ping_ollama(session, ip, port, timeout=4):
    url = f"http://{ip}:{port}/"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            return r.status < 500
    except Exception:
        return False


async def _get_pulled_models(session, host, timeout=4):
    """Return set of model name strings pulled on this Ollama host."""
    try:
        async with session.get(
            f"{host}/api/tags",
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as r:
            if r.status == 200:
                data = await r.json()
                return {m["name"] for m in data.get("models", [])}
    except Exception:
        pass
    return set()


def _model_in_set(model: str, pulled: set) -> bool:
    if model in pulled:
        return True
    bare = model.split(":")[0]
    return any(m == model or m == f"{model}:latest" or m.split(":")[0] == bare
               for m in pulled)


async def _ping_service(session, url, timeout=4):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            return r.status < 500
    except Exception:
        return False


def _thought_count(db_path):
    if not db_path or not os.path.exists(db_path):
        return 0
    try:
        conn = sqlite3.connect(db_path, timeout=3)
        count = conn.execute("SELECT COUNT(*) FROM thoughts").fetchone()[0]
        conn.close()
        return count
    except Exception:
        return 0


def _last_thought_time(db_path):
    if not db_path or not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path, timeout=3)
        row = conn.execute(
            "SELECT timestamp FROM thoughts ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def _latest_thought(db_path):
    if not db_path or not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(db_path, timeout=3)
        row = conn.execute(
            "SELECT thought FROM thoughts WHERE mode LIKE 'wander%' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row and row[0]:
            text = row[0].strip()
            return text[:200] + ("…" if len(text) > 200 else "")
        return None
    except Exception:
        return None


def _kin_db_stats(db_path):
    """(thought_count, last_timestamp, latest_wander_thought) from one connection.

    Opened read-only: the wander process writes these DBs concurrently, and a
    plain connect() silently creates an empty database when the path is wrong.
    Blocking — always call this through asyncio.to_thread().
    """
    if not db_path or not os.path.exists(db_path):
        return (0, None, None)
    conn = None
    try:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
        except Exception:
            conn = sqlite3.connect(db_path, timeout=3)

        count = conn.execute("SELECT COUNT(*) FROM thoughts").fetchone()[0]

        row     = conn.execute(
            "SELECT timestamp FROM thoughts ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last_ts = row[0] if row else None

        row    = conn.execute(
            "SELECT thought FROM thoughts WHERE mode LIKE 'wander%' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        latest = None
        if row and row[0]:
            text   = row[0].strip()
            latest = text[:200] + ("…" if len(text) > 200 else "")

        return (count, last_ts, latest)
    except Exception as e:
        log.debug("thoughts db unreadable (%s): %s", db_path, e)
        return (0, None, None)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _time_ago(ts_str):
    if not ts_str:
        return "unknown"
    try:
        formats = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"]
        for fmt in formats:
            try:
                dt = datetime.strptime(ts_str[:19], fmt)
                break
            except ValueError:
                continue
        else:
            return ts_str
        delta = datetime.now() - dt
        s = int(delta.total_seconds())
        if s < 60:
            return f"{s}s ago"
        if s < 3600:
            return f"{s // 60}m ago"
        if s < 86400:
            return f"{s // 3600}h ago"
        return f"{s // 86400}d ago"
    except Exception:
        return ts_str


async def get_cluster_status():
    """Return live status of all nodes and Kin. Fast — runs in parallel."""
    async with aiohttp.ClientSession() as session:
        node_tasks = []
        for node in NODES:
            if node.get("ollama_port"):
                node_tasks.append(_ping_ollama(session, node["ip"], node["ollama_port"]))
            else:
                node_tasks.append(_ping_service(session, f"http://{node['ip']}/"))
        node_results = await asyncio.gather(*node_tasks, return_exceptions=True)

        nodes_out = [{**node, "up": result is True}
                     for node, result in zip(NODES, node_results)]

        # For every node that's up, fetch its pulled-model list in parallel.
        # Same session as the pings — this used to open a second one.
        up_nodes    = [n for n in nodes_out if n.get("up") and n.get("ollama_port")]
        tag_results = await asyncio.gather(
            *[_get_pulled_models(session, f"http://{n['ip']}:{n['ollama_port']}")
              for n in up_nodes],
            return_exceptions=True,
        )

    pulled_by_node = {}
    for node, tags in zip(up_nodes, tag_results):
        pulled_by_node[node["name"]] = tags if isinstance(tags, set) else set()

    # SQLite is blocking. Reading it inline here stalled the whole event loop —
    # three separate connections per Kin, on every dashboard poll.
    stats = await asyncio.gather(
        *[asyncio.to_thread(_kin_db_stats, k.get("db")) for k in KIN]
    )

    kin_out = []
    for kin, (count, last_ts, latest) in zip(KIN, stats):
        node_up = next((n["up"] for n in nodes_out if n["name"] == kin.get("node")), False)
        pulled  = pulled_by_node.get(kin.get("node"), set())
        kin_out.append({
            **kin,
            "thought_count":  count,
            "last_active":    _time_ago(last_ts),
            "latest_thought": latest,
            "node_up":        node_up,
            "model_ready":    node_up and _model_in_set(kin.get("model", ""), pulled),
        })

    return {"nodes": nodes_out, "kin": kin_out}


def _record_conversation(kin: dict, user_message: str, reply: str):
    """Persist one exchange to the Kin's own thoughts DB.

    Until now nothing anywhere stored a conversation. History lived only in the
    browser tab, so closing it erased every exchange the Kin had ever had — the
    precise opposite of what this product promises. Stored under mode
    'conversation' so it never pollutes the wander feeds, which filter on
    mode LIKE 'wander%'. Blocking; call through asyncio.to_thread.
    """
    db = kin.get("db") or str(
        Path.home() / ".local/share/echo_bloom/kin" / kin["name"].lower() / "thoughts.db"
    )
    db = os.path.expanduser(db)
    conn = None
    try:
        Path(db).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db, timeout=5)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS thoughts (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                mode      TEXT,
                timestamp TEXT,
                prompt    TEXT,
                thought   TEXT
            )
        """)
        conn.execute(
            "INSERT INTO thoughts (mode, timestamp, prompt, thought) VALUES (?, ?, ?, ?)",
            ("conversation",
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             user_message[:4000],
             reply[:8000]),
        )
        conn.commit()
    except Exception:
        log.exception("could not record conversation for %s", kin.get("name"))
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _owner_name() -> str:
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        return cfg.get("owner", {}).get("name", "") or ""
    except Exception:
        return ""


async def stream_chat(kin_name, message, history=None):
    """
    Stream a chat response from a Kin via Ollama.
    Yields text chunks as they arrive.
    Injects memory context from kin_memory if available.
    """
    import sys
    kin = KIN_BY_NAME.get(kin_name)
    if not kin:
        yield f"Unknown Kin: {kin_name}"
        return

    # Load memory context from the installed scripts dir only. ~/Desktop used
    # to be a fallback — that meant any kin_memory.py a user left on their
    # Desktop was imported and executed inside the server on first chat.
    system_ctx = ""
    _scripts = str(Path.home() / ".local/share/echo_bloom/scripts")
    if _scripts not in sys.path:
        sys.path.insert(0, _scripts)
    try:
        from kin_memory import get_context
    except Exception as e:
        log.warning("kin_memory unavailable — chatting without memory context: %s", e)
        get_context = None

    if get_context is not None:
        try:
            # get_context does blocking HTTP (vault, embeddings, Qdrant) whose
            # timeouts total ~28s. Called inline it froze every other request in
            # the app for the duration. Off the event loop it goes.
            system_ctx = await asyncio.to_thread(
                get_context, kin_name, message,
                wander_limit=2, vault_limit=3, db_path=kin.get("db"),
            )
        except Exception as e:
            log.warning("memory context failed for %s: %s", kin_name, e)

    owner = _owner_name()
    caller = f"{owner} is" if owner else "Someone is"
    system_base = (
        f"You are {kin_name}, an AI entity with a home on this machine. "
        f"{caller} speaking with you directly. "
        f"Be yourself."
    )
    if kin.get("system_prompt"):
        system_base = kin["system_prompt"]
    if system_ctx:
        system_base += f"\n\n{system_ctx}"

    messages = [{"role": "system", "content": system_base}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": message})

    reply_parts: list[str] = []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{kin['host']}/api/chat",
                json={
                    "model":    kin["model"],
                    "messages": messages,
                    "stream":   True,
                    # 4096 could not hold the system prompt plus injected memory
                # plus history — Ollama truncated from the front, dropping the
                # memory first.
                "options":  {"temperature": 0.85, "num_ctx": 8192},
                },
                # NOT total=. This is a STREAM: `total` caps the whole
                # response, so a large model writing a long answer gets cut
                # off mid-sentence no matter how healthily it is streaming.
                # Don asked Coda (32.8B) an open question on 2026-08-24 and
                # got "[Connection error: ]" at exactly 120s, twice, while
                # Ollama was answering fine.
                #
                # sock_read is the right shape: it fires only when the socket
                # goes QUIET for that long. Tokens arriving = healthy, however
                # long the answer runs. Silence = a real hang.
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=15,
                                              sock_read=180),
            ) as resp:
                import json
                async for line in resp.content:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        # Ollama reports failures (model not pulled, OOM) as an
                        # error line that parses fine — swallowing it rendered
                        # an empty assistant bubble with no explanation.
                        if data.get("error"):
                            log.warning("ollama error for %s: %s",
                                        kin_name, data["error"])
                            yield f"\n[{data['error']}]"
                            return
                        chunk = data.get("message", {}).get("content", "")
                        if chunk:
                            reply_parts.append(chunk)
                            yield chunk
                        if data.get("done"):
                            break
                    except Exception:
                        continue
    except asyncio.TimeoutError:
        # str(asyncio.TimeoutError()) is the EMPTY STRING, so the old handler
        # rendered "[Connection error: ]" -- a message that names the wrong
        # cause and then says nothing about it. The connection was fine; the
        # model went quiet.
        log.warning("chat stream timed out for %s at %s (no tokens for 180s)",
                    kin_name, kin.get("host"))
        yield (f"\n[{kin_name} stopped responding partway through. The model is "
               f"probably still loading or the machine is busy -- the connection "
               f"itself is fine. Try again in a moment.]")
    except Exception as e:
        detail = str(e) or type(e).__name__      # never render an empty reason
        log.warning("chat stream failed for %s at %s: %s",
                    kin_name, kin.get("host"), detail)
        yield f"\n[Could not reach {kin_name}: {detail}]"

    reply = "".join(reply_parts).strip()
    if reply:
        await asyncio.to_thread(_record_conversation, kin, message, reply)
