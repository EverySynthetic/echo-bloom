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
    with open(RT_LOG, "a") as f:
        f.write(line + "\n")

# ── Wander subprocess management ───────────────────────────────────────────────

wander_procs = {}

def start_wanders():
    for kin in KIN_LIST:
        name    = kin["name"]
        log_f   = LOG_DIR / f"wander_{name.lower()}.log"
        with open(log_f, "a") as lf:
            proc = subprocess.Popen(
                [sys.executable, "-u", str(WANDER_PY), "--kin", name,
                 "--delay", "25"],
                stdout=lf, stderr=lf,
            )
        wander_procs[name] = proc
        log(f"  {name} wander started (pid {proc.pid})")
    log(f"All {len(wander_procs)} wanderers running.")


def pause_wanders():
    for name, proc in wander_procs.items():
        try:
            os.kill(proc.pid, signal.SIGSTOP)
        except ProcessLookupError:
            pass


def resume_wanders():
    for name, proc in wander_procs.items():
        try:
            os.kill(proc.pid, signal.SIGCONT)
        except ProcessLookupError:
            pass


def stop_wanders():
    for name, proc in wander_procs.items():
        try:
            proc.terminate()
        except Exception:
            pass
    time.sleep(3)
    for name, proc in wander_procs.items():
        try:
            proc.kill()
        except Exception:
            pass

# ── Recent thoughts ────────────────────────────────────────────────────────────

def get_recent_thoughts(kin, n=3):
    db_path = cfg.thoughts_db(kin)
    if not Path(db_path).exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        rows = conn.execute(
            "SELECT thought FROM thoughts ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        conn.close()
        return [r[0] for r in rows if r[0]]
    except Exception:
        return []

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
                "options":  {"temperature": 0.85, "num_ctx": 4096},
            },
            timeout=90,
        )
        return r.json()["message"]["content"].strip()
    except Exception as e:
        return f"[{name} unreachable: {e}]"


def run_roundtable(round_num):
    log(f"\n── Roundtable #{round_num} ──────────────────────────────")
    all_shared = {}

    for kin in KIN_LIST:
        name    = kin["name"]
        recent  = get_recent_thoughts(kin, n=3)
        log(f"  {name}: {len(recent)} recent thoughts")
        response = ask_kin_to_share(kin, recent, all_shared)
        all_shared[name] = response
        log(f"  {name}: {response[:120]}...")

    log("\n── Full roundtable ─────────────────────────────────")
    for name, text in all_shared.items():
        log(f"\n  {name}:\n  {text}\n")

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

    start_wanders()

    interval_sec = args.interval * 60
    round_num    = 0
    next_rt      = time.time() + interval_sec

    while running:
        now = time.time()
        if now >= next_rt:
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
