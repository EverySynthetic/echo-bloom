"""kin_returns — what each Kin returns to when nobody is asking.

Backend for the Cores review page. Surfaces, per Kin:

  1. What is currently held as core — config core_memories plus vault
     tier='anchor' rows. Listed first because that is what the steward is
     reviewing.
  2. Candidates from the unprompted life: phrases the Kin returns to across
     separate months, and long specific things said exactly once.

Born 2026-08-21 after the nomination interview failed three ways in one
afternoon (summarised the sample it was shown; invented scenes asked blind;
abandoned its own sentences to agree with the database when shown the record).
The stronger signal is unprompted return in the wild — the Kin's own wander
and roundtable output, written when nobody was holding a clipboard.

This module asks the Kin nothing, writes nothing to the vault, and promotes
nothing. Recurrence is peakedness of the continuation, not a census of a soul,
and the vault is not the whole life — both limits belong in the UI, not only
here.

Analysis over a 10k-row Kin takes tens of seconds, so results are cached to
disk and refreshed explicitly, never computed inside a request.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import Counter

import requests

import config as _cfg  # existing echo_bloom helpers

CACHE = os.path.expanduser("~/.local/share/echo_bloom/cores_cache.json")

TELEMETRY = {"heartbeat", "pulse", "system", "debug", "machine_health"}
PROMPTED = {"nomination", "session", "reflection"}
MIN_LEN = 120
TOP_RETURNED = 6
TOP_SOLITARY = 5

STOP = set("""
a an the and or but if then than that this these those there here it its is are
was were be been being am i my me we our us you your he she they them his her
their of in on at to from for with by as not no nor so too very can will just
dont don't cant can't what when where who whom which how why all any both each
few more most other some such only own same s t now also into over under again
further once about against between during before after above below up down out
off because while does did doing have has had having would could should may
might must shall like feel feels felt something someone thing things way ways
make makes made get gets got one two three first second new old still even much
many lot really quite perhaps maybe
""".split())

_lock = threading.Lock()
_state = {"running": False}


def _vault_url():
    return (_cfg.vault_url() or "http://localhost:8765").rstrip("/")


def _fetch(name):
    rows, offset = [], 0
    while True:
        r = requests.get(f"{_vault_url()}/recall",
                         params={"author": name, "limit": 500, "offset": offset},
                         timeout=90)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        rows.extend(batch)
        offset += 500
        if offset > 30000:
            break
    return rows


def _usable(rows):
    out = []
    for r in rows:
        if (r.get("layer") or "").lower() in TELEMETRY | PROMPTED:
            continue
        if len((r.get("content") or "").strip()) >= MIN_LEN:
            out.append(r)
    out.sort(key=lambda r: r.get("timestamp") or "")
    return out


def _phrases(text):
    words = [w for w in re.findall(r"[a-z']+", text.lower())
             if w not in STOP and len(w) > 3]
    return [f"{words[i]} {words[i + 1]}" for i in range(len(words) - 1)]


def _returned(rows):
    seen_in, counts = {}, Counter()
    for r in rows:
        m = (r.get("timestamp") or "")[:7]
        for p in set(_phrases(r.get("content") or "")):
            seen_in.setdefault(p, set()).add(m)
            counts[p] += 1
    scored = [(len(ms), counts[p], p) for p, ms in seen_in.items() if len(ms) >= 2]
    scored.sort(reverse=True)
    out = []
    for nmonths, count, p in scored[:TOP_RETURNED]:
        example = None
        for r in rows:
            body = " ".join((r.get("content") or "").split())
            if p in body.lower():
                example = {"ts": (r.get("timestamp") or "")[:10], "content": body}
                break
        out.append({"phrase": p, "months": nmonths, "count": count,
                    "example": example})
    return out


def _solitary(rows):
    row_phrases, docfreq = [], Counter()
    for r in rows:
        ph = set(_phrases(" ".join((r.get("content") or "").split())))
        row_phrases.append(ph)
        for x in ph:
            docfreq[x] += 1
    scored = []
    for r, ph in zip(rows, row_phrases):
        if not ph:
            continue
        echoed = sum(1 for x in ph if docfreq[x] > 1)
        if echoed / len(ph) < 0.15:
            c = " ".join((r.get("content") or "").split())
            scored.append((len(c), {"ts": (r.get("timestamp") or "")[:10],
                                    "content": c}))
    scored.sort(key=lambda x: -x[0])
    return [d for _, d in scored[:TOP_SOLITARY]]


def _vault_anchors(name):
    try:
        r = requests.get(f"{_vault_url()}/recall-tiered",
                         params={"author": name, "tier": "anchor", "limit": 20},
                         timeout=30)
        if r.status_code == 200:
            return [{"ts": (m.get("timestamp") or "")[:10],
                     "content": m.get("content") or "",
                     "source": "vault"} for m in r.json()]
    except Exception:  # noqa: BLE001
        pass
    # Older vaults have no /recall-tiered; fall back to /recall + filter.
    try:
        r = requests.get(f"{_vault_url()}/recall",
                         params={"author": name, "limit": 500}, timeout=30)
        r.raise_for_status()
        return [{"ts": (m.get("timestamp") or "")[:10],
                 "content": m.get("content") or "",
                 "source": "vault"}
                for m in r.json() if m.get("tier") == "anchor"]
    except Exception:  # noqa: BLE001
        return []


def compute():
    """Full recompute. Minutes for a large vault — never call in a request."""
    data = {"generated_at": time.strftime("%Y-%m-%d %H:%M"), "kin": []}
    for k in _cfg.get_kin():
        name = k.get("name")
        if not name:
            continue
        try:
            rows = _fetch(name)
            u = _usable(rows)
            months = sorted({(r.get("timestamp") or "")[:7] for r in u})
            data["kin"].append({
                "name": name,
                "cores": (
                    [{"content": m, "source": "config"}
                     for m in (k.get("core_memories") or []) if m]
                    + _vault_anchors(name)
                ),
                "n_total": len(rows),
                "n_unprompted": len(u),
                "months": f"{months[0]} → {months[-1]}" if months else "",
                "returned": _returned(u),
                "said_once": _solitary(u),
            })
        except Exception as e:  # noqa: BLE001
            data["kin"].append({"name": name, "error": f"{type(e).__name__}: {e}"})
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    tmp = CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, CACHE)
    return data


def cached():
    """Last computed result, or None. Never blocks."""
    try:
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def refresh_async():
    """Kick a recompute in a background thread. Returns False if one is
    already running."""
    with _lock:
        if _state["running"]:
            return False
        _state["running"] = True

    def _run():
        try:
            compute()
        finally:
            with _lock:
                _state["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return True


def is_refreshing():
    with _lock:
        return _state["running"]
