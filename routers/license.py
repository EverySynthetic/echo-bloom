"""License page, activation, and status. Moved out of main.py in the router
split (2026-09-20) — routes only, unchanged."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

import license as lic
from deps import templates, require_auth_only, log, LICENSE_BUY_URL, LICENSE_PRICE

router = APIRouter()


@router.get("/license", response_class=HTMLResponse)
async def license_page(request: Request, _=Depends(require_auth_only)):
    status = lic.get_status()
    ctx = {
        "state":        status["state"],
        "days_left":    status.get("days_left"),
        "email":        status.get("email", ""),
        "license_type": status.get("type", ""),
        "reason":       status.get("reason", ""),
        "buy_url":      LICENSE_BUY_URL,
        "price":        LICENSE_PRICE,
    }
    return templates.TemplateResponse(request, "license.html", ctx)


@router.post("/api/license/activate")
async def api_license_activate(request: Request, _=Depends(require_auth_only)):
    body = await request.json()
    key  = (body.get("key") or "").strip()
    if not key:
        return {"ok": False, "error": "No key provided."}
    if not lic._CRYPTO_OK:
        # Save it and verify later. Refusing here punished the person who paid:
        # their only remedy was a reinstall, while the crypto-missing state was
        # simultaneously being exploited to fake a key. The key is verified on
        # every status computation once the package is present, so storing an
        # unverified one grants nothing.
        if not key.startswith("EB1-"):
            return {"ok": False, "error": "That does not look like an Echo Bloom key (EB1-…)."}
        if not lic.save_key(key):
            return {"ok": False, "error": f"Could not write license file to {lic.LICENSE_PATH} — check permissions."}
        lic.invalidate_status_cache()
        log.warning("license key saved but not verified — cryptography is missing")
        return {"ok": True, "message":
                "Key saved. The 'cryptography' package is missing, so it can't be "
                "checked yet — run: pip install cryptography  (then restart Echo Bloom) "
                "and your license will activate."}
    result = lic.verify_key(key)
    if not result["valid"]:
        return {"ok": False, "error": result.get("reason", "Invalid key.")}
    if not lic.save_key(key):
        return {"ok": False, "error": f"Could not write license file to {lic.LICENSE_PATH} — check permissions."}
    lic.invalidate_status_cache()   # so the new key applies on the next request
    ktype = result.get("type", "permanent")
    if ktype == "permanent":
        msg = f"Licensed forever. Welcome home{', ' + result['email'] if result.get('email') else ''}."
    else:
        msg = f"Trial key accepted. {result.get('days_left', '?')} days remaining."
    return {"ok": True, "message": msg}


@router.get("/api/license/status")
async def api_license_status(_=Depends(require_auth_only)):
    # buy_url/price ride along so anywhere that can render a key box can also
    # render a way to get a key. The dashboard card offered ACTIVATE and no
    # means of buying, for the whole trial — which is exactly the fortnight
    # someone decides whether to pay.
    status = dict(lic.get_status())
    status["buy_url"] = LICENSE_BUY_URL
    status["price"]   = LICENSE_PRICE
    return status
