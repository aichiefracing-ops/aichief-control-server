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

CONTROL_API_KEY = (os.getenv("CONTROL_API_KEY") or "").strip()

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
    """
    Accept admin auth from any of these headers.
    """
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
    
    # New Kill List support
    kill_list: List[str] = []

    # --- Garage Info (client UI) ---
    garage_status: str = ""
    garage_note: str = ""
    garage_subnote: str = ""

class KillIn(BaseModel):
    version: str

class UnkillIn(BaseModel):
    version: str


# -------------------------
# Boot defaults
# -------------------------
DEFAULT_SETTINGS = {
    "beta_enabled": True,
    "latest_version": "0.0.0",
    "patch_url": None,
    "force_update": False,
    "kill_list": [],  # List of strings ["1.0.6", "1.0.5"]

    # --- Garage Info (client UI) ---
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

# --- THIS IS THE NEW ENDPOINT THE CLIENT NEEDS ---
@app.get("/settings")
def get_settings(x_aichief_key: Optional[str] = Header(None, alias="x-aichief-key")):
    """
    Public endpoint for clients to pull Latest Version, Garage Info, and Kill List.
    No strict auth required (clients need to know if they are dead).
    """
    current = _load_json(SETTINGS_PATH, DEFAULT_SETTINGS)
    
    # Safety: ensure list exists
    if "kill_list" not in current:
        current["kill_list"] = []
        
    # Filter out sensitive admin stuff if you add any later
    # For now, sending the whole settings object is fine
    return current


# -------------------------
# Client policy (Legacy Support for 1.0.6 and older)
# -------------------------
@app.post("/client/config")
def client_config(body: ClientConfigIn) -> Dict[str, Any]:
    settings = _load_json(SETTINGS_PATH, DEFAULT_SETTINGS)

    beta_enabled = bool(settings.get("beta_enabled", True))
    latest_version = str(settings.get("latest_version", "0.0.0"))
    patch_url = settings.get("patch_url")
    force_update = bool(settings.get("force_update", False))
    
    # Garage info
    garage_status = str(settings.get("garage_status", ""))
    garage_note = str(settings.get("garage_note", ""))
    garage_subnote = str(settings.get("garage_subnote", ""))

    # --- CRITICAL FIX: Check the NEW Kill List ---
    # Old clients don't know about "kill_list", so we must check it for them here
    # and return the result as 'should_lock'.
    kill_list = settings.get("kill_list", [])
    safe_kill_list = [str(k).strip() for k in kill_list]

    should_lock = False
    reason = None

    # 1. Global Beta Lock
    if not beta_enabled:
        should_lock = True
        reason = "Beta is currently disabled."

    # 2. Version Kill Switch (The Fix)
    if str(body.version) in safe_kill_list:
        should_lock = True
        reason = f"This build ({body.version}) has been disabled.\nPlease update."

    # 3. Force Update
    if force_update and latest_version and body.version != latest_version:
        should_lock = True
        reason = f"Update required. Your version {body.version} is behind."
    

    return {
        "ok": True,
        "beta_enabled": beta_enabled,
        "latest_version": latest_version,
        "patch_url": patch_url,
        "force_update": force_update,
        "should_lock": should_lock,  # <--- Old client reads this
        "reason": reason or "",      # <--- Old client reads this
    
        # --- Garage Info (sent to UI) ---
        "garage_status": garage_status,
        "garage_note": garage_note,
        "garage_subnote": garage_subnote,
    }

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
# Admin APIs (Admin Dashboard Calls)
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
    
    # Ensure kill_list is present for the UI
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
    
    # Clean update
    update_data = body.model_dump()
    settings.update(update_data)
    
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


@app.post("/tts/stream")
def tts_stream(
    body: TtsIn,
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
):
    # protect your credits
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)

    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Missing text")

    api_key = (os.getenv("ELEVENLABS_API_KEY") or "").strip()
    voice_id = (os.getenv("ELEVENLABS_VOICE_ID") or "").strip()
    model_id = (os.getenv("ELEVENLABS_MODEL_ID") or "eleven_multilingual_v2").strip()

    if not api_key or not voice_id:
        raise HTTPException(status_code=500, detail="Missing ELEVENLABS_API_KEY or ELEVENLABS_VOICE_ID")

    # -----------------------------------------------------------
    # We ask 11Labs for MP3 (standard), but client converts to WAV
    # -----------------------------------------------------------
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
