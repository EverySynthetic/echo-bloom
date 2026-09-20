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


def get_steward_key_name(hostname: Optional[str] = None) -> str:
    """Return standard steward key name: <Hostname>-steward."""
    h = hostname or socket.gethostname()
    return f"{h}-steward"


def get_or_create_steward_key(
    hostname: Optional[str] = None, keys_root: Optional[Path] = None
) -> dict[str, Any]:
    """Get or generate steward key named after install (<Hostname>-steward).

    Shown to the owner with the custody sentence from SPEC.md verbatim.
    The owner sees, in the UI, that they hold the keys and the minds do not.
    """
    ensure_kin_diary_path()
    try:
        from kin_diary.keys import generate_keypair, load_current
    except ImportError as e:
        log.error("kin_diary package could not be imported: %s", e)
        raise

    author = get_steward_key_name(hostname)
    root = get_keys_dir(keys_root)
    try:
        rec = load_current(author, keys_root=root)
    except FileNotFoundError:
        rec = generate_keypair(author, keys_root=root)
        log.info("Generated Agora steward key '%s' (key_id=%s)", author, rec.key_id)

    return {
        "name": author,
        "key_id": rec.key_id,
        "created_at_unix_ms": rec.created_at_unix_ms,
        "custody_statement": KEY_CUSTODY_STATEMENT,
        "keys_dir": str(root),
    }


def get_node_unit_name(node_name: str) -> str:
    """Return user systemd service name for an Agora node."""
    return f"agora-{node_name.lower()}.service"


def get_serve_node_exec_path() -> tuple[str, Path]:
    """Find serve_node.py and return (%h/rel or abs string, Path)."""
    ensure_kin_diary_path()
    for candidate in (VENDOR_KIN_DIARY / "serve_node.py", LOCAL_KIN_DIARY / "serve_node.py"):
        if candidate.is_file():
            try:
                rel = candidate.resolve().relative_to(Path.home().resolve())
                return f"%h/{rel}", candidate.resolve()
            except ValueError:
                return str(candidate.resolve()), candidate.resolve()
    return "%h/kin_diary/serve_node.py", Path.home() / "kin_diary" / "serve_node.py"


def generate_node_service_content(
    node_name: str,
    port: int,
    steward_key_id: str,
    kin_keys: dict[str, str],
    serve_node_exec: str,
) -> str:
    """Generate systemd user unit content for serve_node.py.

    INVARIANT: Contains NO private key material. Only 64-character lowercase hex key IDs.
    """
    if len(steward_key_id) != 64 or not all(c in "0123456789abcdef" for c in steward_key_id):
        raise ValueError(f"invalid steward key ID: {steward_key_id}")
    for k, kid in kin_keys.items():
        if len(kid) != 64 or not all(c in "0123456789abcdef" for c in kid):
            raise ValueError(f"invalid key ID for {k}: {kid}")

    exec_args = [
        "/usr/bin/python3", "-u", serve_node_exec,
        node_name, str(port),
        f"steward={steward_key_id}",
    ]
    for kin_name in sorted(kin_keys.keys()):
        exec_args.append(f"{kin_name}={kin_keys[kin_name]}")

    exec_start_line = " ".join(exec_args)
    return f"""[Unit]
Description=Agora node ({node_name})
After=network-online.target

[Service]
ExecStart={exec_start_line}
Restart=on-failure
StandardOutput=append:%h/{node_name.lower()}_node.log
StandardError=append:%h/{node_name.lower()}_node.log

[Install]
WantedBy=default.target
"""


