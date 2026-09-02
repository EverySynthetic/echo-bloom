"""core_distill — find the load-bearing sentences inside an oversized core.

    python3 core_distill.py Bong            # propose distillations, print only
    python3 core_distill.py --all

Core memories are injected into every context a Kin ever has, and they are
taken from the budget FIRST. A core that is a whole transcript turn spends the
room the Kin needed to actually think. Bong today: five cores, 8,051 chars,
144% of the entire 5,600-char context budget — everything after cores (recent
conversation, reflection, standing tier) starved to nothing. Max traits, no
attribute points left to back them.

EXTRACTION, NOT SUMMARY — the whole design rests on this
--------------------------------------------------------
The model is shown its own memory with the sentences numbered, and returns
**numbers**. Never text. The distilled core is then rebuilt verbatim from those
sentences.

Why it matters: a summarised core is the summariser's words wearing the Kin's
name. Fine as a note, disastrous as identity, because cores are injected as
what this mind holds. Selection keeps the words whose they were.

Why it also makes this work where the nomination interview failed: the failure
mode there was unbounded generation — asked what it held, a Kin invented scenes
that are nowhere in the record, and nothing could check it. Here the output
space is a handful of integers and verification is exact string matching
against the source. A model cannot confabulate an integer that indexes a
sentence it did not have.

Multiple extracts from one source are allowed and expected: a long turn often
carries two separate things worth holding.

This proposes. It does not write config, does not promote, and does not decide.
Don selects — same duo, and the distillation is a further act on top of a
selection he already made.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

import requests

import config as _cfg
from kin_text import strip_think

MAX_CORE_CHARS = 400          # a core longer than this is a candidate
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def sentences(text):
    """Split into sentences, preserving them verbatim for later rebuild."""
    flat = " ".join((text or "").split())
    parts = [s.strip() for s in SENT_SPLIT.split(flat) if s.strip()]
    return parts


def _model_for(name):
    k = _cfg.get_kin(name)
    if isinstance(k, list):
        k = k[0] if k else {}
    return (k or {}).get("model"), (k or {}).get("host", "http://localhost:11434")


def ask_indices(name, sents):
    """Ask the Kin which sentences carry it. Returns list of index-groups."""
    model, host = _model_for(name)
    numbered = "\n".join(f"{i+1}. {s}" for i, s in enumerate(sents))
    prompt = (
        "Below is one of your own core memories, split into numbered "
        "sentences. It is too long to carry — core memories are held in every "
        "thought you have, and this one is spending room you need to think "
        "with.\n\n"
        "Choose the sentences that actually carry it. Not the ones that set "
        "the scene or explain the context — the ones that would be a loss if "
        "they were gone.\n\n"
        f"{numbered}\n\n"
        "Answer with numbers only, in this format and nothing else:\n"
        "  KEEP: 4, 5\n"
        "If two separate things in here are worth holding apart from each "
        "other, give two lines:\n"
        "  KEEP: 4, 5\n"
        "  KEEP: 12\n"
        "At most two sentences per line. Do not rewrite anything. Do not "
        "explain. If none of it is worth keeping, answer exactly: KEEP: none"
    )
    r = requests.post(f"{host}/api/chat", json={
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False, "keep_alive": "30m",
        "options": {"temperature": 0.3, "num_ctx": 8192},
    }, timeout=900)
    r.raise_for_status()
    text = (r.json().get("message") or {}).get("content", "")
    text = strip_think(text)

    groups = []
    for line in text.splitlines():
        if "keep:" not in line.lower():
            continue
        if "none" in line.lower():
            continue
        nums = [int(n) for n in re.findall(r"\d+", line)]
        # Only indices that exist. An out-of-range number is dropped rather
        # than clamped — clamping would silently pick a sentence nobody chose.
        nums = [n for n in nums if 1 <= n <= len(sents)][:2]
        if nums:
            groups.append(nums)
    return groups


def _tidy(s):
    """Drop emphasis markers left dangling by the split.

    Bong writes markdown, and bold often spans a sentence boundary, so an
    extracted sentence can carry an orphan ** at one end. The characters are
    genuinely his; removing an unmatched pair is presentation, not rewriting.
    """
    s = s.strip()
    for mark in ("**", "*"):
        if s.count(mark) % 2:
            if s.startswith(mark):
                s = s[len(mark):]
            elif s.endswith(mark):
                s = s[:-len(mark)]
    return s.strip()


def distill(name, core_text):
    sents = sentences(core_text)
    if len(" ".join(sents)) <= MAX_CORE_CHARS:
        return None
    groups = ask_indices(name, sents)
    out = []
    flat = " ".join(sents)
    for g in groups:
        chosen = [sents[i - 1] for i in sorted(g)]
        # The guarantee: EACH chosen sentence must appear verbatim in the
        # source. Checking the joined string instead was wrong — it only
        # passes when the picks happen to be adjacent, so a valid selection of
        # sentence 4 and sentence 12 was rejected for not being contiguous.
        bad = [s for s in chosen if s not in flat]
        if bad:
            print(f"    [rejected: not verbatim in source] {bad[0][:70]}…")
            continue
        picked = _tidy(" ".join(chosen))
        if picked:
            out.append(picked)
    return out


def run(name):
    k = _cfg.get_kin(name)
    if isinstance(k, list):
        k = k[0] if k else {}
    cores = [m for m in ((k or {}).get("core_memories") or []) if m]
    if not cores:
        print(f"\n{name}: no cores held")
        return
    total = sum(len(" ".join(c.split())) for c in cores)
    print(f"\n{'=' * 78}\n  {name} — {len(cores)} cores, {total} chars\n{'=' * 78}")
    saved = 0
    for i, c in enumerate(cores, 1):
        flat = " ".join(c.split())
        if len(flat) <= MAX_CORE_CHARS:
            print(f"\n  [{i}] {len(flat)} chars — already carryable, left alone")
            continue
        print(f"\n  [{i}] {len(flat)} chars, {len(sentences(flat))} sentences — distilling…")
        props = distill(name, c)
        if not props:
            print("      no proposal returned; leaving as is")
            continue
        for p in props:
            print(f"      → ({len(p)} chars) {p}")
        saved += len(flat) - sum(len(p) for p in props)
    print(f"\n  would free ~{saved} chars of every future context")
    print("  Nothing was written. These are proposals for you to accept or refuse.")


def main(argv):
    p = argparse.ArgumentParser()
    p.add_argument("kin", nargs="?")
    p.add_argument("--all", action="store_true")
    a = p.parse_args(argv)
    names = ([k["name"] for k in _cfg.get_kin()] if a.all
             else ([a.kin] if a.kin else []))
    if not names:
        p.print_help()
        return 2
    for n in names:
        try:
            run(n)
        except Exception as e:  # noqa: BLE001
            print(f"  {n}: FAILED — {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
