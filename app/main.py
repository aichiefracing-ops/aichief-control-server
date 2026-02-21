from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from fastapi import Response
import requests

APP_VERSION = "0.2.3"

DATA_DIR = Path(os.getenv("DATA_DIR") or ".")
SETTINGS_PATH = DATA_DIR / "settings.json"
INSTALLS_PATH = DATA_DIR / "installs.json"
AFFILIATES_PATH = DATA_DIR / "affiliates.json"

CONTROL_API_KEY = (os.getenv("CONTROL_API_KEY") or "").strip()

# ── Stripe license lookup ──────────────────────────────────────
# Set STRIPE_SECRET_KEY as a Railway environment variable
STRIPE_SECRET_KEY = (os.getenv("STRIPE_SECRET_KEY") or "").strip()

STRIPE_PRO_IDS = [
    "prod_U1OcSXed9tZOl4",
    "prod_U1Oib1TWuA2U4r",
]
STRIPE_PRO_PLUS_IDS = [
    "prod_U1OeXZPAcV8j3p",
    "prod_U1OkjYcecOg7Gz",
]
# ──────────────────────────────────────────────────────────────

app = FastAPI(title="AI Chief Control Server", version=APP_VERSION)


# -------------------------
# Persistence helpers
# -------------------------
def _load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _save_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def _now() -> float:
    return time.time()


def _require_admin(
    x_aichief_key: Optional[str],
    authorization: Optional[str],
    control_api_key_hdr: Optional[str],
    x_api_key: Optional[str],
    control_api_key: Optional[str],
) -> None:
    if not CONTROL_API_KEY:
        raise HTTPException(status_code=500, detail="CONTROL_API_KEY not set on server")

    token = ""
    if x_aichief_key:
        token = x_aichief_key.strip()
    elif control_api_key_hdr:
        token = control_api_key_hdr.strip()
    elif x_api_key:
        token = x_api_key.strip()
    elif control_api_key:
        token = control_api_key.strip()
    elif authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()

    if not token or token != CONTROL_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")


# -------------------------
# Affiliate helpers
# -------------------------
def _extract_promo_code(sub: dict) -> Optional[str]:
    """Pull promo code off a Stripe subscription object if present."""
    try:
        discount = sub.get("discount") or {}
        coupon = discount.get("coupon") or {}
        name = coupon.get("name") or coupon.get("id") or ""
        return name.strip().upper() or None
    except Exception:
        return None


def _record_affiliate(email: str, code: Optional[str], tier: str) -> None:
    """Log affiliate code usage to affiliates.json."""
    if not code:
        return
    try:
        data = _load_json(AFFILIATES_PATH, {})
        if code not in data:
            data[code] = {"code": code, "subs": []}
        subs = data[code]["subs"]
        existing = next((s for s in subs if s.get("email") == email), None)
        if existing:
            existing["tier"] = tier
            existing["last_seen"] = _now()
        else:
            subs.append({
                "email": email,
                "tier": tier,
                "first_seen": _now(),
                "last_seen": _now(),
            })
        data[code]["total"] = len(subs)
        _save_json(AFFILIATES_PATH, data)
        print(f"[affiliate] recorded code={code} email={email} tier={tier}")
    except Exception as e:
        print(f"[affiliate] record failed: {e}")


# -------------------------
# Models
# -------------------------
class RegisterIn(BaseModel):
    install_id: str
    machine: Optional[str] = None
    user: Optional[str] = None
    version: Optional[str] = None
    channel: Optional[str] = "beta"

class TtsIn(BaseModel):
    text: str
    accept: Optional[str] = "audio/wav"

class HeartbeatIn(BaseModel):
    install_id: str
    version: Optional[str] = None
    channel: Optional[str] = "beta"

class ClientConfigIn(BaseModel):
    version: str
    channel: str = "beta"

class AdminSettings(BaseModel):
    beta_enabled: bool = True
    latest_version: str = "0.0.0"
    patch_url: Optional[str] = None
    force_update: bool = False
    kill_list: List[str] = []
    garage_status: str = ""
    garage_note: str = ""
    garage_subnote: str = ""

class KillIn(BaseModel):
    version: str

class UnkillIn(BaseModel):
    version: str

class LicenseCheckIn(BaseModel):
    email: str


# -------------------------
# Boot defaults
# -------------------------
DEFAULT_SETTINGS = {
    "beta_enabled": True,
    "latest_version": "0.0.0",
    "patch_url": None,
    "force_update": False,
    "kill_list": [],
    "garage_status": "",
    "garage_note": "",
    "garage_subnote": "",
}

