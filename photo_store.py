"""Normalise and (optionally) keep shared photos.

The original upload bytes are never written to disk. Pillow decodes, applies
EXIF orientation, strips all metadata, caps the long side at 1280, and
re-encodes JPEG. Retention:

  none     — nothing on disk; bytes live only for the request
  90d      — ~/.local/share/echo_bloom/photos/<kin>/<id>.jpg + meta, expires
  forever  — same path, no expiry

Files are 0600. Sweep drops expired 90d photos at app start and every 6 hours.
"""
from __future__ import annotations

import io
import json
import os
import secrets
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Lazy Pillow import so a missing pin fails at the call site with a clear error,
# not at module import of every router.
def _pil():
    from PIL import Image, ImageOps, ExifTags  # noqa: F401
    return Image, ImageOps


PHOTOS_ROOT = Path.home() / ".local" / "share" / "echo_bloom" / "photos"
MAX_BYTES = 20 * 1024 * 1024
MAX_SIDE = 1280
JPEG_QUALITY = 85
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "GIF", "BMP"}
# What the browser may send; extension is never trusted alone.
ALLOWED_CONTENT_TYPES = {
    "image/jpeg", "image/jpg", "image/png", "image/webp",
    "image/gif", "image/bmp", "image/x-ms-bmp",
}
RETENTIONS = ("none", "90d", "forever")


class PhotoError(ValueError):
    """Customer-facing plain words."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return "p_" + secrets.token_hex(8)


def kin_dir(kin: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (kin or "room"))[:64]
    return PHOTOS_ROOT / (safe or "room")


def normalize(data: bytes) -> bytes:
    """Decode, EXIF-transpose, strip metadata, resize, JPEG. Never writes disk."""
    if not data:
        raise PhotoError("That file was empty.")
    if len(data) > MAX_BYTES:
        raise PhotoError(
            f"That photo is too large (max {MAX_BYTES // (1024 * 1024)} MB). "
            "Try a smaller copy or resize it first."
        )
    Image, ImageOps = _pil()
    try:
        img = Image.open(io.BytesIO(data))
        img.load()  # force decode — extension lies
    except Exception:
        raise PhotoError(
            "That file is not a photo we can read. "
            "Use a JPEG, PNG, WebP, GIF or BMP. "
            "iPhone HEIC isn't supported here — export as JPEG first."
        ) from None

    fmt = (img.format or "").upper()
    if fmt not in ALLOWED_FORMATS:
        raise PhotoError(
            f"That image type ({fmt or 'unknown'}) isn't supported. "
            "Use a JPEG, PNG, WebP, GIF or BMP. "
            "iPhone HEIC isn't supported here — export as JPEG first."
        )

    # First frame only for GIF/animated.
    if getattr(img, "is_animated", False):
        try:
            img.seek(0)
        except Exception:
            pass

    # Apply EXIF orientation, then drop every tag (GPS included).
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass

    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    elif img.mode == "L":
        img = img.convert("RGB")

    w, h = img.size
    long_side = max(w, h)
    if long_side > MAX_SIDE:
        scale = MAX_SIDE / float(long_side)
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)

    out = io.BytesIO()
    # No exif=, no icc_profile — stripped.
    img.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue()


def jpeg_has_exif(data: bytes) -> bool:
    """True if any APP1/EXIF segment remains. Used by tests."""
    # EXIF in JPEG is an APP1 segment starting with Exif\x00\x00
    return b"Exif\x00\x00" in data[: min(len(data), 65536)] or b"exif" in data[:1024].lower()


def get_retention(cfg: dict | None = None) -> str:
    if cfg is None:
        import auth
        cfg = auth.load_config()
    r = (cfg or {}).get("photo_retention", "none")
    return r if r in RETENTIONS else "none"


def set_retention(value: str) -> str:
    if value not in RETENTIONS:
        raise PhotoError('Keep photos must be "none", "90d", or "forever".')
    import auth
    cfg = auth.load_config()
    cfg["photo_retention"] = value
    auth.save_config(cfg)
    return value


def _meta_path(jpg: Path) -> Path:
    return jpg.with_suffix(".meta.json")


def save(jpeg: bytes, kin: str, retention: str, photo_id: str | None = None) -> dict:
    """Write normalised JPEG + meta when retention keeps it. Returns info dict."""
    pid = photo_id or new_id()
    if retention == "none":
        return {
            "id": pid,
            "kept": False,
            "expires_at": None,
            "path": None,
            "thumb_url": None,
        }

    d = kin_dir(kin)
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except Exception:
        pass

    path = d / f"{pid}.jpg"
    tmp = path.with_suffix(".jpg.tmp")
    tmp.write_bytes(jpeg)
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass

    created = _now()
    expires_at = None
    if retention == "90d":
        expires_at = created + timedelta(days=90)

    meta = {
        "id": pid,
        "kin": kin,
        "created_at": created.isoformat(),
        "expires_at": expires_at.isoformat() if expires_at else None,
        "retention": retention,
    }
    mp = _meta_path(path)
    mp.write_text(json.dumps(meta))
    try:
        os.chmod(mp, 0o600)
    except Exception:
        pass

    return {
        "id": pid,
        "kept": True,
        "expires_at": meta["expires_at"],
        "path": str(path),
        "thumb_url": f"/api/photo/{pid}",
    }


def find(photo_id: str) -> Path | None:
    if not photo_id or not photo_id.startswith("p_") or "/" in photo_id or ".." in photo_id:
        return None
    if not PHOTOS_ROOT.is_dir():
        return None
    for p in PHOTOS_ROOT.glob(f"*/{photo_id}.jpg"):
        return p
    return None


def read_meta(jpg: Path) -> dict:
    mp = _meta_path(jpg)
    if not mp.is_file():
        return {}
    try:
        return json.loads(mp.read_text())
    except Exception:
        return {}


def kept_count() -> int:
    if not PHOTOS_ROOT.is_dir():
        return 0
    return sum(1 for _ in PHOTOS_ROOT.glob("*/*.jpg"))


def sweep(now: datetime | None = None) -> int:
    """Delete expired 90d photos. Returns how many JPEGs removed."""
    now = now or _now()
    if not PHOTOS_ROOT.is_dir():
        return 0
    deleted = 0
    for jpg in list(PHOTOS_ROOT.glob("*/*.jpg")):
        meta = read_meta(jpg)
        exp = meta.get("expires_at")
        if not exp:
            continue
        try:
            exp_dt = datetime.fromisoformat(exp)
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if exp_dt <= now:
            try:
                jpg.unlink(missing_ok=True)
                _meta_path(jpg).unlink(missing_ok=True)
                deleted += 1
            except Exception:
                pass
    return deleted


def purge_all() -> int:
    """Delete every kept photo. Explicit button only — never on retention change."""
    if not PHOTOS_ROOT.is_dir():
        return 0
    deleted = 0
    for jpg in list(PHOTOS_ROOT.glob("*/*.jpg")):
        try:
            jpg.unlink(missing_ok=True)
            _meta_path(jpg).unlink(missing_ok=True)
            deleted += 1
        except Exception:
            pass
    # empty kin dirs
    for d in list(PHOTOS_ROOT.iterdir()) if PHOTOS_ROOT.is_dir() else []:
        if d.is_dir():
            try:
                d.rmdir()
            except Exception:
                pass
    return deleted
