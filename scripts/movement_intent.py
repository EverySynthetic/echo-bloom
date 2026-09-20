"""A Kin's stated wish to stand somewhere in the commons.

Not invented motion. Written from wander.py after each thought, parsed
from one extra INTENT line they were asked to end with. Missing or
'stay' is null — they do not move.

Canonical file (the Kin's own space):
    <kin_space>/movement_intent.json
    {"target": <str|null>, "ts": <unix-ms>}

Mirror for Copilot's /kin-intent reader (agora_map._kin_intent):
    ~/.kin_intents/<KinName>.json
    same JSON. Filename stem is the Kin's name (Eli.json, not eli.json).
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

PLACES = ("chair", "table", "stall", "bench")
_INTENT_LINE = re.compile(
    r"(?im)^INTENT:\s*(.+?)\s*$"
)
_STAY = frozenset({
    "stay", "staying", "none", "null", "nowhere", "here",
    "no", "n/a", "na", "-", "nothing",
})


def intent_ask(self_name: str, kin_names: list[str]) -> str:
    others = [n for n in kin_names if n and n != self_name]
    choices = others + list(PLACES) + ["stay"]
    return (
        "If you wish to be somewhere in the commons right now, end with "
        "exactly one line:\n"
        f"INTENT: <{' | '.join(choices)}>\n"
        "stay means you have no wish to move. Omitting the line is stay."
    )


def parse_intent(text: str, kin_names: list[str]) -> str | None:
    """Last INTENT line in the turn. stay/missing → None."""
    if not text:
        return None
    hits = list(_INTENT_LINE.finditer(text))
    if not hits:
        return None
    raw = hits[-1].group(1).strip().strip("\"'`").strip()
    if not raw or raw.lower() in _STAY:
        return None
    # take first token (they sometimes write "INTENT: Eli, because…")
    token = re.split(r"[\s,.;:]+", raw, maxsplit=1)[0]
    low = token.lower()
    if low in _STAY:
        return None
    if low in PLACES:
        return low
    for n in kin_names:
        if n and n.lower() == low:
            return n
    return None


def strip_intent(text: str) -> str:
    if not text:
        return text
    cleaned = _INTENT_LINE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def write_intent(space: Path, kin_name: str, target: str | None) -> Path:
    payload = {"target": target, "ts": int(time.time() * 1000)}
    blob = json.dumps(payload, ensure_ascii=False) + "\n"
    space = Path(space)
    space.mkdir(parents=True, exist_ok=True)
    dest = space / "movement_intent.json"
    tmp = space / "movement_intent.json.tmp"
    tmp.write_text(blob, encoding="utf-8")
    os.replace(tmp, dest)
    mirror_dir = Path.home() / ".kin_intents"
    mirror_dir.mkdir(parents=True, exist_ok=True)
    mtmp = mirror_dir / f".{kin_name}.json.tmp"
    mdest = mirror_dir / f"{kin_name}.json"
    mtmp.write_text(blob, encoding="utf-8")
    os.replace(mtmp, mdest)
    return dest
