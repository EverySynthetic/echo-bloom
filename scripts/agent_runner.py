#!/usr/bin/env python3
"""
agent_runner.py — Simple reusable runner for Agents.

Runs a task against local Ollama, choosing the most capable general-purpose
model actually installed. That sentence used to say "qwen3:4b or first
available small model" and the fallback did not exist -- the code went straight
to qwen3:4b and failed outright if it was not pulled.
Optional api_key enables external API fallback (XAI, OpenAI, Anthropic, etc.).

Called by /api/agent/spawn. On completion the caller should call
kin_presence.record_thought_return("agent-default", result) and heartbeat().
"""

import sys
import json
import asyncio
import aiohttp
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))           # scripts/ (config)
sys.path.insert(0, str(_HERE.parent))    # repo root (kin_presence)
import config as cfg
from kin_presence import heartbeat

# What we suggest pulling when nothing suitable is installed. Not a hard
# default any more: the picker prefers whatever capable model is already here.
SUGGESTED_MODEL = "qwen3:8b"
DEFAULT_MODEL   = SUGGESTED_MODEL      # kept: main.py imports this name
AGENT_NAME      = "agent-default"

# Below this the answers stop being worth reading for anything factual. A 4B
# model asked for Hunter S. Thompson's last book returned a fabricated title, a
# fabricated publisher and a death date thirteen years wrong, formatted as
# confidently as a correct answer would have been. Small models are fine for
# summarising text you hand them and not fine for recall.
MIN_USEFUL_PARAMS_B = 7.0

# Generous on purpose. A cold 27B load is minutes on a modest box, and the
# first agent run on any machine is always a cold load.
LOCAL_TIMEOUT_S = 600

# A confident wrong answer is worse than an admitted gap, because it is only
# discovered by someone who already knew.
AGENT_SYSTEM = (
    "Answer only from what you actually know. If you are not certain, say so "
    "plainly and say what would need to be checked. Never invent titles, "
    "dates, names, numbers, publishers or sources -- if you cannot recall one, "
    "say you cannot. A short answer that admits a gap is a correct answer; a "
    "confident answer that turns out to be wrong is a failure."
)


class ModelUnavailable(RuntimeError):
    """No usable local model. Carries what the UI needs to offer a fix."""

    def __init__(self, requested, installed, suggestion):
        self.requested  = requested
        self.installed  = installed
        self.suggestion = suggestion
        if requested:
            msg = (f"The model '{requested}' is not installed on this machine. "
                   f"Install it with:  ollama pull {requested}")
        else:
            msg = ("No model capable enough for agent work is installed. "
                   f"Install one with:  ollama pull {suggestion}")
        super().__init__(msg)


def _params_b(model: dict) -> float:
    """Parameter count in billions, from Ollama's own metadata."""
    raw = str((model.get("details") or {}).get("parameter_size") or "").strip().upper()
    try:
        if raw.endswith("B"):
            return float(raw[:-1])
        if raw.endswith("M"):
            return float(raw[:-1]) / 1000.0
    except ValueError:
        pass
    return 0.0


# Substrings that mark a model as trained for one job rather than general use.
# Matched on the name, since Ollama's `family` does not distinguish a coder
# from its general sibling (qwen3-coder reports family "qwen3moe").
_SPECIALIST_MARKERS = ("coder", "-code", "code-", "vl", "vision", "llava",
                       "moondream", "embed")


def _is_general(model: dict) -> bool:
    name = model.get("name", "").lower()
    return not any(mark in name for mark in _SPECIALIST_MARKERS)


def _persona_models() -> set:
    """Models belonging to a configured Kin.

    These carry a baked-in identity and a system prompt of their own. Handing
    one an agent task makes the Kin answer as itself, which is both a worse
    answer and a small violation of what that model is for.
    """
    try:
        kin = (cfg.load() or {}).get("kin") or []
        return {(k.get("model") or "").strip() for k in kin if k.get("model")}
    except Exception:
        return set()


async def list_installed(session) -> list[dict]:
    async with session.get("http://localhost:11434/api/tags",
                           timeout=aiohttp.ClientTimeout(total=10)) as resp:
        return ((await resp.json()) or {}).get("models") or []


def choose_model(installed: list[dict], requested: str | None):
    """(model_name, note). Raises ModelUnavailable when nothing will do.

    Checked BEFORE the task runs. Previously a missing model was discovered by
    Ollama mid-request and surfaced to the user as its raw API string.
    """
    names = [m.get("name", "") for m in installed]
    if requested:
        if requested in names or f"{requested}:latest" in names:
            return requested, ""
        raise ModelUnavailable(requested, names, SUGGESTED_MODEL)

    personas = _persona_models()
    usable = [
        m for m in installed
        if m.get("name") not in personas
        and "embed" not in m.get("name", "").lower()
        and (m.get("details") or {}).get("family") != "nomic-bert"
    ]
    if not usable:
        raise ModelUnavailable(None, names, SUGGESTED_MODEL)

    # Rank general-purpose above specialist before ranking by size. Sorting on
    # size alone picked a 30B *coding* model to answer a question about a
    # writer's bibliography, purely because it was the largest thing installed.
    # Bigger is not better-for-this.
    usable.sort(key=lambda m: (_is_general(m), _params_b(m)), reverse=True)
    best = usable[0]
    size = _params_b(best)
    if size < MIN_USEFUL_PARAMS_B:
        note = (f"Using {best['name']} ({size:g}B) because it is the largest "
                f"installed. Models under {MIN_USEFUL_PARAMS_B:g}B invent "
                f"details when asked to recall facts — treat this answer as a "
                f"draft. `ollama pull {SUGGESTED_MODEL}` for something better.")
    else:
        note = ""
    return best["name"], note


