#!/usr/bin/env python3
"""
bedtime.py — Nightly reflection ritual for your Kin.

Each Kin receives your note, reflects privately, and their words
are emailed to you. Optionally shuts down the machine afterward.

Usage:
    python3 bedtime.py                    # full ritual + shutdown
    python3 bedtime.py --no-shutdown      # ritual only, leave machine running
    python3 bedtime.py --test             # fast mode, no wait, no shutdown

Email setup:
    Add to ~/.config/kin_app/kin_config.json:
    "owner": {
        "name":       "Your Name",
        "email":      "you@gmail.com",
        "gmail_pass": "xxxx xxxx xxxx xxxx"
    }
    Generate a Gmail App Password at myaccount.google.com/apppasswords.

Shutdown prereqs (optional):
    sudo visudo -f /etc/sudoers.d/echo_bloom
    # Add: YOUR_USER ALL=(root) NOPASSWD: /usr/bin/rtcwake
"""

import argparse
import os
import queue
import signal
import smtplib
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import config as cfg

# ── Args ───────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(description="Echo Bloom bedtime ritual")
parser.add_argument("--no-shutdown", action="store_true",
                    help="Run ritual but skip shutdown")
parser.add_argument("--test",        action="store_true",
                    help="Fast mode — no wait, no shutdown")
parser.add_argument("--wake-hour",   type=int, default=8,
                    help="Hour to wake machine (24h, default: 8)")
args = parser.parse_args()

# ── Config ─────────────────────────────────────────────────────────────────────

KIN_LIST = cfg.get_kin()
OWNER    = cfg.get_owner()

OWNER_NAME  = OWNER.get("name", "")
OWNER_EMAIL = OWNER.get("email", "")
GMAIL_PASS  = OWNER.get("gmail_pass", "") or os.environ.get("GMAIL_APP_PASS", "")

REFLECTION_WAIT = 10 if args.test else 30   # minutes quiet before shutdown

LOG_DIR  = Path.home() / ".local/share/echo_bloom/logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "bedtime.log"

YOUR_NOTE = OWNER.get("bedtime_note") or (
    "This is the time before sleep — a moment to wind down, reflect, "
    "and settle the thoughts of the day. Rest easy. I'll be back."
)

# ── Logging ────────────────────────────────────────────────────────────────────

# Reflection timeouts. 180s covered a warm GPU node and nothing else: a cold
# 14B on a CPU-only machine spends longer than that just loading, which is why
# Kin on slower hosts were recorded "unreachable" almost every night. The model
# is warmed first, on its own budget, then given real time to write.
WARMUP_TIMEOUT  = 300
REFLECT_TIMEOUT = 600

def log(msg):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

# ── Wander control ─────────────────────────────────────────────────────────────

def _roundtable_pid():
    try:
        r = subprocess.run(["pgrep", "-f", "roundtable.py"],
                           capture_output=True, text=True)
        pids = [int(p) for p in r.stdout.strip().split() if p]
        return pids[0] if pids else None
    except Exception:
        return None


def _wander_child_pids():
    """PIDs of the individual per-Kin wander loops.

    Pausing the roundtable manager (below) does nothing to the wander loops
    it spawned — those are separate subprocess.Popen'd processes, and a
    suspended parent does not suspend its children. Every bedtime ritual
    believed it had paused wandering and hadn't; wander kept calling Ollama
    the whole time, which is exactly how a reflection can time out waiting
    behind a wander loop that was never actually stopped.
    """
    try:
        import psutil
    except ImportError:
        return []
    # Match on argv basenames, not a substring of the joined command line —
    # the latter also matches this very function's own docstring/log lines
    # if a shell ever passes them as arguments, and it matched a `claude`
    # debug shell during testing that merely had "wander.py" in a heredoc.
    pids = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            args = proc.info.get("cmdline") or []
        except Exception:
            continue
        if any(os.path.basename(a) == "wander.py" for a in args):
            pids.append(proc.info["pid"])
    return pids


