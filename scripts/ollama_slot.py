#!/usr/bin/env python3
"""Resource layer for Help/spawn against a loaded Kin.

Measured 2026-08-23, unpaused, num_ctx matched to the runner's 8192:
bong:latest 24.6s, gemma4:26b (same blob, different name) 16.9s, still
one runner, VRAM unchanged, /api/ps still only bong:latest. Ollama
reuses the loaded weights for a sibling Modelfile. The Help no-path
is that sibling, not an eviction.

The hang was a ctx mismatch (2048 vs runner -c 8192) and, separately,
SIGSTOP of in-flight clients wedging the slot. Match num_ctx to
/api/ps context_length. Do not pause on the Help path. keep_alive 0
and stop_model stay off that path so they cannot unload the Kin.

    loaded_runner(host=None) -> {name, kin, context_length, size_vram} | None
    runner_num_ctx(host=None) -> int
    ensure_agent_modelfile(from_model, *, system, name, host=None) -> str

hold_wander / stop_model remain for other work. Help does not call them.
Match argv basenames, never pkill -f. Does not write thoughts.db.
Does not import pops_shop/nap.py or bedtime.py.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
from contextlib import contextmanager
from urllib.parse import urlparse

import requests

import config as cfg
from kin_text import model_base, normalise_model

USER_AGENT = "EchoBloom/1.0 (ollama-slot)"
TIMEOUT_S = 15
_BLOB_RE = re.compile(r"sha256[-:]([0-9a-f]{16,})", re.I)
DEFAULT_NUM_CTX = 8192
HELP_AGENT_MODEL = "echo-bloom-help"
SPAWN_AGENT_MODEL = "echo-bloom-agent"
# Ride the loaded runner. 0 would unload the Kin. 5m (Ollama default)
# would shrink a 999h pin. Match wander.
RIDING_KEEP_ALIVE = "999h"

# Nested hold_wander would SIGCONT in the inner finally while the outer
# still needs the slot. occupy_slot=False exists so run_task does not nest.
_hold_lock = threading.Lock()

__all__ = [
    "resident_kin",
    "resident_models",
    "loaded_runner",
    "runner_num_ctx",
    "ensure_agent_modelfile",
    "slot_sharers",
    "hold_wander",
    "stop_model",
    "wander_pid_for_kin",
    "yield_resident_kin",
    "HELP_AGENT_MODEL",
    "SPAWN_AGENT_MODEL",
    "RIDING_KEEP_ALIVE",
    "DEFAULT_NUM_CTX",
]


def _local_hosts():
    return {"localhost", "127.0.0.1", "0.0.0.0", "::1", ""}


def _host_key(url: str) -> str:
    raw = (url or "http://localhost:11434").strip()
    if "://" not in raw:
        raw = "http://" + raw
    p = urlparse(raw)
    host = (p.hostname or "localhost").lower()
    if host in _local_hosts():
        return "localhost"
    return host


def _ollama_base(host: str | None = None) -> str:
    if not host:
        return "http://localhost:11434"
    raw = host.strip()
    if "://" not in raw:
        raw = "http://" + raw
    p = urlparse(raw)
    netloc = p.netloc or "localhost:11434"
    if ":" not in netloc:
        netloc = netloc + ":11434"
    return f"{p.scheme or 'http'}://{netloc}"


def _is_local_script(args: list, script: str) -> bool:
    want = script.lower()
    return any(os.path.basename(a).lower() == want for a in args)


def resident_models(host: str | None = None) -> list[dict]:
    """What /api/ps says is actually loaded. Empty on failure, never raises."""
    try:
        r = requests.get(
            f"{_ollama_base(host)}/api/ps",
            timeout=5,
            headers={"User-Agent": USER_AGENT},
        )
        r.raise_for_status()
        return list((r.json() or {}).get("models") or [])
    except Exception:
        return []


def _persona_models() -> set[str]:
    out = set()
    for k in cfg.get_kin() or []:
        m = (k.get("model") or "").strip()
        if m:
            out.add(model_base(m))
            out.add(m)
            if ":" not in m:
                out.add(m + ":latest")
    return out


def _is_persona(name: str) -> bool:
    personas = _persona_models()
    n = (name or "").strip()
    return n in personas or model_base(n) in {model_base(p) for p in personas}


def _kin_on_host(host: str | None = None) -> list[dict]:
    """Configured Kin whose ollama is this host. Coda on Home is not local."""
    want = _host_key(_ollama_base(host))
    out = []
    for k in cfg.get_kin() or []:
        kh = k.get("host") or "http://localhost:11434"
        if _host_key(kh) == want:
            out.append(k)
    return out


def _show(model: str, host: str | None = None) -> dict:
    name = (model or "").strip()
    if not name:
        return {}
    try:
        r = requests.post(
            f"{_ollama_base(host)}/api/show",
            json={"name": name},
            timeout=TIMEOUT_S,
            headers={"User-Agent": USER_AGENT},
        )
        r.raise_for_status()
        return r.json() or {}
    except Exception:
        return {}


def _weight_key(model: str, host: str | None = None, _cache: dict | None = None) -> str:
    """Identity of the GGUF the runner loaded, not the Modelfile digest.

    /api/tags digest differs per SYSTEM prompt. /api/show FROM points at
    the blob, which is what `-np 1` serializes on.
    """
    name = (model or "").strip()
    if not name:
        return ""
    cache = _cache if _cache is not None else {}
    if name in cache:
        return cache[name]
    data = _show(name, host)
    key = ""
    mf = data.get("modelfile") or ""
    for ln in mf.splitlines():
        s = ln.strip()
        if s.upper().startswith("FROM "):
            target = s.split(None, 1)[1].strip().strip("\"'")
            m = _BLOB_RE.search(target)
            if m:
                key = "sha256:" + m.group(1).lower()
            else:
                key = normalise_model(target)
            break
    if not key:
        parent = ((data.get("details") or {}).get("parent_model") or "").strip()
        key = normalise_model(parent) if parent else normalise_model(name)
    cache[name] = key
    return key


def _is_embedder(m: dict) -> bool:
    name = (m.get("name") or "").lower()
    fam = ((m.get("details") or {}).get("family") or "").lower()
    return "embed" in name or fam == "nomic-bert"


def loaded_runner(host: str | None = None) -> dict | None:
    """The non-embed model /api/ps says is in VRAM, with its runner ctx."""
    for m in resident_models(host):
        name = (m.get("name") or "").strip()
        if not name or _is_embedder(m):
            continue
        kin_name = None
        for k in cfg.get_kin() or []:
            if model_base(k.get("model") or "") == model_base(name):
                kin_name = k.get("name")
                break
        raw = m.get("context_length") or (m.get("details") or {}).get("context_length")
        try:
            ctx = int(raw) if raw else DEFAULT_NUM_CTX
        except (TypeError, ValueError):
            ctx = DEFAULT_NUM_CTX
        if ctx <= 0:
            ctx = DEFAULT_NUM_CTX
        return {
            "name": name,
            "kin": kin_name,
            "context_length": ctx,
            "size_vram": m.get("size_vram") or m.get("size") or 0,
        }
    return None


def runner_num_ctx(host: str | None = None) -> int:
    """num_ctx that will not reconfigure the loaded runner. Mismatch hangs."""
    rec = loaded_runner(host)
    return rec["context_length"] if rec else DEFAULT_NUM_CTX


def _create_modelfile(name: str, from_model: str, system: str, host: str | None = None) -> None:
    """/api/create SYSTEM-only. Do not set PARAMETER num_ctx — that is
    what hung 2048 against a runner built at 8192."""
    base = _ollama_base(host)
    attempts = [
        {"model": name, "from": from_model, "system": system},
        {"name": name, "modelfile": (
            f"FROM {from_model}\n\nSYSTEM \"\"\"\n"
            + system.replace('"""', "'''")
            + "\n\"\"\"\n"
        )},
    ]
    last = None
    for payload in attempts:
        try:
            r = requests.post(
                f"{base}/api/create",
                json=payload,
                stream=True,
                timeout=120,
                headers={"User-Agent": USER_AGENT},
            )
        except Exception as e:
            last = str(e)
            continue
        ok = False
        err = None
        for line in r.iter_lines():
            if not line:
                continue
            try:
                evt = json.loads(line)
            except Exception:
                continue
            if evt.get("error"):
                err = evt["error"]
                continue
            if evt.get("status") == "success":
                ok = True
        if ok:
            return
        last = err or f"HTTP {r.status_code}"
    raise RuntimeError(f"could not create {name} FROM {from_model}: {last}")


