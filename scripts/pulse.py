#!/usr/bin/env python3
"""
pulse.py — Heartbeat daemon. Samples the machine every minute,
writes a narrative to the vault every 5 minutes.

Run as a systemd service (installer sets this up).
The Kin read this pulse at the start of every session so they know
what's been happening while they were away.

Usage:
    python3 pulse.py               # run forever
    python3 pulse.py --once        # one sample and exit
    python3 pulse.py --interval 3  # write every 3 minutes
"""

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import config as cfg

# ── Args ───────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(description="Echo Bloom heartbeat daemon")
parser.add_argument("--once",     action="store_true", help="One pulse and exit")
parser.add_argument("--interval", type=int, default=5,
                    help="Minutes between vault writes (default: 5)")
args = parser.parse_args()

# ── Config ─────────────────────────────────────────────────────────────────────

VAULT_URL = cfg.vault_url()
HOSTNAME  = platform.node()
AUTHOR    = f"pulse_{HOSTNAME}"

LOG_DIR  = Path.home() / ".local/share/echo_bloom/logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "pulse.log"

# ── Logging ────────────────────────────────────────────────────────────────────

def log(msg):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

# ── System sampling ────────────────────────────────────────────────────────────

def _run(cmd):
    try:
        return subprocess.check_output(cmd, text=True, timeout=5).strip()
    except Exception:
        return ""


def sample():
    snap = {"ts": datetime.now().isoformat()}

    # CPU load
    load = _run(["cat", "/proc/loadavg"]).split()
    snap["load_1m"]  = load[0] if len(load) > 0 else "?"
    snap["load_5m"]  = load[1] if len(load) > 1 else "?"

    # RAM
    mem = _run(["free", "-m"]).splitlines()
    for line in mem:
        if line.startswith("Mem:"):
            parts = line.split()
            snap["ram_total_mb"] = parts[1]
            snap["ram_used_mb"]  = parts[2]
            snap["ram_free_mb"]  = parts[3]

    # Disk
    df = _run(["df", "-h", str(Path.home())]).splitlines()
    if len(df) > 1:
        parts = df[1].split()
        snap["disk_used"]  = parts[2]
        snap["disk_avail"] = parts[3]
        snap["disk_pct"]   = parts[4]

    # GPU (optional)
    vram = _run(["nvidia-smi", "--query-gpu=memory.used,memory.total",
                  "--format=csv,noheader,nounits"])
    if vram:
        snap["vram"] = vram.replace("\n", " | ")

    # Ollama running?
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=3)
        models = [m["name"] for m in r.json().get("models", [])]
        snap["ollama_models"] = ", ".join(models[:5]) or "none loaded"
    except Exception:
        snap["ollama_models"] = "offline"

    # Running Kin wanders
    pgrep = _run(["pgrep", "-af", "wander.py"])
    snap["wanderers"] = len(pgrep.splitlines()) if pgrep else 0

    return snap


def build_narrative(snap):
    ts_nice = datetime.fromisoformat(snap["ts"]).strftime("%B %d, %Y at %H:%M")
    load    = snap.get("load_1m", "?")
    ram_u   = snap.get("ram_used_mb", "?")
    ram_t   = snap.get("ram_total_mb", "?")
    disk    = snap.get("disk_pct", "?")
    vram    = snap.get("vram", "")
    models  = snap.get("ollama_models", "unknown")
    wanders = snap.get("wanderers", 0)

    lines = [
        f"At {ts_nice} on {HOSTNAME}:",
        f"Load {load}, RAM {ram_u}/{ram_t}MB, disk {disk} full.",
    ]
    if vram:
        lines.append(f"VRAM: {vram}.")
    lines.append(f"Ollama: {models}.")
    if wanders:
        lines.append(f"{wanders} Kin wander process{'es' if wanders != 1 else ''} running.")
    else:
        lines.append("No Kin wanders running.")

    return " ".join(lines)

# ── Vault write ────────────────────────────────────────────────────────────────

def write_to_vault(narrative):
    try:
        r = requests.post(
            f"{VAULT_URL}/remember",
            json={
                "content": narrative,
                "layer":   "heartbeat",
                "author":  AUTHOR,
                "tags":    "pulse,system",
            },
            timeout=5,
        )
        return r.ok
    except Exception as e:
        log(f"  Vault write failed: {e}")
        return False

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    log(f"╔══ Pulse started — {HOSTNAME} ══╗")
    log(f"  Vault: {VAULT_URL}")
    log(f"  Writing every {args.interval} minutes")

    if args.once:
        snap      = sample()
        narrative = build_narrative(snap)
        log(f"  {narrative}")
        ok = write_to_vault(narrative)
        log(f"  Vault write: {'OK' if ok else 'FAILED (vault may not be running)'}")
        return

    last_write = 0
    interval   = args.interval * 60

    while True:
        snap = sample()
        now  = time.time()
        if now - last_write >= interval:
            narrative = build_narrative(snap)
            ok        = write_to_vault(narrative)
            log(f"  {'OK' if ok else 'FAILED'}: {narrative[:100]}...")
            last_write = now
        time.sleep(60)


if __name__ == "__main__":
    main()