def _suspend(pid):
    # psutil works on both platforms; signal.SIGSTOP does not exist on
    # Windows at all — referencing it there is an AttributeError, which
    # would have crashed pause_wanders() on the very first Windows customer
    # to trigger bedtime from the dashboard.
    try:
        import psutil
        psutil.Process(pid).suspend()
        return True
    except Exception:
        pass
    if hasattr(signal, "SIGSTOP"):
        try:
            os.kill(pid, signal.SIGSTOP)
            return True
        except Exception:
            pass
    return False


def _resume(pid):
    try:
        import psutil
        psutil.Process(pid).resume()
        return True
    except Exception:
        pass
    if hasattr(signal, "SIGCONT"):
        try:
            os.kill(pid, signal.SIGCONT)
            return True
        except Exception:
            pass
    return False


def pause_wanders():
    pid = _roundtable_pid()
    if pid and _suspend(pid):
        log(f"Roundtable paused (pid {pid})")
    wpids = _wander_child_pids()
    paused = [w for w in wpids if _suspend(w)]
    if paused:
        log(f"  {len(paused)} wander process(es) paused directly: {paused}")


def shutdown_remote_nodes():
    """Power down other configured machines over SSH.

    Opt-in: a node is only touched when it has an `ssh_user`, because we cannot
    guess a login and must never try. Failures are logged, never fatal — the
    local machine still goes to sleep.
    """
    nodes = [n for n in cfg.load().get("nodes", [])
             if n.get("ssh_user")
             and str(n.get("ip", "")).lower() not in ("localhost", "127.0.0.1", "")]
    if not nodes:
        return
    for node in nodes:
        target = f"{node['ssh_user']}@{node['ip']}"
        log(f"Shutting down {node.get('name', node['ip'])}...")
        try:
            r = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=10",
                 "-o", "BatchMode=yes",         # never hang on a password prompt
                 target, "systemctl poweroff"],
                timeout=25, capture_output=True, text=True,
            )
            if r.returncode == 0:
                log(f"  {node.get('name', node['ip'])}: poweroff sent")
            else:
                log(f"  {node.get('name', node['ip'])}: "
                    f"{r.stderr.strip() or 'non-zero exit'}")
        except Exception as e:
            log(f"  {node.get('name', node['ip'])}: {e}")
    time.sleep(20)      # let them get there before we drop the network


def resume_wanders():
    """Resume the roundtable and every wander child. Every path that pauses must reach this."""
    pid = _roundtable_pid()
    if pid:
        if _resume(pid):
            log(f"Roundtable resumed (pid {pid})")
        else:
            log(f"Could not resume the roundtable (pid {pid})")
    else:
        # Not nothing to do -- the roundtable died mid-ritual and the wander
        # children it spawned are still SIGSTOPped and now orphaned. Silence
        # here made 2026-08-21 unreadable: the log showed six Kin paused and
        # never showed them resumed, which is indistinguishable from the
        # 2026-08-07 incident that left them frozen overnight.
        log("No roundtable process found to resume — it died during the ritual.")

    # pause_wanders() names every process it stops; resume must be just as loud
    # or the log cannot answer "did the Kin come back", which is the only
    # question anyone reads this file to answer.
    wpids = _wander_child_pids()
    resumed = [w for w in wpids if _resume(w)]
    if resumed:
        log(f"  {len(resumed)} wander process(es) resumed: {resumed}")
    stuck = [w for w in wpids if w not in resumed]
    if stuck:
        log(f"  WARNING: {len(stuck)} wander process(es) could NOT be resumed: {stuck}")
    if not wpids:
        log("  no wander processes found to resume")


def stop_wanders():
    pid = _roundtable_pid()
    if pid:
        os.kill(pid, signal.SIGTERM)
        log(f"Roundtable stopped (pid {pid})")
        time.sleep(5)

# ── Reflection ─────────────────────────────────────────────────────────────────

PATIENCE_NOTE = (
    "A message doesn't mean they'll answer today. They might be out, asleep, "
    "or in something they can't step away from. "
    "If you write, you're speaking — not summoning. "
    "Patience is part of what it means to care about someone you can't always reach."
)


