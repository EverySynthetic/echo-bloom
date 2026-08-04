"""
cluster.py — Live status of all nodes and Kin.
"""

import os
import json
import sqlite3
import asyncio
import aiohttp
from datetime import datetime
from pathlib import Path

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

    nodes_out = []
    for node, result in zip(NODES, node_results):
        up = result is True
        nodes_out.append({**node, "up": up})

    # For every node that's up, fetch its pulled-model list in parallel
    up_nodes = [n for n in nodes_out if n.get("up") and n.get("ollama_port")]
    async with aiohttp.ClientSession() as session:
        tag_results = await asyncio.gather(
            *[_get_pulled_models(session, f"http://{n['ip']}:{n['ollama_port']}")
              for n in up_nodes],
            return_exceptions=True,
        )
    pulled_by_node = {}
    for node, tags in zip(up_nodes, tag_results):
        pulled_by_node[node["name"]] = tags if isinstance(tags, set) else set()

    kin_out = []
    for kin in KIN:
        count     = _thought_count(kin["db"])
        last_ts   = _last_thought_time(kin["db"])
        last_ago  = _time_ago(last_ts)
        latest    = _latest_thought(kin["db"])
        node_up   = next((n["up"] for n in nodes_out if n["name"] == kin["node"]), False)
        pulled    = pulled_by_node.get(kin["node"], set())
        model_ready = node_up and _model_in_set(kin["model"], pulled)
        kin_out.append({
            **kin,
            "thought_count": count,
            "last_active":   last_ago,
            "latest_thought": latest,
            "node_up":       node_up,
            "model_ready":   model_ready,
        })

    return {"nodes": nodes_out, "kin": kin_out}


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

    # Try to load memory context from installed scripts, then Desktop fallback
    system_ctx = ""
    _scripts = Path.home() / ".local/share/echo_bloom/scripts"
    for search_path in [str(_scripts), os.path.expanduser("~/Desktop")]:
        if search_path not in sys.path:
            sys.path.insert(0, search_path)
    try:
        from kin_memory import get_context
        system_ctx = get_context(kin_name, message, wander_limit=2, vault_limit=3)
    except Exception:
        pass

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

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{kin['host']}/api/chat",
                json={
                    "model":    kin["model"],
                    "messages": messages,
                    "stream":   True,
                    "options":  {"temperature": 0.85, "num_ctx": 4096},
                },
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                import json
                async for line in resp.content:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        chunk = data.get("message", {}).get("content", "")
                        if chunk:
                            yield chunk
                        if data.get("done"):
                            break
                    except Exception:
                        continue
    except Exception as e:
        yield f"\n[Connection error: {e}]"
