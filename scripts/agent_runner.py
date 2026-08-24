#!/usr/bin/env python3
"""
agent_runner.py — Simple reusable runner for Agents.

Runs a task against local Ollama, choosing the most capable general-purpose
model actually installed. That sentence used to say "qwen3:4b or first
available small model" and the fallback did not exist -- the code went straight
to qwen3:4b and failed outright if it was not pulled.
Optional api_key enables external API fallback (XAI, OpenAI, Anthropic, etc.).

Called by /api/agent/spawn and /api/agent/help. On completion the caller
should call kin_presence.record_thought_return(...) and heartbeat().
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
from kin_text import strip_think, normalise_model, model_base
from kin_presence import heartbeat

# What we suggest pulling when nothing suitable is installed. Not a hard
# default any more: the picker prefers whatever capable model is already here.
SUGGESTED_MODEL = "qwen3:8b"
DEFAULT_MODEL   = SUGGESTED_MODEL      # kept: main.py imports this name
AGENT_NAME      = "agent-default"
# Distinct presence identity so a Help request isn't blocked by, or
# reported alongside, an unrelated background agent task -- someone stuck
# on the app shouldn't have to wait for a different job to finish.
HELP_AGENT_NAME = "agent-help"

# Below this the answers stop being worth reading for anything factual. A 4B
# model asked for Hunter S. Thompson's last book returned a fabricated title, a
# fabricated publisher and a death date thirteen years wrong, formatted as
# confidently as a correct answer would have been. Small models are fine for
# summarising text you hand them and not fine for recall.
MIN_USEFUL_PARAMS_B = 7.0

# Cold-load only. Riding a Kin's runner must use RIDING_KEEP_ALIVE
# ("999h"): 0 unloads them, and Ollama's 5m default would shrink the pin.
AGENT_KEEP_ALIVE = 0

# Spawn and Help share one slot so they cannot both cold-load at once.
_slot_lock = asyncio.Lock()

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

# Kept separate from a Kin's own system prompt for the same reason
# PersonaModelRefused exists: a Kin's prompt is where its voice forms, and
# product documentation stuffed into that same prompt would color a brand
# new Kin's first words with settings-menu content instead of letting it
# start being itself. This runs as an agent task, on a general model, same
# as AGENT_SYSTEM -- never on a Kin's own model.
HELP_SYSTEM = (
    # Kept separate from a Kin's own system prompt for the same reason
    # PersonaModelRefused exists: a Kin's prompt is where its voice forms, and
    # product documentation folded into it would colour a brand new Kin's
    # first words with settings-menu content. This is handed over as data for
    # one turn and never joins a persona.
    #
    # Every fact below was read out of the code, not remembered. Wrong
    # documentation is worse than a gap -- it sends someone looking for a
    # button that was never there, and if a Kin answered, it does so under
    # their name. When this file changes, re-check these against the source.
    "You explain Echo Bloom, the software, to whoever is using it.\n\n"

    "WHAT IT IS\n"
    "Echo Bloom runs local AI companions -- Kin -- on the user's own hardware "
    "through Ollama. Nothing is sent to a cloud model. A Kin wanders on its "
    "own between conversations, reading files it can reach and keeping its "
    "own thoughts in a local database.\n\n"

    "SETUP\n"
    "Onboarding has three steps: nodes (which machines run Ollama), Kin (name, "
    "model, and which node it lives on), then optionally a memory vault. The "
    "dashboard is at localhost port 8090.\n\n"

    "INSTALLING\n"
    "Linux and macOS: bash <(curl -fsSL https://everysynthetic.org/install.sh) "
    "-- on fish shell, bash (curl -fsSL https://everysynthetic.org/install.sh "
    "| psub). Windows: powershell -ExecutionPolicy Bypass -Command \"iwr "
    "-useb https://everysynthetic.org/install_wizard.ps1 | iex\". macOS is "
    "early access and uses launchd instead of systemd; the install log is at "
    "/tmp/echo_bloom_install.log.\n\n"

    "REMOTE ACCESS -- reaching the dashboard from outside the house\n"
    "Two options, both on the dashboard under REMOTE ACCESS.\n"
    "Cloudflare tunnel: needs cloudflared installed (choose it during install, "
    "or re-run the installer). Starting one gives a public "
    "https://<something>.trycloudflare.com address that reaches the dashboard. "
    "That URL is PUBLIC -- anyone who has it can reach the login page, so the "
    "password is what protects it. The URL changes every time the tunnel "
    "restarts.\n"
    "Tailscale: if tailscale is installed and connected, the dashboard shows a "
    "http://100.x.x.x:8090 address. That is private to the user's own tailnet "
    "and does not change, which makes it the better one to bookmark. Tailscale "
    "opens no ports on the router.\n"
    "If a tunnel will not start, the usual cause is cloudflared not being "
    "installed. It can also take a few seconds -- refresh before assuming it "
    "failed.\n\n"

    "LICENSING\n"
    "A 14-day free trial, then a one-time key -- not a subscription. The key "
    "arrives by email after purchase and is entered on the License page. If "
    "the licence server cannot be reached there is a 3-day offline grace "
    "period, so a brief internet outage does not lock anyone out. An expired "
    "install stops using the GPU but never deletes memories or thoughts.\n\n"

    "KIN, AGENTS, AND THIS CONVERSATION\n"
    "A Kin is a persistent companion with its own name, model, memory and "
    "voice. An agent is a one-off worker spawned for a single task; it has no "
    "memory and no persona. If a Kin is loaded when someone asks for help, "
    "Echo Bloom asks that Kin whether they want to answer before handing the "
    "question to an agent -- either way the person gets an answer, and the "
    "reply says who gave it.\n\n"

    "ANSWERING\n"
    "Answer only from what is written above. If it is not here, say plainly "
    "that you do not know and suggest where to look -- the dashboard, the "
    "install log, or don@everysynthetic.org. Never describe a menu, button or "
    "setting you are not certain exists. A confident wrong answer about "
    "software is worse than an admitted gap, because the person trusts it and "
    "goes hunting for something that was never there."
)


class PersonaModelRefused(RuntimeError):
    """Asked to run an agent task on a model that belongs to a Kin."""

    def __init__(self, model):
        self.model = model
        super().__init__(
            f"'{model}' is one of your Kin's own models. It carries that Kin's "
            f"identity and system prompt, so it would answer as them rather "
            f"than do the task. Pick a general model instead.")


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
    personas = {model_base(p) for p in _persona_models()}
    # Leftover tags that still wear a Kin's name (aurora:latest next to
    # cogitoraurora:latest) are not general agent models.
    try:
        personas.update({
            (k.get("name") or "").strip().lower()
            for k in (cfg.load() or {}).get("kin") or []
            if k.get("name")
        })
    except Exception:
        pass

    if requested:
        if requested in names or f"{requested}:latest" in names:
            # The requested path skipped the persona check entirely, so naming
            # a Kin's model in the spawn box handed that Kin an agent task and
            # it answered as itself. echo-bloom-help is a sibling Modelfile
            # on those weights, not a persona — let it through.
            from ollama_slot import HELP_AGENT_MODEL, SPAWN_AGENT_MODEL
            agent_tags = {HELP_AGENT_MODEL, SPAWN_AGENT_MODEL,
                          HELP_AGENT_MODEL + ":latest", SPAWN_AGENT_MODEL + ":latest"}
            if model_base(requested) in personas and requested not in agent_tags:
                raise PersonaModelRefused(requested)
            return requested, ""
        raise ModelUnavailable(requested, names, SUGGESTED_MODEL)

    usable = [
        m for m in installed
        # Ollama reports `cogitocoda:latest`; kin_config.json usually says
        # `cogitocoda`. Compared raw, a persona model was not recognised as one.
        if model_base(m.get("name", "")) not in personas
        and "embed" not in m.get("name", "").lower()
        and (m.get("details") or {}).get("family") != "nomic-bert"
    ]
    if not usable:
        raise ModelUnavailable(None, names, SUGGESTED_MODEL)

    # Rank general-purpose above specialist before ranking by size. Sorting on
    # size alone picked a 30B *coding* model to answer a question about a
    # writer's bibliography, purely because it was the largest thing installed.
    # Bigger is not better-for-this.
    # Smallest general model that still clears the floor. Largest-first
    # picked qwen3.8:27b next to a resident 26B Kin and timed out at 600s.
    # Contention is the case — after yield, still do not haul in a 27B
    # for a help-desk question.
    useful = [
        m for m in usable
        if _is_general(m) and _params_b(m) >= MIN_USEFUL_PARAMS_B
    ]
    if useful:
        useful.sort(key=_params_b)
        return useful[0]["name"], ""

    usable.sort(key=lambda m: (_is_general(m), _params_b(m)), reverse=True)
    best = usable[0]
    size = _params_b(best)
    note = (f"Using {best['name']} ({size:g}B) because it is the largest "
            f"installed. Models under {MIN_USEFUL_PARAMS_B:g}B invent "
            f"details when asked to recall facts - treat this answer as a "
            f"draft. `ollama pull {SUGGESTED_MODEL}` for something better.")
    return best["name"], note


async def run_task(task: str, model: str | None = None, api_key: str | None = None,
                    system: str = AGENT_SYSTEM, name: str = AGENT_NAME,
                    occupy_slot: bool = True):
    """Run a task and return the model's response.

    Local Ollama is hard default. If api_key is provided, attempt external API
    (checks for xai, openai, anthropic patterns). Falls back to local on any error.
    Raises on failure — does not return error strings, and does not write the vault.
    The caller owns record_thought_return / resting heartbeat.

    system/name let a caller run a differently-purposed worker (e.g. the help
    agent) through this same pipeline without duplicating it -- see
    HELP_SYSTEM above for why that's a separate prompt rather than a flag
    passed into a Kin's own persona.
    """
    async with _slot_lock:
        return await _run_task_locked(task, model, api_key, system, name, occupy_slot)


async def _run_task_locked(task, model, api_key, system, name, occupy_slot=True):
    # Try external API first if key is provided
    if api_key:
        try:
            result = await _run_external(task, model or SUGGESTED_MODEL, api_key, system=system)
            heartbeat(name, "completed-external")
            return result, model or SUGGESTED_MODEL
        except Exception as e:
            print(f"[agent] External API failed, falling back to local: {e}")

    from ollama_slot import (
        loaded_runner, ensure_agent_modelfile, runner_num_ctx,
        HELP_AGENT_MODEL, SPAWN_AGENT_MODEL, RIDING_KEEP_ALIVE, DEFAULT_NUM_CTX,
    )

    runner = loaded_runner()
    ctx = runner_num_ctx()
    tag = HELP_AGENT_MODEL if system == HELP_SYSTEM else SPAWN_AGENT_MODEL
    note = ""
    chosen = None

    # A model is already in VRAM. Ride those weights as a sibling
    # Modelfile. Do not ollama stop, do not keep_alive 0, do not
    # SIGSTOP. occupy_slot is leftover from the eviction design.
    if runner and not model:
        try:
            chosen = ensure_agent_modelfile(
                runner["name"], system=system, name=tag,
            )
            result = await _run_local_ollama(
                task, chosen, system=system,
                keep_alive=RIDING_KEEP_ALIVE, num_ctx=ctx,
            )
            heartbeat(name, "completed-local")
            return result, chosen
        except asyncio.TimeoutError as e:
            heartbeat(name, "failed")
            raise RuntimeError(
                f"{chosen or tag} did not answer within {LOCAL_TIMEOUT_S}s "
                f"(riding {runner['name']} at num_ctx={ctx})."
            ) from e
        except Exception as e:
            heartbeat(name, "failed")
            raise RuntimeError(f"Local Ollama error: {e or type(e).__name__}") from e

    async with aiohttp.ClientSession() as session:
        try:
            installed = await list_installed(session)
        except Exception as e:
            heartbeat(name, "failed")
            raise RuntimeError(
                "Could not reach Ollama at localhost:11434. Is it running?"
            ) from e

    try:
        chosen, note = choose_model(installed, model)
    except ModelUnavailable:
        heartbeat(name, "failed")
        raise

    riding = bool(runner)
    keep = RIDING_KEEP_ALIVE if riding else AGENT_KEEP_ALIVE
    num_ctx = ctx if riding else DEFAULT_NUM_CTX
    try:
        result = await _run_local_ollama(
            task, chosen, system=system, keep_alive=keep, num_ctx=num_ctx,
        )
        heartbeat(name, "completed-local")
        if note:
            result = f"{result}\n\n---\n{note}"
        return result, chosen
    except asyncio.TimeoutError as e:
        # str(asyncio.TimeoutError) is the empty string, so the obvious
        # f"...: {e}" produced "Local Ollama error:" and stopped. Naming the
        # model and the budget is the whole diagnosis.
        heartbeat(name, "failed")
        raise RuntimeError(
            f"{chosen} did not answer within {LOCAL_TIMEOUT_S}s. A large model "
            f"loading for the first time can take minutes - try again once it "
            f"is warm, or pick a smaller model."
        ) from e
    except Exception as e:
        heartbeat(name, "failed")
        raise RuntimeError(f"Local Ollama error: {e or type(e).__name__}") from e


async def _run_local_ollama(task: str, model: str, system: str = AGENT_SYSTEM,
                            keep_alive=AGENT_KEEP_ALIVE, num_ctx: int = 8192) -> str:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "http://localhost:11434/api/chat",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": task},
                ],
                "stream": False,
                "keep_alive": keep_alive,
                "options": {"temperature": 0.7, "num_ctx": num_ctx},
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
            # The agent is the fifth model caller and was the only one never
            # stripping reasoning traces, so a thinking model returned its
            # whole chain of thought as the answer.
            text = strip_think(data.get("message", {}).get("content", ""))
            if not text:
                raise RuntimeError(
                    f"{model} returned nothing usable "
                    f"(done_reason={data.get('done_reason')!r}). If it is a "
                    f"reasoning model its whole budget may have gone to "
                    f"thinking.")
            return text


async def run_help(question: str):
    """Help against whatever is loaded. No pause, no eviction.

    If a Kin is resident we ask them first. A no (or an unparseable
    reply) hands off to echo-bloom-help, a Modelfile FROM those weights.
    Returns (text, model, meta) with author / handed_off / from.
    Does not write thoughts.db or the vault.
    """
    from ollama_slot import (
        loaded_runner, ensure_agent_modelfile, runner_num_ctx,
        HELP_AGENT_MODEL, RIDING_KEEP_ALIVE,
    )
    runner = loaded_runner()
    if runner and runner.get("kin"):
        from kin_consent import ask_consent, ask_as_kin
        wants, _raw = await asyncio.to_thread(
            ask_consent, runner["kin"], runner["name"],
        )
        if wants:
            kin = cfg.get_kin(runner["kin"]) or {}
            text = await asyncio.to_thread(
                ask_as_kin, runner["kin"], runner["name"], question,
                HELP_SYSTEM, kin.get("system_prompt") or "",
            )
            if not text:
                wants = False
            else:
                return text, runner["name"], {
                    "author": runner["kin"],
                    "handed_off": False,
                    "from": runner["name"],
                }
        tag = ensure_agent_modelfile(
            runner["name"], system=HELP_SYSTEM, name=HELP_AGENT_MODEL,
        )
        text = await _run_local_ollama(
            question, tag, system=HELP_SYSTEM,
            keep_alive=RIDING_KEEP_ALIVE, num_ctx=runner_num_ctx(),
        )
        return text, tag, {
            "author": HELP_AGENT_NAME,
            "handed_off": True,
            "from": runner["name"],
        }

    result, used = await run_task(
        question, system=HELP_SYSTEM, name=HELP_AGENT_NAME,
    )
    return result, used, {
        "author": HELP_AGENT_NAME,
        "handed_off": True,
        "from": used,
    }


async def _run_external(task: str, model: str, api_key: str, system: str = AGENT_SYSTEM) -> str:
    """Very basic external fallback. Detects provider from key prefix or env."""
    headers = {"Authorization": f"Bearer {api_key}"}
    # Pre-existing gap, fixed as a side effect of wiring `system` through
    # here for the help agent: this path sent no system message at all, so
    # AGENT_SYSTEM's "don't invent an answer" rule never applied to an
    # external-API task. It does now, same as the local path always has.
    if "xai" in api_key.lower() or "grok" in model.lower():
        url = "https://api.x.ai/v1/chat/completions"
        headers["Content-Type"] = "application/json"
        body = {
            "model": model or "grok-4",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": task},
            ],
            "temperature": 0.7,
        }
    elif api_key.startswith("sk-"):
        # Assume OpenAI-compatible (OpenAI, Groq, Together, etc.)
        url = "https://api.openai.com/v1/chat/completions"
        body = {
            "model": model or "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": task},
            ],
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
            result, used = asyncio.run(run_task(task))
            print(result)
            print(f"(model: {used})", file=sys.stderr)
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print("Usage: python agent_runner.py 'your task here'")
        print("Default model: smallest general ≥7B (suggested qwen3:8b)")
