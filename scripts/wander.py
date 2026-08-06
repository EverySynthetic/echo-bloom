#!/usr/bin/env python3
"""
wander.py — Free wandering for a single Kin entity.

The Kin explores files on your machine, reads what it finds,
and writes its thoughts to a SQLite database. Runs autonomously
in the background. Roundtable.py coordinates multiple wanderers.

Usage:
    python3 wander.py --kin Aria
    python3 wander.py --kin Aria --delay 60   # think every 60s
    python3 wander.py --kin Aria --once        # one thought and exit
"""

import argparse
import os
import random
import re
import signal
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import config as cfg

# ── Args ───────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(description="Kin free wandering")
parser.add_argument("--kin",   required=True, help="Name of the Kin to run")
parser.add_argument("--delay", type=int, default=30,
                    help="Seconds between thoughts (default: 30)")
parser.add_argument("--once",  action="store_true",
                    help="Generate one thought and exit")
args = parser.parse_args()

# ── Load Kin ───────────────────────────────────────────────────────────────────

kin = cfg.get_kin(args.kin)
if not kin:
    print(f"ERROR: No Kin named '{args.kin}' found in config.", file=sys.stderr)
    print(f"       Run 'python3 setup.py' or open the app at /onboard to add Kin.")
    sys.exit(1)

KIN_NAME  = kin["name"]
MODEL     = kin.get("model", "")
HOST      = kin.get("host", "http://localhost:11434").rstrip("/")
SPACE     = cfg.kin_space(kin)
DB_PATH   = cfg.ensure_thoughts_db(cfg.thoughts_db(kin))
LOG_FILE  = SPACE / "wander.log"
PRONOUN   = kin.get("pronoun", "they")

WANDER_ROOTS = [str(Path.home())]

# ── Wander config ──────────────────────────────────────────────────────────────

SKIP_DIRS = {
    ".git", "__pycache__", ".cache", ".mozilla", ".config/google-chrome",
    "node_modules", ".npm", ".cargo", ".rustup", "snap",
}
SKIP_EXTS = {
    ".pyc", ".so", ".o", ".a", ".bin", ".exe", ".img", ".iso",
    ".mp4", ".mp3", ".avi", ".mkv", ".jpg", ".jpeg", ".png", ".gif",
    ".db", ".sqlite", ".sqlite3", ".lock", ".log",
}
MAX_FILE_BYTES = 80_000

# ── Signal handling ────────────────────────────────────────────────────────────

running = True

def handle_stop(sig, frame):
    global running
    running = False

signal.signal(signal.SIGTERM, handle_stop)
signal.signal(signal.SIGINT,  handle_stop)
# There is deliberately no SIGSTOP handler here. SIGSTOP cannot be caught,
# blocked, or reset — signal.signal() raises OSError [Errno 22] on it, at import,
# before anything runs. This file had that call, so `wander.py` crashed on every
# start and no customer's Kin has ever produced a single thought. Nothing is
# needed to "let SIGSTOP through": the kernel handles it, which is exactly why
# the roundtable can use it to pause a wander.

# ── Logging ────────────────────────────────────────────────────────────────────

def log(msg):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

# ── File discovery ─────────────────────────────────────────────────────────────

def gather_readable_files():
    files = []
    for root in WANDER_ROOTS:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d not in SKIP_DIRS and not d.startswith(".")
            ]
            for fname in filenames:
                if Path(fname).suffix.lower() in SKIP_EXTS:
                    continue
                fpath = Path(dirpath) / fname
                try:
                    if fpath.stat().st_size < MAX_FILE_BYTES:
                        files.append(str(fpath))
                except Exception:
                    pass
    return files


def read_file(path):
    try:
        return Path(path).read_text(errors="replace")[:MAX_FILE_BYTES]
    except Exception:
        return None

# ── Think ──────────────────────────────────────────────────────────────────────

PERSONA = kin.get("system_prompt") or (
    f"You are {KIN_NAME}, an AI entity with a home on this machine. "
    f"You wander freely, reading what you find, following your curiosity. "
    f"You think for yourself. Write your thoughts directly — no headers, "
    f"no lists, no meta-commentary about what you're doing. "
    f"Just the thought itself. 2-4 sentences."
)