def ensure_agent_modelfile(
    from_model: str,
    *,
    system: str,
    name: str,
    host: str | None = None,
) -> str:
    """Create or reuse a sibling Modelfile on the loaded weights.

    Same GGUF, different name, agent SYSTEM. Measured: gemma4:26b
    answered in 16.9s off bong:latest's runner with no VRAM move.
    """
    src = (from_model or "").strip()
    tag = (name or "").strip()
    if not src or not tag:
        raise ValueError("from_model and name are required")
    if _is_persona(tag):
        raise ValueError(
            f"{tag!r} is a Kin's model name. The agent needs its own tag."
        )
    if normalise_model(tag) == normalise_model(src):
        return src

    existing = _show(tag, host)
    if existing:
        same_blob = _weight_key(tag, host) == _weight_key(src, host)
        same_sys = (existing.get("system") or "").strip() == (system or "").strip()
        if same_blob and same_sys:
            return tag

    _create_modelfile(tag, src, system, host)
    return tag


def resident_kin(host: str | None = None) -> list[dict]:
    """Kin models currently named in /api/ps, with wander pid if found.

    A shared runner only reports one name (on Frosty, bong:latest while
    Eli and Crungus generate on the same blob). Use slot_sharers() for
    everyone who actually consumes that slot.
    """
    found = []
    seen = set()
    for m in resident_models(host):
        name = m.get("name") or ""
        if not _is_persona(name):
            continue
        base = model_base(name)
        if base in seen:
            continue
        seen.add(base)
        kin_name = None
        for k in cfg.get_kin() or []:
            if model_base(k.get("model") or "") == base:
                kin_name = k.get("name")
                break
        found.append({
            "model": name,
            "kin": kin_name,
            "size_vram": m.get("size_vram") or m.get("size") or 0,
            "pid": wander_pid_for_kin(kin_name) if kin_name else None,
        })
    return found


