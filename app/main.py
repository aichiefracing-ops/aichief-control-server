from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Optional, Any, Dict

import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response, JSONResponse
from pydantic import BaseModel

app = FastAPI(title="AI Chief Control Server", version="0.2.0")


# ------------------------- helpers -------------------------

def _require_key(x_aichief_key: Optional[str]) -> None:
    expected = (os.getenv("CONTROL_API_KEY") or "").strip()
    if not expected:
        raise HTTPException(status_code=500, detail="Server missing CONTROL_API_KEY")
    if (x_aichief_key or "").strip() != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

def _env_bool(name: str, default: bool = False) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if v == "":
        return default
    return v in ("1", "true", "yes", "y", "on")

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _kill_matches(kill_build: str, version: str, channel: str) -> bool:
    kb = (kill_build or "").strip().lower()
    ver = (version or "").strip().lower()
    ch = (channel or "").strip().lower()

    if kb in ("", "none", "off", "0"):
        return False
    if kb == "all":
        return True
    if kb == ch:  # e.g. "beta" kills beta channel
        return True
    return kb == ver


# ------------------------- models -------------------------

class RegisterIn(BaseModel):
    install_id: str
    version: str
    platform: str = "windows"
    channel: str = "beta"
    machine_hash: Optional[str] = None

class HeartbeatIn(BaseModel):
    install_id: str
    version: str
    channel: str = "beta"
    app_uptime_s: Optional[int] = None

class ClientConfigIn(BaseModel):
    install_id: Optional[str] = None
    version: str = "0.0.0"
    channel: str = "beta"


# ------------------------- routes -------------------------

@app.get("/")
def root() -> Dict[str, Any]:
    return {"ok": True, "name": "AI Chief Control Server", "server_time": _now()}


@app.post("/install/register")
def register(body: RegisterIn, x_aichief_key: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    _require_key(x_aichief_key)

    # Minimal: accept + ack. (We can store in Postgres later.)
    return {"ok": True, "server_time": _now()}


@app.post("/install/heartbeat")
def heartbeat(body: HeartbeatIn, x_aichief_key: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    _require_key(x_aichief_key)

    # Minimal: accept + ack. (We can store in Postgres later.)
    return {"ok": True, "server_time": _now()}


@app.post("/client/config")
def client_config(body: ClientConfigIn, x_aichief_key: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    """
    This is what the UI will poll.
    Toggle these in Railway Variables:
      BETA_ENABLED=true/false
      KILL_BUILD=all OR beta OR 0.2.1
      KILL_REASON=...
      LATEST_VERSION=0.2.5
      PATCH_URL=https://.../patch.zip
    """
    _require_key(x_aichief_key)

    beta_enabled = _env_bool("BETA_ENABLED", default=True)
    kill_build = (os.getenv("KILL_BUILD") or "").strip()
    kill_reason = (os.getenv("KILL_REASON") or "Build disabled by server.").strip()
    latest_version = (os.getenv("LATEST_VERSION") or "0.0.0").strip()
    patch_url = (os.getenv("PATCH_URL") or "").strip()

    should_kill = (not beta_enabled) or _kill_matches(kill_build, body.version, body.channel)

    return {
        "ok": True,
        "server_time": _now(),
        "beta_enabled": beta_enabled,
        "kill_build": kill_build,
        "kill_reason": kill_reason,
        "should_lock": bool(should_kill),
        "latest_version": latest_version,
        "patch_url": patch_url,
    }


@app.post("/tts/synthesize")
def tts_synthesize(payload: Dict[str, Any], x_aichief_key: Optional[str] = Header(default=None)):
    """
    Proxies ElevenLabs so users never have your ElevenLabs key.
    Requires Railway Variables:
      CONTROL_API_KEY=...
      ELEVENLABS_API_KEY=...
      ELEVENLABS_VOICE_ID=...
    Client posts: { "text": "...", "accept": "audio/wav" or "audio/mpeg" }
    """
    _require_key(x_aichief_key)

    api_key = (os.getenv("ELEVENLABS_API_KEY") or "").strip()
    voice_id = (os.getenv("ELEVENLABS_VOICE_ID") or "").strip()
    if not api_key or not voice_id:
        raise HTTPException(status_code=500, detail="Server missing ELEVENLABS_API_KEY or ELEVENLABS_VOICE_ID")

    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="Missing 'text'")

    accept = (payload.get("accept") or "audio/mpeg").strip().lower()
    if accept not in ("audio/mpeg", "audio/wav"):
        accept = "audio/mpeg"

    # ElevenLabs endpoint
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": api_key,
        "accept": accept,
        "Content-Type": "application/json",
    }

    # Keep it simple: model_id optional
    body = {
        "text": text,
        "model_id": payload.get("model_id") or "eleven_monolingual_v1",
    }
    # voice_settings optional
    if isinstance(payload.get("voice_settings"), dict):
        body["voice_settings"] = payload["voice_settings"]

    r = requests.post(url, headers=headers, data=json.dumps(body), timeout=60)
    if not r.ok:
        raise HTTPException(status_code=502, detail=f"elevenlabs error {r.status_code}: {r.text[:300]}")

    media_type = "audio/wav" if accept == "audio/wav" else "audio/mpeg"
    return Response(content=r.content, media_type=media_type)
