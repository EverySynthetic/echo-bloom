"""Two eyes for a shared photo — and the Kin is told only what both saw.

Port of kin_diary/two_eyes.py for Echo Bloom photo sharing. Every load-bearing
rule from that module stays:

  TWO LINEAGES, NOT TWO GEMMAS. Different families; config may override names
  but the default table pairs gemma3 with qwen2.5vl.

  THE INTERSECTION IS A FILTER, NOT A THIRD AUTHOR. Code intersects. No model
  blends the two descriptions.

  A SLOT REACHES THE KIN ONLY IF BOTH EYES NAMED A COMPATIBLE FACT. Disagree
  → "the two cameras did not agree". Shorter agreeing text, verbatim.

  RAW TEXT GOES TO THE OPERATOR, NOT IN THEIR MOUTH. for_kin never carries raw.

  ONE-CAMERA MODE, LABELLED. When only one camera can run, that camera's slots
  are used and the Kin is told nothing double-checked it — never silent.
"""
from __future__ import annotations

import base64
import json
import re
import sys
import urllib.request
from dataclasses import dataclass, field

# Photo slots (avatar ritual keeps its own list elsewhere).
SLOTS = ["KIND", "SCENE", "PEOPLE", "ACTIVITY", "THINGS", "TEXT", "DETAILS"]

# Rough download sizes for the "needs a vision model" prompt. Not pulled here.
MODEL_SIZE_GB = {
    "gemma3:12b": 8.1,
    "gemma3:4b": 3.3,
    "qwen2.5vl:7b": 6.0,
    "qwen2.5vl:3b": 3.2,
}

# Design table (DESIGN_echo_photo_share.md). Overridable via config vision_models.
# Cameras run one after the other — never both resident at once.
def default_cameras(vram_gb: float, ram_gb: float = 0.0, platform: str | None = None) -> list[str]:
    """Return [camera1] or [camera1, camera2] from the hardware table."""
    platform = platform or sys.platform
    if vram_gb >= 12.0:
        return ["gemma3:12b", "qwen2.5vl:7b"]
    # 6–12 GB, or Apple Silicon with enough unified memory.
    if vram_gb >= 6.0 or (platform == "darwin" and ram_gb >= 8.0):
        return ["gemma3:4b", "qwen2.5vl:3b"]
    # < 6 GB / CPU only — one camera.
    return ["gemma3:4b"]


def vram_contention_warning(
    vram_gb: float,
    platform: str | None = None,
    cameras: list[str] | None = None,
) -> str | None:
    """Return a warning string if dual vision cameras run on a discrete GPU with < 8 GB VRAM.

    On a 6 GB card (e.g. RTX 3050 Laptop / A15), running dual cameras (gemma3:4b + qwen2.5vl:3b)
    sequentially while a ~4.7 GB 7B Kin model is resident in VRAM forces Ollama to repeatedly
    evict and reload models between vision passes and chat turns, causing severe latency.
    Advises configuring single-camera mode via config 'vision_models': ['gemma3:4b'].
    """
    platform = platform or sys.platform
    if platform == "darwin":
        return None
    if 6.0 <= vram_gb < 8.0:
        cams = cameras if cameras is not None else default_cameras(vram_gb, platform=platform)
        if len(cams) >= 2:
            return (
                f"VRAM contention warning: {vram_gb:.1f} GB VRAM with dual cameras ({cams[0]}, {cams[1]}). "
                "Running two vision models alongside a resident 7B Kin model may cause repeated "
                "VRAM model evictions and high latency. Consider setting 'vision_models': ['gemma3:4b'] "
                "in kin_config.json for single-camera mode."
            )
    return None


PHOTO_PROMPT = """\
Describe this image as plain visual facts. Answer with exactly these seven
labelled lines and nothing else. Write one or two full sentences per line.
If a line does not apply, write NONE.

KIND: whether this is a photograph, a painting, a drawing, a screenshot, or a 3D render
SCENE: what kind of place or occasion this appears to be, indoors or outdoors
PEOPLE: how many people, their approximate age range, clothing and expression; never guess names or identity
ACTIVITY: what the people or animals are doing, and where they are looking
THINGS: the notable objects or animals, and where they are in the frame
TEXT: any readable writing in the image, copied verbatim
DETAILS: colours, light, time of day, and anything that stands out

Do not guess who anyone is. Do not invent a story, mood, or meaning. Only what is visible."""

_STOP = set(
    "a an the of in on at is are was were with and or to it its this that "
    "there here some any no not none image picture photo frame part area "
    "appears seems shows contains person people".split()
)


