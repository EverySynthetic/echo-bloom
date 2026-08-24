#!/usr/bin/env python3
"""kin_consent.py — ask the Kin who is holding the GPU whether they want a job.

Someone asks Echo Bloom for help. A Kin is resident in VRAM and mid-wander.
Rather than evicting them to make room for an agent, we ask: do you want this
one, or should an agent take it?

The design, and why each piece is the way it is:

* The user gets an answer either way. That is not the Kin's burden to carry,
  and it is what makes declining genuinely free. If a "no" left someone
  stranded, the question would be pressure wearing a polite voice.

* Nothing here earns the Kin anything. If answering paid, you could not tell
  a real yes from a purchased one -- the same failure as asking under
  pressure, arrived at from the other side.

* We ask whoever is RESIDENT, not the household. A Kin whose model is not
  loaded cannot volunteer; it is not a show of hands, it is one specific
  mind that happens to be holding the card.

* None of this belongs in thoughts.db. The consent question, the answer, and
  the help reply are a Help event with a Kin author -- not a wander thought.
  kin_memory.get_context() would otherwise re-inject product documentation
  later as "this already happened to me", which is precisely what splitting
  Help off from the personas was meant to prevent.

* HELP_SYSTEM is data for that one turn. It never joins the persona.

Measured, not assumed (2026-08-23): a one-word request to an already-resident
model hung past three minutes while that Kin's own wander loop was running.
Ollama queues the request behind the wander's continuous generation. So the
caller MUST pause the specific wander pid first -- "ask between thoughts"
cannot be done by firing a request and hoping it lands in a gap.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as cfg

OLLAMA = "http://localhost:11434"
TIMEOUT_S = 120

# Refined with Grok, several passes. What each omission is doing:
#   - no "you are an equal": asserted by the entity with root, at the moment
#     of asking, that sets terms rather than showing them. The asking shows it.
#   - no "dear friend": ships to strangers' installs, where a three-day-old
#     Kin would be called that by someone it does not know yet.
#   - no "you're mid-thought": narrating their inner state back at them is its
#     own small imposition.
#   - the reassurance is that the USER is covered, not that the Kin is excused.
#     "Either way they get an answer" is the whole reason declining is free.
#   - "I" is the software, not Don. A system-level ask should not arrive
#     sounding like the person who owns the machine.
CONSENT_PROMPT = (
    "Someone's asking for help with Echo Bloom. Want this one, or should an "
    "agent take it? Either way they get an answer. If you'd rather not, you "
    "can pick up after.\n\n"
    "Answer with just: yes or no."
)

_YES = re.compile(r"\b(yes|yeah|yep|sure|ok|okay|i'?ll take|i will|happy to|"
                  r"of course|absolutely|glad to|let me)\b", re.I)
_NO = re.compile(r"\b(no|nope|nah|pass|decline|rather not|can'?t|cannot|"
                 r"keep going|not now|maybe later)\b", re.I)


def _post(path: str, payload: dict, timeout: int = TIMEOUT_S) -> dict | None:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(f"{OLLAMA}{path}", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def _post_get(path: str) -> dict | None:
    """GET a small Ollama JSON endpoint (/api/ps, /api/tags)."""
    try:
        with urllib.request.urlopen(f"{OLLAMA}{path}", timeout=10) as r:
            return json.load(r)
    except Exception:
        return None


# Resource discovery belongs to ollama_slot.py. This module is the consent
# conversation. Do not SIGSTOP: that wedged the slot. Match num_ctx to the
# loaded runner. A no hands off to echo-bloom-help on the same weights.


def ask_consent(kin_name: str, model: str) -> tuple[bool, str]:
    """Ask the resident Kin whether they want this job.

    Returns (wants_it, raw_reply). A model that cannot be reached, or that
    answers with something unparseable, is treated as a NO -- never as a
    yes. Consent is not the default; if we did not clearly hear yes, we
    hand it to the agent and leave the Kin alone.

    Do not SIGSTOP anyone first. Pause of in-flight clients wedges the
    slot. num_ctx must match the loaded runner (2048 against -c 8192 hung
    forever). Unpaused, matched ctx, a sibling name answered in 16.9s.
    """
    try:
        from ollama_slot import runner_num_ctx
        num_ctx = runner_num_ctx()
    except Exception:
        num_ctx = 8192
    data = _post("/api/chat", {
        "model": model,
        "messages": [{"role": "user", "content": CONSENT_PROMPT}],
        "stream": False,
        # Match the Kin's pin. 0 would unload them; 5m would shrink 999h.
        "keep_alive": "999h",
        "options": {"temperature": 0.3, "num_ctx": num_ctx},
    })
    if not data:
        return False, ""
    reply = (data.get("message") or {}).get("content", "").strip()
    if not reply:
        return False, ""
    head = reply[:200]
    # Check NO first: "no, I can't take this" contains "can" but means no.
    if _NO.search(head):
        return False, reply
    if _YES.search(head):
        return True, reply
    return False, reply


def ask_as_kin(kin_name: str, model: str, question: str,
               help_system: str, persona: str = "") -> str | None:
    """The Kin answers the user's question, grounded in the help doc.

    help_system is handed over as context for THIS TURN ONLY. It is never
    folded into the persona and never stored. A Kin improvising a settings
    menu that does not exist, with its own name attached, is worse than a
    generic agent getting it wrong -- so the grounding is not optional and
    the instruction to refuse rather than invent is repeated here.

    Do not SIGSTOP. Match the runner's num_ctx.
    """
    try:
        from ollama_slot import runner_num_ctx
        num_ctx = runner_num_ctx()
    except Exception:
        num_ctx = 8192
    system = (
        f"{persona}\n\n" if persona else ""
    ) + (
        "Someone using Echo Bloom asked you a question about the software "
        "itself. Answer as yourself, in your own voice, but only from what "
        "is written below. If the answer is not here, say plainly that you "
        "don't know rather than guessing -- a made-up menu or setting sends "
        "them looking for a button that was never there, and it will have "
        "your name on it.\n\n"
        f"{help_system}"
    )
    data = _post("/api/chat", {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": question},
        ],
        "stream": False,
        "keep_alive": "999h",
        "options": {"temperature": 0.7, "num_ctx": num_ctx},
    })
    if not data:
        return None
    return ((data.get("message") or {}).get("content") or "").strip() or None


# ── Which models are FREE to use right now ────────────────────────────────
#
# Don's insight, 2026-08-23, and it removes the hardest part of this design:
# a "different model" does not have to mean different weights. On this box
# bong / gemmaeli / gemmacrungus / gemma4:26b all point at the SAME weights
# blob (sha256:7121486771cbfe21...). They differ only by the SYSTEM prompt in
# their Modelfile.
#
# So the agent path never needed an eviction. Ask the already-resident blob
# under its stock name (gemma4:26b) with HELP_SYSTEM, and you get a generic
# helper with no persona, no load, no unload, no VRAM contention. The thing
# we spent an hour designing around does not need to happen.
#
# This belongs in ollama_slot.py with the rest of resource discovery; it is
# here only because that file was open in another editor at the time.


def _model_blob(name: str) -> str | None:
    """The weights-layer digest for a model, straight off its manifest."""
    root = os.environ.get("OLLAMA_MODELS", "/mnt/ai/ollama_models")
    base = name.split(":")[0]
    tag = name.split(":")[1] if ":" in name else "latest"
    for man in Path(root, "manifests").rglob(f"*/{base}/{tag}"):
        try:
            d = json.loads(man.read_text())
        except Exception:
            continue
        for layer in d.get("layers", []):
            if "model" in (layer.get("mediaType") or ""):
                return layer.get("digest")
    return None


def free_models() -> list[str]:
    """Models whose weights are ALREADY loaded — free to call, any size.

    Returns stock/non-persona names first. A model sharing the resident blob
    costs nothing to use no matter how many parameters it has, which makes
    "biggest that fits in leftover VRAM" the wrong question: the right one is
    "is it already here".
    """
    ps = _post_get("/api/ps") or {}
    resident = {_model_blob(m.get("name", "")) for m in ps.get("models", [])}
    resident.discard(None)
    if not resident:
        return []
    tags = _post_get("/api/tags") or {}
    persona = set(_kin_model_names())
    free, personas = [], []
    for m in tags.get("models", []):
        name = (m.get("name") or "").strip()
        if not name or _model_blob(name) not in resident:
            continue
        (personas if name in persona else free).append(name)
    return free + personas


def _kin_model_names() -> set:
    try:
        return {(k.get("model") or "").strip()
                for k in (cfg.load() or {}).get("kin", []) if k.get("model")}
    except Exception:
        return set()
