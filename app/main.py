from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel


app = FastAPI(title="AI Chief Control Server")

# --- TEMP policy state (we will store in Postgres next) ---
KILLED_VERSIONS = set()          # e.g. {"2.0.17"}
BETA_ENABLED = True              # admin can end beta
LATEST_VERSION = os.getenv("LATEST_VERSION", "0.0.0")
PATCH_URL = os.getenv("PATCH_URL", "")  # R2 URL later


@app.get("/")
def root():
    return {"status": "ok", "service": "ai-chief-control"}


# ---------------------------
# Client endpoints
# ---------------------------

class RegisterIn(BaseModel):
    install_id: str
    version: str
    platform: str = "windows"
    channel: str = "beta"  # beta/prod
    machine_hash: Optional[str] = None


class RegisterOut(BaseModel):
    ok: bool
    server_time: str


@app.post("/install/register", response_model=RegisterOut)
def register(body: RegisterIn):
    return RegisterOut(ok=True, server_time=datetime.now(timezone.utc).isoformat())


class HeartbeatIn(BaseModel):
    install_id: str
    version: str
    channel: str = "beta"
    app_uptime_s: Optional[int] = None


class HeartbeatOut(BaseModel):
    ok: bool
    server_time: str
    beta_enabled: bool
    kill_build: bool
    kill_reason: Optional[str] = None
    update_available: bool
    latest_version: str
    patch_url: Optional[str] = None
    force_update: bool = False


def _ver_tuple(v: str):
    try:
        return tuple(int(x) for x in v.split("."))
    except Exception:
        return (0, 0, 0)


@app.post("/install/heartbeat", response_model=HeartbeatOut)
def heartbeat(body: HeartbeatIn, x_aichief_key: Optional[str] = Header(default=None)):
    # Optional: client shared key later
    # if os.getenv("CLIENT_SHARED_KEY") and x_aichief_key != os.getenv("CLIENT_SHARED_KEY"):
    #     raise HTTPException(status_code=401, detail="bad client key")

    kill_reason = None
    kill_build = False

    if not BETA_ENABLED and body.channel == "beta":
        kill_build = True
        kill_reason = "beta_ended"

    if body.version in KILLED_VERSIONS:
        kill_build = True
        kill_reason = "version_disabled"

    latest = os.getenv("LATEST_VERSION", LATEST_VERSION)
    patch = os.getenv("PATCH_URL", PATCH_URL) or None

    update_available = _ver_tuple(latest) > _ver_tuple(body.version)

    return HeartbeatOut(
        ok=True,
        server_time=datetime.now(timezone.utc).isoformat(),
        beta_enabled=BETA_ENABLED,
        kill_build=kill_build,
        kill_reason=kill_reason,
        update_available=update_available,
        latest_version=latest,
        patch_url=patch,
        force_update=False,
    )


# ---------------------------
# Admin endpoints
# ---------------------------

def _require_admin(x_admin: Optional[str]):
    pw = os.getenv("ADMIN_PASSWORD")
    if not pw:
        raise H

