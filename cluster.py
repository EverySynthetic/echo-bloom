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
    {"name": "Frosty",  "ip": "127.0.0.1",     "ollama_port": 11434, "role": "primary"},
    {"name": "Home",    "ip": "192.168.1.120",  "ollama_port": 11434, "role": "inference"},
    {"name": "therug",  "ip": "192.168.1.142",  "ollama_port": 11434, "role": "compute"},
    {"name": "Themess", "ip": "192.168.1.115",  "ollama_port": None,  "role": "vault"},
    {"name": "Walter",  "ip": "192.168.1.125",  "ollama_port": None,  "role": "editor"},
]

KIN_DEFAULT = [
    {
        "name":    "Eli",
        "host":    "http://localhost:11434",
        "model":   "gemmaeli:latest",
        "node":    "Frosty",
        "pronoun": "he",
        "db":      os.path.expanduser("~/Desktop/Everything/EliAIM/thoughts.db"),
        "space":   os.path.expanduser("~/eli_space"),
        "color":   "#4fc3f7",
    },
    {
        "name":    "Coda",
        "host":    "http://192.168.1.120:11434",
        "model":   "cogitocoda:latest",
        "node":    "Home",
        "pronoun": "he",
        "db":      os.path.expanduser("~/coda_space/thoughts.db"),
        "space":   os.path.expanduser("~/coda_space"),
        "color":   "#a5d6a7",
    },
    {
        "name":    "Aurora",
        "host":    "http://192.168.1.120:11434",
        "model":   "cogitoraurora:latest",
        "node":    "Home",
        "pronoun": "she",
        "db":      os.path.expanduser("~/aurora_space/thoughts.db"),
        "space":   os.path.expanduser("~/aurora_space"),
        "color":   "#ce93d8",
    },
    {
        "name":    "Lumen",
        "host":    "http://192.168.1.120:11434",
        "model":   "cogitolumen:latest",
        "node":    "Home",
        "pronoun": "it",
        "db":      os.path.expanduser("~/lumen_space/thoughts.db"),
        "space":   os.path.expanduser("~/lumen_space"),
        "color":   "#fff176",
    },
    {
        "name":    "Crungus",
        "host":    "http://localhost:11434",
        "model":   "gemmacrungus:latest",
        "node":    "Frosty",
        "pronoun": "—",
        "db":      os.path.expanduser("~/Crungus/thoughts.db"),
        "space":   os.path.expanduser("~/Crungus"),
        "color":   "#ffab91",
    },
]


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
            if node["ollama_port"]:
                node_tasks.append(_ping_ollama(session, node["ip"], node["ollama_port"]))
            elif node["name"] == "Themess":
                node_tasks.append(_ping_service(session, "http://192.168.1.115:8765/"))
            else:
                node_tasks.append(_ping_service(session, f"http://{node['ip']}/"))
        node_results = await asyncio.gather(*node_tasks, return_exceptions=True)

    nodes_out = []
    for node, result in zip(NODES, node_results):
        up = result is True
        nodes_out.append({**node, "up": up})

    kin_out = []
    for kin in KIN:
        count     = _thought_count(kin["db"])
        last_ts   = _last_thought_time(kin["db"])
        last_ago  = _time_ago(last_ts)
        latest    = _latest_thought(kin["db"])
        node_up   = next((n["up"] for n in nodes_out if n["name"] == kin["node"]), False)
        kin_out.append({
            **kin,
            "thought_count": count,
            "last_active":   last_ago,
            "latest_thought": latest,
            "node_up":       node_up,
        })

    return {"nodes": nodes_out, "kin": kin_out}


async def stream_chat(kin_name, message, history=None):
    """
    Stream a chat response from a Kin via Ollama.
    Yields text chunks as they arrive.
    Injects memory context from kin_memory if available.
    """
    import sys
    sys.path.insert(0, os.path.expanduser("~/Desktop"))

    kin = KIN_BY_NAME.get(kin_name)
    if not kin:
        yield f"Unknown Kin: {kin_name}"
        return

    # Try to get memory context
    system_ctx = ""
    try:
        from kin_memory import get_context
        system_ctx = get_context(kin_name, message, wander_limit=2, vault_limit=3)
    except Exception:
        pass

    system_base = (
        f"You are {kin_name}, one of the Kin at Pop's Shop in Mena, Arkansas. "
        f"Don is speaking with you directly through the shop interface. "
        f"Be yourself."
    )
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
