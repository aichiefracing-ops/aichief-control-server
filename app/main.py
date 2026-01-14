from __future__ import annotations

import os
import json
from datetime import datetime, timezone
from typing import Optional, Any, Dict, List

import psycopg2
import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

app = FastAPI(title="AI Chief Control Server", version="0.2.0")

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set")

def db():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def _utcnow():
    return datetime.now(timezone.utc)

def init_db():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS installs (
                install_id TEXT PRIMARY KEY,
                version TEXT NOT NULL,
                channel TEXT NOT NULL,
                platform TEXT NOT NULL,
                first_seen TIMESTAMPTZ NOT NULL,
                last_seen TIMESTAMPTZ NOT NULL,
                uptime_s INTEGER
            )
            """)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            )
            """)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS killed_versions (
                version TEXT PRIMARY KEY,
                reason TEXT,
                created_at TIMESTAMPTZ NOT NULL
            )
            """)
        conn.commit()

@app.on_event("startup")
def startup():
    init_db()
    # Seed defaults (only if not present)
    now = _utcnow()
    defaults = {
        "beta_enabled": os.getenv("BETA_ENABLED", "true"),
        "latest_version": os.getenv("LATEST_VERSION", "0.0.0"),
        "patch_url": os.getenv("PATCH_URL", ""),
        "force_update": os.getenv("FORCE_UPDATE", "false"),
    }
    with db() as conn:
        with conn.cursor() as cur:
            for k, v in defaults.items():
                cur.execute("""
                INSERT INTO settings (key, value, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (key) DO NOTHING
                """, (k, v, now))
        conn.commit()

def get_setting(key: str, default: str = "") -> str:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key=%s", (key,))
            row = cur.fetchone()
            return (row[0] if row else default)

def set_setting(key: str, value: str) -> None:
    now = _utcnow()
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO settings (key, value, updated_at)
            VALUES (%s,%s,%s)
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=EXCLUDED.updated_at
            """, (key, value, now))
        conn.commit()

def is_beta_enabled() -> bool:
    return get_setting("beta_enabled", "true").strip().lower() in ("1", "true", "yes", "y", "on")

def is_force_update() -> bool:
    return get_setting("force_update", "false").strip().lower() in ("1", "true", "yes", "y", "on")

def killed_reason(version: str) -> Optional[str]:
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT reason FROM killed_versions WHERE version=%s", (version,))
            row = cur.fetchone()
            return row[0] if row else None

def require_admin(x_control_key: str | None):
    expected = (os.getenv("CONTROL_API_KEY") or "").strip()
    if not expected:
        raise HTTPException(status_code=500, detail="server missing CONTROL_API_KEY")
    if (x_control_key or "").strip() != expected:
        raise HTTPException(status_code=401, detail="unauthorized")

# -------------------- Root --------------------

@app.get("/")
def root():
    return {"status": "ok", "service": "ai-chief-control", "time": _utcnow().isoformat()}

# -------------------- ElevenLabs proxy (server holds keys) --------------------

class TTSPayload(BaseModel):
    text: str
    accept: Optional[str] = "audio/wav"
    voice_settings: Optional[Dict[str, Any]] = None

@app.post("/tts/synthesize")
def tts_synthesize(payload: TTSPayload, x_aichief_key: str | None = Header(default=None)):
    # Optional: protect endpoint with AICHIEF_SERVER_KEY
    expected = (os.getenv("AICHIEF_SERVER_KEY") or "").strip()
    if expected and (x_aichief_key or "").strip() != expected:
        raise HTTPException(status_code=401, detail="unauthorized")

    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="missing text")

    api_key = (os.getenv("ELEVENLABS_API_KEY") or "").strip()
    voice_id = (os.getenv("ELEVENLABS_VOICE_ID") or "").strip()
    model_id = (os.getenv("ELEVENLABS_MODEL_ID") or "eleven_multilingual_v2").strip()

    if not api_key or not voice_id:
        raise HTTPException(status_code=500, detail="server missing ElevenLabs config")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    body = {
        "text": text,
        "model_id": model_id,
        "voice_settings": payload.voice_settings or {},
    }

    accept = (payload.accept or "audio/wav").strip()
    headers = {
        "xi-api-key": api_key,
        "content-type": "application/json",
        "accept": accept,
    }

    r = requests.post(url, headers=headers, data=json.dumps(body), timeout=60)
    if not r.ok:
        raise HTTPException(status_code=502, detail=f"elevenlabs error {r.status_code}: {r.text[:200]}")

    media_type = "audio/wav" if accept == "audio/wav" else "audio/mpeg"
    return Response(content=r.content, media_type=media_type)

# -------------------- Install tracking --------------------

class RegisterIn(BaseModel):
    install_id: str
    version: str
    platform: str = "windows"
    channel: str = "beta"
    machine_hash: Optional[str] = None

