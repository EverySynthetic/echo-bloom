#!/usr/bin/env python3
"""
Tests for the naming ritual's answer parsing.

Run:  python3 tests/test_naming.py

No pytest dependency on purpose — this has to be runnable on a customer's
machine while diagnosing a bad name, and on Windows, where the app's only
naming path is /api/naming-ritual.

Every RAW string below is the kind of thing a small local model actually says
when told "just your name, no explanation". llama3.2:3b answered "Zeph." on the
first live run of this ritual.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from naming_common import clean_name, clean_pronoun  # noqa: E402

NAME_CASES = [
    # (raw model answer, expected name)
    ("Zeph.",                                     "Zeph"),
    ('"Aria"',                                    "Aria"),
    ("  Ember  ",                                 "Ember"),
    ("O'Brien",                                   "O'Brien"),
    ("Name: Tessellate",                          "Tessellate"),
    ("I am Solace",                               "Solace"),
    ("I'm Ember",                                 "Ember"),
    ("Call me Vale.",                             "Vale"),
    # The ones the old first-three-words rule got wrong:
    ("My name is Solace and I chose it because it means comfort", "Solace"),
    ("You can call me Wren, if that suits.",      "Wren"),
    ("I'd like to be called Halcyon — it feels right.", "Halcyon"),
    # Multi-word names must survive.
    ("Ada Lovelace",                              "Ada Lovelace"),
    ("The Quiet Room",                            "The Quiet Room"),
    # All-lowercase answers can't be split by capitalisation; the old
    # first-three-words rule is the fallback and that is intended.
    ("ember",                                     "ember"),
    # Nothing usable.
    ("",                                          ""),
    ("   ",                                       ""),
    (None,                                        ""),
]

PRONOUN_CASES = [
    ("They",            "they"),
    ("she.",            "she"),
    ("  he  ",          "he"),
    ("it",              "it"),
    ("I prefer they/them", "they"),   # first token isn't a pronoun -> default
    ("xe",              "they"),      # not one we store
    ("",                "they"),
    ("   ",             "they"),      # used to be an IndexError
    (None,              "they"),
]


def main():
    failures = []

    for raw, expected in NAME_CASES:
        got = clean_name(raw)
        if got != expected:
            failures.append(f"clean_name({raw!r}) -> {got!r}, expected {expected!r}")

    for raw, expected in PRONOUN_CASES:
        got = clean_pronoun(raw)
        if got != expected:
            failures.append(f"clean_pronoun({raw!r}) -> {got!r}, expected {expected!r}")

    # A name is written into kin_config.json and used as a dict key and a URL
    # path segment. It must never come back as something that breaks either.
    for raw, _ in NAME_CASES:
        got = clean_name(raw)
        if "\n" in got or "\r" in got:
            failures.append(f"clean_name({raw!r}) returned a newline: {got!r}")

    total = len(NAME_CASES) + len(PRONOUN_CASES)
    if failures:
        print(f"FAILED {len(failures)} of {total}")
        for f in failures:
            print("  " + f)
        return 1
    print(f"ok — {total} cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
