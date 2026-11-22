"""
voicepod_watch.py — watches the GPU voice server and pings Kory's phone (2026-11-21).

Runs inside the control server (Railway is always up; a dead pod can't report itself).
  * every VOICEPOD_WATCH_S (60 s) GET <TTS_PRIMARY_URL>/health
  * VOICEPOD_FAILS (3) misses in a row -> ONE "voice server DOWN" push via ntfy,
    with a "Restart voice server" button right on the notification
  * "stuck" push: health says ready, but drivers keep getting 503 busy and no line
    has been made for a while (needs the pod's /admin/status)
  * "back up" push when it recovers
Restart = POST <pod>/admin/restart with X-Admin-Key -> the server exits rc=75 and
/workspace/run_voice.sh relaunches it. Only the voice server, never the pod.

Railway env vars (none of these go in the repo — it's public):
  VOICEPOD_ADMIN_KEY   same value as /workspace/chief_admin_key.txt on the pod (NOT the TTS key)
  NTFY_TOPIC           long random topic name (anyone who knows it can read the pushes)
  NTFY_SERVER          default https://ntfy.sh
  PUBLIC_BASE_URL      this control server's https URL, for the restart button
  VOICEPOD_WATCH       "0" turns the watcher off
"""
from __future__ import annotations

import os
import secrets
import threading
import time
from typing import Any, Dict, Optional

import requests

EVERY_S = float(os.getenv("VOICEPOD_WATCH_S", "60") or 60)
FAILS_TO_ALERT = int(os.getenv("VOICEPOD_FAILS", "3") or 3)
TOKEN_TTL_S = 2 * 3600          # a restart button stays good for 2 h, and works once
STUCK_NO_LINE_S = 120.0         # no line made in this long while drivers get 503s = stuck


def _env(k: str, d: str = "") -> str:
    return (os.getenv(k) or d).strip()


def pod_url() -> str:
    return _env("TTS_PRIMARY_URL").rstrip("/")


STATE: Dict[str, Any] = {
    "status": "unknown",        # up | down | stuck | unknown
    "fails": 0,
    "since": None,              # when the current status started (epoch)
    "last_check": None,
    "last_error": None,
    "alerted": False,           # a down/stuck push has gone out for this outage
    "pod": None,                # last /admin/status from the pod
    "busy_hist": [],            # busy_503s at the last few checks
}
_TOKENS: Dict[str, float] = {}
_LOCK = threading.Lock()
_STARTED = False


# ---------------------------------------------------------------- push
def push(title: str, msg: str, priority: str = "default", tags: str = "", restart_button: bool = False) -> bool:
    topic = _env("NTFY_TOPIC")
    if not topic:
        print(f"[voicepod] (no NTFY_TOPIC) {title}: {msg}")
        return False
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = tags
    base = _env("PUBLIC_BASE_URL").rstrip("/")
    if restart_button and base:
        tok = new_token()
        headers["Actions"] = f"http, Restart voice server, {base}/voicepod/restart?t={tok}, method=POST, clear=true"
    try:
        r = requests.post(f"{_env('NTFY_SERVER', 'https://ntfy.sh').rstrip('/')}/{topic}",
                          data=msg.encode("utf-8"), headers=headers, timeout=10)
        return r.status_code < 300
    except Exception as e:
        print(f"[voicepod] push failed: {e}")
        return False


def new_token() -> str:
    tok = secrets.token_urlsafe(24)
    now = time.time()
    with _LOCK:
        for k in [k for k, exp in _TOKENS.items() if exp < now]:
            _TOKENS.pop(k, None)
        _TOKENS[tok] = now + TOKEN_TTL_S
    return tok


def use_token(tok: str) -> bool:
    """One-time: a token works once and only before it expires."""
    if not tok:
        return False
    with _LOCK:
        exp = _TOKENS.pop(tok, None)
    return bool(exp and exp >= time.time())


