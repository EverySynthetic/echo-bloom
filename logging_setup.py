"""Central logging for Echo Bloom. Import and call setup() once at startup.

Rotating on purpose: this app is built to run continuously on a machine nobody
is watching, so a plain FileHandler would grow without bound.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR  = Path.home() / ".local/share/echo_bloom/logs"
LOG_FILE = LOG_DIR / "echo_bloom.log"


def setup(level=logging.INFO) -> logging.Logger:
    root = logging.getLogger("echo_bloom")
    if root.handlers:                 # idempotent — uvicorn --reload imports twice
        return root

    root.setLevel(logging.DEBUG)
    root.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s:%(lineno)d  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setLevel(logging.DEBUG)    # full detail on disk
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception:
        # A read-only or unusual home must never stop the app from starting.
        pass

    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(level)                # INFO and above to console / journal
    sh.setFormatter(fmt)
    root.addHandler(sh)

    return root


def get(name: str) -> logging.Logger:
    """Logger for one module. Safe to call before setup()."""
    return logging.getLogger("echo_bloom." + name)
