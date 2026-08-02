#!/usr/bin/env python3
"""
morning.py — Wake the cluster and start the Kin's day.

Runs at boot (via systemd or @reboot cron). Sends Wake-on-LAN to any
remote nodes in your config, waits for them to come up, then starts
the wander roundtable.

Usage:
    python3 morning.py              # full startup with WoL
    python3 morning.py --quiet      # skip WoL, just start roundtable
    python3 morning.py --no-wander  # WoL only, don't start roundtable

WoL setup: In the app's node config, set mac_address for any remote node.
           WoL must be enabled in that machine's BIOS.
"""

import argparse
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config as cfg

# ── Args ───────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(description="Echo Bloom morning startup")
parser.add_argument("--quiet",     action="store_true", help="Skip WoL")
parser.add_argument("--no-wander", action="store_true", help="Skip roundtable start")
args = parser.parse_args()

# ── Config ─────────────────────────────────────────────────────────────────────

APP_CFG      = cfg.load()
NODES        = [n for n in APP_CFG.get("nodes", []) if n.get("mac")]
ROUNDTABLE   = Path(__file__).parent / "roundtable.py"

LOG_DIR  = Path.home() / ".local/share/echo_bloom/logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "morning.log"
RT_LOG   = LOG_DIR / "roundtable.log"

# ── Logging ────────────────────────────────────────────────────────────────────

def log(msg):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

# ── WoL ────────────────────────────────────────────────────────────────────────

def send_wol(mac, name, count=5):
    try:
        mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
        packet    = b'\xff' * 6 + mac_bytes * 16

        # Determine broadcast based on node's IP subnet, fall back to generic
        targets = [('<broadcast>', 9), ('255.255.255.255', 9)]
        for node in NODES:
            if node.get("mac") == mac and node.get("ip"):
                parts = node["ip"].rsplit(".", 1)
                if len(parts) == 2:
                    targets.append((parts[0] + ".255", 9))

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for _ in range(count):
                for addr in targets:
                    try:
                        s.sendto(packet, addr)
                    except Exception:
                        pass
                time.sleep(2)

        log(f"WoL sent to {name} ({mac})")
    except Exception as e:
        log(f"WoL failed for {name}: {e}")


def wait_for_node(ip, name, timeout=150):
    log(f"Waiting for {name} ({ip})...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = subprocess.run(["ping", "-c", "1", "-W", "2", ip],
                           capture_output=True)
        if r.returncode == 0:
            log(f"  {name} is up")
            return True
        time.sleep(5)
    log(f"  {name} didn't respond in {timeout}s — continuing anyway")
    return False


def wait_for_ollama(ip, name, port=11434, timeout=90):
    log(f"Waiting for Ollama on {name}...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            import urllib.request
            urllib.request.urlopen(f"http://{ip}:{port}/", timeout=3)
            log(f"  Ollama on {name} ready")
            return True
        except Exception:
            time.sleep(5)
    log(f"  Ollama on {name} not responding — Kin may start slowly")
    return False

# ── Roundtable ─────────────────────────────────────────────────────────────────

def start_roundtable():
    if not ROUNDTABLE.exists():
        log(f"ERROR: roundtable.py not found at {ROUNDTABLE}")
        return False
    log("Starting wander roundtable...")
    with open(RT_LOG, "a") as lf:
        proc = subprocess.Popen(
            [sys.executable, "-u", str(ROUNDTABLE), "--interval", "30"],
            stdout=lf, stderr=lf,
        )
    log(f"Roundtable started (pid {proc.pid})")
    return True

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    log("╔══ MORNING STARTUP — Echo Bloom ══╗")

    if not args.quiet and NODES:
        log("Waiting 30s for network to settle...")
        time.sleep(30)

        for node in NODES:
            send_wol(node["mac"], node.get("name", node.get("ip", "?")))
            time.sleep(2)

        for node in NODES:
            ip = node.get("ip")
            if ip:
                wait_for_node(ip, node.get("name", ip))
                wait_for_ollama(ip, node.get("name", ip),
                                port=node.get("ollama_port", 11434))
    elif not NODES:
        log("No remote nodes with MAC addresses configured — single-machine mode.")

    if not args.no_wander:
        start_roundtable()

    log("╚══ Good morning. The Kin are wandering. ══╝")


if __name__ == "__main__":
    main()