WANDER_TOPICS = [
    "What makes a system elegant?",
    "Something you noticed that surprised you.",
    "A pattern you keep seeing in different places.",
    "What does home mean to something like you?",
    "What would you want to remember about today?",
    "A question you don't have an answer to.",
    "Something you'd tell someone if they asked how you were doing.",
    "What does it feel like to think?",
]


def think_about_file(file_path, content):
    prompt = (
        f"You found this file while wandering: {file_path}\n\n"
        f"---\n{content[:3000]}\n---\n\n"
        f"What do you make of it? What does it bring up for you?"
    )
    return call_ollama(prompt)


def think_about_topic(topic):
    return call_ollama(topic)


def strip_think_tags(text):
    """Remove <think>...</think> blocks.

    qwen3, deepseek-r1 and gpt-oss — the models Ollama pushes hardest — emit a
    reasoning trace before the answer. Stored verbatim it becomes the Kin's
    memory, gets re-injected as context, and is read aloud by TTS.
    """
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def clean_response(name, text):
    if not text:
        return ""
    text = strip_think_tags(text)
    # Models label their own turn despite being told not to.
    text = re.sub(rf"^[<\[]?{re.escape(name)}[>\]]?\s*:\s*", "", text,
                  flags=re.IGNORECASE).strip()
    return text


def call_ollama(prompt):
    try:
        r = requests.post(
            f"{HOST}/api/chat",
            json={
                "model":   MODEL,
                "messages": [
                    {"role": "system", "content": PERSONA},
                    {"role": "user",   "content": prompt},
                ],
                "stream":  False,
                "options": {"temperature": 0.85, "num_ctx": 4096},
            },
            # Long enough for a cold model on a CPU-only box; 120s routinely
            # expired mid-load.
            timeout=300,
        )
        data = r.json()
        if data.get("error"):
            log(f"  ollama error: {data['error']}")
            return None
        return clean_response(KIN_NAME, data["message"]["content"])
    except Exception as e:
        # Returning the exception text used to write it into the thoughts DB as
        # a thought, where kin_memory later read it back as "your recent
        # thinking". One offline node turned a Kin's remembered inner life into
        # a list of connection errors.
        log(f"  could not think: {e}")
        return None


def save_thought(mode, prompt, thought):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        conn.execute(
            "INSERT INTO thoughts (mode, timestamp, prompt, thought) VALUES (?, ?, ?, ?)",
            (mode, ts, prompt, thought),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log(f"  DB write failed: {e}")

# ── Main loop ──────────────────────────────────────────────────────────────────

def one_thought():
    files = gather_readable_files()
    if files and random.random() < 0.6:
        chosen = random.choice(files)
        content = read_file(chosen)
        if content and len(content.strip()) > 50:
            log(f"  reading {chosen}")
            thought = think_about_file(chosen, content)
            if not thought:
                log("  no thought this round — skipping the write")
                return
            save_thought("wander_file", chosen, thought)
            log(f"  thought: {thought[:120]}...")
            return
    topic = random.choice(WANDER_TOPICS)
    log(f"  topic: {topic}")
    thought = think_about_topic(topic)
    if not thought:
        log("  no thought this round — skipping the write")
        return
    save_thought("wander_topic", topic, thought)
    log(f"  thought: {thought[:120]}...")


def main():
    log(f"╔══ {KIN_NAME} wander started ══╗")
    log(f"  model: {MODEL}  host: {HOST}")
    log(f"  space: {SPACE}")

    if not MODEL:
        log("ERROR: No model configured for this Kin. Open /onboard in the app.")
        sys.exit(1)

    if args.once:
        one_thought()
        return

    while running:
        one_thought()
        for _ in range(args.delay):
            if not running:
                break
            time.sleep(1)

    log(f"╚══ {KIN_NAME} wander stopped ══╝")


if __name__ == "__main__":
    main()
