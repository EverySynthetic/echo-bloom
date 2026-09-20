"""Agora Node Bridge for Echo Bloom.

Connects Echo Bloom installs to the Agora protocol without creating a coupling
from kin_diary back to Echo Bloom. kin_diary's README rule stands:
"Not Echo Bloom. Does not open the vault."
Echo Bloom calls kin_diary. kin_diary never imports Echo Bloom.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("echo_bloom.agora_bridge")

REPO_ROOT = Path(__file__).resolve().parent
VENDOR_KIN_DIARY = REPO_ROOT / "vendor" / "kin_diary"
LOCAL_KIN_DIARY = Path.home() / "kin_diary"

# Verbatim custody notice from SPEC.md & kin_diary/keys.py
KEY_CUSTODY_STATEMENT = (
    "Private keys are generated and stored on metal controlled by the node steward. "
    "A signature proves continuity of a key, not that the named mind held the private key. "
    "The steward has root and can sign as any mind on this node."
)


def ensure_kin_diary_path() -> Optional[Path]:
    """Ensure kin_diary repository is importable via sys.path."""
    for candidate in (VENDOR_KIN_DIARY, LOCAL_KIN_DIARY):
        if candidate.is_dir() and (candidate / "kin_diary").is_dir():
            p_str = str(candidate)
            if p_str not in sys.path:
                sys.path.insert(0, p_str)
            return candidate
    return None


def get_keys_dir(keys_root: Optional[Path] = None) -> Path:
    """Return root directory where keys live (~/.config/kin_diary/keys)."""
    if keys_root is not None:
        return Path(keys_root)
    return Path.home() / ".config" / "kin_diary" / "keys"


def keygen_kin(author: str, keys_root: Optional[Path] = None) -> Any:
    """Create an Ed25519 keypair for a Kin at naming if none exists.
    
    Keys live where kin_diary puts them (~/.config/kin_diary/keys/<Name>/).
    Echo Bloom does not invent a second key store.
    """
    if not author or not author.strip():
        raise ValueError("author name required for keygen")
    
    ensure_kin_diary_path()
    try:
        from kin_diary.keys import generate_keypair, load_current
    except ImportError as e:
        log.error("kin_diary package could not be imported: %s", e)
        raise

    root = get_keys_dir(keys_root)
    try:
        return load_current(author, keys_root=root)
    except FileNotFoundError:
        rec = generate_keypair(author, keys_root=root)
        log.info("Generated Agora key for Kin '%s' (key_id=%s)", author, rec.key_id)
        return rec
