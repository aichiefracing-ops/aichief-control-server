from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# ------------------------------------------------------------
# App
# ------------------------------------------------------------
app = FastAPI(title="AI Chief Control Server", version="0.1.0")

# ------------------------------------------------------------
# Database (Railway Postgres)
# ------------------------------------------------------------
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set")

_db_initialized = False


def db():
    """
    Railway Postgres typically requires SSL.
    """
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db():
    """
    Lazy init to avoid Railway cold-start race where Postgres isn't ready yet.
    We'll attempt table creation on first use.
    """
    global _db_initialized
    if _db_initialized:
        return

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS installs (
                    install_id TEXT PRIMARY KEY,
                    version TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    first_seen TIMESTAMPTZ NOT NULL,
                    last_seen TIMESTAMPTZ NOT NULL,
                    uptime_s INTEGER
                )
                """
            )
        conn.commit()

    _db_initialized = True


# ------------------------------------------------------------
# Root
# ------------------------------------------------------------
@app.get("/")
def root():
    return {"status": "ok", "service": "ai-chief-control"}


# ------------------------------------------------------------
# Install registration + heartbeat
# ------------------------------------------------------------
class RegisterIn(BaseModel):
    install_id: str
    version: str
    platform: str = "windows"
    channel: str = "beta"
    machine_hash: Optional[str] = None


@app.post("/install/register")
def register(body: RegisterIn):
    init_db()

    now = datetime.now(timezone.utc)
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO installs (install_id, version, channel, platform, first_seen, last_seen)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (install_id)
                DO UPDATE SET
                    version = EXCLUDED.version,
                    channel = EXCLUDED.channel,
                    platform = EXCLUDED.platform,
                    last_seen = EXCLUDED.last_seen
                """,
                (body.install_id, body.version, body.channel, body.platform, now, now),
            )
        conn.commit()
    return {"ok": True}


class HeartbeatIn(BaseModel):
    install_id: str
    version: str
    channel: str = "beta"
    app_uptime_s: Optional[int] = None


# simple in-memory switches for now (later move these to DB)
KILLED_VERSIONS = set()
BETA_ENABLED = True


@app.post("/install/heartbeat")
def heartbeat(body: HeartbeatIn):
    init_db()

    now = datetime.now(timezone.utc)
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE installs
                SET version=%s, channel=%s, last_seen=%s, uptime_s=%s
                WHERE install_id=%s
                """,
                (body.version, body.channel, now, body.app_uptime_s, body.install_id),
            )
        conn.commit()

    kill_build = False
    kill_reason = None

    if not BETA_ENABLED and body.channel == "beta":
        kill_build = True
        kill_reason = "beta_ended"

    if body.version in KILLED_VERSIONS:
        kill_build = True
        kill_reason = "version_disabled"

    latest = (os.getenv("LATEST_VERSION") or "0.0.0").strip()

    return {
        "ok": True,
        "server_time": now.isoformat(),
        "beta_enabled": BETA_ENABLED,
        "kill_build": kill_build,
        "kill_reason": kill_reason,
        "update_available": latest > body.version,
        "latest_version": latest,
        "patch_url": os.getenv("PATCH_URL"),
        "force_update": False,
    }


# ------------------------------------------------------------
# ElevenLabs Proxy - streaming MP3
# ------------------------------------------------------------
def _require_control_key(x_aichief_key: str | None):
    expected = (os.getenv("CONTROL_API_KEY") or "").strip()
    if expected and (x_aichief_key or "").strip() != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


@app.post("/tts/stream")
def tts_stream(body: dict, x_aichief_key: str | None = Header(default=None)):
    """
    Client sends: {"text": "...", "voice_settings": {...} (optional)}
    Server returns: MP3 bytes (audio/mpeg), streaming.
    """
    _require_control_key(x_aichief_key)

    api_key = (os.getenv("ELEVENLABS_API_KEY") or "").strip()
    voice_id = (os.getenv("ELEVENLABS_VOICE_ID") or "").strip()
    model_id = (os.getenv("ELEVENLABS_MODEL_ID") or "").strip()
    if not api_key or not voice_id:
        raise HTTPException(status_code=500, detail="server missing ElevenLabs config")

    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")

    max_chars = int(os.getenv("TTS_MAX_CHARS", "800"))
    if len(text) > max_chars:
        raise HTTPException(status_code=400, detail=f"text too long (max {max_chars})")

    voice_settings = body.get("voice_settings") or None

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
    payload: dict = {"text": text}
    if model_id:
        payload["model_id"] = model_id
    if voice_settings:
        payload["voice_settings"] = voice_settings

    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }

    r = requests.post(url, json=payload, headers=headers, stream=True, timeout=60)
    if r.status_code != 200:
        try:
            err = r.json()
        except Exception:
            err = {"text": r.text[:250]}
        raise HTTPException(status_code=502, detail={"elevenlabs_status": r.status_code, "error": err})

    return StreamingResponse(r.iter_content(chunk_size=64 * 1024), media_type="audio/mpeg")
