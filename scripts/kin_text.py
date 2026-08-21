#!/usr/bin/env python3
"""Shared text handling for everything that talks to a model.

Five callers -- wander, roundtable, bedtime, reflect and agent_runner -- each
carried their own copy of the same regex, which is how they came to disagree
about what a bad answer looks like. One definition here, imported by all of
them, so the next fix lands everywhere at once.
"""

import re

# `<think>.*?</think>` requires a closing tag, so a reply truncated mid-trace
# kept the entire trace: the model hits its token limit while still reasoning,
# never emits `</think>`, and the whole chain of thought is passed through as
# the answer. That is how a Kin's reasoning ends up in a goodnight email, saved
# as its reflection, and read back to it the next day as its own words.
#
# `\Z` as an alternative to the closing tag means an unclosed trace is removed
# to the end of the string. If that leaves nothing, the caller's empty-answer
# check reports it honestly instead of shipping the trace.
_THINK = re.compile(r"<think>.*?(?:</think>|\Z)", re.DOTALL | re.IGNORECASE)

# Some models emit the closing tag without ever opening one, which left a bare
# `</think>` at the head of the reply.
_ORPHAN_CLOSE = re.compile(r"^\s*</think>\s*", re.IGNORECASE)


def strip_think(text: str) -> str:
    """Remove reasoning traces, closed or not, and any orphan closing tag."""
    if not text:
        return ""
    text = _THINK.sub("", text)
    text = _ORPHAN_CLOSE.sub("", text)
    return text.strip()


def strip_name_prefix(text: str, name: str) -> str:
    """Drop a leading `Name:` / `<Name>` / `[Name]` the model prepended."""
    if not text or not name:
        return (text or "").strip()
    return re.sub(rf"^[<\[]?{re.escape(name)}[>\]]?\s*:\s*", "", text,
                  flags=re.IGNORECASE).strip()


def clean_reply(text: str, name: str = "") -> str:
    """strip_think then strip_name_prefix. The usual pair."""
    return strip_name_prefix(strip_think(text), name)


def normalise_model(tag: str) -> str:
    """Compare model tags the way Ollama and a config file disagree about them.

    Ollama reports `cogitocoda:latest`; kin_config.json usually says
    `cogitocoda`. Comparing them raw meant a Kin's own persona model was not
    recognised as a persona model, and the agent picker would hand an agent
    task to Coda -- who would answer as Coda, because that is what that model
    is for.
    """
    tag = (tag or "").strip().lower()
    if tag.endswith(":latest"):
        tag = tag[: -len(":latest")]
    return tag