# ---------------------------------------------------------------- pod calls
def pod_status() -> Optional[dict]:
    key = _env("VOICEPOD_ADMIN_KEY")
    if not key or not pod_url():
        return None
    try:
        r = requests.get(f"{pod_url()}/admin/status", headers={"X-Admin-Key": key}, timeout=8)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def restart_voice() -> Dict[str, Any]:
    """Restart JUST the voice server process. Never the pod."""
    key = _env("VOICEPOD_ADMIN_KEY")
    if not pod_url():
        return {"ok": False, "why": "TTS_PRIMARY_URL not set"}
    if not key:
        return {"ok": False, "why": "VOICEPOD_ADMIN_KEY not set on Railway"}
    try:
        r = requests.post(f"{pod_url()}/admin/restart", headers={"X-Admin-Key": key}, timeout=10)
    except requests.exceptions.RequestException as e:
        return {"ok": False, "why": ("voice server not answering at all - the process and its restart loop "
                                     "are both gone (container restart?). Needs the pod: SSH in and run "
                                     "run_voice.sh, or restart the pod."), "detail": str(e)[:200]}
    if r.status_code == 200:
        return {"ok": True, "why": "restart sent - back in about a minute (model load)"}
    if r.status_code == 404:
        return {"ok": False, "why": "pod refused the admin key (or the pod's server is an old version without /admin)"}
    return {"ok": False, "why": f"pod answered HTTP {r.status_code}"}


# ---------------------------------------------------------------- watcher
def _mins(t0: Optional[float]) -> str:
    if not t0:
        return "?"
    m = (time.time() - t0) / 60.0
    return f"{m:.0f} min" if m < 120 else f"{m / 60:.1f} h"


def check_once() -> str:
    """One health check + state update + pushes. Returns the status. Safe to call from tests."""
    now = time.time()
    ok, err = False, None
    if not pod_url():
        err = "TTS_PRIMARY_URL not set"
    else:
        try:
            r = requests.get(f"{pod_url()}/health", timeout=8)
            ok = r.status_code == 200 and bool(r.json().get("ready"))
            if not ok:
                err = f"health {r.status_code} {r.text[:80]}"
        except requests.exceptions.Timeout:
            err = "no answer in 8 s (timeout)"
        except requests.exceptions.ConnectionError:
            err = "no answer (connection refused / unreachable)"
        except Exception as e:
            err = str(e)[:160]

    st = pod_status() if ok else None
    stuck = False
    if st:
        hist = (STATE["busy_hist"] + [int(st.get("busy_503s") or 0)])[-4:]
        STATE["busy_hist"] = hist
        rising = len(hist) >= 3 and hist[-1] - hist[0] >= 3
        last_ok = st.get("last_ok_s_ago")
        stuck = rising and (last_ok is None or last_ok > STUCK_NO_LINE_S)
    STATE.update(last_check=now, pod=st, last_error=err)

    prev = STATE["status"]
    if ok and not stuck:
        if STATE["alerted"]:
            push("Voice server back up", f"Was {prev} for {_mins(STATE['since'])}.", "default", "white_check_mark")
        if prev != "up":
            STATE["since"] = now
        STATE.update(status="up", fails=0, alerted=False)
    elif stuck:
        if prev != "stuck":
            STATE["since"] = now
        STATE["status"] = "stuck"
        if not STATE["alerted"]:
            STATE["alerted"] = True
            push("Voice server STUCK",
                 f"Health says ready but drivers are getting 'busy' and no line in "
                 f"{st.get('last_ok_s_ago')} s. Up {st.get('uptime_h')} h.",
                 "high", "warning", restart_button=True)
    else:
        STATE["fails"] += 1
        if STATE["fails"] >= FAILS_TO_ALERT:
            if prev != "down":
                STATE["since"] = now - EVERY_S * (FAILS_TO_ALERT - 1)
            STATE["status"] = "down"
            if not STATE["alerted"]:
                STATE["alerted"] = True
                push("Voice server DOWN", f"{FAILS_TO_ALERT} checks failed. Last: {err}", "urgent",
                     "rotating_light", restart_button=True)
    return STATE["status"]


def _loop():
    time.sleep(20)   # let the control server finish booting
    while True:
        try:
            check_once()
        except Exception as e:
            print(f"[voicepod] check error: {e}")
        time.sleep(EVERY_S)


def start() -> None:
    global _STARTED
    if _STARTED or _env("VOICEPOD_WATCH", "1") == "0":
        return
    _STARTED = True
    threading.Thread(target=_loop, name="voicepod-watch", daemon=True).start()
    print(f"[voicepod] watching {pod_url() or '(no TTS_PRIMARY_URL)'} every {EVERY_S:.0f}s")


def snapshot() -> Dict[str, Any]:
    s = dict(STATE)
    s.pop("busy_hist", None)
    s["down_for"] = _mins(s["since"]) if s["status"] in ("down", "stuck") else None
    s["pod_url_set"] = bool(pod_url())
    s["admin_key_set"] = bool(_env("VOICEPOD_ADMIN_KEY"))
    s["ntfy_set"] = bool(_env("NTFY_TOPIC"))
    return s
