#!/usr/bin/env python3
"""report.py — bundle what a tester's machine actually knows, for a bug report.

A tester says "it didn't work". That sentence costs a round trip and usually a
second one. This gathers the things we would ask for anyway -- version,
platform, licence state, and the tail of whatever logs exist -- so the first
message already contains the answer.

Two rules this module exists to honour:

* The tester sees the payload before it leaves. Logs carry their file paths and
  their Kin's thoughts. Sending that silently would be a small betrayal of
  someone doing us a favour, and a tester who can read what goes out is a
  tester who will keep sending them.

* Nothing secret rides along. Licence keys, tokens and API keys get redacted
  before the preview is built, not on the way out -- so what they approve is
  exactly what we get.

Log locations differ per platform and this is where the feature would have
silently failed on the tester's Mac: launchd writes to
~/Library/Logs/EchoBloom, not ~/.local/share/echo_bloom/logs.
"""

from __future__ import annotations

import os
import platform
import re
import sys
from datetime import datetime
from pathlib import Path

TAIL_LINES = 120
MAX_FIELD = 20_000

# Anything key-shaped, before a human ever sees the preview.
_REDACT = [
    (re.compile(r"\bEB1-[A-Za-z0-9_\-\.]+", re.I), "EB1-[redacted]"),
    (re.compile(r"\bxai-[A-Za-z0-9]{20,}", re.I), "xai-[redacted]"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}", re.I), "sk-[redacted]"),
    (re.compile(r"\bwhsec_[A-Za-z0-9]{20,}", re.I), "whsec_[redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{30,}\.[A-Za-z0-9_\-\.]+"), "[redacted token]"),
    (re.compile(r"(password|passwd|secret|token|api[_-]?key)\s*[=:]\s*\S+", re.I),
     r"\1=[redacted]"),
]


def _redact(text: str) -> str:
    for pat, sub in _REDACT:
        text = pat.sub(sub, text)
    return text


def log_dirs() -> list[Path]:
    """Every place this app's logs land, per platform.

    macOS runs under launchd, which captures stdout/stderr to
    ~/Library/Logs/EchoBloom -- a crash lands THERE, not in the app's own log
    dir. Reading only the Linux path would have made this feature return
    "no logs found" on exactly the machine we built it for.
    """
    home = Path.home()
    dirs = [home / ".local/share/echo_bloom/logs"]
    if sys.platform == "darwin":
        dirs.append(home / "Library/Logs/EchoBloom")
    elif os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(Path(local) / "EchoBloom" / "logs")
            dirs.append(Path(local) / "EchoBloom")
    return dirs


def _tail(path: Path, n: int = TAIL_LINES) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:
        return f"[could not read: {e}]"
    return "\n".join(lines[-n:])


def collect_logs() -> tuple[dict, list[str]]:
    """(name -> tail, missing[]). Missing is reported, never silently empty."""
    wanted = ["echo_bloom.log", "app.log", "install.log", "install_wizard.log",
              "roundtable.log", "reflect.log", "bedtime.log", "morning.log"]
    found, missing = {}, []
    for name in wanted:
        for d in log_dirs():
            p = d / name
            if p.is_file():
                found[str(p)] = _redact(_tail(p))
                break
        else:
            missing.append(name)
    return found, missing


def _licence_state() -> str:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import license as _lic
        allowed, state = _lic.services_should_run()
        return f"{state} (services_should_run={allowed})"
    except Exception as e:
        return f"unavailable ({type(e).__name__})"


def environment() -> dict:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from version import VERSION
    except Exception:
        VERSION = "unknown"
    return {
        "version": VERSION,
        "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "python": sys.version.split()[0],
        "app_dir": str(Path(__file__).resolve().parent.parent),
        "licence": _licence_state(),
        "when": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def build(description: str = "") -> dict:
    """The whole bundle, redacted, ready to preview or send."""
    logs, missing = collect_logs()
    env = environment()
    parts = [
        "=== ECHO BLOOM PROBLEM REPORT ===",
        "",
        "--- what the person said ---",
        (description or "(no description given)").strip()[:MAX_FIELD],
        "",
        "--- their setup ---",
    ]
    parts += [f"{k:10} {v}" for k, v in env.items()]
    if missing:
        parts += ["", f"--- not present on this machine: {', '.join(missing)} ---"]
    for path, tail in logs.items():
        parts += ["", f"--- {path} (last {TAIL_LINES} lines) ---", tail[:MAX_FIELD]]
    if not logs:
        parts += ["", "--- no log files found in any known location ---",
                  "searched: " + ", ".join(str(d) for d in log_dirs())]
    return {
        "preview": "\n".join(parts),
        "attached": list(logs.keys()),
        "missing": missing,
        "environment": env,
    }


if __name__ == "__main__":
    b = build(" ".join(sys.argv[1:]))
    print(b["preview"])
    print(f"\n[attached {len(b['attached'])} log(s); missing: {b['missing']}]")