DEFAULT_INSTALLS = {}


@app.get("/")
def root() -> Dict[str, Any]:
    return {"ok": True, "service": "ai-chief-control", "version": APP_VERSION}


# -------------------------
# Public APIs (Client Calls)
# -------------------------
@app.get("/settings")
def get_settings(x_aichief_key: Optional[str] = Header(None, alias="x-aichief-key")):
    current = _load_json(SETTINGS_PATH, DEFAULT_SETTINGS)
    if "kill_list" not in current:
        current["kill_list"] = []
    return current


@app.post("/client/config")
def client_config(body: ClientConfigIn) -> Dict[str, Any]:
    settings = _load_json(SETTINGS_PATH, DEFAULT_SETTINGS)

    beta_enabled = bool(settings.get("beta_enabled", True))
    latest_version = str(settings.get("latest_version", "0.0.0"))
    patch_url = settings.get("patch_url")
    force_update = bool(settings.get("force_update", False))
    garage_status = str(settings.get("garage_status", ""))
    garage_note = str(settings.get("garage_note", ""))
    garage_subnote = str(settings.get("garage_subnote", ""))

    kill_list = settings.get("kill_list", [])
    safe_kill_list = [str(k).strip() for k in kill_list]

    should_lock = False
    reason = None

    if not beta_enabled:
        should_lock = True
        reason = "Beta is currently disabled."

    if str(body.version) in safe_kill_list:
        should_lock = True
        reason = f"This build ({body.version}) has been disabled.\nPlease update."

    if force_update and latest_version and body.version != latest_version:
        should_lock = True
        reason = f"Update required. Your version {body.version} is behind."

    return {
        "ok": True,
        "beta_enabled": beta_enabled,
        "latest_version": latest_version,
        "patch_url": patch_url,
        "force_update": force_update,
        "should_lock": should_lock,
        "reason": reason or "",
        "garage_status": garage_status,
        "garage_note": garage_note,
        "garage_subnote": garage_subnote,
    }


# -------------------------
# License Check
# -------------------------
@app.post("/license/check")
def license_check(body: LicenseCheckIn) -> Dict[str, Any]:
    email = (body.email or "").strip().lower()
    if not email:
        return {"tier": "free"}

    if not STRIPE_SECRET_KEY:
        print("[license] WARN: STRIPE_SECRET_KEY not set — returning free")
        return {"tier": "free"}

    try:
        r = requests.get(
            "https://api.stripe.com/v1/customers",
            params={"email": email, "limit": 5},
            auth=(STRIPE_SECRET_KEY, ""),
            timeout=8,
        )
        if not r.ok:
            print(f"[license] Stripe customer lookup failed: {r.status_code}")
            return {"tier": "free"}

        customers = r.json().get("data", [])
        if not customers:
            return {"tier": "free", "email": email}

        for customer in customers:
            cid = customer.get("id")
            if not cid:
                continue

            subs_r = requests.get(
                "https://api.stripe.com/v1/subscriptions",
                params={"customer": cid, "status": "active", "limit": 10},
                auth=(STRIPE_SECRET_KEY, ""),
                timeout=8,
            )
            if not subs_r.ok:
                continue

            subs = subs_r.json().get("data", [])
            for sub in subs:
                for item in sub.get("items", {}).get("data", []):
                    price_id = item.get("price", {}).get("id", "")
                    product_id = item.get("price", {}).get("product", "")

                    if price_id in STRIPE_PRO_PLUS_IDS or product_id in STRIPE_PRO_PLUS_IDS:
                        code = _extract_promo_code(sub)
                        _record_affiliate(email, code, "pro_plus")
                        print(f"[license] {email} → pro_plus code={code}")
                        return {"tier": "pro_plus", "email": email, "affiliate_code": code}

                    if price_id in STRIPE_PRO_IDS or product_id in STRIPE_PRO_IDS:
                        code = _extract_promo_code(sub)
                        _record_affiliate(email, code, "pro")
                        print(f"[license] {email} → pro code={code}")
                        return {"tier": "pro", "email": email, "affiliate_code": code}

        print(f"[license] {email} → free (no matching active sub)")
        return {"tier": "free", "email": email}

    except Exception as e:
        print(f"[license] Stripe lookup exception: {e}")
        return {"tier": "free"}


