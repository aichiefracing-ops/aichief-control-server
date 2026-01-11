from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel


app = FastAPI(title="AI Chief Control Server")

# --- Simple in-memory config (temporary) ---
# We'll move this to Postgres in the next step.
KILLED_VERSIONS = set()  # e.g. {"2.0.17"}
BETA_ENABLED = True
LATEST_VERSION = os.getenv("LATEST_VERSION", "0.0.0")
PATCH_URL = os.getenv("PATCH_URL", "")  # R2 signed/public URL later


@app.get("/")
def root():
    return {"status": "ok", "service": "ai-chief-control"}


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
    # For now we just acknowledge. Next step: write into Postgres.
    return RegisterOut(ok=True, server_time=datetime.now(timezone.utc).isoformat())


class HeartbeatIn(BaseModel):
    install_id: str
    version: str
    channel: str = "beta"
    app_uptime_s: Optional[int] = None


class HeartbeatOut(BaseModel):
    ok: bool
    server_time: str

    # policy
    beta_enabled: bool
    kill_build: bool
    kill_reason: Optional[str] = None

    # updates
    update_available: bool
    latest_version: str
    patch_url: Optional[str] = None
    force_update: bool = False


@app.post("/install/heartbeat", response_model=HeartbeatOut)
def heartbeat(body: HeartbeatIn, x_aichief_key: Optional[str] = Header(default=None)):
    # Optional shared secret for clients (we’ll add later)
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

    update_available = False
    patch_url = PATCH_URL or None

    # naive semver compare (good enough for now if you use 2.0.18 style)
    def _tuple(v: str):
        try:
            return tuple(int(x) for x in v.split("."))
        except Exception:
            return (0, 0, 0)

    if _tuple(LATEST_VERSION) > _tuple(body.version):
        update_available = True

    return HeartbeatOut(
        ok=True,
        server_time=datetime.now(timezone.utc).isoformat(),
        beta_enabled=BETA_ENABLED,
        kill_build=kill_build,
        kill_reason=kill_reason,
        update_available=update_available,
        latest_version=LATEST_VERSION,
        patch_url=patch_url,
        force_update=False,
    )


# --- Admin endpoints (temporary, simple password in header) ---
def _require_admin(x_admin: Optional[str]):
    pw = os.getenv("ADMIN_PASSWORD")
    if not pw:
        raise HTTPException(status_code=500, detail="ADMIN_PASSWORD not set")
    if x_admin != pw:
        raise HTTPException(status_code=401, detail="unauthorized")


class AdminKillVersionIn(BaseModel):
    version: str


@app.post("/admin/kill_version")
def admin_kill_version(body: AdminKillVersionIn, x_admin: Optional[str] = Header(default=None)):
    _require_admin(x_admin)
    KILLED_VERSIONS.add(body.version)
    return {"ok": True, "killed_versions": sorted(KILLED_VERSIONS)}


@app.post("/admin/beta_end")
def admin_beta_end(x_admin: Optional[str] = Header(default=None)):
    _require_admin(x_admin)
    global BETA_ENABLED
    BETA_ENABLED = False
    return {"ok": True, "beta_enabled": BETA_ENABLED}

