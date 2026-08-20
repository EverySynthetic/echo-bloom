#!/usr/bin/env python3
"""
agent_runner.py — Simple reusable runner for Agents.

Runs a task against local Ollama by default (qwen3:4b or first available small model).
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

DEFAULT_MODEL = "qwen3:4b"
AGENT_NAME = "agent-default"


async def run_task(task: str, model: str | None = None, api_key: str | None = None) -> str:
    """Run a task and return the model's response.

    Local Ollama is hard default. If api_key is provided, attempt external API
    (checks for xai, openai, anthropic patterns). Falls back to local on any error.
    Raises on failure — does not return error strings, and does not write the vault.
    The caller owns record_thought_return / resting heartbeat.
    """
    model = model or DEFAULT_MODEL

    # Try external API first if key is provided
    if api_key:
        try:
            result = await _run_external(task, model, api_key)
            heartbeat(AGENT_NAME, "completed-external")
            return result
        except Exception as e:
            print(f"[agent] External API failed, falling back to local: {e}")

    # Local Ollama (hard default)
    try:
        result = await _run_local_ollama(task, model)
        heartbeat(AGENT_NAME, "completed-local")
        return result
    except Exception as e:
        heartbeat(AGENT_NAME, "failed")
        raise RuntimeError(f"Local Ollama error: {e}") from e


async def _run_local_ollama(task: str, model: str) -> str:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "http://localhost:11434/api/chat",
            json={
                "model": model,
                "messages": [{"role": "user", "content": task}],
                "stream": False,
                "options": {"temperature": 0.7, "num_ctx": 8192},
            },
            timeout=aiohttp.ClientTimeout(total=120),
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