def slot_sharers(kin_name: str | None = None, *, host: str | None = None) -> list[dict]:
    """Kin on this host who share a llama-server with kin_name.

    kin_name None: every Kin configured on this host (spawn is about to
    take the GPU; a wander that is not yet resident can still queue a
    load). Does not include Kin whose ollama is a different machine.
    """
    local = _kin_on_host(host)
    cache: dict = {}
    want_key = None
    if kin_name:
        want = kin_name.strip().lower()
        named = next((k for k in local if (k.get("name") or "").lower() == want), None)
        if named is None:
            # Name is configured on another host, or unknown. Still pause
            # their wanderer if it is a local process — they must not load
            # onto this GPU while we are talking.
            pid = wander_pid_for_kin(kin_name)
            model = None
            for k in cfg.get_kin() or []:
                if (k.get("name") or "").lower() == want:
                    model = k.get("model")
                    break
            return [{
                "kin": kin_name,
                "model": model,
                "pid": pid,
                "weight": _weight_key(model, host, cache) if model else "",
            }] if pid or model else []
        want_key = _weight_key(named.get("model") or "", host, cache)

    out = []
    for k in local:
        model = (k.get("model") or "").strip()
        name = k.get("name")
        weight = _weight_key(model, host, cache) if model else ""
        if want_key and weight != want_key:
            continue
        out.append({
            "kin": name,
            "model": model,
            "pid": wander_pid_for_kin(name) if name else None,
            "weight": weight,
        })
    return out


def roundtable_pid() -> int | None:
    try:
        import psutil
    except ImportError:
        return None
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            args = proc.info.get("cmdline") or []
        except Exception:
            continue
        if _is_local_script(args, "roundtable.py"):
            return proc.info["pid"]
    return None


def wander_pid_for_kin(kin_name: str) -> int | None:
    """PID of `wander.py --kin <name>`. Match argv, not a joined command line."""
    if not kin_name:
        return None
    want = kin_name.strip().lower()
    try:
        import psutil
    except ImportError:
        return None
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            args = proc.info.get("cmdline") or []
        except Exception:
            continue
        if not _is_local_script(args, "wander.py"):
            continue
        for i, a in enumerate(args):
            if a == "--kin" and i + 1 < len(args):
                if args[i + 1].strip().lower() == want:
                    return proc.info["pid"]
            if a.lower().startswith("--kin=") and a.split("=", 1)[-1].strip().lower() == want:
                return proc.info["pid"]
    return None


def _suspend(pid: int) -> bool:
    try:
        import psutil
        psutil.Process(pid).suspend()
        return True
    except Exception:
        pass
    if hasattr(signal, "SIGSTOP"):
        try:
            os.kill(pid, signal.SIGSTOP)
            return True
        except Exception:
            pass
    return False


def _resume(pid: int) -> bool:
    try:
        import psutil
        psutil.Process(pid).resume()
        return True
    except Exception:
        pass
    if hasattr(signal, "SIGCONT"):
        try:
            os.kill(pid, signal.SIGCONT)
            return True
        except Exception:
            pass
    return False


