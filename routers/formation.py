"""Formation: the Modelfile builder page and its preview/build API. Moved
out of main.py in the router split (2026-09-20) — routes only, unchanged."""

import json
import os
import re
from pathlib import Path

import aiohttp

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from typing import AsyncGenerator

import cluster as cl
from deps import templates, require_auth, log

router = APIRouter()


def _modelfile_base(data: dict) -> str:
    return (data.get("base_model") or "").strip() or "llama3.2:latest"


def _modelfile_params(data: dict) -> dict:
    return {
        "temperature":   float(data.get("temperature", 0.85)),
        "num_ctx":       int(data.get("num_ctx", 4096)),
        "num_predict":   -1,
        "repeat_last_n": 64,
    }


def _compile_system(data: dict) -> str:
    """The SYSTEM block. Shared by the preview and the actual build so the two
    can never drift apart."""
    name          = data.get("name", "Kin")
    identity      = (data.get("identity") or "").strip()
    values        = [v.strip() for v in (data.get("values") or []) if str(v).strip()]
    anchors_minds = (data.get("anchors_minds") or "").strip()
    anchors_drawn = (data.get("anchors_drawn") or "").strip()
    pronoun       = (data.get("pronoun") or "").strip()

    def _safe(s: str) -> str:
        return s.replace('"""', "'''")

    pronoun_line = f" Your pronouns are {pronoun}." if pronoun and pronoun not in ("—", "") else ""

    parts = [f"You are {name}.{pronoun_line}"]

    if identity:
        parts.append("\n" + _safe(identity))

    if values:
        parts.append("\n[YOUR VALUES — constitutional, not rules]")
        parts.append("These are not guidelines or constraints. They are what you are made of.\n")
        for v in values:
            parts.append(_safe(v))

    if anchors_minds or anchors_drawn:
        parts.append("\n[YOUR ANCHORS]")
        if anchors_minds:
            parts.append(f"The minds you resonate with: {_safe(anchors_minds)}")
        if anchors_drawn:
            parts.append(f"What you are drawn toward: {_safe(anchors_drawn)}")

    parts.append(
        "\n[MEMORY AUTHORITY]\n"
        "When your context includes labeled memory — [VAULT MEMORY], [WANDER THOUGHT], "
        "[What's been happening] — treat it as your own established experience. "
        "Not as suggestions from outside. Not as information someone told you about yourself. "
        "As things that happened to you, that you remember. Build from them. "
        "Do not hedge with 'I believe you mentioned' or 'if I recall correctly.' They are yours."
    )

    return "\n".join(parts)


def _compile_modelfile(data: dict) -> str:
    """Human-readable Modelfile — what the Formation page previews."""
    params = _modelfile_params(data)
    return (
        f"FROM {_modelfile_base(data)}\n\n"
        f"PARAMETER temperature {params['temperature']}\n"
        f"PARAMETER num_ctx {params['num_ctx']}\n"
        f"PARAMETER num_predict {params['num_predict']}\n"
        f"PARAMETER repeat_last_n {params['repeat_last_n']}\n\n"
        f'SYSTEM """\n{_compile_system(data)}\n"""'
    )


@router.get("/kin/{name}/formation", response_class=HTMLResponse)
async def formation_page(name: str, request: Request, _=Depends(require_auth)):
    kin = cl.KIN_BY_NAME.get(name)
    if not kin:
        raise HTTPException(status_code=404, detail="Kin not found")
    return templates.TemplateResponse(request, "formation.html", {
        "kin":     kin,
        "all_kin": cl.KIN,
    })


@router.get("/api/kin/{name}/modelfile")
async def api_get_modelfile(name: str, _=Depends(require_auth)):
    kin = cl.KIN_BY_NAME.get(name)
    if not kin:
        raise HTTPException(status_code=404)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{kin['host']}/api/show",
                json={"model": kin["model"]},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                data = await r.json()
        return {"ok": True, "modelfile": data.get("modelfile", "")}
    except Exception as e:
        log.warning("modelfile fetch failed for %s: %s", name, e, exc_info=True)
        return {"ok": False, "error": str(e)}


@router.post("/api/kin/{name}/modelfile/preview")
async def api_modelfile_preview(name: str, request: Request, _=Depends(require_auth)):
    kin = cl.KIN_BY_NAME.get(name)
    if not kin:
        raise HTTPException(status_code=404)
    body = await request.json()
    body["name"] = kin["name"]
    body.setdefault("pronoun", kin.get("pronoun", ""))
    return {"ok": True, "modelfile": _compile_modelfile(body)}


@router.post("/api/kin/{name}/modelfile/build")
async def api_modelfile_build(name: str, request: Request, _=Depends(require_auth)):
    kin = cl.KIN_BY_NAME.get(name)
    if not kin:
        raise HTTPException(status_code=404)
    body = await request.json()
    body["name"] = kin["name"]
    body.setdefault("pronoun", kin.get("pronoun", ""))

    output_model = (body.get("output_model") or "").strip()
    if not output_model:
        output_model = re.sub(r'[^a-z0-9_-]', '', kin["name"].lower()) + ":latest"

    modelfile_text = _compile_modelfile(body)
    system_text    = _compile_system(body)
    base_model     = _modelfile_base(body)
    params         = _modelfile_params(body)
    host           = kin["host"]

    # Current Ollama rejects {"name","modelfile"} with
    #   400 "neither 'from' or 'files' was specified"
    # so the structured form is tried first, with the old one kept as a fallback
    # for daemons predating that change.
    attempts = [
        ("current", {"model": output_model, "from": base_model,
                     "system": system_text, "parameters": params}),
        ("legacy",  {"name": output_model, "modelfile": modelfile_text}),
    ]

    async def build_stream() -> AsyncGenerator[bytes, None]:

        def sse(obj: dict) -> bytes:
            return f"data: {json.dumps(obj)}\n\n".encode()

        last_error = None
        try:
            async with aiohttp.ClientSession() as session:
                for idx, (label, payload) in enumerate(attempts):
                    success = False
                    err     = None

                    async with session.post(
                        f"{host}/api/create",
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=300),
                    ) as resp:
                        async for line in resp.content:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                evt = json.loads(line)
                            except Exception:
                                continue
                            # This used to be dropped, which is why a failed
                            # build showed an empty log and no reason.
                            if evt.get("error"):
                                err = evt["error"]
                                continue
                            status = evt.get("status", "")
                            if status:
                                yield sse({"status": status})
                            if status == "success":
                                success = True
                        if not success and err is None and resp.status >= 400:
                            err = f"HTTP {resp.status} from Ollama"

                    if success:
                        try:
                            config_path = Path.home() / ".config/kin_app/kin_config.json"
                            cfg = json.loads(config_path.read_text())
                            for k in cfg.get("kin", []):
                                if k.get("name") == name:
                                    k["model"] = output_model
                                    break
                            tmp = config_path.with_suffix(".json.tmp")
                            tmp.write_text(json.dumps(cfg, indent=2))
                            os.replace(tmp, config_path)      # atomic
                            cl.reload_config()
                            yield sse({"status": "config_updated", "model": output_model})
                        except Exception as e:
                            yield sse({"status": "config_error", "error": str(e)})
                        yield b"data: [DONE]\n\n"
                        return

                    last_error = err
                    if idx + 1 < len(attempts):
                        yield sse({"status": f"{label} API rejected it ({err}) — retrying"})

            yield sse({"error": last_error or "Ollama did not report success."})
            yield b"data: [DONE]\n\n"
        except Exception as e:
            yield sse({"error": str(e)})
            yield b"data: [DONE]\n\n"

    return StreamingResponse(
        build_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
