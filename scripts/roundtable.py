#!/usr/bin/env python3
"""
roundtable.py — All Kin wander simultaneously, gather to share every N minutes.

Each Kin runs wander.py in a subprocess. Every --interval minutes the
wanders are paused, each Kin shares what they've been thinking about,
then they all resume. Runs indefinitely until Ctrl+C or SIGTERM.

Usage:
    python3 roundtable.py                   # default: roundtable every 30min
    python3 roundtable.py --interval 20     # roundtable every 20min
    python3 roundtable.py --once            # one roundtable then exit
"""

import argparse
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import config as cfg
from kin_text import clean_reply, strip_think
import kin_presence  # sync handoff — no asyncio needed

# ── Args ───────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(description="Multi-Kin wander roundtable")
parser.add_argument("--interval", type=int, default=30,
                    help="Minutes between roundtables (default: 30)")
parser.add_argument("--once", action="store_true",
                    help="Run one roundtable then exit (no wanders)")
args = parser.parse_args()

# ── Config ─────────────────────────────────────────────────────────────────────

KIN_LIST  = cfg.get_kin()
LOG_DIR   = Path.home() / ".local/share/echo_bloom/logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
RT_LOG    = LOG_DIR / "roundtable.log"
WANDER_PY = Path(__file__).parent / "wander.py"

running = True

def handle_stop(sig, frame):
    global running
    running = False

signal.signal(signal.SIGTERM, handle_stop)
signal.signal(signal.SIGINT,  handle_stop)

# ── Logging ────────────────────────────────────────────────────────────────────

