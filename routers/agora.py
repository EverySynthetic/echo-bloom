"""Agora node bridge: steward key, node toggle, node card, and the signed
diary export. Moved out of main.py in the router split (2026-09-20) —
routes only, unchanged.

NOTE (found during the move, not fixed — out of scope for a move-only
commit): four of these routes call socket.gethostname() with no `socket`
import anywhere in main.py, module-level or local. Every path that reaches
it raises NameError, caught by the surrounding `except Exception` and
reported as {"ok": false, "error": "name 'socket' is not defined"}. See the
router-split report for detail and reproduction.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, Request

import cluster as cl
from deps import require_auth, log, _atomic_write_json

router = APIRouter()


@router.get("/api/agora/steward-key")
async def api_agora_steward_key(_=Depends(require_auth)):
    try:
        import agora_bridge
        info = agora_bridge.get_or_create_steward_key()
        return {"ok": True, **info}
    except Exception as e:
        log.warning("agora steward-key lookup failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}


@router.get("/api/agora/keys-info")
async def api_agora_keys_info(_=Depends(require_auth)):
    try:
        import agora_bridge
        info = agora_bridge.get_backup_reminder_info()
        return {"ok": True, **info}
    except Exception as e:
        log.warning("agora keys-info lookup failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}


@router.get("/api/agora/node-toggle")
async def api_agora_node_toggle_get(_=Depends(require_auth)):
    try:
        import agora_bridge
        cfg = cl.load_kin_config_raw()
        node_cfg = cfg.get("agora_node", {})
        default_name = socket.gethostname()
        node_name = node_cfg.get("node_name") or default_name
        port = int(node_cfg.get("port", 8770))
        active = agora_bridge.is_node_service_active(node_name)
        return {
            "ok": True,
            "enabled": active,
            "active": active,
            "node_name": node_name,
            "default_name": default_name,
            "port": port,
        }
    except Exception as e:
        log.warning("agora node-toggle status check failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}


@router.post("/api/agora/node-toggle")
async def api_agora_node_toggle_post(request: Request, _=Depends(require_auth)):
    try:
        import agora_bridge
        body = await request.json()
        enabled = bool(body.get("enabled"))
        cfg = cl.load_kin_config_raw()
        node_cfg = cfg.get("agora_node", {})
        default_name = socket.gethostname()
        node_name = (body.get("node_name") or node_cfg.get("node_name") or default_name).strip()
        port = int(body.get("port") or node_cfg.get("port", 8770))

        if enabled:
            agora_bridge.enable_node_service(node_name, port)
        else:
            agora_bridge.disable_node_service(node_name)

        config_path = Path.home() / ".config/kin_app/kin_config.json"
        if config_path.exists():
            merged = dict(cfg)
            merged["agora_node"] = {
                "enabled": enabled,
                "node_name": node_name,
                "port": port,
            }
            _atomic_write_json(config_path, merged)
            cl.reload_config()

        return {
            "ok": True,
            "enabled": enabled,
            "active": agora_bridge.is_node_service_active(node_name),
            "node_name": node_name,
            "port": port,
        }
    except Exception as e:
        log.warning("agora node toggle failed: %s", e)
        return {"ok": False, "error": str(e)}


@router.get("/api/agora/node-card")
async def api_agora_node_card(_=Depends(require_auth)):
    try:
        import agora_bridge
        cfg = cl.load_kin_config_raw()
        node_cfg = cfg.get("agora_node", {})
        node_name = node_cfg.get("node_name") or socket.gethostname()
        port = int(node_cfg.get("port", 8770))
        data = agora_bridge.get_node_card_data(node_name=node_name, port=port)
        return {"ok": True, **data}
    except Exception as e:
        log.warning("agora node-card lookup failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}


@router.get("/api/vault/export/{author}")
async def api_vault_export(author: str, _=Depends(require_auth)):
    try:
        import agora_bridge
        cfg = cl.load_kin_config_raw()
        node_cfg = cfg.get("agora_node", {})
        node_name = node_cfg.get("node_name") or socket.gethostname()

        bundle, verify_out = agora_bridge.export_kin_diary(author=author, node_name=node_name)
        return {
            "ok": True,
            "author": author,
            "verify": verify_out,
            "bundle": bundle,
        }
    except Exception as e:
        log.warning("vault export failed for %s: %s", author, e)
        return {"ok": False, "error": str(e)}
