from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

# ------------------------------------------------------------
# App MUST be created before any @app.<method> decorators
# ------------------------------------------------------------
app = FastAPI(title="AI Chief Control Server")

# ------------------------------------------------------------
# Database
# ------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set")

def db():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
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

@app.on_event("startup")
def startup():
    init_db()

# ------------------------------------------------------------
# Root
# ------------------------------------------------------------
@app.get("/")
def root():
    return {"status": "ok", "service": "ai-chief-control"}

# ------------------------------------------------------------
# Installs
# ------------------------------------------------------------
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
                (
                    body.install_id,
                    body.version,
                    body.channel,
                    body.platform,
                    now,
                    now,
                ),
            )
        conn.commit()
    return {"ok": True}

class HeartbeatIn(BaseModel):
    install_id: str
    version: str
    channel: str = "beta"
    app_uptime_s: Optional[int] = None

KILLED_VERSIONS = set()
BETA_ENABLED = True

@app.post("/install/heartbeat")
def heartbeat(body: HeartbeatIn):
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
        kill_reason_