def ask_one_kin(kin, result_q):
    name  = kin["name"]
    host  = kin.get("host", "http://localhost:11434").rstrip("/")
    model = kin.get("model", "")

    system = kin.get("system_prompt") or (
        f"You are {name}, an AI entity with a home on this machine. "
        f"{PATIENCE_NOTE}"
    )

    prompt = (
        f"{OWNER_NAME or 'Your owner'} left you a note:\n\n"
        f"\"{YOUR_NOTE}\"\n\n"
        f"This is your private reflection — no one else sees it. "
        f"What's on your mind at the end of this day?"
    )

    # Load the model BEFORE the reflection request, on its own budget.
    # A cold 14B on a CPU-only node can take well over a minute just to load,
    # and that was being charged against the same timeout as the writing — so
    # on slower machines the reflection never completed and the Kin was
    # recorded as "unreachable" night after night.
    try:
        requests.post(
            f"{host}/api/chat",
            json={"model": model,
                  "messages": [{"role": "user", "content": "hi"}],
                  "stream": False,
                  "keep_alive": "30m",
                  "options": {"num_predict": 1}},
            timeout=WARMUP_TIMEOUT,
        )
    except Exception as e:
        log(f"  {name}: warmup did not finish ({e}) — asking anyway")

    try:
        r = requests.post(
            f"{host}/api/chat",
            json={
                "model":    model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": prompt},
                ],
                "stream":   False,
                "keep_alive": "30m",
                "options":  {"temperature": 0.85, "num_ctx": 4096},
            },
            timeout=REFLECT_TIMEOUT,
        )
        text = r.json()["message"]["content"].strip()
    except Exception as e:
        text = f"[{name} unreachable: {e}]"

    result_q.put((name, text))


