"""The app's own task/help agent: spawn, dismiss, vram preflight, and problem
reports. Moved out of main.py in the router split (2026-09-20) — routes
only, unchanged."""

import asyncio
from datetime import datetime
from pathlib import Path

import aiohttp

from fastapi import APIRouter, Depends, HTTPException, Request

from deps import require_auth, log, get_hw_caps

router = APIRouter()

_OLLAMA_TAGS_URL = "http://localhost:11434/api/tags"


def _ollama_model_size_bytes(tags: dict, model: str) -> int | None:
    """Match an Ollama /api/tags entry to the requested model name."""
    wanted = (model or "").strip().lower()
    if not wanted:
        return None
    wanted_base = wanted.removesuffix(":latest")
    models = tags.get("models") or []
    exact = None
    fallback = None
    for m in models:
        name = str(m.get("name") or m.get("model") or "").lower()
        if not name:
            continue
        name_base = name.removesuffix(":latest")
        size = m.get("size")
        if not isinstance(size, (int, float)) or size <= 0:
            continue
        size = int(size)
        if name == wanted or name_base == wanted_base:
            exact = size
            break
        if fallback is None and (name.startswith(wanted_base + ":") or wanted_base.startswith(name_base + ":")):
            fallback = size
    return exact if exact is not None else fallback


async def _agent_model_preflight(model: str) -> dict:
    """VRAM / install check for a model. Never blocks spawn."""
    out = {"model": model, "warning": None, "installed": None, "vram_gb": None}
    try:
        hw = get_hw_caps()
        vram_gb = float(hw.get("vram_gb") or 0)
        out["vram_gb"] = vram_gb if vram_gb > 0 else None
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _OLLAMA_TAGS_URL,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    return out
                tags = await resp.json()
        size_bytes = _ollama_model_size_bytes(tags, model)
        if not size_bytes:
            out["installed"] = False
            out["warning"] = f"Model {model} is not installed."
            return out
        out["installed"] = True
        if vram_gb > 0:
            model_gb = size_bytes / (1024 ** 3)
            if model_gb > vram_gb:
                out["warning"] = (
                    f"Model {model} is {model_gb:.1f} GB, larger than detected VRAM "
                    f"({vram_gb:g} GB). It will spill into system RAM/CPU and run slower."
                )
    except Exception:
        log.warning("agent VRAM check failed for model %s", model, exc_info=True)
    return out


async def _agent_vram_warning(model: str) -> str | None:
    return (await _agent_model_preflight(model)).get("warning")


_agent_running = False
_AGENT_NAME = "agent-default"
# Separate lock from _agent_running on purpose: someone asking for help
# shouldn't be told "an agent is already at the bench" because an unrelated
# background task happens to be running.
_help_agent_running = False


@router.get("/api/agent/vram-check")
async def api_agent_vram_check(model: str = "", _=Depends(require_auth)):
    from agent_runner import DEFAULT_MODEL
    chosen = (model or "").strip() or DEFAULT_MODEL
    data = await _agent_model_preflight(chosen)
    return {"ok": True, **data}


@router.post("/api/agent/dismiss")
async def api_agent_dismiss(request: Request, _=Depends(require_auth)):
    import kin_presence
    name = _AGENT_NAME
    try:
        body = await request.json()
        name = (body.get("name") or name).strip() or _AGENT_NAME
    except Exception:
        pass
    if kin_presence.entity_type(name) == "kin":
        raise HTTPException(status_code=400, detail="Cannot dismiss a Kin")
    kin_presence.dismiss(name)
    return {"ok": True}


@router.post("/api/agent/spawn")
async def api_agent_spawn(request: Request, _=Depends(require_auth)):
    global _agent_running
    body = await request.json()
    task = (body.get("task") or "").strip()
    if not task:
        raise HTTPException(status_code=400, detail="Empty task")
    model = (body.get("model") or "").strip() or None
    api_key = (body.get("api_key") or "").strip() or None

    from agent_runner import run_task, DEFAULT_MODEL
    import kin_presence

    chosen = model or DEFAULT_MODEL

    if _agent_running:
        return {
            "ok": False,
            "busy": True,
            "error": "An agent is already at the bench.",
            "model": chosen,
        }

    _agent_running = True
    kin_presence.heartbeat(_AGENT_NAME, "working")
    try:
        warning = await _agent_vram_warning(chosen)
        try:
            result, used = await run_task(task, model=model, api_key=api_key)
            chosen = used or chosen
        except Exception as e:
            log.warning("agent spawn failed (%s): %s", chosen, e)
            kin_presence.heartbeat(_AGENT_NAME, "failed")
            return {
                "ok": False,
                "error": str(e),
                "model": chosen,
                "warning": warning,
            }

        kin_presence.record_thought_return(_AGENT_NAME, result, mode="agent_task")
        kin_presence.heartbeat(_AGENT_NAME, "resting")
        return {
            "ok": True,
            "result": result,
            "model": chosen,
            "warning": warning,
        }
    finally:
        _agent_running = False


