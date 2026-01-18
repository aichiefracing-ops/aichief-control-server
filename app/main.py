from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from fastapi import Response
import requests

APP_VERSION = "0.2.2"

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
    Accept admin auth from any of these headers (so UI can be sloppy and we still work):
      - x-aichief-key: <key>
      - Authorization: Bearer <key>
      - CONTROL_API_KEY: <key>
      - x-api-key: <key>
      - control-api-key: <key>
    """
    if not CONTROL_API_KEY:
        raise HTTPException(status_code=500, detail="CONTROL_API_KEY not set on server")

    # Pick the first provided token
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

    # --- Garage Info (client UI) ---
    garage_status: str = ""
    garage_note: str = ""
    garage_subnote: str = ""



class KillIn(BaseModel):
    version: str
    reason: Optional[str] = "version_disabled"


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

    # --- Garage Info (client UI) ---
    "garage_status": "",
    "garage_note": "",
    "garage_subnote": "",

    "killed_versions": {},  # version -> reason
}

DEFAULT_INSTALLS = {}  # install_id -> {last_seen, version, channel, machine, user}


@app.get("/")
def root() -> Dict[str, Any]:
    return {"ok": True, "service": "ai-chief-control", "version": APP_VERSION}


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
# Client policy (UI calls this)
# -------------------------
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

    killed = settings.get("killed_versions", {}) or {}
    kill_reason = killed.get(body.version)

    should_lock = False
    reason = None

    if not beta_enabled:
        should_lock = True
        reason = "Beta is currently disabled."

    if kill_reason:
        should_lock = True
        reason = f"This build is disabled: {kill_reason}"

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
    
        # --- Garage Info (sent to UI) ---
        "garage_status": garage_status,
        "garage_note": garage_note,
        "garage_subnote": garage_subnote,
    }


# -------------------------
# Admin APIs (Admin UI calls these)
# -------------------------
@app.get("/admin/settings")
def admin_get_settings(
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),

    # accept common alternates without changing the UI
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
) -> Dict[str, Any]:
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)
    settings = _load_json(SETTINGS_PATH, DEFAULT_SETTINGS)
    return {
        "beta_enabled": bool(settings.get("beta_enabled", True)),
        "latest_version": str(settings.get("latest_version", "0.0.0")),
        "patch_url": settings.get("patch_url"),
        "force_update": bool(settings.get("force_update", False)),
        "killed_versions": settings.get("killed_versions", {}) or {},
            # --- Garage Info (client UI) ---
        "garage_status": str(settings.get("garage_status", "") or ""),
        "garage_note": str(settings.get("garage_note", "") or ""),
        "garage_subnote": str(settings.get("garage_subnote", "") or ""),
    }


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
    killed = settings.get("killed_versions", {}) or {}
    killed[str(body.version)] = body.reason or "version_disabled"
    settings["killed_versions"] = killed
    _save_json(SETTINGS_PATH, settings)
    return {"ok": True}


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
    killed = settings.get("killed_versions", {}) or {}
    killed.pop(str(body.version), None)
    settings["killed_versions"] = killed
    _save_json(SETTINGS_PATH, settings)
    return {"ok": True}
    
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

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
    headers = {
        "xi-api-key": api_key,
        "Accept": "audio/mpeg",  # ElevenLabs streams MP3
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

    # Return MP3 bytes; your warmer/client converts MP3->WAV and deletes MP3
    return Response(content=r.content, media_type="audio/mpeg")