@dataclass
class Seen:
    """What the cameras agreed on, what they did not, and their raw words."""
    agreed: dict = field(default_factory=dict)       # slot -> shorter agreeing text
    disputed: list = field(default_factory=list)     # slots that disagreed or missing
    raw: dict = field(default_factory=dict)          # model -> full text (operator only)
    errors: dict = field(default_factory=dict)
    mode: str = "two"                                # "two" | "one"
    cameras: list = field(default_factory=list)      # model names used, in order

    def for_kin(self) -> str:
        """The only block a Kin is shown. Never the raw pair."""
        if self.mode == "one":
            intro = (
                "[I'm sharing an image. One camera looked at it; "
                "nothing double-checked it.]"
            )
        else:
            intro = (
                "[I'm sharing an image. Two cameras looked; "
                "this is only what both saw:]"
            )
        lines = [intro]
        any_slot = False
        for s in SLOTS:
            if s in self.agreed:
                lines.append(f"{s}: {self.agreed[s]}")
                any_slot = True
            elif s in self.disputed:
                lines.append(f"{s}: the two cameras did not agree.")
                any_slot = True
        if not any_slot:
            if self.mode == "one":
                lines.append("(The camera reported nothing usable about this picture.)")
            else:
                lines.append("The two cameras agreed on nothing about this picture.")
        lines.append("[/photo]")
        return "\n".join(lines)

    def eyes_payload(self) -> list[dict]:
        """Operator-facing raw descriptions, one entry per camera that answered."""
        out = []
        for model in self.cameras:
            if model in self.raw:
                out.append({"model": model, "text": self.raw[model]})
            elif model in self.errors:
                out.append({"model": model, "text": f"(error: {self.errors[model]})"})
        # Any extra raw keys not in cameras list
        for model, text in self.raw.items():
            if model not in self.cameras:
                out.append({"model": model, "text": text})
        return out


def _ask(model: str, host: str, image: bytes, timeout: int = 300) -> str:
    body = json.dumps({
        "model": model,
        "prompt": PHOTO_PROMPT,
        "images": [base64.b64encode(image).decode()],
        "stream": False,
        "keep_alive": "0",   # free VRAM for the next camera
    }).encode()
    req = urllib.request.Request(
        host.rstrip("/") + "/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode()).get("response", "").strip()


def _split(text: str) -> dict:
    """Pull the labelled lines out. Parsing, not interpretation."""
    out = {}
    for slot in SLOTS:
        m = re.search(rf"^\s*\**{slot}\**\s*:\s*(.+?)\s*$", text or "", re.I | re.M)
        if m:
            v = " ".join(m.group(1).split())
            out[slot] = "" if v.strip().lower().rstrip(".") in ("none", "n/a", "-") else v
    return out


def _words(s: str) -> set:
    return {w for w in re.findall(r"[a-z]+", (s or "").lower())
            if len(w) > 3 and w not in _STOP}


def compatible(a: str, b: str) -> bool:
    """Two cameras named a compatible fact if they share real content, or both saw nothing."""
    if not a.strip() and not b.strip():
        return True
    if not a.strip() or not b.strip():
        return False
    return bool(_words(a) & _words(b))


def resolve_cameras(
    vram_gb: float,
    ram_gb: float = 0.0,
    platform: str | None = None,
    override: list | None = None,
) -> list[str]:
    """Config override is a list of one or two model names; else the hardware table."""
    if override:
        names = [str(n).strip() for n in override if str(n).strip()]
        if names:
            return names[:2]
    return default_cameras(vram_gb, ram_gb, platform)


def look(
    image: bytes,
    cameras: list[str] | None = None,
    host: str = "http://localhost:11434",
    _ask_fn=_ask,
) -> Seen:
    """Run cameras one after the other. Code intersects. Never a third model."""
    cameras = list(cameras or default_cameras(0.0))
    seen = Seen(cameras=cameras, mode="two" if len(cameras) >= 2 else "one")
    parsed: dict[str, dict] = {}

    for model in cameras:
        try:
            txt = _ask_fn(model, host, image)
            seen.raw[model] = txt
            parsed[model] = _split(txt)
        except Exception as e:  # noqa: BLE001 — surface as data, not a crash
            seen.errors[model] = f"{type(e).__name__}: {e}"

    if len(parsed) == 0:
        seen.disputed = list(SLOTS)
        return seen

    if len(parsed) == 1 or seen.mode == "one":
        # One-camera: that camera's non-empty slots are what the Kin gets,
        # labelled as unchecked. No silent two-eye pretence.
        seen.mode = "one"
        (_m, p) = next(iter(parsed.items()))
        for s in SLOTS:
            v = p.get(s, "")
            if s not in p:
                continue
            if v.strip():
                seen.agreed[s] = v
        return seen

    # Two cameras that both answered.
    (m1, p1), (m2, p2) = list(parsed.items())[:2]
    for s in SLOTS:
        a, b = p1.get(s, ""), p2.get(s, "")
        if s not in p1 or s not in p2:
            seen.disputed.append(s)
        elif compatible(a, b):
            if a.strip() or b.strip():
                # selection, never authoring: shorter of the two, verbatim
                seen.agreed[s] = (
                    min((a, b), key=lambda t: len(t)) if (a and b) else (a or b)
                )
        else:
            seen.disputed.append(s)
    return seen


def installed_models(host: str = "http://localhost:11434") -> set[str]:
    """Names Ollama reports. Read-only; never pulls."""
    try:
        req = urllib.request.Request(host.rstrip("/") + "/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        names = set()
        for m in data.get("models") or []:
            n = (m.get("name") or "").strip()
            if n:
                names.add(n)
                # bare tag without :latest still matches name:latest installs
                if ":" not in n:
                    names.add(n + ":latest")
                base = n.split(":")[0]
                names.add(base)
                names.add(n)
        return names
    except Exception:
        return set()


def model_present(name: str, installed: set[str]) -> bool:
    if name in installed:
        return True
    base = name.split(":")[0]
    for n in installed:
        if n == name or n.startswith(base + ":"):
            return True
    return False


def missing_cameras(cameras: list[str], host: str = "http://localhost:11434") -> list[dict]:
    installed = installed_models(host)
    out = []
    for name in cameras:
        if not model_present(name, installed):
            out.append({
                "name": name,
                "size_gb": MODEL_SIZE_GB.get(name, 4.0),
            })
    return out