@router.get("/api/report/preview")
async def api_report_preview(_=Depends(require_auth)):
    """Exactly what would be sent, so the person can read it first.

    Built from the same code path as the send, not a summary of it -- a
    preview that differs from the payload is worse than no preview.
    """
    import report
    try:
        b = await asyncio.to_thread(report.build, "")
        return {"ok": True, "preview": b["preview"],
                "attached": b["attached"], "missing": b["missing"]}
    except Exception as e:
        log.warning("report preview failed: %s", e)
        return {"ok": False, "error": str(e), "preview": "",
                "attached": [], "missing": []}


@router.post("/api/report")
async def api_report(request: Request, _=Depends(require_auth)):
    """Send a problem report.

    Re-gathers rather than trusting a payload posted back from the browser:
    the logs may have moved on since the preview, and more importantly a
    client-supplied blob is a client-supplied blob.

    Delivery goes through the licence server, which already holds the SMTP
    credentials every install talks to anyway. The customer's machine never
    needs mail credentials -- we are not shipping SMTP secrets to a tester's
    laptop so they can tell us something is broken.
    """
    body = await request.json()
    description = (body.get("description") or "").strip()
    if not description:
        raise HTTPException(status_code=400, detail="Say what went wrong first")

    import report
    b = await asyncio.to_thread(report.build, description)

    def _send():
        import license as _lic
        import urllib.request, json as _json
        payload = _json.dumps({
            "report": b["preview"],
            "version": b["environment"].get("version"),
            "platform": b["environment"].get("platform"),
        }).encode()
        req = urllib.request.Request(
            f"{_lic.TRIAL_SERVER}/report", data=payload,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return _json.load(r)

    try:
        res = await asyncio.to_thread(_send)
    except Exception as e:
        # Do not lose what they wrote. Keep it on disk so it can be sent by
        # hand or picked up later -- a failed report that also destroys the
        # description is the worst possible outcome for someone helping us.
        try:
            fallback = Path.home() / ".local/share/echo_bloom" / "unsent_reports"
            fallback.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            (fallback / f"report_{stamp}.txt").write_text(b["preview"], encoding="utf-8")
            saved = str(fallback / f"report_{stamp}.txt")
        except Exception:
            saved = None
        log.warning("report send failed: %s", e)
        # str() on a TimeoutError is the EMPTY string, so this rendered
        # "Could not reach the report server: " and stopped -- in the one
        # feature whose entire job is telling us what went wrong.
        why = str(e) or type(e).__name__
        return {"ok": False, "error": f"Could not reach the report server: {why}",
                "saved_locally": saved}

    return {"ok": True, "delivered": bool(res.get("ok")),
            "attached": b["attached"], "missing": b["missing"]}


@router.post("/api/agent/help")
async def api_agent_help(request: Request, _=Depends(require_auth)):
    """Ask the resident Kin, or hand off to echo-bloom-help on the same
    weights. No pause, no eviction, nothing in thoughts.db or the vault."""
    global _help_agent_running
    body = await request.json()
    question = (body.get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="Empty question")

    from agent_runner import run_help, DEFAULT_MODEL, HELP_AGENT_NAME
    import kin_presence

    chosen = DEFAULT_MODEL

    if _help_agent_running:
        return {
            "ok": False,
            "busy": True,
            "error": "Already answering another help question.",
            "model": chosen,
        }

    _help_agent_running = True
    kin_presence.heartbeat(HELP_AGENT_NAME, "working")
    try:
        try:
            result, used, meta = await run_help(question)
            chosen = used or chosen
        except Exception as e:
            log.warning("help agent failed (%s): %s", chosen, e)
            kin_presence.heartbeat(HELP_AGENT_NAME, "failed")
            return {
                "ok": False,
                "error": str(e),
                "model": chosen,
            }

        # Presence only, and only the help worker. Do not scribble the
        # Kin's wander presence. Do not vault-remember: Help is not a thought.
        kin_presence.heartbeat(HELP_AGENT_NAME, "resting")
        return {
            "ok": True,
            "result": result,
            "model": chosen,
            "author": meta.get("author") or HELP_AGENT_NAME,
            "handed_off": bool(meta.get("handed_off")),
            "from": meta.get("from"),
        }
    finally:
        _help_agent_running = False