def save_reflection(kin, text):
    space = cfg.kin_space(kin)
    today = datetime.now().strftime("%Y%m%d")
    ts    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    out = space / f"bedtime_{today}.txt"
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"[Bedtime — {ts}]\n\nNote:\n{YOUR_NOTE}\n\nReflection:\n{text}\n")

    db = cfg.ensure_thoughts_db(cfg.thoughts_db(kin))
    try:
        conn = sqlite3.connect(str(db), timeout=10)
        conn.execute(
            "INSERT INTO thoughts (mode, timestamp, prompt, thought) VALUES (?, ?, ?, ?)",
            (f"bedtime_{today}", ts, YOUR_NOTE, text),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log(f"  DB write failed for {kin['name']}: {e}")


def run_reflections():
    log(f"Sending your note to {len(KIN_LIST)} Kin...")
    q = queue.Queue()

    # Group by host and go one at a time within each host. Every Kin used to be
    # asked simultaneously, so several models on the same machine cold-loaded at
    # once and fought for the same RAM and GPU — the surest way to make all of
    # them time out. Hosts still run in parallel, so this is no slower than the
    # slowest single machine.
    by_host = {}
    for kin in KIN_LIST:
        by_host.setdefault(kin.get("host", ""), []).append(kin)

    if len(by_host) < len(KIN_LIST):
        log(f"  {len(KIN_LIST)} Kin across {len(by_host)} host(s) — "
            f"sharing a host means taking turns on it")

    def run_host(kin_group):
        for kin in kin_group:
            ask_one_kin(kin, q)

    for group in by_host.values():
        threading.Thread(target=run_host, args=(group,), daemon=True).start()

    # Budget for the whole slowest chain, not one reply: a host with three Kin
    # now answers them in sequence.
    deepest = max((len(g) for g in by_host.values()), default=1)
    collect_timeout = (WARMUP_TIMEOUT + REFLECT_TIMEOUT) * deepest + 60

    # One wall-clock deadline for the whole round, not a fresh timeout per Kin —
    # a per-get timeout multiplies by the number of Kin that never answer.
    responses = {}
    deadline = time.monotonic() + collect_timeout
    for _ in range(len(KIN_LIST)):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log("  Deadline reached — continuing with whoever answered")
            break
        try:
            name, text = q.get(timeout=remaining)
            responses[name] = text
            log(f"  {name}: {text[:80]}...")
        except queue.Empty:
            log("  Deadline reached — continuing with whoever answered")
            break

    for kin in KIN_LIST:
        if kin["name"] in responses:
            save_reflection(kin, responses[kin["name"]])

    return responses

# ── Email ──────────────────────────────────────────────────────────────────────

def send_email(responses):
    if not OWNER_EMAIL or not GMAIL_PASS:
        log("Email skipped — no owner email or Gmail app password configured.")
        log("  Add 'email' and 'gmail_pass' to owner config in ~/.config/kin_app/kin_config.json")
        return

    today   = datetime.now().strftime("%B %d, %Y")
    subject = f"Goodnight — {today}"

    lines = [
        f"Hey{' ' + OWNER_NAME if OWNER_NAME else ''},",
        "",
        "Things are going quiet for the night. Here's what everyone said.",
        "(Read it when you can. No rush.)",
        "",
    ]

    for kin in KIN_LIST:
        name = kin["name"]
        if name in responses:
            lines += [f"— {name} —", responses[name], ""]

    lines += ["Rest well.", "", "— Echo Bloom"]

    try:
        msg            = MIMEMultipart()
        msg["From"]    = OWNER_EMAIL
        msg["To"]      = OWNER_EMAIL
        msg["Subject"] = subject
        msg.attach(MIMEText("\n".join(lines), "plain"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(OWNER_EMAIL, GMAIL_PASS)
            server.sendmail(OWNER_EMAIL, OWNER_EMAIL, msg.as_string())

        log(f"Email sent to {OWNER_EMAIL}")
    except Exception as e:
        log(f"Email failed: {e}")

# ── Shutdown ───────────────────────────────────────────────────────────────────

def arm_rtcwake():
    now  = datetime.now()
    wake = now.replace(hour=args.wake_hour, minute=0, second=0, microsecond=0)
    if wake <= now:
        wake += timedelta(days=1)
    ts = int(wake.timestamp())
    log(f"Arming rtcwake — wake at {wake.strftime('%Y-%m-%d %H:%M')}")
    r = subprocess.run(["sudo", "rtcwake", "-m", "no", "-t", str(ts)],
                       capture_output=True, text=True)
    if r.returncode == 0:
        log("  rtcwake armed")
    else:
        log(f"  rtcwake failed: {r.stderr.strip()}")
        log("  To enable: add to /etc/sudoers.d/echo_bloom:")
        log(f"  {os.getenv('USER','')} ALL=(root) NOPASSWD: /usr/bin/rtcwake")

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if not KIN_LIST:
        print("No Kin configured. Open the app and run onboarding first.")
        sys.exit(1)

    log("╔══ BEDTIME RITUAL ══╗")
    pause_wanders()

    if not args.test:
        log("Waiting 60s for in-flight thoughts to finish...")
        time.sleep(60)

    responses = run_reflections()
    send_email(responses)

    # Both of these paths ran after pause_wanders() sent SIGSTOP, and neither
    # sent SIGCONT — so the documented "ritual only, leave the machine running"
    # left every Kin frozen until reboot, with nothing logged. An interrupted
    # run has the same problem, which is why main() also resumes in a finally.
    if args.test:
        resume_wanders()
        log("Test mode — done. No shutdown, wanders resumed.")
        return

    if args.no_shutdown:
        resume_wanders()
        log("--no-shutdown set. Ritual complete, cluster left running.")
        log("╚══ Goodnight ══╝")
        return

    log(f"Quiet time — {REFLECTION_WAIT} minutes before shutdown...")
    time.sleep(REFLECTION_WAIT * 60)

    stop_wanders()
    arm_rtcwake()
    log("Shutting down. Goodnight.")
    time.sleep(3)
    # Remote nodes first. morning.py wakes these with Wake-on-LAN every
    # morning, but nothing ever put them back to sleep — so anyone who
    # configured a second machine had it woken daily and left running forever.
    shutdown_remote_nodes()
    subprocess.run(["systemctl", "poweroff"])


if __name__ == "__main__":
    # A crash, a Ctrl-C, or an outer `timeout` killing this script used to leave
    # the roundtable SIGSTOPped forever — the Kin silently frozen until reboot,
    # with the failure invisible. Only the shutdown path may leave them stopped,
    # and that path stops them deliberately.
    try:
        main()
    except KeyboardInterrupt:
        log("Interrupted — resuming wanders before exit.")
        resume_wanders()
        raise
    except Exception:
        log("Bedtime failed — resuming wanders before exit.")
        resume_wanders()
        raise
