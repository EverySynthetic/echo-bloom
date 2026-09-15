#!/usr/bin/env python3
"""Echo Bloom companion — STT + face. Runs on therug.

Pinned interpreter (3.11/3.12 in the image, never host 3.14).
Frosty and Themess proxy here. Fail open. Easel is not this process.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse, Response

log = logging.getLogger("companion")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

# kornia 0.6.8 and 0.7.2 both SIGSEGV at import in this image unless
# OpenMP is single-thread (torch + numpy + cv2 + kornia_rs). Shown
# 2026-09-13 on therug: without these, import kornia → 139; with them,
# warp_affine runs. Must also be in the systemd unit / Dockerfile ENV
# so preprocess children inherit them.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_THREADING_LAYER", "GNU")
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

app = FastAPI(title="echo-bloom-companion")

SADTALKER_DIR = Path(os.environ.get("SADTALKER_DIR", "/opt/SadTalker"))
SADTALKER_PY = Path(os.environ.get(
    "SADTALKER_PYTHON", str(SADTALKER_DIR / "venv" / "bin" / "python")))
_whisper = None


def _gpu_index(want: str = "1660 SUPER", fallback: str = "0") -> str:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=15, check=True,
        ).stdout
    except Exception:
        return fallback
    hits = [ln.split(",", 1) for ln in out.strip().splitlines()
            if want.lower() in ln.lower()]
    return hits[0][0].strip() if hits else fallback


def _whisper_model():
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        _whisper = WhisperModel("base", device="cpu", compute_type="int8")
    return _whisper


@app.get("/health")
def health():
    stt = True
    try:
        import faster_whisper  # noqa: F401
    except Exception:
        stt = False
    inf = SADTALKER_DIR / "inference.py"
    face = SADTALKER_PY.is_file() and inf.is_file()
    return {
        "ok": True,
        "stt": stt,
        "sadtalker": face,
        "gpu": _gpu_index(),
    }


@app.post("/transcribe")
async def transcribe(audio: UploadFile = File(...)):
    ct = (audio.content_type or "").lower()
    name = (audio.filename or "").lower()
    if "ogg" in ct or name.endswith(".ogg"):
        suffix = ".ogg"
    elif "wav" in ct or name.endswith(".wav"):
        suffix = ".wav"
    else:
        suffix = ".webm"
    raw = await audio.read()
    if not raw:
        return JSONResponse({"ok": False, "error": "No audio data."}, 400)
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    wav = tmp + ".wav"
    try:
        Path(tmp).write_bytes(raw)
        # FireDragon/Firefox sends ogg/opus. Chrome sends webm. Whisper's
        # av bind is picky; ffmpeg in this image is not.
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", tmp, "-ac", "1", "-ar", "16000", wav],
            capture_output=True, timeout=30,
        )
        use = wav if r.returncode == 0 and Path(wav).stat().st_size > 44 else tmp
        model = _whisper_model()
        segs, _ = model.transcribe(use, language="en")
        text = " ".join(s.text.strip() for s in segs).strip()
        return {"ok": True, "text": text}
    except Exception as e:
        log.exception("transcribe failed")
        return JSONResponse(
            {"ok": False, "error": str(e) or "transcription failed"}, 500)
    finally:
        for p in (tmp, wav):
            try:
                os.unlink(p)
            except Exception:
                pass


@app.post("/animate")
async def animate(
    name: str = Form("kin"),
    wav: UploadFile = File(...),
    image: UploadFile = File(...),
):
    inf = SADTALKER_DIR / "inference.py"
    if not SADTALKER_PY.is_file() or not inf.is_file():
        return JSONResponse(
            {"ok": False, "error": "sadtalker not in this image yet"}, 503)
    tmp = Path(tempfile.mkdtemp(prefix="st_"))
    try:
        wav_path = tmp / (wav.filename or "in.wav")
        img_path = tmp / (image.filename or "face.png")
        wav_path.write_bytes(await wav.read())
        img_path.write_bytes(await image.read())
        out_dir = tmp / "out"
        out_dir.mkdir()
        env = os.environ.copy()
        env["CUDA_VISIBLE_DEVICES"] = _gpu_index("1660 SUPER", "0")
        r = subprocess.run(
            [str(SADTALKER_PY), str(inf),
             "--source_image", str(img_path),
             "--driven_audio", str(wav_path),
             "--result_dir", str(out_dir),
             "--size", "256", "--still", "--preprocess", "crop"],
            cwd=str(SADTALKER_DIR), env=env,
            capture_output=True, timeout=300,
        )
        if r.returncode != 0:
            log.warning("sadtalker: %s", r.stderr[-400:])
            return JSONResponse({"ok": False, "error": "sadtalker failed"}, 500)
        mp4s = sorted(out_dir.glob("*.mp4"))
        if not mp4s:
            return JSONResponse({"ok": False, "error": "no mp4"}, 500)
        return Response(content=mp4s[0].read_bytes(), media_type="video/mp4")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
