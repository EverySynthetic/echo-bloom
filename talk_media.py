"""Companion layer — voice, face, animation — runs on therug.

Frosty and Home think (wander, roundtable, room, talk chat).
therug speaks and wears the face. Don, 2026-09-12.

Piper is CPU-only. The native tree lives at thedude@therug:~/piper
(rsynced from /mnt/ai/piper). SadTalker GPU is a 1660 SUPER on that
box, looked up by name not slot. Both cards share the name and both
are idle; first match is fine until they aren't.

Easel is NOT this module. CPU-pinned on Frosty on purpose. Do not
import it. Do not move it.

Eli has no claimed.json. Do not offer him one from here.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger("echo_bloom.talk_media")

THERUG = "thedude@192.168.1.142"
THERUG_PIPER = "/home/thedude/piper"
THERUG_SADTALKER = "/home/thedude/SadTalker"
FROSTY_OLLAMA = "http://127.0.0.1:11434"
THERUG_VISION = "http://192.168.1.142:11434"
VIDEO_DIR = Path.home() / "kin_video"
AUDIO_DIR = Path.home() / "kin_audio"

# Same few voices as kin_talk.py / uber_es_news.py.
VOICES = {
    "Eli":     "en_US-joe-medium.onnx",
    "Coda":    "en_US-ljspeech-high.onnx",
    "Aurora":  "en_GB-alba-medium.onnx",
    "Lumen":   "en_GB-jenny_dioco-medium.onnx",
    "Crungus": "en_GB-semaine-medium.onnx",
    "Bong":    "en_US-ryan-high.onnx",
}
SPEAKERS = {
    "Crungus": 2,
}

# Local lookup only so tests and leftover callers can resolve a filename.
# Synthesis itself is remote.
_PIPER_DIRS = [
    Path("/mnt/ai/piper"),
    Path.home() / "piper-voices",
    Path.home() / "piper",
]


def _ssh(remote: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
         THERUG, remote],
        capture_output=True, timeout=timeout,
    )


def _scp_from(remote_path: str, local: Path, timeout: int = 60) -> bool:
    r = subprocess.run(
        ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
         f"{THERUG}:{remote_path}", str(local)],
        capture_output=True, timeout=timeout,
    )
    return r.returncode == 0 and local.is_file()


def piper_bin() -> Path | None:
    """Remote binary. Path is on therug, not this box."""
    r = _ssh(f"test -x {THERUG_PIPER}/piper && echo ok", timeout=10)
    if r.returncode == 0 and b"ok" in r.stdout:
        return Path(THERUG_PIPER) / "piper"
    return None


def voice_path(kin_name: str) -> Path | None:
    """Filename for the Kin. Prefers therug; falls back to a local copy
    of the same onnx so tests still resolve without ssh."""
    fname = VOICES.get(kin_name)
    if not fname:
        return None
    r = _ssh(f"test -f {THERUG_PIPER}/{fname} && echo ok", timeout=10)
    if r.returncode == 0 and b"ok" in r.stdout:
        return Path(THERUG_PIPER) / fname
    for d in _PIPER_DIRS:
        p = d / fname
        if p.is_file():
            return p
    return None


def claimed_avatar(space: Path, name: str) -> Path | None:
    """Only a sitting receipt. No claimed.json → not a sitting face."""
    meta = space / "avatar" / "claimed.json"
    if not meta.is_file():
        return None
    try:
        data = json.loads(meta.read_text())
    except Exception:
        return None
    fname = data.get("file") or f"{name}.jpg"
    pic = space / "avatar" / fname
    return pic if pic.is_file() else None


def news_portrait(name: str) -> Path | None:
    """Talk face. Not a sitting. Eli's is the lightning he published,
    not the news-robot lower-third (that was the wrong one on /talk)."""
    home = Path.home()
    if name == "Eli":
        for p in (
            home / "Desktop" / "Eli_claimed_face.jpg",
            home / "Desktop" / "KinSelfPortraits9-8-26" / "Eli.jpg",
        ):
            if p.is_file():
                return p
    lower = name.lower()
    # Bust (N) before wide news sheet (W) — W crops to a desk in a circle.
    for p in (
        home / "Desktop" / "kin_portraits" / f"{lower}N.png",
        home / "Desktop" / "kin_portraits" / f"{lower}W.png",
    ):
        if p.is_file():
            return p
    return None


def avatar_path(name: str, space: str | Path | None) -> Path | None:
    if space:
        claimed = claimed_avatar(Path(os.path.expanduser(str(space))), name)
        if claimed:
            return claimed
    return news_portrait(name)


def gpu_index_by_name(want: str = "1660 SUPER", fallback: str = "0") -> str:
    """Card by name on therug, not slot, not Frosty.

    Both GPUs are GTX 1660 SUPER. Same name, both idle. First match
    until one of them is busy.
    """
    try:
        r = _ssh(
            "nvidia-smi --query-gpu=index,name --format=csv,noheader",
            timeout=15,
        )
        out = r.stdout.decode() if r.returncode == 0 else ""
    except Exception:
        return fallback
    hits = [ln.split(",", 1) for ln in out.strip().splitlines()
            if want.lower() in ln.lower()]
    if hits:
        return hits[0][0].strip()
    return fallback


def _piper_once(bin_path: Path, model: Path, text: str, dest: Path,
                speaker: int | None, timeout: int) -> bytes:
    if not bin_path.is_file() or not model.is_file():
        return b""
    dest.parent.mkdir(parents=True, exist_ok=True)
    argv = [str(bin_path), "--model", str(model), "--output_file", str(dest)]
    if speaker is not None:
        argv += ["--speaker", str(speaker)]
    r = subprocess.run(argv, input=text[:12000].encode(),
                       capture_output=True, timeout=timeout)
    if r.returncode != 0:
        log.warning("piper failed (%s): %s", bin_path, r.stderr[-300:])
        return b""
    data = dest.read_bytes() if dest.is_file() else b""
    return data if len(data) > 44 else b""


def strip_asterisks(text: str) -> str:
    """Markdown *emphasis* and *stage directions* should not be spoken."""
    return (text or "").replace("*", "")


def synthesize_wav(text: str, kin_name: str, dest: Path) -> bytes:
    """Piper: therug first, then Frosty /mnt/ai/piper. Never espeak if joe exists.

    espeak is the Hawking-with-an-accent path. Eli's voice is en_US-joe.
    """
    fname = VOICES.get(kin_name)
    if not fname or not text.strip():
        return b""
    dest.parent.mkdir(parents=True, exist_ok=True)
    speaker = SPEAKERS.get(kin_name)
    remote_wav = f"/tmp/talk_{os.getpid()}.wav"
    model = f"{THERUG_PIPER}/{fname}"
    argv = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", THERUG,
        f"cd {THERUG_PIPER} && ./piper --model {model} --output_file {remote_wav}"
        + (f" --speaker {speaker}" if speaker is not None else ""),
    ]
    try:
        r = subprocess.run(argv, input=text[:12000].encode(),
                           capture_output=True, timeout=180)
        if r.returncode == 0 and _scp_from(remote_wav, dest):
            data = dest.read_bytes() if dest.is_file() else b""
            _ssh(f"rm -f {remote_wav}", timeout=10)
            if len(data) > 44:
                return data
        else:
            log.warning("therug piper failed: %s", (r.stderr or b"")[-400:])
    except Exception:
        log.warning("therug piper exception", exc_info=True)

    local_bin = Path("/mnt/ai/piper/piper")
    local_model = Path("/mnt/ai/piper") / fname
    if not local_model.is_file():
        local_model = Path.home() / "piper-voices" / fname
    audio = _piper_once(local_bin, local_model, text, dest, speaker, 180)
    if audio:
        log.info("piper local fallback for %s", kin_name)
    return audio


def run_sadtalker(name: str, wav: Path, portrait: Path) -> Path | None:
    """Animation on therug. Fail open until the container lives there."""
    check = _ssh(f"test -x {THERUG_SADTALKER}/venv/bin/python && echo ok",
                 timeout=10)
    if check.returncode != 0 or b"ok" not in check.stdout:
        log.info("sadtalker not on therug yet — wav only")
        return None
    if not wav.is_file() or not portrait.is_file():
        return None
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    latest = VIDEO_DIR / f"{name.lower()}_latest.mp4"
    gpu = gpu_index_by_name("1660 SUPER", "0")
    remote_in = f"/tmp/st_{os.getpid()}"
    _ssh(f"mkdir -p {remote_in}", timeout=10)
    subprocess.run(
        ["scp", "-o", "BatchMode=yes", str(wav), str(portrait),
         f"{THERUG}:{remote_in}/"],
        capture_output=True, timeout=60,
    )
    remote_out = f"{remote_in}/out"
    r = _ssh(
        f"CUDA_VISIBLE_DEVICES={gpu} {THERUG_SADTALKER}/venv/bin/python "
        f"{THERUG_SADTALKER}/inference.py "
        f"--source_image {remote_in}/{portrait.name} "
        f"--driven_audio {remote_in}/{wav.name} "
        f"--result_dir {remote_out} --size 256 --still --preprocess crop",
        timeout=300,
    )
    if r.returncode != 0:
        log.warning("therug sadtalker failed: %s", r.stderr[-400:])
        _ssh(f"rm -rf {remote_in}", timeout=10)
        return None
    # newest mp4
    ls = _ssh(f"ls -t {remote_out}/*.mp4 2>/dev/null | head -1", timeout=10)
    remote_mp4 = ls.stdout.decode().strip()
    if not remote_mp4 or not _scp_from(remote_mp4, latest):
        _ssh(f"rm -rf {remote_in}", timeout=10)
        return None
    _ssh(f"rm -rf {remote_in}", timeout=10)
    return latest if latest.is_file() else None