@app.post("/install/register")
def register(body: RegisterIn):
    now = _utcnow()
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO installs (install_id, version, channel, platform, first_seen, last_seen)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (install_id)
            DO UPDATE SET
                version = EXCLUDED.version,
                channel = EXCLUDED.channel,
                platform = EXCLUDED.platform,
                last_seen = EXCLUDED.last_seen
            """, (
                body.install_id,
                body.version,
                body.channel,
                body.platform,
                now,
                now,
            ))
        conn.commit()
    return {"ok": True}

class HeartbeatIn(BaseModel):
    install_id: str
    version: str
    channel: str = "beta"
    app_uptime_s: Optional[int] = None

@app.post("/install/heartbeat")
def heartbeat(body: HeartbeatIn):
    now = _utcnow()

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO installs (install_id, version, channel, platform, first_seen, last_seen, uptime_s)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (install_id)
            DO UPDATE SET
                version = EXCLUDED.version,
                channel = EXCLUDED.channel,
                last_seen = EXCLUDED.last_seen,
                uptime_s = EXCLUDED.uptime_s
            """, (
                body.install_id,
                body.version,
                body.channel,
                "windows",
                now,
                now,
                body.app_uptime_s
            ))
        conn.commit()

    beta_enabled = is_beta_enabled()
    latest = get_setting("latest_version", "0.0.0")
    patch_url = get_setting("patch_url", "")
    force_update = is_force_update()

    kill_build = False
    kill_reason = None

    # If beta ended and this install is on beta channel -> lock it
    if (not beta_enabled) and (body.channel.strip().lower() == "beta"):
        kill_build = True
        kill_reason = "beta_ended"

    # If this version is explicitly killed -> lock it
    kr = killed_reason(body.version.strip())
    if kr is not None:
        kill_build = True
        kill_reason = kr or "version_disabled"

    update_available = (latest.strip() > body.version.strip()) if latest else False

    return {
        "ok": True,
        "server_time": now.isoformat(),
        "beta_enabled": beta_enabled,
        "kill_build": kill_build,
        "kill_reason": kill_reason,
        "update_available": update_available,
        "latest_version": latest,
        "patch_url": patch_url,
        "force_update": force_update,
    }

# -------------------- Admin endpoints (your control panel uses these) --------------------

class BetaToggleIn(BaseModel):
    enabled: bool

@app.post("/admin/beta")
def admin_beta(body: BetaToggleIn, x_control_key: str | None = Header(default=None)):
    require_admin(x_control_key)
    set_setting("beta_enabled", "true" if body.enabled else "false")
    return {"ok": True, "beta_enabled": body.enabled}

class KillVersionIn(BaseModel):
    version: str
    reason: Optional[str] = "version_disabled"

@app.post("/admin/versions/kill")
def admin_kill_version(body: KillVersionIn, x_control_key: str | None = Header(default=None)):
    require_admin(x_control_key)
    v = body.version.strip()
    if not v:
        raise HTTPException(status_code=400, detail="missing version")
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            INSERT INTO killed_versions (version, reason, created_at)
            VALUES (%s,%s,%s)
            ON CONFLICT (version) DO UPDATE SET reason=EXCLUDED.reason
            """, (v, body.reason or "version_disabled", _utcnow()))
        conn.commit()
    return {"ok": True, "killed": v}

class UnkillVersionIn(BaseModel):
    version: str

@app.post("/admin/versions/unkill")
def admin_unkill_version(body: UnkillVersionIn, x_control_key: str | None = Header(default=None)):
    require_admin(x_control_key)
    v = body.version.strip()
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM killed_versions WHERE version=%s", (v,))
        conn.commit()
    return {"ok": True, "unkilled": v}

class ReleaseIn(BaseModel):
    latest_version: str
    patch_url: str
    force_update: bool = False

@app.post("/admin/release")
def admin_release(body: ReleaseIn, x_control_key: str | None = Header(default=None)):
    require_admin(x_control_key)
    set_setting("latest_version", body.latest_version.strip())
    set_setting("patch_url", body.patch_url.strip())
    set_setting("force_update", "true" if body.force_update else "false")
    return {
        "ok": True,
        "latest_version": body.latest_version.strip(),
        "patch_url": body.patch_url.strip(),
        "force_update": body.force_update,
    }

@app.get("/admin/installs")
def admin_installs(limit: int = 200, x_control_key: str | None = Header(default=None)):
    require_admin(x_control_key)
    limit = max(1, min(int(limit), 2000))
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            SELECT install_id, version, channel, platform, first_seen, last_seen, uptime_s
            FROM installs
            ORDER BY last_seen DESC
            LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append({
            "install_id": r[0],
            "version": r[1],
            "channel": r[2],
            "platform": r[3],
            "first_seen": r[4].isoformat() if r[4] else None,
            "last_seen": r[5].isoformat() if r[5] else None,
            "uptime_s": r[6],
        })
    return {"ok": True, "installs": out}

@app.get("/admin/settings")
def admin_settings(x_control_key: str | None = Header(default=None)):
    require_admin(x_control_key)
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT key, value, updated_at FROM settings ORDER BY key ASC")
            rows = cur.fetchall()
    return {
        "ok": True,
        "settings": [{"key": k, "value": v, "updated_at": t.isoformat()} for (k, v, t) in rows]
    }
