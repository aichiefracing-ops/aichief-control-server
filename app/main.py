from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

import psycopg2
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel


app = FastAPI(title="AI Chief Control Server")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set")


def db():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


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
        conn.commit()


@app.on_event("startup")
def startup():
    init_db()


KILLED_VERSIONS = set()
BETA_ENABLED = True


@app.get("/")
def root():
    return {"status": "ok", "service": "ai-chief-control"}


class RegisterIn(BaseModel):
    install_id: str
    version: str
    platform: str = "windows"
    channel: str = "beta"
    machine_hash: Optional[str] = None


@app.post("/install/register")
def register(body: RegisterIn):
    now = datetime.now(timezone.utc)
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
    now = datetime.now(timezone.utc)

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            UPDATE installs
            SET version=%s, channel=%s, last_seen=%s, uptime_s=%s
            WHERE install_id=%s
            """, (
                body.version,
                body.channel,
                now,
                body.app_uptime_s,
                body.install_id
            ))
        conn.commit()

    kill_build = False
    kill_reason = None

    if not BETA_ENABLED and body.channel == "beta":
        kill_build = True
        kill_reason = "beta_ended"

    if body.version in KILLED_VERSIONS:
        kill_build = True
        kill_reason = "version_disabled"

    latest = os.getenv("LATEST_VERSION", "0.0.0")

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