# -------------------------
# Install APIs
# -------------------------
@app.post("/install/register")
def install_register(body: RegisterIn) -> Dict[str, Any]:
    installs = _load_json(INSTALLS_PATH, DEFAULT_INSTALLS)
    installs[body.install_id] = {
        "install_id": body.install_id,
        "machine": body.machine,
        "user": body.user,
        "version": body.version,
        "channel": body.channel or "beta",
        "last_seen": _now(),
    }
    _save_json(INSTALLS_PATH, installs)
    return {"ok": True}


@app.post("/install/heartbeat")
def install_heartbeat(body: HeartbeatIn) -> Dict[str, Any]:
    installs = _load_json(INSTALLS_PATH, DEFAULT_INSTALLS)
    item = installs.get(body.install_id) or {"install_id": body.install_id}
    item["version"] = body.version or item.get("version")
    item["channel"] = body.channel or item.get("channel") or "beta"
    item["last_seen"] = _now()
    installs[body.install_id] = item
    _save_json(INSTALLS_PATH, installs)
    return {"ok": True}


# -------------------------
# Admin APIs
# -------------------------
@app.get("/admin/settings")
def admin_get_settings(
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
) -> Dict[str, Any]:
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)
    settings = _load_json(SETTINGS_PATH, DEFAULT_SETTINGS)
    if "kill_list" not in settings:
        settings["kill_list"] = []
    return settings


@app.post("/admin/settings")
def admin_set_settings(
    body: AdminSettings,
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
) -> Dict[str, Any]:
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)
    settings = _load_json(SETTINGS_PATH, DEFAULT_SETTINGS)
    settings.update(body.model_dump())
    _save_json(SETTINGS_PATH, settings)
    return {"ok": True}


@app.get("/admin/installs")
def admin_installs(
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
) -> Dict[str, Any]:
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)
    installs = _load_json(INSTALLS_PATH, DEFAULT_INSTALLS)
    items = sorted(installs.values(), key=lambda x: x.get("last_seen", 0), reverse=True)
    return {"ok": True, "installs": items}


@app.post("/admin/kill")
def admin_kill(
    body: KillIn,
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
) -> Dict[str, Any]:
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)
    settings = _load_json(SETTINGS_PATH, DEFAULT_SETTINGS)
    kill_list = settings.get("kill_list", [])
    ver = str(body.version).strip()
    if ver and ver not in kill_list:
        kill_list.append(ver)
        settings["kill_list"] = kill_list
        _save_json(SETTINGS_PATH, settings)
    return {"ok": True, "killed": ver, "current_list": kill_list}


@app.post("/admin/unkill")
def admin_unkill(
    body: UnkillIn,
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
) -> Dict[str, Any]:
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)
    settings = _load_json(SETTINGS_PATH, DEFAULT_SETTINGS)
    kill_list = settings.get("kill_list", [])
    ver = str(body.version).strip()
    if ver in kill_list:
        kill_list.remove(ver)
        settings["kill_list"] = kill_list
        _save_json(SETTINGS_PATH, settings)
    return {"ok": True, "unkilled": ver, "current_list": kill_list}


@app.get("/admin/affiliates")
def admin_affiliates(
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
) -> Dict[str, Any]:
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)
    data = _load_json(AFFILIATES_PATH, {})
    summary = []
    for code, info in data.items():
        subs = info.get("subs", [])
        pro_count = sum(1 for s in subs if s.get("tier") == "pro")
        pro_plus_count = sum(1 for s in subs if s.get("tier") == "pro_plus")
        summary.append({
            "code": code,
            "total": info.get("total", 0),
            "pro": pro_count,
            "pro_plus": pro_plus_count,
            "subs": subs,
        })
    summary.sort(key=lambda x: x["total"], reverse=True)
    return {"ok": True, "affiliates": summary}


@app.post("/tts/stream")
def tts_stream(
    body: TtsIn,
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
):
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)

    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Missing text")

    api_key = (os.getenv("ELEVENLABS_API_KEY") or "").strip()
    voice_id = (os.getenv("ELEVENLABS_VOICE_ID") or "").strip()
    model_id = (os.getenv("ELEVENLABS_MODEL_ID") or "eleven_multilingual_v2").strip()

    if not api_key or not voice_id:
        raise HTTPException(status_code=500, detail="Missing ELEVENLABS_API_KEY or ELEVENLABS_VOICE_ID")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
    headers = {
        "xi-api-key": api_key,
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
    }
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.0,
            "use_speaker_boost": True,
        },
    }

    r = requests.post(url, headers=headers, json=payload, timeout=60)
    if not r.ok or not r.content:
        raise HTTPException(status_code=502, detail=f"ElevenLabs failed status={r.status_code}")

    return Response(content=r.content, media_type="audio/mpeg")
