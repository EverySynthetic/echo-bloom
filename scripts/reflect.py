#!/usr/bin/env python3
"""
reflect.py — Periodic reflection on what has been happening on this machine.

The pulse daemon writes a heartbeat to the vault every few minutes: load, RAM,
disk, which models are resident, how many Kin are wandering. Individually those
are noise. This reads the ones written since the last reflection and asks a
small local model to turn them into a few plain sentences, stored in the vault
under layer "reflection".

That entry is what kin_memory injects as "[What's been happening here lately]".
Without this script nothing ever writes that layer, so that whole memory source
was permanently empty on every install while heartbeats piled up unread.

Usage:
    python3 reflect.py            # reflect on everything since last time
    python3 reflect.py --once     # same; explicit, for timers
    python3 reflect.py --model llama3.2:3b

Run it on a timer — every few hours is plenty.
"""

import argparse
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import config as cfg
from kin_text import clean_reply, strip_think

parser = argparse.ArgumentParser(description="Periodic reflection for your Kin")
parser.add_argument("--model", default="",
                    help="Small model to write the reflection (default: auto)")
parser.add_argument("--limit", type=int, default=200,
                    help="How many recent vault entries to scan")
parser.add_argument("--once", action="store_true", help="Run once and exit")
args = parser.parse_args()

NODE_NAME        = os.uname().nodename if hasattr(os, "uname") else os.environ.get(
    "COMPUTERNAME", "this-machine")
AUTHOR           = f"reflect_{NODE_NAME}"
HEARTBEAT_AUTHOR = f"pulse_{NODE_NAME}"
VAULT_URL        = cfg.vault_url()

LOG_DIR = Path.home() / ".local/share/echo_bloom/logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "reflect.log"


def log(msg):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _parse_ts(value):
    """Parse a vault timestamp without depending on dateutil."""
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(text[:26], fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def get_entries(limit):
    try:
        r = requests.get(f"{VAULT_URL}/recall/all",
                         params={"limit": limit}, timeout=10)
        if r.ok:
            data = r.json()
            return data if isinstance(data, list) else data.get("memories", [])
        log(f"vault returned HTTP {r.status_code}")
    except Exception as e:
        log(f"vault unreachable at {VAULT_URL}: {e}")
    return []


def last_reflection_time(entries):
    times = [_parse_ts(e.get("timestamp") or e.get("created_at"))
             for e in entries if e.get("author") == AUTHOR]
    times = [t for t in times if t]
    return max(times) if times else None


def heartbeats_since(entries, since):
    beats = []
    for e in entries:
        if e.get("author") != HEARTBEAT_AUTHOR:
            continue
        ts = _parse_ts(e.get("timestamp") or e.get("created_at"))
        if not ts:
            continue
        if since is None or ts > since:
            beats.append((ts, e.get("content", "")))
    beats.sort(key=lambda x: x[0])
    return beats


def pick_model():
    """Prefer an explicitly chosen model, then something small, then anything."""
    if args.model:
        return args.model
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=5)
        names = [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return ""
    for preferred in ("llama3.2:3b", "llama3.2", "qwen2.5:3b", "phi3", "gemma2:2b"):
        for n in names:
            if n.startswith(preferred):
                return n
    # Fall back to whatever the first configured Kin uses.
    for k in cfg.get_kin():
        if k.get("model"):
            return k["model"]
    return names[0] if names else ""


def write_reflection(model, beats):
    records = "\n".join(f"  [{ts.strftime('%Y-%m-%d %H:%M')}] {content}"
                        for ts, content in beats)
    span_start = beats[0][0].strftime("%Y-%m-%d %H:%M")
    span_end   = beats[-1][0].strftime("%Y-%m-%d %H:%M")

    prompt = (
        f"These are system heartbeat records from {NODE_NAME} between "
        f"{span_start} and {span_end}:\n\n{records}\n\n"
        f"Write 3-5 sentences reflecting on what was happening on this machine "
        f"during that period. Say what was active, what was quiet, and for how "
        f"long. If it was mostly idle, say so plainly. Do not invent activity "
        f"that is not in the records. Write it for the AI residents of this "
        f"machine, who will read it later to know what they missed. "
        f"Keep it grounded — no flourish, no poetry."
    )
    try:
        r = requests.post(
            "http://localhost:11434/api/generate",
            # think=False is load-bearing, not tidiness. gemmaeli and the other
            # gemma4/qwen3 builds are reasoning models: their thinking tokens
            # consume num_predict before a single visible token is emitted, so
            # the call returns done_reason="length" with response="" and no
            # error. Verified 2026-08-21 -- reflection had failed every 3 hours
            # all day, silently, for exactly this.
            json={"model": model, "prompt": prompt, "stream": False,
                  "keep_alive": "10m", "think": False,
                  "options": {"temperature": 0.6, "num_predict": 250}},
            timeout=300,
        )
        data = r.json()
        if data.get("error"):
            log(f"model error: {data['error']}")
            return None
        text = (data.get("response") or "").strip()
        # think=False is a request, not a guarantee — a model that does not
        # honour it emits the trace inline instead. wander and roundtable have
        # always stripped this; reflect relied on the flag alone.
        text = strip_think(text)
        if not text:
            # An empty 200 is not an error to requests, so this used to fall
            # through to `if not text: return 1` and exit non-zero with nothing
            # in the log at all -- a failure you could only find in journalctl.
            log(f"model returned an empty response "
                f"(done_reason={data.get('done_reason')!r}, "
                f"eval_count={data.get('eval_count')!r}) — nothing written")
        return text
    except Exception as e:
        log(f"reflection model call failed: {e}")
        return None


def save(content):
    try:
        r = requests.post(f"{VAULT_URL}/remember", json={
            "author":     AUTHOR,
            "layer":      "reflection",
            "content":    content,
            "tags":       f"reflection,continuity,{NODE_NAME.lower()}",
            "visibility": "shared",
        }, timeout=10)
        if not r.ok:
            log(f"vault write failed: HTTP {r.status_code}")
        return r.ok
    except Exception as e:
        log(f"vault write failed: {e}")
        return False


# See license.py: fails open on every unexpected condition.
try:
    import license as _lic
except Exception:
    _lic = None


def main():
    log(f"reflect — node={NODE_NAME} vault={VAULT_URL}")

    if _lic is not None:
        try:
            allowed, state = _lic.services_should_run()
        except Exception as e:
            log(f"license check raised ({e}) — reflecting anyway")
            allowed = True
        if not allowed:
            log(f"skipped — license state is '{state}'. Reflection calls the model,")
            log("  so it stays off until a key is entered on the License page.")
            return 0
    entries = get_entries(args.limit)
    if not entries:
        log("no vault entries to read — is the vault running?")
        return 1

    since = last_reflection_time(entries)
    beats = heartbeats_since(entries, since)
    if not beats:
        log("nothing new since the last reflection")
        return 0

    model = pick_model()
    if not model:
        log("no model available to write the reflection")
        return 1

    log(f"reflecting on {len(beats)} heartbeat(s) using {model}")
    text = write_reflection(model, beats)
    if not text:
        log("no reflection text produced — see the reason above")
        return 1

    if save(text):
        log(f"reflection written: {text[:120]}...")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