async def run_task(task: str, model: str | None = None, api_key: str | None = None) -> str:
    """Run a task and return the model's response.

    Local Ollama is hard default. If api_key is provided, attempt external API
    (checks for xai, openai, anthropic patterns). Falls back to local on any error.
    Raises on failure — does not return error strings, and does not write the vault.
    The caller owns record_thought_return / resting heartbeat.
    """
    # Try external API first if key is provided
    if api_key:
        try:
            result = await _run_external(task, model or SUGGESTED_MODEL, api_key)
            heartbeat(AGENT_NAME, "completed-external")
            return result
        except Exception as e:
            print(f"[agent] External API failed, falling back to local: {e}")

    # Local Ollama. Decide which model BEFORE spending 120 seconds finding out
    # Ollama does not have it.
    async with aiohttp.ClientSession() as session:
        try:
            installed = await list_installed(session)
        except Exception as e:
            heartbeat(AGENT_NAME, "failed")
            raise RuntimeError(
                "Could not reach Ollama at localhost:11434. Is it running?"
            ) from e

    try:
        chosen, note = choose_model(installed, model)
    except ModelUnavailable:
        heartbeat(AGENT_NAME, "failed")
        raise

    try:
        result = await _run_local_ollama(task, chosen)
        heartbeat(AGENT_NAME, "completed-local")
        if note:
            result = f"{result}\n\n---\n{note}"
        return result
    except asyncio.TimeoutError as e:
        # str(asyncio.TimeoutError) is the empty string, so the obvious
        # f"...: {e}" produced "Local Ollama error:" and stopped. Naming the
        # model and the budget is the whole diagnosis.
        heartbeat(AGENT_NAME, "failed")
        raise RuntimeError(
            f"{chosen} did not answer within {LOCAL_TIMEOUT_S}s. A large model "
            f"loading for the first time can take minutes — try again once it "
            f"is warm, or pick a smaller model."
        ) from e
    except Exception as e:
        heartbeat(AGENT_NAME, "failed")
        raise RuntimeError(f"Local Ollama error: {e or type(e).__name__}") from e


async def _run_local_ollama(task: str, model: str) -> str:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "http://localhost:11434/api/chat",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": AGENT_SYSTEM},
                    {"role": "user",   "content": task},
                ],
                "stream": False,
                "options": {"temperature": 0.7, "num_ctx": 8192},
            },
            # 120s was enough for a 4B model and is not enough for the
            # capable one the picker now prefers: a 27B cold load alone can
            # exceed it, so raising the model quality without raising this
            # turned every first run into a timeout. The rest of the codebase
            # uses 300s against the same endpoint for the same reason.
            timeout=aiohttp.ClientTimeout(total=LOCAL_TIMEOUT_S),
        ) as resp:
            data = await resp.json()
            if "error" in data:
                raise RuntimeError(data["error"])
            return data.get("message", {}).get("content", "[no response]").strip()


async def _run_external(task: str, model: str, api_key: str) -> str:
    """Very basic external fallback. Detects provider from key prefix or env."""
    headers = {"Authorization": f"Bearer {api_key}"}
    if "xai" in api_key.lower() or "grok" in model.lower():
        url = "https://api.x.ai/v1/chat/completions"
        headers["Content-Type"] = "application/json"
        body = {
            "model": model or "grok-4",
            "messages": [{"role": "user", "content": task}],
            "temperature": 0.7,
        }
    elif api_key.startswith("sk-"):
        # Assume OpenAI-compatible (OpenAI, Groq, Together, etc.)
        url = "https://api.openai.com/v1/chat/completions"
        body = {
            "model": model or "gpt-4o-mini",
            "messages": [{"role": "user", "content": task}],
            "temperature": 0.7,
        }
    else:
        raise ValueError("Unsupported API key format")

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=body, headers=headers, timeout=60) as resp:
            data = await resp.json()
            if "error" in data:
                raise RuntimeError(str(data["error"]))
            return data.get("choices", [{}])[0].get("message", {}).get("content", "[no response]").strip()


if __name__ == "__main__":
    import asyncio
    if len(sys.argv) > 1:
        task = " ".join(sys.argv[1:])
        try:
            result = asyncio.run(run_task(task))
            print(result)
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print("Usage: python agent_runner.py 'your task here'")
        print("Default model: qwen3:4b (local Ollama)")