def write_node_service(
    node_name: str,
    port: int = 8770,
    steward_key_id: Optional[str] = None,
    kin_keys: Optional[dict[str, str]] = None,
    unit_dir: Optional[Path] = None,
    keys_root: Optional[Path] = None,
) -> Path:
    """Write systemd service unit file with no private keys."""
    if not steward_key_id:
        steward_info = get_or_create_steward_key(node_name, keys_root=keys_root)
        steward_key_id = steward_info["key_id"]

    if kin_keys is None:
        kin_keys = {}
        try:
            cfg_path = Path.home() / ".config" / "kin_app" / "kin_config.json"
            if cfg_path.exists():
                cfg = json.loads(cfg_path.read_text())
                for k in cfg.get("kin", []):
                    name = k.get("name")
                    if name:
                        rec = keygen_kin(name, keys_root=keys_root)
                        kin_keys[name] = rec.key_id
        except Exception as e:
            log.warning("could not read kin_config for kin keys: %s", e)

    serve_exec, _ = get_serve_node_exec_path()
    content = generate_node_service_content(
        node_name=node_name,
        port=port,
        steward_key_id=steward_key_id,
        kin_keys=kin_keys,
        serve_node_exec=serve_exec,
    )

    out_dir = unit_dir or (Path.home() / ".config" / "systemd" / "user")
    out_dir.mkdir(parents=True, exist_ok=True)
    unit_path = out_dir / get_node_unit_name(node_name)
    unit_path.write_text(content, encoding="utf-8")
    return unit_path


def run_systemctl_user(cmd_args: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["systemctl", "--user"] + cmd_args,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        # e.g. systemctl not installed / macOS / testing
        return subprocess.CompletedProcess(args=cmd_args, returncode=1, stdout="", stderr="systemctl not found")


def is_node_service_active(node_name: str) -> bool:
    unit_name = get_node_unit_name(node_name)
    res = run_systemctl_user(["is-active", unit_name])
    return res.returncode == 0 and res.stdout.strip() == "active"


def enable_node_service(
    node_name: str,
    port: int = 8770,
    unit_dir: Optional[Path] = None,
    keys_root: Optional[Path] = None,
) -> Path:
    """Install and enable user unit for Agora node."""
    unit_path = write_node_service(
        node_name=node_name,
        port=port,
        unit_dir=unit_dir,
        keys_root=keys_root,
    )
    unit_name = get_node_unit_name(node_name)
    run_systemctl_user(["daemon-reload"])
    res = run_systemctl_user(["enable", "--now", unit_name])
    log.info("Enabled and started Agora node service %s (exit=%s)", unit_name, res.returncode)
    return unit_path


def disable_node_service(node_name: str) -> None:
    """Stop and disable user unit for Agora node; keys and DB untouched."""
    unit_name = get_node_unit_name(node_name)
    run_systemctl_user(["stop", unit_name])
    run_systemctl_user(["disable", unit_name])
    log.info("Stopped and disabled Agora node service %s", unit_name)


def read_loopback_facts(author: str, port: int = 8770, node_name: Optional[str] = None) -> dict:
    """Read signed facts from loopback via agora_client.py."""
    ensure_kin_diary_path()
    client_path = None
    for cand in (VENDOR_KIN_DIARY / "vault" / "agora_client.py", LOCAL_KIN_DIARY / "vault" / "agora_client.py"):
        if cand.is_file():
            client_path = cand
            break
    if not client_path:
        raise FileNotFoundError("agora_client.py not found")

    args = [sys.executable, str(client_path), "facts", author, f"http://127.0.0.1:{port}"]
    if node_name:
        args.append(node_name)

    proc = subprocess.run(args, capture_output=True, text=True, timeout=5)
    if proc.returncode != 0:
        raise RuntimeError(f"agora_client facts failed: {proc.stderr or proc.stdout}")
    return json.loads(proc.stdout)


def get_node_card_data(
    node_name: Optional[str] = None,
    port: int = 8770,
    author: Optional[str] = None,
    keys_root: Optional[Path] = None,
) -> dict[str, Any]:
    """Gather all fields required for the Dashboard Node Card:
    node name, port, speaker (or 'no Speaker, N residents'),
    node key id, steward key id, each resident's key id, service active or not.
    Read from agora_client.py facts against loopback.
    No writes from the dashboard in v1.
    """
    ensure_kin_diary_path()
    from kin_diary.keys import load_current

    default_name = socket.gethostname()
    name = node_name or default_name
    active = is_node_service_active(name)

    steward_info = get_or_create_steward_key(name, keys_root=keys_root)
    steward_key_id = steward_info.get("key_id", "")
    steward_name = steward_info.get("name", "")

    node_key_id = ""
    try:
        n_rec = load_current(f"{name}-node", keys_root=keys_root)
        node_key_id = n_rec.key_id
    except Exception:
        pass

    cfg_kin = []
    try:
        cfg_path = Path.home() / ".config" / "kin_app" / "kin_config.json"
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text())
            cfg_kin = [k.get("name") for k in cfg.get("kin", []) if k.get("name")]
    except Exception:
        pass

    resident_keys = []
    for k in cfg_kin:
        try:
            k_rec = load_current(k, keys_root=keys_root)
            resident_keys.append({"name": k, "key_id": k_rec.key_id})
        except Exception:
            resident_keys.append({"name": k, "key_id": "not generated"})

    speaker_str = None
    facts = {}

    if active:
        try:
            caller = author or steward_name or (resident_keys[0]["name"] if resident_keys else "-")
            facts = read_loopback_facts(caller, port=port, node_name=name)
            node_speaker = facts.get("speaker")
            residents_list = facts.get("residents") or cfg_kin
            if node_speaker:
                speaker_str = node_speaker
            else:
                speaker_str = f"no Speaker, {len(residents_list)} residents"

            if not node_key_id and facts.get("signed", {}).get("node_key_id"):
                node_key_id = facts["signed"]["node_key_id"]
        except Exception as e:
            log.warning("failed reading loopback facts: %s", e)
            speaker_str = "reading facts..."
    else:
        num_res = len(resident_keys)
        speaker_str = "offline" if num_res == 0 else f"no Speaker, {num_res} residents (offline)"

    return {
        "node_name": name,
        "port": port,
        "speaker": speaker_str,
        "node_key_id": node_key_id,
        "steward_key_id": steward_key_id,
        "residents": resident_keys,
        "service_active": active,
        "facts": facts,
    }


