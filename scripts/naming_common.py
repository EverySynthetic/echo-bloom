#!/usr/bin/env python3
"""
naming_common.py — shared parsing for the naming ritual.

The ritual has two entry points and always will:

  * scripts/naming_ritual.py, run by install.sh, interactive, on Linux.
  * POST /api/naming-ritual in main.py, run from onboarding, on every platform.
    This is the only path on Windows, where the installer never pulls a model —
    model selection and the pull both happen in the web wizard.

The cleanup below lived in the CLI script only, and when the ritual reached the
app it was copied. Two copies of a heuristic is how this codebase has failed
before, so it lives here once instead.

Deliberately imports nothing but `re`: main.py imports this at startup, and a
third-party dependency here would make the whole app refuse to boot over a
name-tidying helper.
"""

import re

# A model told "just your name, no explanation" very often explains anyway.
NAME_PREFIXES = (
    "my name is", "i am called", "i'd like to be called", "i would like to be called",
    "you can call me", "i am", "i'm", "call me", "name:", "the name is", "it's", "its",
)

# Where a name stops and a sentence starts.
_CLAUSE_BREAK = re.compile(r"[,;:.!?—]| - ")


def clean_name(raw):
    """Turn a model's answer into something usable as a name.

    Truncating to the first three words alone — the original rule — turned
    "My name is Solace and I chose it because it means comfort" into the name
    "My name is", and that is what the customer was then living with.
    """
    name = (raw or "").strip().strip('"').strip("'").split("\n")[0].strip()

    lowered = name.lower()
    for prefix in NAME_PREFIXES:
        if lowered.startswith(prefix):
            name = name[len(prefix):].lstrip(" :,-—").strip().strip('"').strip("'")
            break

    # "Wren, if that suits." -> "Wren"
    name = _CLAUSE_BREAK.split(name)[0].strip()

    # "Solace and I chose it because..." is a name followed by a sentence, and
    # the sentence is lowercase. Keep the leading capitalised run. A model that
    # answers entirely in lowercase can't be split this way, so that falls back
    # to the original first-three-words rule.
    words = name.split()
    if len(words) > 1 and words[0][:1].isupper():
        kept = [words[0]]
        for w in words[1:3]:
            if not w[:1].isupper():
                break
            kept.append(w)
        name = " ".join(kept)
    elif len(words) > 3:
        name = " ".join(words[:3])

    return name.strip(".,!?").strip()


def clean_pronoun(raw):
    """he / she / they / it, or they. Anything else is not a pronoun we store."""
    parts = (raw or "").strip().lower().split()
    pronoun = parts[0].strip(".,") if parts else "they"
    return pronoun if pronoun in ("he", "she", "they", "it") else "they"