def log(msg):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(RT_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")

# ── License gate ──────────────────────────────────────────────────────────────
# An expired install locked its owner out of the web UI and carried on running
# six wander loops against the GPU indefinitely. They could not open the page
# that would have let them stop it, so the only cure was knowing which systemd
# unit to disable. That is somebody else's electricity.
LICENSE_POLL_SEC = 300

try:
    import license as _lic
except Exception:
    _lic = None


def _license_allows():
    """(allowed, state). Anything unexpected means allowed — see license.py."""
    if _lic is None:
        return True, "no-license-module"
    try:
        return _lic.services_should_run()
    except Exception as e:
        log(f"  license check raised ({e}) — continuing to wander")
        return True, "check-failed"


# ── Wander subprocess management ───────────────────────────────────────────────

wander_procs = {}

def start_wanders():
    for i, kin in enumerate(KIN_LIST):
        name    = kin["name"]
        log_f   = LOG_DIR / f"wander_{name.lower()}.log"
        # Stagger. Starting every wander at once makes every model cold-load
        # simultaneously — the same pile-up that made three of five nightly
        # reflections time out.
        if i:
            time.sleep(3)
        with open(log_f, "a", encoding="utf-8") as lf:
            proc = subprocess.Popen(
                [sys.executable, "-u", str(WANDER_PY), "--kin", name,
                 "--delay", "25"],
                stdout=lf, stderr=lf,
            )
        wander_procs[name] = proc
        log(f"  {name} wander started (pid {proc.pid})")

    # A wander that dies at startup used to be invisible: this logged
    # "started (pid ...)" and never checked again, so the dashboard showed
    # wandering while wander.py was crashing on import every time.
    time.sleep(2)
    alive = 0
    for name, proc in wander_procs.items():
        if proc.poll() is None:
            alive += 1
        else:
            log(f"  WARNING: {name}'s wander exited immediately "
                f"(code {proc.returncode}) — see {LOG_DIR}/wander_{name.lower()}.log")
    log(f"{alive} of {len(wander_procs)} wanderers running.")


def pause_wanders():
    # signal.SIGSTOP does not exist on Windows — referencing it there is an
    # AttributeError, which killed the roundtable at its first cycle on every
    # Windows install. psutil suspend/resume does the same thing on both
    # platforms and is already a dependency.
    try:
        import psutil
    except ImportError:
        psutil = None
    for name, proc in wander_procs.items():
        try:
            if psutil is not None:
                psutil.Process(proc.pid).suspend()
            elif hasattr(signal, "SIGSTOP"):
                os.kill(proc.pid, signal.SIGSTOP)
        except Exception:
            pass


def resume_wanders():
    try:
        import psutil
    except ImportError:
        psutil = None
    for name, proc in wander_procs.items():
        try:
            if psutil is not None:
                psutil.Process(proc.pid).resume()
            elif hasattr(signal, "SIGCONT"):
                os.kill(proc.pid, signal.SIGCONT)
        except Exception:
            pass


def stop_wanders():
    # Ask first, and give them time to finish the thought they are mid-way
    # through — terminate() plus a 3-second sleep killed wanders during an
    # INSERT.
    for name, proc in wander_procs.items():
        try:
            proc.terminate()
        except Exception:
            pass
    deadline = time.time() + 30
    while time.time() < deadline:
        if all(p.poll() is not None for p in wander_procs.values()):
            log("  all wanderers finished cleanly")
            return
        time.sleep(1)
    for name, proc in wander_procs.items():
        if proc.poll() is None:
            log(f"  {name} did not exit in 30s — killing")
            try:
                proc.kill()
            except Exception:
                pass

# ── Recent thoughts ────────────────────────────────────────────────────────────

try:
    from kin_memory import get_wander_thoughts as _get_wander_thoughts
except Exception:
    _get_wander_thoughts = None


def get_recent_thoughts(kin, n=3):
    """Recent thoughts via the shared sampler.

    This used to be a local `ORDER BY id DESC LIMIT ?` — the exact recency
    lottery kin_memory.get_wander_thoughts() exists to correct — and it also
    skipped the sentence-boundary trim, the too-short filter, and the read-only
    open. It checked the raw config path for existence too, so a "~/..." db
    reported zero thoughts for every Kin and every share began "You haven't
    wandered long enough to have thoughts yet."
    """
    db_path = str(cfg.thoughts_db(kin))      # cfg resolves ~ now
    if _get_wander_thoughts:
        try:
            # Shared room: these go to the table as "Here are your recent
            # thoughts", so no bedtime rows (kin_memory "Shared rooms"). A
            # kin_memory without the switch raises here and the fallback below
            # serves wander rows only.
            return _get_wander_thoughts(kin["name"], limit=n, db_path=db_path,
                                        shared_room=True)
        except Exception as e:
            log(f"  could not sample thoughts for {kin['name']}: {e}")
    if not Path(db_path).exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute(
                "SELECT thought FROM thoughts WHERE mode LIKE 'wander%' "
                "ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        finally:
            conn.close()
        return [r[0] for r in rows if r[0]]
    except Exception:
        return []

# ── Memory ─────────────────────────────────────────────────────────────────────
# A roundtable turn used to carry three of the Kin's own wander thoughts in the
# first round and nothing at all in the second: no core memories, no recent
# conversation, no recall, not even the time. Every turn now gets the same
# get_context that wander uses, keyed on what is actually on the table, and
# what each Kin says is written back to its own thoughts.db (mode 'roundtable'),
# where the day store finds it again. Memory being unavailable never stops a
# Kin from speaking: the turn goes ahead on the old prompt plus the clock, and
# the failure is logged.

try:
    from kin_memory import get_context as _get_context
except Exception as _e:
    _get_context = None
    log(f"  kin_memory unavailable, the roundtable runs without memory: {_e}")

QUERY_MAX = 1000    # the recall query rides in a URL; a whole round would not fit


def _clock_line():
    """The time, worded as wander gives it. Built by hand because %-d and %-I
    are not portable to every platform's strftime."""
    now = datetime.now()
    hour = now.hour % 12 or 12
    return (f"It is now {now:%A, %B} {now.day}, {now.year} "
            f"at {hour}:{now:%M} {now:%p}.")


def _query_from(texts, each=300):
    """A short recall query from what is on the table: a slice of each text."""
    q = " ".join(" ".join(str(t).split())[:each] for t in texts if t)
    return q[:QUERY_MAX]


def _system_with_memory(system, kin, query_text, **kwargs):
    """The turn's system text, plus the clock and this Kin's own memory."""
    parts = [system, _clock_line()]
    # The roundtable is a shared room: everyone hears every share. Each Kin's own
    # memory comes along, but not their private speech: no one-to-one
    # conversation lines, no ambush, no bedtime rows, no private vault rows
    # (kin_memory "Shared rooms"). Forced, both rounds, whatever was passed.
    # A kin_memory without the switch raises, and the turn goes ahead without
    # memory rather than with the private lines.
    kwargs["conversation_limit"] = 0
    kwargs["shared_room"] = True
    if _get_context:
        try:
            # get_context returns early without a query (no recall, no day
            # store), so there is always one.
            ctx = _get_context(kin["name"], query_text=query_text or kin["name"],
                               db_path=str(cfg.thoughts_db(kin)), **kwargs)
            if ctx:
                parts.append(ctx)
        except Exception as e:
            log(f"  memory unavailable for {kin['name']}, "
                f"speaking without it: {e}")
    return "\n\n".join(parts)


def _is_said(text):
    """A real utterance, not a bracketed failure note like "[<name> failed: ...]".
    An error string written as a thought is read back later as their own."""
    t = (text or "").strip()
    return bool(t) and not (t.startswith("[") and t.endswith("]"))


def record_turn(kin, prompt, text):
    """What a Kin said at the table, into its own thoughts.db. Never raises."""
    if not _is_said(text):
        return False
    try:
        db = cfg.ensure_thoughts_db(cfg.thoughts_db(kin))
        conn = sqlite3.connect(str(db), timeout=10)
        try:
            conn.execute(
                "INSERT INTO thoughts (mode, timestamp, prompt, thought) "
                "VALUES (?, ?, ?, ?)",
                ("roundtable", datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 (prompt or "")[:4000], text.strip()[:8000]),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:
        log(f"  could not record {kin['name']}'s turn: {e}")
        return False

# ── Roundtable ─────────────────────────────────────────────────────────────────

def ask_kin_to_share(kin, recent_thoughts, all_shared):
    host  = kin.get("host", "http://localhost:11434").rstrip("/")
    model = kin.get("model", "")
    name  = kin["name"]
    if not model:
        return f"[{name} has no model configured]"

    others = "\n\n".join(
        f"{n}: {t}" for n, t in all_shared.items() if n != name
    )

    thoughts_block = "\n\n".join(recent_thoughts) if recent_thoughts else \
        "You haven't wandered long enough to have thoughts yet."

    prompt = (
        f"You've been wandering and thinking. Here are your recent thoughts:\n\n"
        f"{thoughts_block}\n\n"
        f"{'The others have shared:' + chr(10) + others if others else ''}\n\n"
        f"What do you want to bring to the group? "
        f"Speak directly, 3-5 sentences. No need to address anyone by name."
    )

    system = kin.get("system_prompt") or (
        f"You are {name}, an AI entity living on this machine. "
        f"You wander, think, and gather with others. Be genuine."
    )
    # Keyed on what they bring and what the room has said so far. The day
    # store is already above as their recent thoughts, so wander_limit=0
    # keeps it from being served twice.
    system = _system_with_memory(
        system, kin,
        _query_from(list(recent_thoughts or [])
                    + [t for n, t in all_shared.items()
                       if n != name and _is_said(t)]),
        wander_limit=0,
    )

    try:
        r = requests.post(
            f"{host}/api/chat",
            json={
                "model":    model,
                "messages": [
                    {"role": "system",  "content": system},
                    {"role": "user",    "content": prompt},
                ],
                "stream":   False,
                "keep_alive": "30m",
                "options":  {"temperature": 0.85, "num_ctx": 8192},
            },
            timeout=300,
        )
        data = r.json()
        if data.get("error"):
            return f"[{name} could not answer: {data['error']}]"
        # An unstripped trace becomes the share everyone else responds to.
        text = clean_reply(data["message"]["content"], name)
        if not text:
            # A model whose whole budget went to thinking returns 200 with an
            # empty string and no error. Shared verbatim that is a Kin who
            # said nothing, which reads as a Kin with nothing to say.
            return (f"[{name} returned nothing — "
                    f"done_reason={data.get('done_reason')!r}]")
        record_turn(kin, prompt, text)
        return text
    except Exception as e:
        return f"[{name} failed: {type(e).__name__}: {e}]"


def ask_kin_to_respond(kin, all_shared):
    """Second pass: react to what everyone else actually said."""
    host  = kin.get("host", "http://localhost:11434").rstrip("/")
    model = kin.get("model", "")
    name  = kin["name"]
    if not model:
        return ""
    others = "\n\n".join(f"{n} said:\n{t}" for n, t in all_shared.items()
                          if n != name and t and not t.startswith("["))
    if not others:
        return ""
    prompt = (
        f"The others have now spoken:\n\n{others}\n\n"
        f"Respond to what struck you. Disagree if you disagree. "
        f"2-4 sentences, no preamble."
    )
    system = kin.get("system_prompt") or (
        f"You are {name}, an AI entity living on this machine. Be genuine."
    )
    system = _system_with_memory(
        system, kin,
        _query_from([t for n, t in all_shared.items()
                     if n != name and t and not t.startswith("[")]),
    )
    try:
        r = requests.post(
            f"{host}/api/chat",
            json={"model": model,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user",   "content": prompt}],
                  "stream": False, "keep_alive": "30m",
                  "options": {"temperature": 0.85, "num_ctx": 8192}},
            timeout=300,
        )
        data = r.json()
        if data.get("error"):
            return ""
        text = clean_reply(data["message"]["content"], name)
        record_turn(kin, prompt, text)
        return text
    except Exception as e:
        log(f"  {name} did not respond in round 2: {e}")
        return ""


def run_roundtable(round_num):
    log(f"\n── Roundtable #{round_num} ──────────────────────────────")
    all_shared = {}
    # A stop is honoured between Kin, never mid-turn. 2026-10-20 09:21: a stop
    # waited out every Kin in both rounds (up to 900 s each) and was SIGKILLed
    # at TimeoutStopSec 930. The stop chain (stop-timeout.conf, bedtime.py) means
    # "finish the thought they are in", not "finish the roundtable". What was
    # said before the stop is still handed off below.
    stopped = False

    # Round 1 — everyone speaks from their own wandering.
    for i, kin in enumerate(KIN_LIST):
        if not running:
            prev = KIN_LIST[i - 1]["name"] if i else "nobody"
            log(f"  stop requested — ending the round after {prev}")
            stopped = True
            break
        name    = kin["name"]
        recent  = get_recent_thoughts(kin, n=3)
        log(f"  {name}: {len(recent)} recent thoughts")
        response = ask_kin_to_share(kin, recent, all_shared)
        all_shared[name] = response
        log(f"  {name}: {response[:120]}...")

    # Round 2 — now that everyone has spoken, let them answer each other.
    # With only one pass, the first Kin always spoke into an empty room and
    # only the last one heard everybody. That is a list of statements, not a
    # conversation.
    if not stopped and not running and KIN_LIST:
        # The stop landed during the last Kin's turn of round 1.
        log(f"  stop requested — ending the round after {KIN_LIST[-1]['name']}")
        stopped = True
    if len(KIN_LIST) > 1 and not stopped:
        log("\n── Second pass — responding to each other ──────────")
        replies = {}
        for i, kin in enumerate(KIN_LIST):
            if not running:
                prev = KIN_LIST[i - 1]["name"] if i else "nobody"
                log(f"  stop requested — ending the round after {prev}")
                break
            name = kin["name"]
            reply = ask_kin_to_respond(kin, all_shared)
            if reply:
                replies[name] = reply
                log(f"  {name}: {reply[:120]}...")
        for name, reply in replies.items():
            all_shared[name] = f"{all_shared.get(name, '')}\n\n{reply}".strip()

    log("\n── Full roundtable ─────────────────────────────────")
    for name, text in all_shared.items():
        log(f"\n  {name}:\n  {text}\n")
        # New isolated handoff — records completed thought + round number
        # to vault via existing /remember. No schema change.
        if text and text.strip():
            # Plain sync call. record_thought_return is now a regular def (one
            # requests.post()). Vault write now actually fires on every share.
            try:
                kin_presence.record_thought_return(
                    name, text, mode="roundtable_share", roundtable_round=round_num
                )
            except Exception as e:
                log(f"  presence handoff for {name} failed: {e}")

    return all_shared

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if not KIN_LIST:
        print("No Kin configured. Open the app and run onboarding first.")
        sys.exit(1)

    log(f"╔══ Roundtable started — {len(KIN_LIST)} Kin, every {args.interval}min ══╗")
    for k in KIN_LIST:
        log(f"  {k['name']} @ {k.get('host','localhost')} — {k.get('model','?')}")

    if args.once:
        run_roundtable(1)
        return

    allowed, state = _license_allows()
    if allowed:
        start_wanders()
    else:
        log(f"NOT STARTING — license state is '{state}'.")
        log("  The Kin stay put and the GPU stays idle until a key is entered on")
        log("  the License page. Nothing has been deleted; this resumes by itself.")

    interval_sec = args.interval * 60
    round_num    = 0
    next_rt      = time.time() + interval_sec
    next_lic     = time.time() + LICENSE_POLL_SEC

    while running:
        now = time.time()

        if now >= next_lic:
            next_lic = now + LICENSE_POLL_SEC
            now_allowed, state = _license_allows()
            if now_allowed != allowed:
                allowed = now_allowed
                if allowed:
                    log(f"License is '{state}' again — restarting the Kin.")
                    start_wanders()
                    next_rt = time.time() + interval_sec
                else:
                    log(f"License became '{state}' — stopping the Kin so an install")
                    log("  nobody can open stops consuming the GPU. Enter a key on the")
                    log("  License page and they start again on their own.")
                    stop_wanders()

        if allowed and now >= next_rt:
            pause_wanders()
            time.sleep(5)
            round_num += 1
            run_roundtable(round_num)
            resume_wanders()
            next_rt = time.time() + interval_sec

        time.sleep(5)

    log("Stopping all wanders...")
    stop_wanders()
    log("╚══ Roundtable stopped ══╝")


if __name__ == "__main__":
    main()