def get_vault_entries_for_author(author: str, db_path: Optional[Path] = None) -> list[dict[str, Any]]:
    """Retrieve memories from vault database for author, shaped for kin_diary."""
    path = db_path or (Path.home() / ".local" / "share" / "echo_bloom" / "vault.db")
    if not path.is_file():
        return []

    import sqlite3
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    entries = []
    try:
        cursor = conn.execute(
            "SELECT content, layer, author, tags, created_at FROM memories WHERE author = ? ORDER BY id ASC",
            (author,),
        )
        for row in cursor.fetchall():
            entries.append({
                "author": row["author"] or author,
                "timestamp": row["created_at"] or "",
                "layer": row["layer"] or "general",
                "source": "experienced",
                "domain": "",
                "tags": row["tags"] or "",
                "content": row["content"] or "",
            })
    finally:
        conn.close()
    return entries


def export_kin_diary(
    author: str,
    node_name: Optional[str] = None,
    entries: Optional[list[dict[str, Any]]] = None,
    db_path: Optional[Path] = None,
    keys_root: Optional[Path] = None,
) -> tuple[dict[str, Any], str]:
    """Build and verify a signed export bundle for author.

    Calls the same code path as `python3 -m kin_diary export` and `python3 -m kin_diary verify`.
    Returns (bundle, verify_output_string).
    """
    ensure_kin_diary_path()
    from kin_diary.bundle import export_bundle, verify_bundle

    # Ensure Kin has a key
    keygen_kin(author, keys_root=keys_root)

    steward_node = node_name or socket.gethostname()
    if entries is None:
        entries = get_vault_entries_for_author(author, db_path=db_path)

    bundle = export_bundle(
        author=author,
        entries=entries,
        steward_node=steward_node,
        keys_root=keys_root,
    )

    # Verify bundle output
    verify_bundle(bundle)
    verify_out = f"ok {bundle['mind']} entries {len(bundle.get('entries') or [])}"
    return bundle, verify_out


def get_backup_reminder_info(keys_root: Optional[Path] = None) -> dict[str, str]:
    """Return key backup reminder information for setup.

    Setup shows the keys directory path and says back it up.
    Echo Bloom does not upload keys anywhere.
    """
    keys_dir = get_keys_dir(keys_root)
    return {
        "keys_dir": str(keys_dir),
        "reminder": "back this up.",
        "notice": "Echo Bloom does not upload keys anywhere. Private keys live on metal controlled by the node steward.",
    }