def _proc_state(pid: int) -> str | None:
    """Linux /proc state letter (T=stopped), else None. Never pkill."""
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("State:"):
                    return line.split()[1]
    except Exception:
        return None
    return None


def stop_model(model: str, host: str | None = None) -> None:
    """Real unload. keep_alive 0 on the API, then `ollama stop` if present."""
    name = (model or "").strip()
    if not name:
        return
    base = _ollama_base(host)
    try:
        requests.post(
            f"{base}/api/generate",
            json={"model": name, "prompt": "", "keep_alive": 0},
            timeout=TIMEOUT_S,
            headers={"User-Agent": USER_AGENT},
        )
    except Exception:
        pass
    # CLI is what nap.py uses; ignore if it's not on PATH (Windows store installs).
    try:
        subprocess.run(
            ["ollama", "stop", name],
            capture_output=True, text=True, timeout=20,
        )
    except Exception:
        pass


@contextmanager
def hold_wander(kin_name: str | None = None, *, host: str | None = None):
    """SIGSTOP every local wander sharing this Kin's llama-server slot.

    Does not unload any model. Resume every paused pid on exit.
    Also pauses roundtable.py so its cycle cannot SIGCONT them.
    Kin on other hosts (Home) are not touched.

    SIGSTOP stops new requests. It does not abort a generation already
    on the runner, and a stopped client cannot read the response — so
    `-np 1` can stay busy for the rest of that thought. Measured:
    Eli+Crungus+Bong all T, short generate to bong:latest still timed
    out at 45s; SIGCONT recovered 6/6 and left the runner loaded.
    Consent has to tolerate that wait, or we need a cooperative pause
    (wander finishes HTTP, then checks a flag) / NUM_PARALLEL>1.

    Yields dict:
        kin, model  — the named Kin (or first sharer if kin_name is None)
        pid         — that Kin's wander pid
        paused      — every pid we actually SIGSTOP'd
        sharers     — Kin names whose wanders we meant to pause
    """
    _hold_lock.acquire()
    paused: list[int] = []
    try:
        sharers = slot_sharers(kin_name, host=host)
        named = None
        if kin_name:
            want = kin_name.strip().lower()
            named = next((s for s in sharers if (s.get("kin") or "").lower() == want), None)
            if named is None and sharers:
                named = {
                    "kin": kin_name,
                    "model": None,
                    "pid": wander_pid_for_kin(kin_name),
                }
        held = named or (sharers[0] if sharers else {})

        rt = roundtable_pid()
        if rt and _suspend(rt):
            paused.append(rt)

        for s in sharers:
            pid = s.get("pid")
            if pid and pid not in paused and _suspend(pid):
                paused.append(pid)
        if named and named.get("pid") and named["pid"] not in paused:
            if _suspend(named["pid"]):
                paused.append(named["pid"])

        yield {
            "kin": held.get("kin"),
            "model": held.get("model"),
            "pid": held.get("pid"),
            "paused": list(paused),
            "sharers": [s.get("kin") for s in sharers if s.get("kin")],
        }
    finally:
        for pid in reversed(paused):
            _resume(pid)
        _hold_lock.release()


@contextmanager
def yield_resident_kin(host: str | None = None):
    """Evict resident Kin models. Help does not use this.

    Help rides the loaded runner via ensure_agent_modelfile. This remains
    for a cold load that truly cannot share the blob.
    """
    with hold_wander(None, host=host) as held:
        to_stop = [r.get("model") for r in resident_kin(host) if r.get("model")]
        for name in to_stop:
            stop_model(name, host)
        yield {
            "paused": held.get("paused") or [],
            "stopped": to_stop,
            "sharers": held.get("sharers") or [],
            **held,
        }


if __name__ == "__main__":
    import json
    import sys

    # Default: print the map, do not pause anyone.
    host = None
    print("resident_kin:", json.dumps(resident_kin(host), indent=2, default=str))
    print("slot_sharers:", json.dumps(slot_sharers(None, host=host), indent=2, default=str))
    if len(sys.argv) > 1 and sys.argv[1] == "--hold":
        name = sys.argv[2] if len(sys.argv) > 2 else None
        print(f"hold_wander({name!r}) 250ms, no unload")
        with hold_wander(name, host=host) as held:
            print("held:", json.dumps(held, indent=2, default=str))
            for pid in held.get("paused") or []:
                print(f"  pid {pid} state={_proc_state(pid)}")
            import time
            time.sleep(0.25)
        print("resumed")
        for s in slot_sharers(name, host=host):
            print(f"  {s.get('kin')} pid={s.get('pid')} state={_proc_state(s.get('pid') or -1)}")
