from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel
from fastapi import Response
from fastapi.responses import RedirectResponse, FileResponse, HTMLResponse
import requests
#test
# ── Postgres (Prime session storage) ──────────────────────────
try:
    import psycopg2
    import psycopg2.extras
    _PG_URL = (os.getenv("DATABASE_URL") or "").strip()
#test#
    def _pg_conn():
        return psycopg2.connect(_PG_URL, sslmode="require")

    def _init_prime_table():
        if not _PG_URL:
            return
        try:
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS prime_sessions (
                            id          SERIAL PRIMARY KEY,
                            uuid        TEXT NOT NULL,
                            received_at TIMESTAMPTZ DEFAULT NOW(),
                            payload     JSONB NOT NULL
                        );
                        CREATE INDEX IF NOT EXISTS prime_uuid_idx ON prime_sessions(uuid);
                    """)
                conn.commit()
            print("[prime] DB table ready")
        except Exception as e:
            print(f"[prime] DB init error: {e}")

    def _init_affiliate_table():
        if not _PG_URL:
            return
        try:
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS affiliate_profiles (
                            code        TEXT PRIMARY KEY,
                            data        JSONB NOT NULL,
                            updated_at  TIMESTAMPTZ DEFAULT NOW()
                        );
                    """)
                conn.commit()
            print("[affiliate] DB table ready")
        except Exception as e:
            print(f"[affiliate] DB init error: {e}")

    def _init_seats_table():
        if not _PG_URL:
            return
        try:
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS license_seats (
                            email       TEXT NOT NULL,
                            install_id  TEXT NOT NULL,
                            tier        TEXT,
                            machine     TEXT,
                            first_seen  TIMESTAMPTZ DEFAULT NOW(),
                            last_seen   TIMESTAMPTZ DEFAULT NOW(),
                            PRIMARY KEY (email, install_id)
                        );
                        CREATE INDEX IF NOT EXISTS seats_email_idx   ON license_seats(email);
                        CREATE INDEX IF NOT EXISTS seats_install_idx ON license_seats(install_id);
                    """)
                conn.commit()
            print("[seat] DB table ready")
        except Exception as e:
            print(f"[seat] DB init error: {e}")

    def _init_prime_recordings_table():
        if not _PG_URL:
            return
        try:
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS prime_recordings (
                            id          SERIAL PRIMARY KEY,
                            uuid        TEXT NOT NULL,
                            received_at TIMESTAMPTZ DEFAULT NOW(),
                            meta        JSONB NOT NULL,
                            path        TEXT NOT NULL,
                            bytes       BIGINT NOT NULL
                        );
                        CREATE INDEX IF NOT EXISTS prime_rec_uuid_idx ON prime_recordings(uuid);
                    """)
                conn.commit()
            print("[recording] DB table ready")
        except Exception as e:
            print(f"[recording] DB init error: {e}")

    def _init_qa_findings_table():
        if not _PG_URL:
            return
        try:
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS prime_qa_findings (
                            id             SERIAL PRIMARY KEY,
                            recording_file TEXT,
                            uuid           TEXT,
                            build          TEXT,
                            track          TEXT,
                            car            TEXT,
                            worker         TEXT,
                            ticks          INTEGER DEFAULT 0,
                            status         TEXT,
                            summary        TEXT,
                            report         JSONB,
                            created_at     TIMESTAMPTZ DEFAULT NOW()
                        );
                        CREATE INDEX IF NOT EXISTS qa_findings_created_idx ON prime_qa_findings(created_at DESC);
                        CREATE INDEX IF NOT EXISTS qa_findings_file_idx ON prime_qa_findings(recording_file);
                    """)
                    # verdict: NULL/'real' = a genuine find (scores +3); 'false_positive'
                    # = a find that turned out wrong (scores -1). Added late, so IF NOT EXISTS.
                    cur.execute(
                        "ALTER TABLE prime_qa_findings ADD COLUMN IF NOT EXISTS verdict TEXT"
                    )
                conn.commit()
            print("[qa] DB table ready")
        except Exception as e:
            print(f"[qa] DB init error: {e}")

    _init_prime_table()
    _init_affiliate_table()
    _init_seats_table()
    _init_prime_recordings_table()
    _init_qa_findings_table()
    _PRIME_DB_OK = bool(_PG_URL)
except ImportError:
    _PRIME_DB_OK = False
    print("[prime] psycopg2 not found — falling back to JSONL")
# ──────────────────────────────────────────────────────────────

APP_VERSION = "0.2.3"

DATA_DIR = Path(os.getenv("DATA_DIR") or ".")
SETTINGS_PATH = DATA_DIR / "settings.json"
INSTALLS_PATH = DATA_DIR / "installs.json"
AFFILIATES_PATH = DATA_DIR / "affiliates.json"
PRIME_PATH = DATA_DIR / "prime_sessions.jsonl"
RECORDINGS_DIR = DATA_DIR / "recordings"
QA_FINDINGS_INDEX = DATA_DIR / "qa_findings.jsonl"
RECORDINGS_INDEX = DATA_DIR / "prime_recordings.jsonl"
# Full ns-stream recordings are far bigger than event batches; cap the upload so a
# runaway/abusive POST can't exhaust the box. Endurance gzip recordings can be tens of
# MB — 300 MB is a generous ceiling. Override with MAX_RECORDING_BYTES.
MAX_RECORDING_BYTES = int(os.getenv("MAX_RECORDING_BYTES", str(300 * 1024 * 1024)))
AFFILIATE_PROFILES_PATH = DATA_DIR / "affiliate_profiles.json"
TESTER_OVERRIDES_PATH   = DATA_DIR / "tester_overrides.json"

CONTROL_API_KEY = (os.getenv("CONTROL_API_KEY") or "").strip()

# ── Seat binding (per-email device limit) ──────────────────────
# Paid emails are limited to SEAT_LIMIT distinct machines. A machine
# unseen for SEAT_STALE_DAYS is auto-reclaimed on the next check.
SEAT_LIMIT      = int(os.getenv("SEAT_LIMIT", "2"))
SEAT_STALE_DAYS = int(os.getenv("SEAT_STALE_DAYS", "7"))
SEATS_PATH      = DATA_DIR / "license_seats.json"
# ──────────────────────────────────────────────────────────────

# ── Stripe license lookup ──────────────────────────────────────
# Set STRIPE_SECRET_KEY as a Railway environment variable
STRIPE_SECRET_KEY = (os.getenv("STRIPE_SECRET_KEY") or "").strip()

STRIPE_PRO_IDS = [
    "prod_U3xVv4KtiMXTyp",
    "prod_U3xVAtBHDwLdrn",
]
STRIPE_PRO_PLUS_IDS = [
    "prod_U1OeXZPAcV8j3p",
    "prod_U1OkjYcecOg7Gz",
]
# Dev accounts — is_dev=true returned only for these emails
DEV_EMAILS = {"ksherman618@gmail.com"}
# ──────────────────────────────────────────────────────────────

# ── Self-hosted GPU voice server (paid tiers) ──────────────────
# Paid clients synth dynamic Chief lines DIRECTLY against this GPU box.
# Railway is NOT in the audio path — it only hands the URL + key down
# inside the license check so the client knows where to call and how
# to authenticate. Everything here is injected as Railway env vars.
TTS_PRIMARY_URL = (os.getenv("TTS_PRIMARY_URL") or "").strip()
TTS_BACKUP_URL  = (os.getenv("TTS_BACKUP_URL") or "").strip()
TTS_SERVER_KEY  = (os.getenv("TTS_SERVER_KEY") or "").strip()


def _with_tts(resp: Dict[str, Any]) -> Dict[str, Any]:
    """Attach the GPU voice-server endpoint + key to paid (pro / pro_plus)
    license responses. No-op for free, and a no-op if no server URL is set."""
    try:
        if resp.get("tier") in ("pro", "pro_plus") and TTS_PRIMARY_URL:
            resp["tts_url"] = TTS_PRIMARY_URL
            resp["tts_backup_url"] = TTS_BACKUP_URL
            resp["tts_key"] = TTS_SERVER_KEY
    except Exception:
        pass
    return resp
# ──────────────────────────────────────────────────────────────

# ── Spotter DLC — Daisy (one-time purchase) ────────────────────
STRIPE_DAISY_PRICE_ID = (os.getenv("STRIPE_DAISY_PRICE_ID") or "").strip()

STRIPE_DAISY_PRODUCT_IDS = [
    p.strip() for p in (os.getenv("STRIPE_DAISY_PRODUCT_IDS") or "").split(",") if p.strip()
]

STRIPE_DAISY_PAYMENT_LINK = (os.getenv("STRIPE_DAISY_PAYMENT_LINK") or "").strip()

DAISY_CHECKOUT_SUCCESS_URL = (
    os.getenv("DAISY_CHECKOUT_SUCCESS_URL")
    or "https://aichiefracing.com/daisy-thanks"
).strip()
DAISY_CHECKOUT_CANCEL_URL = (
    os.getenv("DAISY_CHECKOUT_CANCEL_URL")
    or "https://aichiefracing.com/spotters"
).strip()

# Where the Daisy WAV pack (.zip) lives. Set ONE:
#   DAISY_DLC_ZIP_URL  — a URL we 302-redirect the client to (your R2 URL)
#   DAISY_DLC_ZIP_PATH — a local file on the control server we stream.
DAISY_DLC_ZIP_URL = (os.getenv("DAISY_DLC_ZIP_URL") or "").strip()
DAISY_DLC_ZIP_PATH = (os.getenv("DAISY_DLC_ZIP_PATH") or "").strip()

# Manual entitlement overrides (comp testers / refunds). email -> {"spotter_daisy": true}
DLC_OVERRIDES_PATH = DATA_DIR / "dlc_overrides.json"
# ──────────────────────────────────────────────────────────────

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


# -------------------------
# Seat binding helpers
# -------------------------
def _seat_stale_cutoff_ts() -> float:
    return _now() - (SEAT_STALE_DAYS * 86400)


def _seat_claim(email: str, install_id: str, tier: str, machine: Optional[str] = None):
    """
    Enforce the per-email device limit with stale reclaim.

    Returns (allowed: bool, seats_used: int).

    Fails OPEN on any error or missing install_id — we never lock a paying
    customer out of the app because of a DB glitch or an old client that
    doesn't send an install_id yet.
    """
    email = (email or "").strip().lower()
    install_id = (install_id or "").strip()
    if not email or not install_id:
        return True, 0  # old client / missing id -> don't gate

    cutoff = _seat_stale_cutoff_ts()

    if _PRIME_DB_OK:
        try:
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    # 1) reclaim stale seats for this email (~7-day rule)
                    cur.execute(
                        "DELETE FROM license_seats WHERE email=%s AND last_seen < to_timestamp(%s)",
                        (email, cutoff),
                    )
                    # 2) already a seat for this machine? -> refresh it
                    cur.execute(
                        "SELECT 1 FROM license_seats WHERE email=%s AND install_id=%s",
                        (email, install_id),
                    )
                    exists = cur.fetchone() is not None
                    if exists:
                        cur.execute(
                            "UPDATE license_seats SET last_seen=NOW(), tier=%s, "
                            "machine=COALESCE(%s, machine) WHERE email=%s AND install_id=%s",
                            (tier, machine, email, install_id),
                        )
                        cur.execute("SELECT COUNT(*) FROM license_seats WHERE email=%s", (email,))
                        used = int(cur.fetchone()[0])
                        conn.commit()
                        return True, used
                    # 3) new machine -> only if under the limit
                    cur.execute("SELECT COUNT(*) FROM license_seats WHERE email=%s", (email,))
                    used = int(cur.fetchone()[0])
                    if used >= SEAT_LIMIT:
                        conn.commit()
                        return False, used
                    cur.execute(
                        "INSERT INTO license_seats (email, install_id, tier, machine, first_seen, last_seen) "
                        "VALUES (%s,%s,%s,%s,NOW(),NOW())",
                        (email, install_id, tier, machine),
                    )
                    conn.commit()
                    return True, used + 1
        except Exception as e:
            print(f"[seat] DB error, failing OPEN: {e}")
            return True, 0

    # ---- JSON fallback ----
    try:
        data = _load_json(SEATS_PATH, {})
        seats = data.get(email, {}) or {}
        # reclaim stale
        seats = {iid: s for iid, s in seats.items()
                 if float(s.get("last_seen", 0)) >= cutoff}
        if install_id in seats:
            seats[install_id].update({"last_seen": _now(), "tier": tier})
            if machine:
                seats[install_id]["machine"] = machine
            data[email] = seats
            _save_json(SEATS_PATH, data)
            return True, len(seats)
        if len(seats) >= SEAT_LIMIT:
            data[email] = seats
            _save_json(SEATS_PATH, data)
            return False, len(seats)
        seats[install_id] = {
            "tier": tier, "machine": machine,
            "first_seen": _now(), "last_seen": _now(),
        }
        data[email] = seats
        _save_json(SEATS_PATH, data)
        return True, len(seats)
    except Exception as e:
        print(f"[seat] JSON error, failing OPEN: {e}")
        return True, 0


def _seat_touch(install_id: str) -> None:
    """Keep an active machine's seat fresh (called from heartbeat/register)."""
    install_id = (install_id or "").strip()
    if not install_id:
        return
    if _PRIME_DB_OK:
        try:
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE license_seats SET last_seen=NOW() WHERE install_id=%s",
                        (install_id,),
                    )
                conn.commit()
            return
        except Exception as e:
            print(f"[seat] touch DB error: {e}")
    try:
        data = _load_json(SEATS_PATH, {})
        changed = False
        for _email, seats in data.items():
            if install_id in seats:
                seats[install_id]["last_seen"] = _now()
                changed = True
        if changed:
            _save_json(SEATS_PATH, data)
    except Exception as e:
        print(f"[seat] touch JSON error: {e}")


def _seats_all() -> List[Dict[str, Any]]:
    """Every seat row (for the admin panel), enriched with machine/user from installs."""
    installs = _load_json(INSTALLS_PATH, DEFAULT_INSTALLS)
    rows: List[Dict[str, Any]] = []
    if _PRIME_DB_OK:
        try:
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT email, install_id, tier, machine, "
                        "EXTRACT(EPOCH FROM first_seen), EXTRACT(EPOCH FROM last_seen) "
                        "FROM license_seats ORDER BY email, last_seen DESC"
                    )
                    for em, iid, tier, machine, fs, ls in cur.fetchall():
                        inst = installs.get(iid, {})
                        rows.append({
                            "email": em, "install_id": iid, "tier": tier,
                            "machine": machine or inst.get("machine"),
                            "user": inst.get("user"),
                            "first_seen": float(fs) if fs else None,
                            "last_seen": float(ls) if ls else None,
                        })
            return rows
        except Exception as e:
            print(f"[seat] list DB error, falling back to JSON: {e}")
    data = _load_json(SEATS_PATH, {})
    for em, seats in data.items():
        for iid, s in (seats or {}).items():
            inst = installs.get(iid, {})
            rows.append({
                "email": em, "install_id": iid, "tier": s.get("tier"),
                "machine": s.get("machine") or inst.get("machine"),
                "user": inst.get("user"),
                "first_seen": s.get("first_seen"),
                "last_seen": s.get("last_seen"),
            })
    rows.sort(key=lambda r: (r.get("email") or "", -(r.get("last_seen") or 0)))
    return rows


def _seat_free(email: str, install_id: Optional[str] = None) -> int:
    """Admin: free one seat (or all seats for an email if install_id omitted).
    Returns number of seats removed."""
    email = (email or "").strip().lower()
    install_id = (install_id or "").strip()
    if not email:
        return 0
    if _PRIME_DB_OK:
        try:
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    if install_id:
                        cur.execute(
                            "DELETE FROM license_seats WHERE email=%s AND install_id=%s",
                            (email, install_id),
                        )
                    else:
                        cur.execute("DELETE FROM license_seats WHERE email=%s", (email,))
                    removed = cur.rowcount or 0
                conn.commit()
            return int(removed)
        except Exception as e:
            print(f"[seat] free DB error: {e}")
    try:
        data = _load_json(SEATS_PATH, {})
        seats = data.get(email, {}) or {}
        if install_id:
            removed = 1 if seats.pop(install_id, None) is not None else 0
            if seats:
                data[email] = seats
            else:
                data.pop(email, None)
        else:
            removed = len(seats)
            data.pop(email, None)
        _save_json(SEATS_PATH, data)
        return removed
    except Exception as e:
        print(f"[seat] free JSON error: {e}")
        return 0


def _require_admin(
    x_aichief_key: Optional[str],
    authorization: Optional[str],
    control_api_key_hdr: Optional[str],
    x_api_key: Optional[str],
    control_api_key: Optional[str],
) -> None:
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
# Affiliate helpers
# -------------------------
def _fetch_coupon_name(coupon_id: str) -> Optional[str]:
    """Look up a Stripe coupon by ID and return its name."""
    try:
        r = requests.get(
            f"https://api.stripe.com/v1/coupons/{coupon_id}",
            auth=(STRIPE_SECRET_KEY, ""),
            timeout=6,
        )
        if r.ok:
            data = r.json()
            name = data.get("name") or data.get("id") or ""
            return name.strip().upper() or None
    except Exception:
        pass
    return None


def _extract_promo_code_from_invoice(invoice_id: str) -> Optional[str]:
    """Fetch a Stripe invoice and extract the affiliate code from discounts array."""
    try:
        inv_r = requests.get(
            f"https://api.stripe.com/v1/invoices/{invoice_id}",
            params=[("expand[]", "discounts")],
            auth=(STRIPE_SECRET_KEY, ""),
            timeout=6,
        )
        if not inv_r.ok:
            return None
        inv = inv_r.json()
        discounts = inv.get("discounts") or []
        for d in discounts:
            if not isinstance(d, dict):
                continue
            # Check promotion_code path first
            promo = d.get("promotion_code")
            if isinstance(promo, dict):
                code = promo.get("code") or ""
                if code:
                    return code.strip().upper()
            # Check source.coupon path (how Stripe returns it in newer API)
            source = d.get("source") or {}
            if source.get("type") == "coupon":
                coupon_id = source.get("coupon") or ""
                if coupon_id:
                    return _fetch_coupon_name(coupon_id)
            # Direct coupon object
            coupon = d.get("coupon") or {}
            if coupon:
                name = coupon.get("name") or coupon.get("id") or ""
                if name:
                    return name.strip().upper()
    except Exception:
        pass
    return None


def _extract_promo_code(sub: dict, customer: Optional[dict] = None) -> Optional[str]:
    """Extract affiliate code from a subscription, trying all known Stripe discount paths."""
    try:
        # Path 1: sub.discount.promotion_code (classic path)
        discount = sub.get("discount") or {}
        promo = discount.get("promotion_code")
        if isinstance(promo, dict):
            code = promo.get("code") or ""
            if code:
                return code.strip().upper()
        coupon = discount.get("coupon") or {}
        name = coupon.get("name") or ""
        if name:
            return name.strip().upper()

        # Path 2: customer.discount (Stripe sometimes puts it here)
        if customer and isinstance(customer, dict):
            cdiscount = customer.get("discount") or {}
            promo = cdiscount.get("promotion_code")
            if isinstance(promo, dict):
                code = promo.get("code") or ""
                if code:
                    return code.strip().upper()
            coupon = cdiscount.get("coupon") or {}
            name = coupon.get("name") or ""
            if name:
                return name.strip().upper()

        # Path 3: invoice.discounts[].source.coupon (newest Stripe API — what we actually see)
        latest_invoice = sub.get("latest_invoice")
        if latest_invoice and isinstance(latest_invoice, str):
            code = _extract_promo_code_from_invoice(latest_invoice)
            if code:
                return code

        return None
    except Exception:
        return None


def _record_affiliate(email: str, code: Optional[str], tier: str) -> None:
    """Log affiliate code usage to affiliates.json."""
    if not code:
        return
    try:
        data = _load_json(AFFILIATES_PATH, {})
        if code not in data:
            data[code] = {"code": code, "subs": []}
        subs = data[code]["subs"]
        existing = next((s for s in subs if s.get("email") == email), None)
        if existing:
            existing["tier"] = tier
            existing["last_seen"] = _now()
        else:
            subs.append({
                "email": email,
                "tier": tier,
                "first_seen": _now(),
                "last_seen": _now(),
            })
        data[code]["total"] = len(subs)
        _save_json(AFFILIATES_PATH, data)
        print(f"[affiliate] recorded code={code} email={email} tier={tier}")
    except Exception as e:
        print(f"[affiliate] record failed: {e}")
# -------------------------
# Affiliate Profile helpers
# -------------------------

TIER_MONTHLY_RATE = {
    "pro_monthly": 2.00,
    "pro_plus_monthly": 4.00,
}
TIER_YEARLY_RATE = {
    "pro_yearly": 20.00,
    "pro_plus_yearly": 40.00,
}

def _load_profiles() -> dict:
    """Load all affiliate profiles. Postgres primary, JSON file fallback."""
    if _PRIME_DB_OK:
        try:
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT code, data FROM affiliate_profiles;")
                    rows = cur.fetchall()
            if rows:
                return {row[0]: row[1] for row in rows}
            # No rows yet — try migrating from JSON file if it exists
            file_data = _load_json(AFFILIATE_PROFILES_PATH, {})
            if file_data:
                print("[affiliate] Migrating JSON file to Postgres...")
                _save_profiles(file_data)
            return file_data
        except Exception as e:
            print(f"[affiliate] DB load error, falling back to JSON: {e}")
    return _load_json(AFFILIATE_PROFILES_PATH, {})


def _save_profiles(data: dict) -> None:
    """Save all affiliate profiles. Postgres primary, JSON file fallback."""
    # Always write JSON as backup
    _save_json(AFFILIATE_PROFILES_PATH, data)
    if not _PRIME_DB_OK:
        return
    try:
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                for code, profile in data.items():
                    cur.execute("""
                        INSERT INTO affiliate_profiles (code, data, updated_at)
                        VALUES (%s, %s, NOW())
                        ON CONFLICT (code) DO UPDATE
                            SET data = EXCLUDED.data,
                                updated_at = NOW();
                    """, (code, psycopg2.extras.Json(profile)))
            conn.commit()
        print(f"[affiliate] saved {len(data)} profile(s) to Postgres")
    except Exception as e:
        print(f"[affiliate] DB save error (JSON backup written): {e}")

def _compute_balance(profile: dict) -> float:
    """Compute current balance from log entries."""
    total = 0.0
    for entry in profile.get("log", []):
        etype = entry.get("type", "")
        if etype in ("new_sub_yearly", "recurring", "payout"):
            total += entry.get("amount", 0.0)
    return round(total, 2)

def _append_log(profile: dict, entry: dict) -> None:
    if "log" not in profile:
        profile["log"] = []
    profile["log"].append(entry)

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
    kill_list: List[str] = []
    garage_status: str = ""
    garage_note: str = ""
    garage_subnote: str = ""
    pro_plus_available: bool = False   # Pro+ launch lever -> /client/config
    upgrade_url: str = ""              # config-driven upgrade page (no client rebuild)

class KillIn(BaseModel):
    version: str

class UnkillIn(BaseModel):
    version: str

class LicenseCheckIn(BaseModel):
    email: str
    install_id: Optional[str] = None
    machine: Optional[str] = None

class AffiliateProfileIn(BaseModel):
    code: str
    name: str
    email: str
    w9: bool = False
    notes: str = ""

class AffiliateSubIn(BaseModel):
    code: str
    sub_name: str
    tier: str          # pro_monthly | pro_yearly | pro_plus_monthly | pro_plus_yearly
    start_date: str    # YYYY-MM-DD
    status: str = "active"  # active | cancelled
    cancelled_date: Optional[str] = None
    sub_id: Optional[str] = None  # for updates

class AffiliatePayoutIn(BaseModel):
    code: str
    amount: float
    date: str          # YYYY-MM-DD
    note: str = ""

class AffiliateGenerateRecurringIn(BaseModel):
    month: str         # YYYY-MM  e.g. "2026-06"

# -------------------------
# Boot defaults
# -------------------------
DEFAULT_SETTINGS = {
    "beta_enabled": True,
    "latest_version": "0.0.0",
    "patch_url": None,
    "force_update": False,
    "kill_list": [],
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
@app.get("/settings")
def get_settings(x_aichief_key: Optional[str] = Header(None, alias="x-aichief-key")):
    current = _load_json(SETTINGS_PATH, DEFAULT_SETTINGS)
    if "kill_list" not in current:
        current["kill_list"] = []
    return current


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
    # Pro+ launch lever: dormant Pro+ code ships in the client; flip this from the
    # admin dashboard to reveal the "UPGRADE CHIEF" button to PRO users (free always
    # sees it, Pro+ never). upgrade_url lets the button re-point at the new Pro+
    # marketing page without a client rebuild.
    pro_plus_available = bool(settings.get("pro_plus_available", False))
    upgrade_url = str(settings.get("upgrade_url", ""))

    kill_list = settings.get("kill_list", [])
    safe_kill_list = [str(k).strip() for k in kill_list]

    should_lock = False
    reason = None

    if not beta_enabled:
        should_lock = True
        reason = "Beta is currently disabled."

    if str(body.version) in safe_kill_list:
        should_lock = True
        reason = f"This build ({body.version}) has been disabled.\nPlease update."

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
        "garage_status": garage_status,
        "garage_note": garage_note,
        "garage_subnote": garage_subnote,
        "pro_plus_available": pro_plus_available,
        "upgrade_url": upgrade_url,
    }


# -------------------------
# License Check
# -------------------------
@app.post("/license/check")
def license_check(body: LicenseCheckIn) -> Dict[str, Any]:
    email = (body.email or "").strip().lower()
    if not email:
        print(f"[lictrace] email=<EMPTY> raw={body.email!r} -> tier=free (no email)")
        return {"tier": "free", "is_dev": False}

    # Dev accounts always get pro_plus — bypass Stripe entirely
    if email in DEV_EMAILS:
        return _with_tts({"tier": "pro_plus", "email": email, "is_dev": True})
        
    # ── Tester override (checked before Stripe) ───────────────
    _overrides = _load_json(TESTER_OVERRIDES_PATH, {})
    if email in _overrides:
        _tier = _overrides[email]
        print(f"[license] {email} → {_tier} (tester override)")
        return _with_tts({"tier": _tier, "email": email, "is_dev": False})
    # ─────────────────────────────────────────────────────────
    if not STRIPE_SECRET_KEY:
        print("[license] WARN: STRIPE_SECRET_KEY not set — returning free")
        return {"tier": "free", "is_dev": email in DEV_EMAILS}

    try:
        r = requests.get(
            "https://api.stripe.com/v1/customers",
            params={"email": email, "limit": 5},
            auth=(STRIPE_SECRET_KEY, ""),
            timeout=8,
        )
        if not r.ok:
            print(f"[license] Stripe customer lookup failed: {r.status_code}")
            return {"tier": "free", "is_dev": email in DEV_EMAILS, "lookup_failed": True}

        customers = r.json().get("data", [])
        print(f"[lictrace] email={email!r} stripe_status={r.status_code} customer_count={len(customers)}")
        if not customers:
            print(f"[lictrace] email={email!r} -> tier=free (no customer in Stripe)")
            return {"tier": "free", "email": email, "is_dev": email in DEV_EMAILS}

        for customer in customers:
            cid = customer.get("id")
            if not cid:
                continue

            subs_r = requests.get(
                "https://api.stripe.com/v1/subscriptions",
                params={"customer": cid, "status": "active", "limit": 10, "expand[]": "data.discount.promotion_code"},
                auth=(STRIPE_SECRET_KEY, ""),
                timeout=8,
            )
            if not subs_r.ok:
                print(f"[license] Stripe subs lookup failed for {cid}: {subs_r.status_code}")
                return {"tier": "free", "email": email, "is_dev": email in DEV_EMAILS, "lookup_failed": True}

            subs = subs_r.json().get("data", [])
            for sub in subs:
                for item in sub.get("items", {}).get("data", []):
                    price_id = item.get("price", {}).get("id", "")
                    product_id = item.get("price", {}).get("product", "")

                    if price_id in STRIPE_PRO_PLUS_IDS or product_id in STRIPE_PRO_PLUS_IDS:
                        code = _extract_promo_code(sub, customer)
                        _record_affiliate(email, code, "pro_plus")
                        allowed, used = _seat_claim(email, body.install_id, "pro_plus", body.machine)
                        if not allowed:
                            print(f"[seat] {email} BLOCKED pro_plus seats={used}/{SEAT_LIMIT}")
                            return {"tier": "free", "email": email, "is_dev": email in DEV_EMAILS,
                                    "seat_limit_reached": True, "seats_used": used, "seats_max": SEAT_LIMIT}
                        print(f"[lictrace] email={email!r} -> tier=pro_plus code={code} price={price_id} prod={product_id} seats={used}/{SEAT_LIMIT}")
                        return _with_tts({"tier": "pro_plus", "email": email, "affiliate_code": code,
                                          "is_dev": email in DEV_EMAILS, "seats_used": used, "seats_max": SEAT_LIMIT})

                    if price_id in STRIPE_PRO_IDS or product_id in STRIPE_PRO_IDS:
                        code = _extract_promo_code(sub, customer)
                        _record_affiliate(email, code, "pro")
                        allowed, used = _seat_claim(email, body.install_id, "pro", body.machine)
                        if not allowed:
                            print(f"[seat] {email} BLOCKED pro seats={used}/{SEAT_LIMIT}")
                            return {"tier": "free", "email": email, "is_dev": email in DEV_EMAILS,
                                    "seat_limit_reached": True, "seats_used": used, "seats_max": SEAT_LIMIT}
                        print(f"[lictrace] email={email!r} -> tier=pro code={code} price={price_id} prod={product_id} seats={used}/{SEAT_LIMIT}")
                        return _with_tts({"tier": "pro", "email": email, "affiliate_code": code,
                                          "is_dev": email in DEV_EMAILS, "seats_used": used, "seats_max": SEAT_LIMIT})

        print(f"[lictrace] email={email!r} -> tier=free (customer found, NO matching active sub — check price/prod IDs)")
        return {"tier": "free", "email": email, "is_dev": email in DEV_EMAILS}

    except Exception as e:
        print(f"[lictrace] email={email!r} -> EXCEPTION {type(e).__name__}: {e}")
        return {"tier": "free", "is_dev": email in DEV_EMAILS, "lookup_failed": True}

# -------------------------
# Tester Override APIs
# -------------------------
class TesterOverrideIn(BaseModel):
    email: str
    tier: str  # "free" | "pro" | "pro_plus"

@app.get("/admin/testers")
def admin_testers(
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
) -> Dict[str, Any]:
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)
    return {"ok": True, "overrides": _load_json(TESTER_OVERRIDES_PATH, {})}

@app.post("/admin/tester/add")
def admin_tester_add(
    body: TesterOverrideIn,
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
) -> Dict[str, Any]:
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)
    email = body.email.strip().lower()
    tier = body.tier.strip().lower()
    if tier not in ("free", "pro", "pro_plus"):
        raise HTTPException(status_code=400, detail="tier must be free, pro, or pro_plus")
    overrides = _load_json(TESTER_OVERRIDES_PATH, {})
    overrides[email] = tier
    _save_json(TESTER_OVERRIDES_PATH, overrides)
    print(f"[tester] override set: {email} → {tier}")
    return {"ok": True, "email": email, "tier": tier}

@app.post("/admin/tester/remove")
def admin_tester_remove(
    body: TesterOverrideIn,
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
) -> Dict[str, Any]:
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)
    email = body.email.strip().lower()
    overrides = _load_json(TESTER_OVERRIDES_PATH, {})
    overrides.pop(email, None)
    _save_json(TESTER_OVERRIDES_PATH, overrides)
    print(f"[tester] override removed: {email}")
    return {"ok": True, "removed": email}

# ═══════════════════════════════════════════════════════════════
# Spotter DLC — Daisy (one-time purchase)
# ═══════════════════════════════════════════════════════════════
class DaisyCheckoutIn(BaseModel):
    email: str

class DlcGrantIn(BaseModel):
    email: str
    dlc: str = "spotter_daisy"


def _dlc_overrides() -> Dict[str, Any]:
    return _load_json(DLC_OVERRIDES_PATH, {})


def _email_has_dlc_override(email: str, dlc: str) -> bool:
    try:
        data = _dlc_overrides().get(email, {})
        return bool(data.get(dlc, False))
    except Exception:
        return False


def _stripe_email_owns_daisy(email: str) -> bool:
    """
    True if this email has a SUCCEEDED one-time payment for the Daisy pack.

    Strategy (best-effort, fail-open to False):
      1) PaymentIntent Search on metadata we stamp at checkout:
             metadata['dlc']='spotter_daisy' AND status='succeeded'
      2) Fallback: find customers by email, list their paid Checkout Sessions
         and match the Daisy price/product on the line items.
    """
    if not STRIPE_SECRET_KEY:
        return False

    # 1) PaymentIntent Search API (stamped metadata) --------------------
    try:
        q = (
            f'metadata["dlc"]:"spotter_daisy" AND '
            f'metadata["email"]:"{email}" AND status:"succeeded"'
        )
        r = requests.get(
            "https://api.stripe.com/v1/payment_intents/search",
            params={"query": q, "limit": 1},
            auth=(STRIPE_SECRET_KEY, ""),
            timeout=8,
        )
        if r.ok and (r.json().get("data") or []):
            print(f"[dlc] {email} owns Daisy (payment_intent search hit)")
            return True
    except Exception as e:
        print(f"[dlc] PI search error: {e}")

    # 2) Fallback: paid checkout sessions per customer ------------------
    if not (STRIPE_DAISY_PRICE_ID or STRIPE_DAISY_PRODUCT_IDS):
        return False
    try:
        cust_r = requests.get(
            "https://api.stripe.com/v1/customers",
            params={"email": email, "limit": 5},
            auth=(STRIPE_SECRET_KEY, ""),
            timeout=8,
        )
        if not cust_r.ok:
            return False
        for customer in cust_r.json().get("data", []):
            cid = customer.get("id")
            if not cid:
                continue
            sess_r = requests.get(
                "https://api.stripe.com/v1/checkout/sessions",
                params={"customer": cid, "limit": 25, "expand[]": "data.line_items"},
                auth=(STRIPE_SECRET_KEY, ""),
                timeout=8,
            )
            if not sess_r.ok:
                continue
            for sess in sess_r.json().get("data", []):
                if sess.get("payment_status") != "paid":
                    continue
                for li in (sess.get("line_items", {}) or {}).get("data", []):
                    price = li.get("price", {}) or {}
                    if STRIPE_DAISY_PRICE_ID and price.get("id") == STRIPE_DAISY_PRICE_ID:
                        return True
                    if price.get("product") in STRIPE_DAISY_PRODUCT_IDS:
                        return True
    except Exception as e:
        print(f"[dlc] session scan error: {e}")
    return False


def _email_owns_daisy(email: str) -> bool:
    email = (email or "").strip().lower()
    if not email:
        return False
    if email in DEV_EMAILS:
        return True
    if _email_has_dlc_override(email, "spotter_daisy"):
        return True
    return _stripe_email_owns_daisy(email)


@app.post("/license/dlc")
def license_dlc(body: LicenseCheckIn) -> Dict[str, Any]:
    """Return which DLC packs an email owns. Public (email-gated) like /license/check."""
    email = (body.email or "").strip().lower()
    owns_daisy = _email_owns_daisy(email)
    print(f"[dlc] {email!r} -> spotter_daisy={owns_daisy}")
    return {"ok": True, "email": email, "dlc": {"spotter_daisy": owns_daisy}}


@app.post("/checkout/spotter-daisy")
def checkout_spotter_daisy(body: DaisyCheckoutIn) -> Dict[str, Any]:
    """Create a one-time Stripe Checkout Session for the Daisy pack; return its URL."""
    email = (body.email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="email required")

    # Already owns it? Tell the client so it can just unlock.
    if _email_owns_daisy(email):
        return {"ok": True, "already_owned": True, "url": ""}

    # Preferred: dynamic Checkout Session (ties purchase to email + stamps metadata)
    if STRIPE_SECRET_KEY and STRIPE_DAISY_PRICE_ID:
        try:
            form = [
                ("mode", "payment"),
                ("customer_creation", "always"),
                ("customer_email", email),
                ("line_items[0][price]", STRIPE_DAISY_PRICE_ID),
                ("line_items[0][quantity]", "1"),
                ("success_url", DAISY_CHECKOUT_SUCCESS_URL),
                ("cancel_url", DAISY_CHECKOUT_CANCEL_URL),
                ("metadata[dlc]", "spotter_daisy"),
                ("metadata[email]", email),
                ("payment_intent_data[metadata][dlc]", "spotter_daisy"),
                ("payment_intent_data[metadata][email]", email),
            ]
            r = requests.post(
                "https://api.stripe.com/v1/checkout/sessions",
                data=form,
                auth=(STRIPE_SECRET_KEY, ""),
                timeout=12,
            )
            if r.ok:
                url = r.json().get("url") or ""
                if url:
                    return {"ok": True, "url": url}
            print(f"[dlc] checkout session create failed: {r.status_code} {r.text[:300]}")
        except Exception as e:
            print(f"[dlc] checkout session exception: {e}")

    # Fallback: pre-made Payment Link with email prefilled
    if STRIPE_DAISY_PAYMENT_LINK:
        sep = "&" if "?" in STRIPE_DAISY_PAYMENT_LINK else "?"
        return {"ok": True, "url": f"{STRIPE_DAISY_PAYMENT_LINK}{sep}prefilled_email={email}"}

    raise HTTPException(status_code=500, detail="Daisy checkout not configured (set STRIPE_DAISY_PRICE_ID or STRIPE_DAISY_PAYMENT_LINK)")


@app.get("/dlc/spotter-daisy")
def dlc_download_daisy(email: str):
    """Serve the Daisy WAV pack (.zip) ONLY to an email that owns it."""
    email = (email or "").strip().lower()
    if not _email_owns_daisy(email):
        raise HTTPException(status_code=403, detail="Daisy not owned by this email")

    if DAISY_DLC_ZIP_URL:
        return RedirectResponse(url=DAISY_DLC_ZIP_URL, status_code=302)

    if DAISY_DLC_ZIP_PATH and Path(DAISY_DLC_ZIP_PATH).exists():
        return FileResponse(
            DAISY_DLC_ZIP_PATH,
            media_type="application/zip",
            filename="daisy_voice.zip",
        )

    raise HTTPException(status_code=404, detail="Daisy pack not hosted (set DAISY_DLC_ZIP_URL or DAISY_DLC_ZIP_PATH)")


@app.get("/admin/dlc")
def admin_dlc_list(
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
) -> Dict[str, Any]:
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)
    return {"ok": True, "overrides": _dlc_overrides()}


@app.post("/admin/dlc/grant")
def admin_dlc_grant(
    body: DlcGrantIn,
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
) -> Dict[str, Any]:
    """Comp a DLC to an email (testers/refund fixes) without a Stripe purchase."""
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)
    email = body.email.strip().lower()
    dlc = body.dlc.strip().lower()
    data = _dlc_overrides()
    data.setdefault(email, {})[dlc] = True
    _save_json(DLC_OVERRIDES_PATH, data)
    print(f"[dlc] granted {dlc} to {email}")
    return {"ok": True, "email": email, "dlc": dlc}


@app.post("/admin/dlc/revoke")
def admin_dlc_revoke(
    body: DlcGrantIn,
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
) -> Dict[str, Any]:
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)
    email = body.email.strip().lower()
    dlc = body.dlc.strip().lower()
    data = _dlc_overrides()
    if email in data:
        data[email].pop(dlc, None)
        if not data[email]:
            data.pop(email, None)
    _save_json(DLC_OVERRIDES_PATH, data)
    print(f"[dlc] revoked {dlc} from {email}")
    return {"ok": True, "email": email, "dlc": dlc, "revoked": True}

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
    _seat_touch(body.install_id)
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
    _seat_touch(body.install_id)
    return {"ok": True}


# -------------------------
# Admin APIs
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


class SeatFreeIn(BaseModel):
    email: str
    install_id: Optional[str] = None  # omit to free ALL seats for the email


@app.get("/admin/seats")
def admin_seats(
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
) -> Dict[str, Any]:
    """List every bound seat (email -> machines), for support/audit."""
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)
    seats = _seats_all()
    # group by email for a friendlier admin view
    by_email: Dict[str, Any] = {}
    for s in seats:
        by_email.setdefault(s["email"], []).append(s)
    return {
        "ok": True,
        "seat_limit": SEAT_LIMIT,
        "stale_days": SEAT_STALE_DAYS,
        "count": len(seats),
        "seats": seats,
        "by_email": by_email,
    }


@app.post("/admin/seats/free")
def admin_seats_free(
    body: SeatFreeIn,
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
) -> Dict[str, Any]:
    """Free a seat so a customer stuck at the limit (e.g. dead PC) can activate a new machine."""
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)
    email = (body.email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="email required")
    removed = _seat_free(email, body.install_id)
    print(f"[seat] admin freed {removed} seat(s) for {email} install_id={body.install_id or 'ALL'}")
    return {"ok": True, "email": email, "install_id": body.install_id, "freed": removed}


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




class AffiliateRecordIn(BaseModel):
    email: str
    code: str
    tier: str = "pro"


@app.post("/admin/affiliates/record")
def admin_affiliate_record(
    body: AffiliateRecordIn,
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
) -> Dict[str, Any]:
    """Manually record an affiliate — used by dashboard Sync from Stripe button."""
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)
    code = (body.code or "").strip().upper()
    email = (body.email or "").strip().lower()
    tier = (body.tier or "pro").strip().lower()
    if not code or not email:
        raise HTTPException(status_code=400, detail="email and code required")
    _record_affiliate(email, code, tier)
    return {"ok": True, "email": email, "code": code, "tier": tier}

@app.get("/admin/affiliates")
def admin_affiliates(
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
) -> Dict[str, Any]:
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)
    data = _load_json(AFFILIATES_PATH, {})
    summary = []
    for code, info in data.items():
        subs = info.get("subs", [])
        pro_count = sum(1 for s in subs if s.get("tier") == "pro")
        pro_plus_count = sum(1 for s in subs if s.get("tier") == "pro_plus")
        summary.append({
            "code": code,
            "total": info.get("total", 0),
            "pro": pro_count,
            "pro_plus": pro_plus_count,
            "subs": subs,
        })
    summary.sort(key=lambda x: x["total"], reverse=True)
    return {"ok": True, "affiliates": summary}


@app.post("/tts/stream")
def tts_stream(
    body: TtsIn,
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
):
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


# ─────────────────────────────────────────────
# Chief Prime — Session ingestion
# ─────────────────────────────────────────────

class PrimeSessionIn(BaseModel):
    v: str
    uuid: str
    session: Dict[str, Any]
    finish: Dict[str, Any]
    events: List[Dict[str, Any]]

# ═══════════════════════════════════════════════════════════════
# Affiliate Profile Endpoints
# ═══════════════════════════════════════════════════════════════

@app.get("/admin/affiliate/profiles")
def admin_affiliate_profiles_get(
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
) -> Dict[str, Any]:
    """Return all affiliate profiles with computed balances."""
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)
    profiles = _load_profiles()
    result = []
    for code, p in profiles.items():
        result.append({
            **p,
            "balance": _compute_balance(p),
        })
    return {"ok": True, "profiles": result}


@app.post("/admin/affiliate/profiles")
def admin_affiliate_profiles_upsert(
    body: AffiliateProfileIn,
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
) -> Dict[str, Any]:
    """Add or update an affiliate profile. Code is the key."""
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)
    code = body.code.strip().upper()
    if not code:
        raise HTTPException(status_code=400, detail="code required")
    profiles = _load_profiles()
    existing = profiles.get(code, {})
    existing.update({
        "code": code,
        "name": body.name.strip(),
        "email": body.email.strip().lower(),
        "w9": body.w9,
        "notes": body.notes.strip(),
        "subs": existing.get("subs", []),
        "log": existing.get("log", []),
    })
    profiles[code] = existing
    _save_profiles(profiles)
    return {"ok": True, "code": code}


@app.post("/admin/affiliate/subs")
def admin_affiliate_sub_add(
    body: AffiliateSubIn,
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
) -> Dict[str, Any]:
    """Add a new subscriber to an affiliate or update status of existing."""
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)
    code = body.code.strip().upper()
    profiles = _load_profiles()
    if code not in profiles:
        raise HTTPException(status_code=404, detail=f"Affiliate {code} not found")

    profile = profiles[code]
    subs = profile.get("subs", [])
    tier = body.tier.strip().lower()
    status = body.status.strip().lower()

    if body.sub_id:
        # Update existing sub status
        for s in subs:
            if s.get("sub_id") == body.sub_id:
                old_status = s.get("status")
                s["status"] = status
                if status == "cancelled" and not s.get("cancelled_date"):
                    s["cancelled_date"] = body.cancelled_date or body.start_date
                if old_status != status:
                    _append_log(profile, {
                        "type": "status_change",
                        "sub_name": s.get("sub_name", ""),
                        "tier": tier,
                        "old_status": old_status,
                        "new_status": status,
                        "date": body.cancelled_date or body.start_date,
                        "amount": 0.0,
                    })
                break
    else:
        # New sub
        import uuid as _uuid
        sub_id = str(_uuid.uuid4())[:8]
        new_sub = {
            "sub_id": sub_id,
            "sub_name": body.sub_name.strip(),
            "tier": tier,
            "start_date": body.start_date,
            "status": status,
            "cancelled_date": body.cancelled_date,
        }
        subs.append(new_sub)
        # Backfill monthly recurring for backdated subs
        if tier in TIER_MONTHLY_RATE and status == "active":
            from datetime import date as _date
            import calendar as _cal
            rate = TIER_MONTHLY_RATE[tier]
            try:
                start_year, start_month, _ = [int(x) for x in body.start_date.split("-")]
                today = _date.today()
                y, m = start_year, start_month
                while (y, m) <= (today.year, today.month):
                    month_str = f"{y:04d}-{m:02d}"
                    # Don't double-fire for this specific sub+month combo
                    sub_label = body.sub_name.strip()
                    already = any(
                        e.get("type") == "recurring"
                        and e.get("month") == month_str
                        and sub_label in (e.get("breakdown") or [""])[0]
                        for e in profile.get("log", [])
                    )
                    if not already:
                        _append_log(profile, {
                            "type": "recurring",
                            "month": month_str,
                            "amount": rate,
                            "breakdown": [f"{sub_label} ({tier}) +${rate:.2f}"],
                            "date": f"{month_str}-01",
                        })
                    m += 1
                    if m > 12:
                        m = 1
                        y += 1
            except Exception as e:
                print(f"[affiliate] backfill failed: {e}")
        # Log the new sub + any upfront yearly payout
        log_entry: Dict[str, Any] = {
            "type": "new_sub",
            "sub_name": body.sub_name.strip(),
            "tier": tier,
            "date": body.start_date,
            "amount": 0.0,
        }
        if tier in TIER_YEARLY_RATE:
            upfront = TIER_YEARLY_RATE[tier]
            log_entry["type"] = "new_sub_yearly"
            log_entry["amount"] = upfront
            log_entry["note"] = f"Yearly upfront — ${upfront:.2f}"
        _append_log(profile, log_entry)

    profile["subs"] = subs
    profiles[code] = profile
    _save_profiles(profiles)
    return {"ok": True, "code": code}


@app.post("/admin/affiliate/payouts")
def admin_affiliate_payout(
    body: AffiliatePayoutIn,
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
) -> Dict[str, Any]:
    """Log a payout — subtracts from balance."""
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)
    code = body.code.strip().upper()
    profiles = _load_profiles()
    if code not in profiles:
        raise HTTPException(status_code=404, detail=f"Affiliate {code} not found")

    profile = profiles[code]
    current_balance = _compute_balance(profile)
    if body.amount > current_balance:
        raise HTTPException(status_code=400, detail=f"Payout ${body.amount:.2f} exceeds balance ${current_balance:.2f}")

    _append_log(profile, {
        "type": "payout",
        "amount": -abs(body.amount),
        "date": body.date,
        "note": body.note.strip(),
    })
    profiles[code] = profile
    _save_profiles(profiles)
    return {"ok": True, "code": code, "paid": body.amount, "new_balance": round(current_balance - body.amount, 2)}


@app.post("/admin/affiliate/generate-recurring")
def admin_affiliate_generate_recurring(
    body: AffiliateGenerateRecurringIn,
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
) -> Dict[str, Any]:
    """Generate monthly recurring commissions for all affiliates. Month format: YYYY-MM.
    Safe to call multiple times — will not double-fire for the same month."""
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)
    month = body.month.strip()  # e.g. "2026-06"
    if not month or len(month) != 7:
        raise HTTPException(status_code=400, detail="month must be YYYY-MM")

    profiles = _load_profiles()
    results = []

    for code, profile in profiles.items():
        # Check if already generated for this month
        already_run = any(
            e.get("type") == "recurring" and e.get("month") == month
            for e in profile.get("log", [])
        )
        if already_run:
            results.append({"code": code, "skipped": True, "reason": "already generated"})
            continue

        total_earned = 0.0
        breakdown = []
        for sub in profile.get("subs", []):
            if sub.get("status") != "active":
                continue
            tier = sub.get("tier", "")
            if tier not in TIER_MONTHLY_RATE:
                continue
            # Only count subs that started on or before this month
            start = sub.get("start_date", "")
            if start[:7] > month:
                continue
            rate = TIER_MONTHLY_RATE[tier]
            total_earned += rate
            breakdown.append(f"{sub.get('sub_name', '?')} ({tier}) +${rate:.2f}")

        if total_earned > 0:
            _append_log(profile, {
                "type": "recurring",
                "month": month,
                "amount": total_earned,
                "breakdown": breakdown,
                "date": f"{month}-01",
            })
            results.append({"code": code, "earned": total_earned, "breakdown": breakdown})
        else:
            results.append({"code": code, "earned": 0.0, "note": "no active monthly subs"})

    _save_profiles(profiles)
    return {"ok": True, "month": month, "results": results}


@app.get("/affiliate/dashboard")
def affiliate_dashboard(email: str) -> Dict[str, Any]:
    """Public endpoint — no auth key. Returns affiliate data for the given email.
    Returns 404 if email is not a registered affiliate."""
    email = (email or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="email required")
    profiles = _load_profiles()
    for code, profile in profiles.items():
        if profile.get("email", "").lower() == email:
            return {
                "ok": True,
                "name": profile.get("name", ""),
                "code": code,
                "w9": profile.get("w9", False),
                "balance": _compute_balance(profile),
                "subs": profile.get("subs", []),
                "log": sorted(profile.get("log", []), key=lambda x: x.get("date", ""), reverse=True),
                "sub_count": sum(1 for s in profile.get("subs", []) if s.get("status") == "active"),
            }
    raise HTTPException(status_code=404, detail="Not an affiliate")
@app.post("/prime/session")

def prime_session(body: PrimeSessionIn) -> Dict[str, Any]:
    """
    Receive a session batch from prime_logger.
    Stored in Postgres (primary) with JSONL fallback.
    """
    try:
        if not body.uuid or len(body.uuid) < 8:
            raise HTTPException(status_code=400, detail="invalid uuid")

        record = {
            "ts": _now(),
            "v": body.v,
            "uuid": body.uuid,
            "session": body.session,
            "finish": body.finish,
            "event_count": len(body.events),
            "events": body.events,
        }

        stored = False

        # Primary: Postgres
        if _PRIME_DB_OK:
            try:
                with _pg_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO prime_sessions (uuid, payload) VALUES (%s, %s)",
                            (body.uuid, psycopg2.extras.Json(record))
                        )
                    conn.commit()
                stored = True
                print(f"[prime] stored uuid={body.uuid[:8]} events={len(body.events)}")
            except Exception as e:
                print(f"[prime] DB write error: {e}")

        # Fallback: JSONL
        if not stored:
            PRIME_PATH.parent.mkdir(parents=True, exist_ok=True)
            with PRIME_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, separators=(",", ":")) + "\n")
            print(f"[prime] JSONL fallback uuid={body.uuid[:8]} events={len(body.events)}")

        return {"ok": True, "events": len(body.events)}

    except HTTPException:
        raise
    except Exception as e:
        return {"ok": False, "detail": str(e)}


# -------------------------
# Prime session RECORDINGS (full ns-stream replay captures)
# -------------------------
# The client's session_recorder POSTs a gzip JSON-Lines ns stream here at race end
# (consent-gated, anonymous UUID). Body = raw gzip bytes; metadata rides in headers
# (X-Chief-UUID, X-Chief-Meta). The gzip blob is written to disk (recordings are far
# larger than the /prime/session event batches — never into JSONB); an index row
# (uuid + meta + path + bytes) goes to Postgres, with a JSONL-index fallback.
@app.post("/prime/recording")
async def prime_recording(
    request: Request,
    x_chief_uuid: Optional[str] = Header(default=None, alias="x-chief-uuid"),
    x_chief_meta: Optional[str] = Header(default=None, alias="x-chief-meta"),
) -> Dict[str, Any]:
    try:
        uuid = (x_chief_uuid or "").strip()
        if not uuid or len(uuid) < 8:
            raise HTTPException(status_code=400, detail="invalid uuid")

        blob = await request.body()
        if not blob:
            raise HTTPException(status_code=400, detail="empty body")
        if len(blob) > MAX_RECORDING_BYTES:
            raise HTTPException(status_code=413, detail="recording too large (%d bytes)" % len(blob))

        meta: Dict[str, Any] = {}
        if x_chief_meta:
            try:
                parsed = json.loads(x_chief_meta)
                meta = parsed if isinstance(parsed, dict) else {"raw": str(parsed)}
            except Exception:
                meta = {"meta_parse_error": True}

        ticks = int(meta.get("ticks") or 0)
        ts = _now()
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        # Filename is server-generated from sanitized parts — no user-controlled path.
        safe_uuid = "".join(ch for ch in uuid[:12] if ch.isalnum()) or "anon"
        fname = f"{safe_uuid}_{int(ts)}_{ticks}.jsonl.gz"
        fpath = RECORDINGS_DIR / fname
        fpath.write_bytes(blob)

        record = {
            "ts": ts, "uuid": uuid, "path": str(fpath), "file": fname,
            "bytes": len(blob), "meta": meta,
        }

        stored = False
        if _PRIME_DB_OK:
            try:
                with _pg_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO prime_recordings "
                            "(uuid, meta, path, bytes, file, track, car, session_type, ticks, status) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')",
                            (uuid, psycopg2.extras.Json(meta), str(fpath), len(blob), fname,
                             str(meta.get("track") or ""), str(meta.get("car") or ""),
                             str(meta.get("session_type") or ""), ticks),
                        )
                    conn.commit()
                stored = True
                print(f"[recording] stored uuid={uuid[:8]} ticks={ticks} "
                      f"bytes={len(blob)} file={fname}")
            except Exception as e:
                print(f"[recording] DB write error: {e}")

        if not stored:
            RECORDINGS_INDEX.parent.mkdir(parents=True, exist_ok=True)
            with RECORDINGS_INDEX.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, separators=(",", ":")) + "\n")
            print(f"[recording] JSONL-index fallback uuid={uuid[:8]} file={fname}")

        return {"ok": True, "bytes": len(blob), "ticks": ticks, "file": fname}

    except HTTPException:
        raise
    except Exception as e:
        return {"ok": False, "detail": str(e)}


@app.get("/admin/recordings")
def admin_recordings(
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
) -> Dict[str, Any]:
    """List captured recordings (newest first) so you can pick which to pull."""
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)
    rows: List[Dict[str, Any]] = []
    if _PRIME_DB_OK:
        try:
            with _pg_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT id, uuid, received_at, meta, path, bytes "
                        "FROM prime_recordings ORDER BY received_at DESC LIMIT 500"
                    )
                    for r in cur.fetchall():
                        d = dict(r)
                        d["file"] = Path(d.get("path") or "").name
                        d["received_at"] = str(d.get("received_at"))
                        rows.append(d)
        except Exception as e:
            print(f"[recording] list DB error: {e}")
    # Surface any files on disk not represented above (e.g. JSONL-fallback ingests).
    try:
        if RECORDINGS_DIR.exists():
            known = {r.get("file") for r in rows}
            for p in sorted(RECORDINGS_DIR.glob("*.jsonl.gz"), reverse=True):
                if p.name not in known:
                    rows.append({"file": p.name, "path": str(p),
                                 "bytes": p.stat().st_size,
                                 "uuid": p.name.split("_")[0],
                                 "meta": None, "on_disk_only": True})
    except Exception:
        pass
    return {"ok": True, "count": len(rows), "recordings": rows}


@app.get("/admin/recording/{fname}")
def admin_recording_download(
    fname: str,
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
):
    """Download one recording's gzip by filename (from /admin/recordings)."""
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)
    # Path-traversal guard: only a bare .gz filename inside RECORDINGS_DIR.
    safe = Path(fname).name
    fpath = RECORDINGS_DIR / safe
    if safe != fname or fpath.suffix != ".gz" or not fpath.exists():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(str(fpath), media_type="application/gzip", filename=safe)


# ==================================================================
# QA — lugnut worker findings (replay-harness results per recording)
# ==================================================================
class QaFindingIn(BaseModel):
    recording_file: str = ""
    uuid: str = ""
    build: str = ""
    track: str = ""
    car: str = ""
    worker: str = ""
    ticks: int = 0
    status: str = "clean"          # "clean" | "found" | "error"
    summary: str = ""
    report: Dict[str, Any] = {}
    error: Optional[str] = None


try:
    QaFindingIn.model_rebuild()   # ensure the schema is fully built (Pydantic v2)
except Exception:
    pass


@app.post("/qa/findings")
def qa_findings_post(
    body: QaFindingIn,
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
) -> Dict[str, Any]:
    """A lugnut worker submits its replay-harness result for one recording."""
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)
    record = {
        "ts": _now(), "recording_file": body.recording_file, "uuid": body.uuid,
        "build": body.build, "track": body.track, "car": body.car,
        "worker": body.worker, "ticks": body.ticks, "status": body.status,
        "summary": body.summary, "report": body.report, "error": body.error,
    }
    stored = False
    if _PRIME_DB_OK:
        try:
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO prime_qa_findings "
                        "(recording_file, uuid, build, track, car, worker, ticks, status, summary, report) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (body.recording_file, body.uuid, body.build, body.track, body.car,
                         body.worker, int(body.ticks or 0), body.status, body.summary,
                         psycopg2.extras.Json(body.report or {})),
                    )
                conn.commit()
            stored = True
            print(f"[qa] finding {body.status} worker={body.worker} file={body.recording_file}")
            # Mark the recording processed and decide -- once, here -- whether it joins
            # the permanent corpus. Never let a keep-rule failure fail the finding POST:
            # the finding ledger is the valuable artefact and must always land.
            try:
                _apply_keep_rules(body.recording_file, body.status, body.ticks,
                                  body.track, body.car)
            except Exception as _e:
                print(f"[retention] keep-rule hook failed: {_e}")
        except Exception as e:
            print(f"[qa] DB write error: {e}")
    if not stored:
        QA_FINDINGS_INDEX.parent.mkdir(parents=True, exist_ok=True)
        with QA_FINDINGS_INDEX.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
        print(f"[qa] JSONL fallback worker={body.worker} file={body.recording_file}")
    return {"ok": True, "status": body.status}


@app.get("/admin/qa/findings.json")
def qa_findings_feed(
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
) -> Dict[str, Any]:
    """Findings feed the dashboard fetches (newest first)."""
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)
    rows: List[Dict[str, Any]] = []
    if _PRIME_DB_OK:
        try:
            with _pg_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT id, recording_file, uuid, build, track, car, worker, ticks, "
                        "status, summary, report, verdict, created_at "
                        "FROM prime_qa_findings ORDER BY created_at DESC LIMIT 200"
                    )
                    for r in cur.fetchall():
                        d = dict(r)
                        d["created_at"] = str(d.get("created_at"))
                        rows.append(d)
        except Exception as e:
            print(f"[qa] feed DB error: {e}")
    elif QA_FINDINGS_INDEX.exists():
        try:
            for line in QA_FINDINGS_INDEX.read_text(encoding="utf-8").splitlines()[-200:]:
                if line.strip():
                    rows.append(json.loads(line))
            rows.reverse()
        except Exception:
            pass
    return {"ok": True, "count": len(rows), "findings": rows}


# Lugnut leaderboard scoring — clean run +1, real bug found +3, false positive -1.
QA_POINTS_CLEAN = int(os.getenv("QA_POINTS_CLEAN", "1"))
QA_POINTS_FOUND = int(os.getenv("QA_POINTS_FOUND", "3"))
QA_POINTS_FALSE = int(os.getenv("QA_POINTS_FALSE", "-1"))


@app.get("/admin/qa/leaderboard.json")
def qa_leaderboard(
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
) -> Dict[str, Any]:
    """All-time lugnut standings: points per worker. Clean run = 1, bug found = 3.
    Aggregated over the WHOLE findings table (not the 200-row feed) so the board is
    a real running total."""
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)
    agg: Dict[str, Dict[str, Any]] = {}

    def _bump(worker: str, status: str, verdict: str, n: int, last_seen: str) -> None:
        w = (worker or "").strip() or "lugnut"
        st = (status or "").strip().lower()
        vd = (verdict or "").strip().lower()
        e = agg.setdefault(w, {"worker": w, "clean": 0, "found": 0, "false_pos": 0,
                               "errors": 0, "runs": 0, "last_seen": ""})
        if st == "clean":
            e["clean"] += n
        elif st == "found":
            if vd == "false_positive":
                e["false_pos"] += n
            else:
                e["found"] += n
        else:
            e["errors"] += n
        e["runs"] += n
        if last_seen and last_seen > (e["last_seen"] or ""):
            e["last_seen"] = last_seen

    if _PRIME_DB_OK:
        try:
            with _pg_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT COALESCE(NULLIF(TRIM(worker), ''), 'lugnut') AS worker, "
                        "LOWER(status) AS st, LOWER(COALESCE(verdict, '')) AS vd, "
                        "COUNT(*) AS n, MAX(created_at) AS last_seen "
                        "FROM prime_qa_findings GROUP BY 1, 2, 3"
                    )
                    for r in cur.fetchall():
                        _bump(r["worker"], r["st"], r["vd"], int(r["n"] or 0), str(r.get("last_seen") or ""))
        except Exception as e:
            print(f"[qa] leaderboard DB error: {e}")
    elif QA_FINDINGS_INDEX.exists():
        try:
            for line in QA_FINDINGS_INDEX.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                d = json.loads(line)
                _bump(d.get("worker", ""), d.get("status", ""), d.get("verdict", ""),
                      1, str(d.get("created_at") or ""))
        except Exception:
            pass

    board = []
    for e in agg.values():
        e["points"] = (e["clean"] * QA_POINTS_CLEAN
                       + e["found"] * QA_POINTS_FOUND
                       + e["false_pos"] * QA_POINTS_FALSE)
        board.append(e)
    board.sort(key=lambda x: (-x["points"], -x["found"], -x["runs"], x["worker"].lower()))
    return {"ok": True,
            "scoring": {"clean": QA_POINTS_CLEAN, "found": QA_POINTS_FOUND, "false_positive": QA_POINTS_FALSE},
            "board": board}


class QAVerdictIn(BaseModel):
    id: int
    verdict: str = ""   # "false_positive" | "real" | "" (clears back to untriaged)


@app.post("/admin/qa/verdict")
def qa_set_verdict(
    body: QAVerdictIn,
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
) -> Dict[str, Any]:
    """Triage a finding: mark a FOUND as a false positive (-1 on the board) or back to a
    real bug. Set by finding id from the dashboard."""
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)
    vd = (body.verdict or "").strip().lower()
    if vd not in ("false_positive", "real", ""):
        return {"ok": False, "error": "verdict must be false_positive, real, or empty"}
    val = vd or None
    if not _PRIME_DB_OK:
        return {"ok": False, "error": "no db"}
    try:
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE prime_qa_findings SET verdict = %s WHERE id = %s",
                            (val, int(body.id)))
            conn.commit()
        print(f"[qa] verdict id={body.id} -> {val}")
        return {"ok": True, "id": body.id, "verdict": val}
    except Exception as e:
        print(f"[qa] verdict error: {e}")
        return {"ok": False, "error": str(e)}


_QA_DASHBOARD_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"/>
<title>AI Chief — Lugnut QA</title>
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --panel2:#1c2330; --line:#2b3444;
    --txt:#e6edf3; --dim:#9aa7b4; --accent:#f2a63b;
    --found:#f2544d; --clean:#3fb950; --err:#e3b341;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
    font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    -webkit-text-size-adjust:100%}
  header{position:sticky;top:0;z-index:5;background:linear-gradient(180deg,#0d1117 70%,rgba(13,17,23,0));
    padding:14px 16px 10px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  h1{font-size:18px;margin:0;letter-spacing:.3px}
  h1 .wrench{color:var(--accent)}
  .sub{color:var(--dim);font-size:12px;margin-left:auto;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
  .btn{background:var(--panel2);border:1px solid var(--line);color:var(--txt);
    border-radius:8px;padding:7px 11px;font-size:13px;cursor:pointer}
  .btn:active{transform:translateY(1px)}
  .btn.small{padding:4px 9px;font-size:12px}
  .wrap{padding:4px 12px 60px;max-width:1000px;margin:0 auto}
  .empty{color:var(--dim);text-align:center;padding:50px 20px}
  .card{background:var(--panel);border:1px solid var(--line);border-left-width:4px;
    border-radius:10px;margin:10px 0;overflow:hidden}
  .card.found{border-left-color:var(--found)}
  .card.clean{border-left-color:var(--clean)}
  .card.error{border-left-color:var(--err)}
  .row{display:flex;align-items:center;gap:10px;padding:11px 13px;cursor:pointer;flex-wrap:wrap}
  .badge{font-size:11px;font-weight:700;letter-spacing:.4px;padding:3px 8px;border-radius:999px;white-space:nowrap}
  .badge.found{background:rgba(242,84,77,.15);color:var(--found)}
  .badge.clean{background:rgba(63,185,80,.15);color:var(--clean)}
  .badge.error{background:rgba(227,179,65,.15);color:var(--err)}
  .worker{font-weight:600}
  .meta{color:var(--dim);font-size:12.5px}
  .grow{flex:1 1 auto;min-width:120px}
  .sum{width:100%;color:var(--txt);font-size:13.5px;margin-top:-2px}
  .sum.found{color:#ffb3af}
  .body{display:none;padding:0 13px 13px;border-top:1px solid var(--line)}
  .card.open .body{display:block}
  .sec{margin:12px 0 4px;color:var(--accent);font-size:12px;text-transform:uppercase;letter-spacing:.5px}
  .item{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:9px 11px;margin:6px 0;font-size:13px}
  .item .k{color:var(--found);font-weight:600}
  .kv{color:var(--dim);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;white-space:pre-wrap;word-break:break-word;margin-top:4px}
  .actions{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
  .ok{color:var(--clean)}
  .toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:var(--accent);color:#111;
    font-weight:600;padding:9px 16px;border-radius:10px;opacity:0;transition:opacity .2s;pointer-events:none;z-index:20}
  .toast.show{opacity:1}
  .filters{display:flex;gap:8px;align-items:center}
  label.tog{color:var(--dim);font-size:12.5px;display:flex;gap:5px;align-items:center;cursor:pointer}
  .keybox{padding:40px 16px;max-width:420px;margin:0 auto;text-align:center}
  .keybox input{width:100%;padding:11px;border-radius:9px;border:1px solid var(--line);background:var(--panel);color:var(--txt);font-size:15px;margin:12px 0}
  .board{margin:6px 0 16px;background:linear-gradient(180deg,var(--panel),var(--panel2));border:1px solid var(--line);border-radius:12px;overflow:hidden;display:none}
  .board.show{display:block}
  .board h2{margin:0;padding:11px 14px;font-size:13px;letter-spacing:.5px;text-transform:uppercase;color:var(--accent);border-bottom:1px solid var(--line);display:flex;align-items:center;gap:8px;justify-content:space-between}
  .board h2 .rule{color:var(--dim);font-size:11px;font-weight:400;text-transform:none;letter-spacing:0}
  .lbrow{display:flex;align-items:center;gap:10px;padding:9px 14px;border-top:1px solid rgba(43,52,68,.5)}
  .lbrow .rank{width:28px;text-align:center;font-weight:800;color:var(--dim);font-size:15px}
  .lbrow.top1{background:linear-gradient(90deg,rgba(242,166,59,.13),transparent)}
  .lbrow.top2{background:linear-gradient(90deg,rgba(155,167,180,.11),transparent)}
  .lbrow.top3{background:linear-gradient(90deg,rgba(205,127,50,.12),transparent)}
  .lbrow .who{font-weight:700;flex:1 1 auto;min-width:80px}
  .lbrow .bd{color:var(--dim);font-size:12px;font-variant-numeric:tabular-nums;white-space:nowrap}
  .lbrow .bd b.f{color:var(--found)} .lbrow .bd b.c{color:var(--clean)} .lbrow .bd b.fp{color:var(--err)}
  .btn.danger{border-color:rgba(242,84,77,.5);color:var(--found)}
  .badge.fpbadge{background:rgba(227,179,65,.15);color:var(--err)}
  .card.fp{opacity:.62}
  .card.fp .sum{text-decoration:line-through;opacity:.8}
  .lbrow .pts{font-weight:850;font-size:18px;font-variant-numeric:tabular-nums;min-width:56px;text-align:right}
  .lbrow .pts small{font-size:10px;color:var(--dim);font-weight:600}
</style>
</head>
<body>
<header>
  <h1><span class="wrench">&#128295;</span> Lugnut QA</h1>
  <div class="sub">
    <div class="filters"><label class="tog"><input type="checkbox" id="onlyIssues"/> issues only</label></div>
    <span id="status">—</span>
    <button class="btn small" id="refresh">Refresh</button>
    <button class="btn small" id="copyall">Copy all issues</button>
  </div>
</header>
<div class="wrap" id="wrap">
  <div id="keybox" class="keybox" style="display:none">
    <div>Enter the control key to view findings.</div>
    <input type="password" id="keyin" placeholder="CONTROL_API_KEY" autocomplete="off"/>
    <button class="btn" id="keygo">View dashboard</button>
  </div>
  <div id="board" class="board"></div>
  <div id="list"></div>
</div>
<div class="toast" id="toast"></div>
<script>
const $=s=>document.querySelector(s);
let KEY=sessionStorage.getItem("qa_key")||"";
let DATA=[];
let TIMER=null;

function toast(m){const t=$("#toast");t.textContent=m;t.classList.add("show");setTimeout(()=>t.classList.remove("show"),1400);}
function esc(x){return (x==null?"":String(x)).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}
function j(o){try{return JSON.stringify(o);}catch(e){return String(o);}}

function claudeBlock(f){
  const r=f.report||{};
  const L=[];
  L.push("AI Chief QA — "+(f.worker||"lugnut")+" — "+String(f.status||"").toUpperCase());
  L.push("recording: "+(f.recording_file||"?"));
  L.push("build "+(f.build||"?")+" | "+(f.car||"?")+" @ "+(f.track||"?")+" | "+(f.ticks||0)+" ticks | "+(f.created_at||""));
  if(f.summary) L.push("summary: "+f.summary);
  if(f.error) L.push("ERROR: "+f.error);
  const V=r.violations||[];
  if(V.length){
    L.push("");L.push("INVARIANT VIOLATIONS ("+V.length+"):");
    V.forEach((v,i)=>{
      L.push("["+(i+1)+"] "+(v.invariant||"?")+" — "+(v.detail||""));
      L.push("    at tick "+(v.first_bad_i)+", lap "+(v.lap)+", session "+(v.session_state));
      if(v.ns_fuel) L.push("    ns_fuel: "+j(v.ns_fuel));
      if(v.state_fuel) L.push("    state_fuel: "+j(v.state_fuel));
      if(v.inc_says&&v.inc_says.length) L.push("    inc_says: "+j(v.inc_says));
    });
  }
  const M=r.service_mismatch||[];
  if(M.length){
    L.push("");L.push("SERVICE-VS-BOX MISMATCH ("+M.length+") — needs triage:");
    M.forEach(m=>L.push("  tick "+m.at_i+": Chief said '"+m.spoke+"' but box='"+m.box+"'"));
  }
  const D=r.service_drift||[];
  if(D.length){
    L.push("");L.push("SERVICE DRIFT IN CAUTION ("+D.length+") — informational:");
    D.forEach(d=>L.push("  caution@"+d.caution_start_i+": "+d.from+" -> "+d.to+" at tick "+d.at_i));
  }
  L.push("");
  L.push("free_tier_silent: "+(r.free_tier_silent===false?"LEAK ("+(r.free_tier_leaks||0)+" ticks)":"ok"));
  L.push("says: total "+(r.says_total||0)+", by_tag "+j(r.say_tags||{}));
  return L.join("\n");
}

function copyText(t){
  if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(t).then(()=>toast("Copied for Claude")).catch(()=>fallback(t));}
  else fallback(t);
}
function fallback(t){const ta=document.createElement("textarea");ta.value=t;document.body.appendChild(ta);ta.select();try{document.execCommand("copy");toast("Copied");}catch(e){toast("Copy failed");}ta.remove();}

function medal(i){return i===0?"\u{1F947}":i===1?"\u{1F948}":i===2?"\u{1F949}":("#"+(i+1));}
function renderBoard(board,scoring){
  const el=$("#board");
  if(!board||!board.length){el.classList.remove("show");el.innerHTML="";return;}
  const fpv=(scoring&&scoring.false_positive!=null)?scoring.false_positive:-1;
  const rule="clean +"+((scoring&&scoring.clean)||1)+" · bug +"+((scoring&&scoring.found)||3)+" · false pos "+fpv;
  el.innerHTML='<h2><span>\u{1F3C1} Lugnut Leaderboard</span><span class="rule">'+rule+'</span></h2>'+
    board.map((e,i)=>{
      const cls=i<3?("top"+(i+1)):"";
      const fp=(e.false_pos||0)>0?' · <b class="fp">'+e.false_pos+'</b> FP':'';
      return '<div class="lbrow '+cls+'">'+
        '<span class="rank">'+medal(i)+'</span>'+
        '<span class="who">'+esc(e.worker)+'</span>'+
        '<span class="bd"><b class="c">'+(e.clean||0)+'</b> clean · <b class="f">'+(e.found||0)+'</b> bug'+((e.found===1)?'':'s')+fp+' · '+(e.runs||0)+' runs</span>'+
        '<span class="pts">'+(e.points||0)+' <small>PTS</small></span>'+
      '</div>';
    }).join("");
  el.classList.add("show");
}

function loadBoard(){
  if(!KEY)return;
  fetch("/admin/qa/leaderboard.json",{headers:{"x-aichief-key":KEY}})
    .then(r=>r.ok?r.json():null)
    .then(d=>{if(d&&d.ok)renderBoard(d.board,d.scoring);})
    .catch(()=>{});
}

function render(){
  const only=$("#onlyIssues").checked;
  const list=$("#list");
  const rows=DATA.filter(f=>!only||f.status==="found"||f.status==="error");
  if(!rows.length){list.innerHTML='<div class="empty">'+(DATA.length?"No issues — all clean.":"No findings yet. Workers report here as recordings come in.")+'</div>';return;}
  list.innerHTML=rows.map((f,idx)=>{
    const st=f.status||"clean";
    const r=f.report||{};
    const nV=(r.violations||[]).length, nM=(r.service_mismatch||[]).length, nD=(r.service_drift||[]).length;
    let details="";
    if(nV){details+='<div class="sec">Invariant violations ('+nV+')</div>'+(r.violations||[]).map(v=>
      '<div class="item"><span class="k">'+esc(v.invariant)+'</span> — '+esc(v.detail||"")+
      '<div class="kv">tick '+esc(v.first_bad_i)+'  lap '+esc(v.lap)+'  session '+esc(v.session_state)+
      (v.state_fuel?'\nstate_fuel: '+esc(j(v.state_fuel)):'')+
      (v.ns_fuel?'\nns_fuel: '+esc(j(v.ns_fuel)):'')+'</div></div>').join("");}
    if(nM){details+='<div class="sec">Service vs box — triage ('+nM+')</div>'+(r.service_mismatch||[]).map(m=>
      '<div class="item">tick '+esc(m.at_i)+': Chief said <b>'+esc(m.spoke)+'</b> but box=<b>'+esc(m.box)+'</b></div>').join("");}
    if(nD){details+='<div class="sec">Service drift ('+nD+')</div>'+(r.service_drift||[]).map(d=>
      '<div class="item">caution@'+esc(d.caution_start_i)+': '+esc(d.from)+' &rarr; '+esc(d.to)+' at tick '+esc(d.at_i)+'</div>').join("");}
    if(f.error){details+='<div class="sec">Error</div><div class="item">'+esc(f.error)+'</div>';}
    if(!details){details='<div class="item ok">Clean — no invariant broke. says total '+esc(r.says_total||0)+', free-tier '+(r.free_tier_silent===false?'LEAK':'ok')+'.</div>';}
    const isFP=(f.verdict==="false_positive");
    let vbtn="";
    if(st==="found"){
      vbtn = isFP
        ? '<button class="btn small" onclick="markVerdict('+(f.id|0)+',\'real\')">&#10003; Real bug after all</button>'
        : '<button class="btn small danger" onclick="markVerdict('+(f.id|0)+',\'false_positive\')">&#10007; False positive (&minus;1)</button>';
    }
    return '<div class="card '+st+(isFP?' fp':'')+'" data-i="'+idx+'">'+
      '<div class="row" onclick="this.parentNode.classList.toggle(\'open\')">'+
        '<span class="badge '+st+'">'+esc(st.toUpperCase())+'</span>'+
        (isFP?'<span class="badge fpbadge">FALSE POS</span>':'')+
        '<span class="worker">'+esc(f.worker||"lugnut")+'</span>'+
        '<span class="meta grow">'+esc(f.car||"")+' @ '+esc(f.track||"")+' &middot; '+esc(f.ticks||0)+' ticks &middot; '+esc((f.created_at||"").replace("T"," ").slice(0,19))+'</span>'+
        '<span class="meta">'+esc(f.recording_file||"")+'</span>'+
        (f.summary?'<div class="sum '+st+'">'+esc(f.summary)+'</div>':'')+
      '</div>'+
      '<div class="body">'+details+
        '<div class="actions"><button class="btn small" onclick="copyOne('+idx+')">&#128203; Copy for Claude</button>'+vbtn+'</div>'+
      '</div></div>';
  }).join("");
}

window.copyOne=function(i){const f=DATA.filter(f=>!$("#onlyIssues").checked||f.status==="found"||f.status==="error")[i];if(f)copyText(claudeBlock(f));};

window.markVerdict=function(id,verdict){
  fetch("/admin/qa/verdict",{method:"POST",headers:{"content-type":"application/json","x-aichief-key":KEY},body:JSON.stringify({id:id,verdict:verdict})})
    .then(r=>r.json())
    .then(d=>{if(d&&d.ok){toast(verdict==="false_positive"?"Marked false positive (−1)":"Marked real bug");load();}else{toast((d&&d.error)||"Failed");}})
    .catch(()=>toast("Failed"));
};

function copyAll(){
  const issues=DATA.filter(f=>f.status==="found"||f.status==="error");
  if(!issues.length){toast("No issues to copy");return;}
  copyText(issues.map(claudeBlock).join("\n\n"+"=".repeat(56)+"\n\n"));
}

function load(){
  if(!KEY){$("#keybox").style.display="block";$("#status").textContent="locked";return;}
  $("#status").textContent="loading…";
  loadBoard();
  fetch("/admin/qa/findings.json",{headers:{"x-aichief-key":KEY}})
    .then(r=>{if(r.status===401){throw new Error("bad key");}return r.json();})
    .then(d=>{DATA=d.findings||[];$("#keybox").style.display="none";
      const iss=DATA.filter(f=>f.status==="found"||f.status==="error").length;
      $("#status").textContent=DATA.length+" runs · "+iss+" with issues";
      render();})
    .catch(e=>{$("#status").textContent=e.message;if(e.message==="bad key"){sessionStorage.removeItem("qa_key");KEY="";$("#keybox").style.display="block";}});
}

$("#keygo").onclick=()=>{KEY=$("#keyin").value.trim();if(KEY){sessionStorage.setItem("qa_key",KEY);load();}};
$("#keyin").addEventListener("keydown",e=>{if(e.key==="Enter")$("#keygo").click();});
$("#refresh").onclick=load;
$("#copyall").onclick=copyAll;
$("#onlyIssues").onchange=render;
load();
TIMER=setInterval(load,20000);
</script>
</body>
</html>
'''


@app.get("/admin/qa", response_class=HTMLResponse)
def qa_dashboard() -> Any:
    """Lugnut QA dashboard — phone + desktop. The page shell loads without auth; it
    prompts for the control key and uses it to fetch the findings feed."""
    return HTMLResponse(_QA_DASHBOARD_HTML)


# ══════════════════════════════════════════════════════════════════════════════
# QA RECORDING RETENTION  (Part A of claude/RECORDING_RETENTION_DESIGN.md,
# plus the golden-corpus keep-rules from claude/GOLDEN_CORPUS_RETENTION_SPEC.md)
#
# WHY THIS EXISTS: the volume is a TRANSIENT INBOX, not an archive. A recording's
# job is to be replayed once by a lugnut; after that the FINDING is the valuable
# artefact and the raw gzip is spent fuel. Without pruning, the 5 GB volume wedges
# and uploads start failing, which silently kills the whole QA pipeline.
#
# MEASURED SIZING (claude/LEAN_RECORDER_VALIDATION_2026-08-20.md): with the v2
# delta recorder a race is ~24 MB, NOT the ~5 MB the design assumed. That is
# ~210 recordings in 5 GB and only ~3-6 hours of peak-night buffer, so DAILY
# cleanup is not enough at the mid/heavy scenarios — run this hourly, and have
# the lugnut prune each clean recording the moment it finishes replaying it.
#
# SAFETY: CLEANUP_DRY_RUN defaults to TRUE. The first deploy cannot delete
# anything — it only logs what it would remove. Flip CLEANUP_DRY_RUN=0 once the
# delete list looks sane.
# ══════════════════════════════════════════════════════════════════════════════

import random as _rnd

# The Postgres block at the top of this file defines _PG_URL and _pg_conn INSIDE a
# `try: import psycopg2`. On a host without psycopg2 the except branch sets only
# _PRIME_DB_OK, so both names are undefined — and anything down here that touches
# them raises NameError AT IMPORT, i.e. the whole server fails to boot rather than
# degrading to the JSONL fallback. Railway has psycopg2 so it would not show there;
# it would show the first time someone ran this locally. Define the fallbacks.
try:
    _PG_URL
except NameError:
    _PG_URL = ""

    def _pg_conn():   # never reached: every call site is guarded on _PG_URL
        raise RuntimeError("psycopg2 unavailable")

RETAIN_CLEAN_DAYS       = int(os.getenv("RETAIN_CLEAN_DAYS", "3"))
COVERAGE_KEEP_PER_COMBO = int(os.getenv("COVERAGE_KEEP_PER_COMBO", "2"))
STORAGE_BUDGET_GB       = float(os.getenv("STORAGE_BUDGET_GB", "4"))
CLEANUP_DRY_RUN         = (os.getenv("CLEANUP_DRY_RUN", "1").strip() != "0")
GOLDEN_RANDOM_RATE      = float(os.getenv("GOLDEN_RANDOM_RATE", "0.02"))
GOLDEN_LONG_KEEP        = int(os.getenv("GOLDEN_LONG_KEEP", "10"))
GOLDEN_LONG_MIN_TICKS   = int(os.getenv("GOLDEN_LONG_MIN_TICKS", "20000"))

# Territory the harness has little or no coverage for. Recordings matching any of
# these are pinned unconditionally — they are the ones a future invariant will
# most need, and today we have ZERO real endurance/team recordings.
UNDERTESTED_TOKENS = tuple(
    t.strip().lower() for t in
    os.getenv("UNDERTESTED_TOKENS",
              "endurance,team,wet,rain,superspeedway,standing,timed").split(",")
    if t.strip()
)


def _migrate_recordings_retention() -> None:
    """Additive schema migration. Every column is nullable/defaulted so existing
    rows migrate cleanly and a re-run is a no-op."""
    if not _PG_URL:
        return
    try:
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    ALTER TABLE prime_recordings
                        ADD COLUMN IF NOT EXISTS file         TEXT,
                        ADD COLUMN IF NOT EXISTS track        TEXT,
                        ADD COLUMN IF NOT EXISTS car          TEXT,
                        ADD COLUMN IF NOT EXISTS session_type TEXT,
                        ADD COLUMN IF NOT EXISTS ticks        INTEGER DEFAULT 0,
                        ADD COLUMN IF NOT EXISTS status       TEXT DEFAULT 'pending',
                        ADD COLUMN IF NOT EXISTS finding      TEXT,
                        ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ,
                        ADD COLUMN IF NOT EXISTS pinned       BOOLEAN DEFAULT FALSE,
                        ADD COLUMN IF NOT EXISTS pin_reason   TEXT,
                        ADD COLUMN IF NOT EXISTS pruned       BOOLEAN DEFAULT FALSE,
                        ADD COLUMN IF NOT EXISTS pruned_at    TIMESTAMPTZ;
                    CREATE INDEX IF NOT EXISTS prime_rec_file_idx   ON prime_recordings(file);
                    CREATE INDEX IF NOT EXISTS prime_rec_status_idx ON prime_recordings(status);
                    -- Backfill `file` for rows written before this column existed.
                    UPDATE prime_recordings
                       SET file = regexp_replace(path, '^.*[/\\\\]', '')
                     WHERE file IS NULL AND path IS NOT NULL;
                """)
            conn.commit()
        print("[retention] schema ready")
    except Exception as e:
        print(f"[retention] migration error: {e}")


_migrate_recordings_retention()


def _is_undertested(track: str, car: str, session_type: str, meta: Optional[Dict[str, Any]] = None) -> bool:
    blob = " ".join(str(x or "") for x in (track, car, session_type)).lower()
    if meta:
        try:
            blob += " " + json.dumps(meta).lower()
        except Exception:
            pass
    return any(tok in blob for tok in UNDERTESTED_TOKENS)


def _pin(cur, fname: str, reason: str) -> None:
    cur.execute("UPDATE prime_recordings SET pinned = TRUE, pin_reason = COALESCE(pin_reason, %s) "
                "WHERE file = %s", (reason, fname))


def _apply_keep_rules(fname: str, finding: str, ticks: int,
                      track: str = "", car: str = "", session_type: str = "") -> Optional[str]:
    """Called when a lugnut reports a finding. Marks the recording processed and
    decides — ONCE, HERE — whether it joins the permanent corpus.

    The random roll happens at FINDING time on purpose. Rolling at prune time
    would bias the corpus toward whatever happened to survive long enough to be
    considered, which is exactly the selection effect a random sample exists to
    avoid. Returns the pin reason, or None."""
    if not _PG_URL:
        return None
    reason = None
    try:
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE prime_recordings SET status='processed', finding=%s, "
                    "processed_at=NOW(), ticks=GREATEST(COALESCE(ticks,0), %s), "
                    "track=COALESCE(NULLIF(track,''),%s), car=COALESCE(NULLIF(car,''),%s) "
                    "WHERE file=%s",
                    (finding, int(ticks or 0), track, car, fname))
                # Read the row back: session_type and the true tick count come from the
                # recording HEADER at upload time, not from the worker's finding payload.
                cur.execute("SELECT track, car, session_type, ticks FROM prime_recordings "
                            "WHERE file=%s", (fname,))
                _r = cur.fetchone() or (track, car, session_type, ticks)
                track, car, session_type = _r[0] or "", _r[1] or "", _r[2] or ""
                ticks = int(_r[3] or ticks or 0)

                # 1. Found a bug -> regression corpus. Never auto-deleted.
                if finding == "found":
                    reason = "found"
                # 2. Undertested territory -> pin until the harness covers it.
                elif _is_undertested(track, car, session_type):
                    reason = "undertested"
                # 3. Long race -> exercises second pit windows, fuel crossovers,
                #    swaps that a sprint never reaches.
                elif int(ticks or 0) >= GOLDEN_LONG_MIN_TICKS:
                    cur.execute("SELECT COUNT(*) FROM prime_recordings "
                                "WHERE pin_reason='golden_long' AND NOT pruned")
                    if int(cur.fetchone()[0]) < GOLDEN_LONG_KEEP:
                        reason = "golden_long"
                # 4. Unbiased random sample — the only rule that can catch a bug
                #    class nobody has thought to bucket for yet.
                if reason is None and finding == "clean" and _rnd.random() < GOLDEN_RANDOM_RATE:
                    reason = "golden_random"

                if reason:
                    _pin(cur, fname, reason)
            conn.commit()
        if reason:
            print(f"[retention] pinned {fname} ({reason})")
    except Exception as e:
        print(f"[retention] keep-rule error for {fname}: {e}")
    return reason


def plan_cleanup(rows: List[Dict[str, Any]], now_ts: float,
                 budget_bytes: float, retain_days: int, coverage_keep: int) -> Dict[str, Any]:
    """PURE decision function — no DB, no filesystem. Given the recording rows,
    return which files to prune and why each survivor was kept.

    Split out from the endpoint so the rules can be unit-tested without a
    database; the delete path is the one place a bug is unrecoverable.

    Hard invariants (never violated, in this order):
      * pinned, finding='found', finding='error', or status != 'processed'
        are NEVER pruned. Unprocessed means a lugnut has not looked yet, and we
        never delete data before it has been looked at even once.
      * the coverage sample (newest N clean per track/car/session) is kept.
      * only then: age-prune, then size-cap eviction oldest-first.
    """
    protected, candidates = [], []
    for r in rows:
        if r.get("pruned"):
            continue
        if (r.get("pinned") or r.get("finding") in ("found", "error")
                or (r.get("status") or "pending") != "processed"):
            protected.append(r)
        else:
            candidates.append(r)

    # Coverage sample: newest N clean per (track, car, session_type).
    combos: Dict[tuple, List[Dict[str, Any]]] = {}
    for r in candidates:
        if r.get("finding") == "clean":
            key = (r.get("track") or "", r.get("car") or "", r.get("session_type") or "")
            combos.setdefault(key, []).append(r)
    keep_coverage = set()
    for key, group in combos.items():
        group.sort(key=lambda x: float(x.get("created_at") or 0), reverse=True)
        for r in group[:max(0, coverage_keep)]:
            keep_coverage.add(r.get("file"))

    prunable = [r for r in candidates if r.get("file") not in keep_coverage]

    cutoff = now_ts - (retain_days * 86400.0)
    to_prune = [r for r in prunable
                if r.get("finding") == "clean" and float(r.get("created_at") or 0) < cutoff]
    pruned_files = {r.get("file") for r in to_prune}

    # Size-cap eviction: belt-and-suspenders so a peak-night flood can never wedge
    # the volume even if nothing has aged out yet.
    def _total(rs):
        return sum(float(r.get("bytes") or 0) for r in rs)

    remaining = [r for r in rows if not r.get("pruned") and r.get("file") not in pruned_files]
    evictable = sorted([r for r in prunable if r.get("file") not in pruned_files],
                       key=lambda x: float(x.get("created_at") or 0))
    evicted_for_size = []
    while _total(remaining) > budget_bytes and evictable:
        victim = evictable.pop(0)
        to_prune.append(victim)
        evicted_for_size.append(victim.get("file"))
        pruned_files.add(victim.get("file"))
        remaining = [r for r in remaining if r.get("file") != victim.get("file")]

    return {
        "to_prune": to_prune,
        "pruned_files": sorted(f for f in pruned_files if f),
        "evicted_for_size": evicted_for_size,
        "protected": len(protected),
        "coverage_kept": sorted(f for f in keep_coverage if f),
        "bytes_after": _total(remaining),
        "freed_bytes": sum(float(r.get("bytes") or 0) for r in to_prune),
    }


@app.post("/admin/recordings/cleanup")
def admin_recordings_cleanup(
    dry_run: Optional[bool] = None,
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
) -> Dict[str, Any]:
    """Prune spent raw recordings. Idempotent; only ever unlinks the gzip, never a
    DB row and never a finding — the finding ledger is the permanent QA history.

    Run HOURLY (Railway cron). Daily is too slow: at ~24 MB a race the heavy
    scenario puts more than the whole budget on the volume in a single evening."""
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)
    if not _PG_URL:
        raise HTTPException(status_code=503, detail="cleanup requires the Postgres index")

    is_dry = CLEANUP_DRY_RUN if dry_run is None else bool(dry_run)
    rows: List[Dict[str, Any]] = []
    try:
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT file, path, bytes, ticks, track, car, session_type, status, "
                    "finding, pinned, pin_reason, pruned, EXTRACT(EPOCH FROM received_at) "
                    "FROM prime_recordings WHERE COALESCE(pruned, FALSE) = FALSE")
                for t in cur.fetchall():
                    rows.append({
                        "file": t[0], "path": t[1], "bytes": t[2], "ticks": t[3],
                        "track": t[4], "car": t[5], "session_type": t[6], "status": t[7],
                        "finding": t[8], "pinned": t[9], "pin_reason": t[10],
                        "pruned": t[11], "created_at": t[12],
                    })
    except Exception as e:
        raise HTTPException(status_code=500, detail="index read failed: %s" % e)

    plan = plan_cleanup(rows, _now(), STORAGE_BUDGET_GB * 1e9,
                        RETAIN_CLEAN_DAYS, COVERAGE_KEEP_PER_COMBO)

    deleted = 0
    if not is_dry:
        for r in plan["to_prune"]:
            fname = r.get("file")
            try:
                p = Path(r.get("path") or (RECORDINGS_DIR / (fname or "")))
                if p.exists():
                    p.unlink()
                with _pg_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("UPDATE prime_recordings SET pruned = TRUE, pruned_at = NOW() "
                                    "WHERE file = %s", (fname,))
                    conn.commit()
                deleted += 1
            except Exception as e:
                print(f"[retention] prune failed for {fname}: {e}")

    out = {
        "ok": True, "dry_run": is_dry,
        "considered": len(rows), "protected": plan["protected"],
        "would_prune" if is_dry else "pruned": len(plan["to_prune"]),
        "deleted": deleted,
        "freed_mb": round(plan["freed_bytes"] / 1e6, 1),
        "budget_gb": STORAGE_BUDGET_GB,
        "used_gb_after": round(plan["bytes_after"] / 1e9, 3),
        "evicted_for_size": len(plan["evicted_for_size"]),
        "coverage_kept": len(plan["coverage_kept"]),
        "files": plan["pruned_files"][:200],
    }
    print(f"[retention] cleanup dry_run={is_dry} considered={len(rows)} "
          f"prune={len(plan['to_prune'])} freed_mb={out['freed_mb']}")
    return out


@app.post("/admin/recording/{fname}/pin")
def admin_recording_pin(
    fname: str,
    unpin: bool = False,
    reason: str = "manual",
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
) -> Dict[str, Any]:
    """Hand-pin (or release) a recording so cleanup never touches it."""
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)
    if not _PG_URL:
        raise HTTPException(status_code=503, detail="pin requires the Postgres index")
    safe = "".join(ch for ch in fname if ch.isalnum() or ch in "._-")
    try:
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                if unpin:
                    cur.execute("UPDATE prime_recordings SET pinned=FALSE, pin_reason=NULL "
                                "WHERE file=%s", (safe,))
                else:
                    cur.execute("UPDATE prime_recordings SET pinned=TRUE, pin_reason=%s "
                                "WHERE file=%s", (reason, safe))
            conn.commit()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "file": safe, "pinned": not unpin, "reason": None if unpin else reason}


@app.get("/admin/storage")
def admin_storage(
    top: int = 15,
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
) -> Dict[str, Any]:
    """What is ACTUALLY on the volume — walked from disk, not from the index.

    /admin/recordings/storage answers "what does Postgres think we are keeping",
    which is a different question and can be wildly optimistic. Two ways it
    drifts, both of which have to be visible or they are unfixable:

      * ORPHANS. A gzip that reached the volume without an index row is invisible
        to plan_cleanup -- it reads `SELECT ... FROM prime_recordings` -- so it is
        never counted toward the budget and can never be pruned. It just sits
        there forever.
      * THE REST OF DATA_DIR. Recordings share the volume with the append-only
        logs (prime_sessions.jsonl, qa_findings.jsonl) and the JSON stores, and
        NOTHING prunes those. A volume can fill with no recordings involved.

    Read-only: this walks and reports. It never unlinks anything.
    """
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)

    indexed: set = set()
    idx_ok = False
    if _PG_URL:
        try:
            with _pg_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT file FROM prime_recordings")
                    indexed = {r[0] for r in cur.fetchall() if r and r[0]}
            idx_ok = True
        except Exception as e:
            print(f"[storage] index read failed: {e}")

    out: Dict[str, Any] = {"ok": True, "data_dir": str(DATA_DIR),
                           "index_readable": idx_ok, "budget_gb": STORAGE_BUDGET_GB}
    buckets: Dict[str, Dict[str, Any]] = {}
    biggest: List[Dict[str, Any]] = []
    total = 0

    def _bucket(rel: str) -> str:
        if rel.startswith("recordings/") or rel.startswith("recordings\\"):
            return "recordings"
        if rel.endswith(".jsonl"):
            return "append-only logs"
        if rel.endswith(".json"):
            return "json stores"
        return "other"

    try:
        root = Path(DATA_DIR)
        for p in root.rglob("*"):
            try:
                if not p.is_file():
                    continue
                sz = p.stat().st_size
            except OSError:
                continue
            rel = str(p.relative_to(root))
            total += sz
            b = buckets.setdefault(_bucket(rel), {"bytes": 0, "files": 0})
            b["bytes"] += sz
            b["files"] += 1
            biggest.append({"file": rel, "mb": round(sz / 1e6, 1)})
    except Exception as e:
        return {"ok": False, "detail": "walk failed: %s" % e}

    # ORPHANS: on the volume, not in the index. These are the ones cleanup cannot
    # see, so name them explicitly rather than burying them in a total.
    orphans: List[Dict[str, Any]] = []
    orphan_bytes = 0
    if idx_ok:
        try:
            if RECORDINGS_DIR.exists():
                for p in RECORDINGS_DIR.glob("*.jsonl.gz"):
                    if p.name not in indexed:
                        sz = p.stat().st_size
                        orphan_bytes += sz
                        orphans.append({"file": p.name, "mb": round(sz / 1e6, 1)})
        except Exception as e:
            print(f"[storage] orphan scan failed: {e}")

    biggest.sort(key=lambda x: x["mb"], reverse=True)
    orphans.sort(key=lambda x: x["mb"], reverse=True)
    out["total_gb"] = round(total / 1e9, 3)
    out["pct_of_budget"] = round(100.0 * total / max(0.001, STORAGE_BUDGET_GB * 1e9), 1)
    out["by_kind"] = {k: {"gb": round(v["bytes"] / 1e9, 3), "files": v["files"]}
                      for k, v in sorted(buckets.items(),
                                         key=lambda kv: -kv[1]["bytes"])}
    out["orphans"] = {
        "count": len(orphans) if idx_ok else None,
        "gb": round(orphan_bytes / 1e9, 3) if idx_ok else None,
        "files": orphans[:max(0, top)],
        # An unreadable index means we CANNOT tell an orphan from a tracked file.
        # Reporting zero there would be a lie in the most useful direction.
        "note": (("gzips on the volume with no index row — plan_cleanup cannot see "
                  "or prune these") if idx_ok else
                 "index unreadable — cannot tell orphans from tracked files"),
    }
    out["biggest"] = biggest[:max(0, top)]
    return out


@app.get("/admin/recordings/storage")
def admin_recordings_storage(
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
) -> Dict[str, Any]:
    """Storage banner for /admin/qa: how full the inbox is and what is being kept."""
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)
    if not _PG_URL:
        return {"ok": False, "detail": "no Postgres index"}
    out: Dict[str, Any] = {"ok": True, "budget_gb": STORAGE_BUDGET_GB}
    try:
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COALESCE(SUM(bytes),0), COUNT(*) FROM prime_recordings "
                            "WHERE COALESCE(pruned,FALSE)=FALSE")
                b, n = cur.fetchone()
                out["raw_gb"] = round(float(b or 0) / 1e9, 3)
                out["kept"] = int(n or 0)
                cur.execute("SELECT COUNT(*) FROM prime_recordings WHERE COALESCE(pruned,FALSE)=TRUE")
                out["pruned"] = int(cur.fetchone()[0])
                cur.execute("SELECT COALESCE(pin_reason,'manual'), COUNT(*) FROM prime_recordings "
                            "WHERE pinned AND NOT COALESCE(pruned,FALSE) GROUP BY 1")
                out["pinned_by_reason"] = {r[0]: int(r[1]) for r in cur.fetchall()}
                cur.execute("SELECT COUNT(*) FROM prime_recordings "
                            "WHERE COALESCE(status,'pending')<>'processed' AND NOT COALESCE(pruned,FALSE)")
                out["unprocessed"] = int(cur.fetchone()[0])
    except Exception as e:
        return {"ok": False, "detail": str(e)}
    out["pct_of_budget"] = round(100.0 * out.get("raw_gb", 0) / max(0.001, STORAGE_BUDGET_GB), 1)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# ENDURANCE TEAM RELAY  (merged from src/team_relay.py — see
# claude/ENDURANCE_RELAY_PROTOCOL.md and ENDURANCE_TEAM_SYNC_DESIGN.md)
#
# The relay is deliberately dumb: it carries the roster, the stint plan, live
# dashboard state and an event ring. It does NOT decide who is driving — every
# client derives that from the SDK. State is versioned; update_plan is
# compare-and-swap on `version` and returns 409 with the current state on
# conflict, which is what team_client's retry expects.
#
# The store functions below are lifted verbatim in behaviour from the reference
# implementation, renamed with a _tr_ prefix so nothing can collide with the
# control server's own helpers.
#
# ── DEPLOYMENT CONSTRAINTS — READ BEFORE SHIPPING ────────────────────────────
# 1. RUN A SINGLE WORKER. Room state lives in this process's memory. With more
#    than one uvicorn worker, create/join land in different processes and rooms
#    go missing at random. If the Railway start command has --workers > 1, this
#    will not work.
# 2. ROOMS DO NOT SURVIVE A REDEPLOY. In-memory, 8h TTL. An endurance race that
#    spans a redeploy loses its room and every client gets unknown_token. Fine
#    for a first teammate test; before real enduros the store wants persisting
#    to Postgres (the state dict is small JSON — write on mutation, lazy-load on
#    cache miss).
# 3. TEAM_RELAY_ENFORCE=1 makes the tier gate fail CLOSED. Leave it 0 only while
#    testing the loop.
# ══════════════════════════════════════════════════════════════════════════════

import secrets as _tr_secrets
import threading as _tr_threading

_TR_LOCK = _tr_threading.RLock()
_TR_ROOMS: Dict[str, Any] = {}      # code -> state dict
_TR_TOKENS: Dict[str, Any] = {}     # member_token -> {"code":.., "custid":..}

_TR_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no O/0/I/1
_TR_ROOM_TTL_S = float(os.getenv("TEAM_ROOM_TTL_S", str(8 * 3600)))
_TR_EVENT_RING = 200
_TR_HISTORY_MAX = 40
# How many event ids to remember per room for duplicate suppression. A 24h race
# is a few hundred events; this is comfortably more, and it is bounded so a room
# cannot grow without limit.
_TR_EID_MEMORY = int(os.getenv("TEAM_EID_MEMORY", "2000"))
# A live block nobody has refreshed in this long is not live data any more. The
# ACTIVE client pushes every 2s, so this only trips on a client that has actually
# stopped -- crashed, alt-F4'd, or lost the internet mid-stint.
_TR_LIVE_STALE_S = float(os.getenv("TEAM_LIVE_STALE_S", "15"))
_TR_MAX_MEMBERS = int(os.getenv("TEAM_MAX_MEMBERS", "8"))
TEAM_RELAY_ENFORCE = (os.getenv("TEAM_RELAY_ENFORCE", "0").strip() == "1")

# Tier lookups are cached: a create/join must not pay a double Stripe round-trip,
# and a reconnect storm must not hammer Stripe.
_TR_TIER_CACHE: Dict[str, Any] = {}
_TR_TIER_TTL_S = float(os.getenv("TEAM_TIER_CACHE_S", "900"))


def _tr_tier_for_email(email: str) -> str:
    """READ-ONLY tier lookup for the relay gate.

    Deliberately NOT license_check(): that function also calls _seat_claim(), so
    using it here would burn a license seat every time somebody opened a team
    room. This resolves tier only and claims nothing. It reuses the same
    STRIPE_PRO_PLUS_IDS / DEV_EMAILS / tester-override sources, so there is one
    place to change price IDs.
    """
    email = (email or "").strip().lower()
    if not email:
        return "free"
    hit = _TR_TIER_CACHE.get(email)
    if hit and (_now() - hit[0]) < _TR_TIER_TTL_S:
        return hit[1]

    tier = "free"
    try:
        if email in DEV_EMAILS:
            tier = "pro_plus"
        else:
            _ov = _load_json(TESTER_OVERRIDES_PATH, {})
            if email in _ov:
                tier = str(_ov[email] or "free")
            elif STRIPE_SECRET_KEY:
                r = requests.get("https://api.stripe.com/v1/customers",
                                 params={"email": email, "limit": 5},
                                 auth=(STRIPE_SECRET_KEY, ""), timeout=8)
                if r.ok:
                    for customer in r.json().get("data", []):
                        cid = customer.get("id")
                        if not cid:
                            continue
                        sr = requests.get("https://api.stripe.com/v1/subscriptions",
                                          params={"customer": cid, "status": "active", "limit": 10},
                                          auth=(STRIPE_SECRET_KEY, ""), timeout=8)
                        if not sr.ok:
                            continue
                        for sub in sr.json().get("data", []):
                            for item in sub.get("items", {}).get("data", []):
                                pid = item.get("price", {}).get("id", "")
                                prod = item.get("price", {}).get("product", "")
                                if pid in STRIPE_PRO_PLUS_IDS or prod in STRIPE_PRO_PLUS_IDS:
                                    tier = "pro_plus"
                                    break
                            if tier == "pro_plus":
                                break
                        if tier == "pro_plus":
                            break
    except Exception as e:
        print(f"[team] tier lookup error for {email}: {e}")
        # Do NOT cache a failed lookup — a Stripe blip would lock a paying
        # customer out of their own enduro for the whole TTL.
        return "error"

    _TR_TIER_CACHE[email] = (_now(), tier)
    return tier


def _tr_seat_matches(email: str, install_id: Optional[str]) -> bool:
    """Optional strengthening: if the client sent an install_id, require that it
    is a seat already claimed by this email.

    Today's team_client sends only {email, license_token, my_id}, so email alone
    is the credential — i.e. the gate is 'do you know a Pro+ email address',
    which is not authentication. When the client starts sending install_id this
    binds a room to a machine that actually activated. Absent install_id we do
    not fail, so this is forward-compatible with the shipped client."""
    if not install_id or not _PG_URL:
        return True
    try:
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM license_seats WHERE email=%s AND install_id=%s",
                            ((email or "").strip().lower(), install_id))
                return cur.fetchone() is not None
    except Exception as e:
        print(f"[team] seat check error: {e}")
        return not TEAM_RELAY_ENFORCE      # fail closed only when enforcing


def verify_pro_plus(email: str, license_token: str = "", install_id: Optional[str] = None) -> bool:
    """True iff this account may open/join a team room.

    TEAM_RELAY_ENFORCE=0 (dev) fails OPEN so the loop is testable without Stripe.
    TEAM_RELAY_ENFORCE=1 (production) fails CLOSED on any error — a lookup blip
    denies rather than admits."""
    if not TEAM_RELAY_ENFORCE:
        return True
    tier = _tr_tier_for_email(email)
    if tier != "pro_plus":
        print(f"[team] DENY {email!r} tier={tier}")
        return False
    if not _tr_seat_matches(email, install_id):
        print(f"[team] DENY {email!r} install_id not a claimed seat")
        return False
    return True


# --------------------------------------------------------------- store helpers
def _tr_gen_code() -> str:
    while True:
        code = "".join(_tr_secrets.choice(_TR_CODE_ALPHABET) for _ in range(6))
        if code not in _TR_ROOMS:
            return code


def _tr_reap() -> None:
    # Age from LAST ACTIVITY, not creation. Reaping 8h after _created killed a 24h
    # room at hour 8 -- mid-race, with everyone still heartbeating into it. It also
    # punished the thing we now expect people to do: open the room early, build the
    # stint plan, and come back at green.
    dead = [c for c, r in _TR_ROOMS.items()
            if (_now() - max(r.get("_touched", 0), r.get("_created", 0))) > _TR_ROOM_TTL_S]
    for c in dead:
        if _TR_ROOMS.pop(c, None):
            for t in [t for t, v in _TR_TOKENS.items() if v.get("code") == c]:
                _TR_TOKENS.pop(t, None)
    if dead:
        print(f"[team] reaped {len(dead)} expired room(s)")


def _tr_new_state(code: str, host_custid) -> Dict[str, Any]:
    return {
        "version": 1, "last_seq": 0, "_created": _now(), "_touched": _now(),
        "_next_mid": 1,
        "room": {"code": code, "created_at": _now(), "host_custid": host_custid,
                 "host_mid": None, "locked": False, "members": []},
        "plan": {"version": 1, "stints": [], "alert_lead_min": 5},
        "live": {}, "resources": {}, "events": [], "stint_history": [],
    }


def _tr_apply_event(st: Dict[str, Any], ev_type, by_custid, data, eid=None) -> bool:
    """Append to the event ring; mirror stint_complete into stint_history so a
    late joiner sees completed stints even after they roll off the ring.

    Returns True when the event was applied, False when it was a duplicate.

    IDEMPOTENCY. The client drains its outbound queue BEFORE the POST and puts
    the events back if the heartbeat fails -- and "failed" includes the case
    where this server accepted the POST and the RESPONSE was lost on the way
    back. The retry then delivers the same stint_complete a second time, and
    stint_history is append-only with nothing identifying a row: two rows in the
    debrief, an avg_burn averaged over the duplicate, and stops_remaining
    (len(stints) - 1 - len(history)) a stop short for the rest of the race.

    So every event the client sends carries an `eid` (uuid4). Remember the ones
    we have applied and drop a repeat. Kept as a LIST, not a set, because the
    room state is meant to be JSON-persistable (see the deployment note above),
    and bounded because a 24h race is thousands of events.
    """
    if eid:
        seen = st.setdefault("_eids", [])
        if eid in seen:
            print("[team] room %s: dropped duplicate %s (eid=%s)"
                  % (st["room"]["code"], ev_type, eid))
            return False
        seen.append(eid)
        if len(seen) > _TR_EID_MEMORY:
            del seen[:-_TR_EID_MEMORY]
    st["last_seq"] += 1
    st["events"].append({"seq": st["last_seq"], "type": ev_type,
                         "by_custid": by_custid, "ts": _now(), "data": data or {}})
    if len(st["events"]) > _TR_EVENT_RING:
        st["events"] = st["events"][-_TR_EVENT_RING:]
    if ev_type == "stint_complete":
        st.setdefault("stint_history", []).append(dict(data or {}))
        if len(st["stint_history"]) > _TR_HISTORY_MAX:
            st["stint_history"] = st["stint_history"][-_TR_HISTORY_MAX:]
    return True


# ===================================================================
# IDENTITY
#
# A member used to BE its iRacing customer id. That made custid the primary key
# for the roster, the plan, live.active_custid and stint history -- and the id
# only exists once you are loaded into a session. Join before that and every one
# of those keys was None: the roster read "Driver None", a teammate joining the
# same way was never added at all (the append is guarded on my_id is not None),
# and _tr_touch could not match anybody, so everyone showed offline forever.
#
# Now each member gets a stable `mid` at join. The plan and the roster reference
# THAT. `custid` is an attribute filled in later, when Chief connects to iRacing
# and the heartbeat carries it -- see _tr_claim_custid. So you can open a room,
# name yourself and build the whole stint plan before the sim is even running.
# ===================================================================
def _tr_next_mid(st: Dict[str, Any]) -> str:
    n = int(st.get("_next_mid", 1))
    st["_next_mid"] = n + 1
    return "m%d" % n


def _tr_unique_name(st: Dict[str, Any], name: str) -> str:
    """Two people typing "Kory" is unambiguous internally (mid) and useless on a
    pit wall. Suffix the second one."""
    base = (name or "").strip()[:24] or "Driver"
    taken = {(m.get("name") or "").strip().lower() for m in st["room"]["members"]}
    if base.lower() not in taken:
        return base
    for i in range(2, 20):
        cand = "%s (%d)" % (base, i)
        if cand.lower() not in taken:
            return cand
    return base


def _tr_member(custid, role, name=None, mid=None, install_id=None) -> Dict[str, Any]:
    return {"mid": mid, "custid": custid, "name": name, "install_id": install_id,
            "role": role, "online": True, "tier_ok": True, "last_seen": _now()}


def _tr_find(st: Dict[str, Any], mid) -> Optional[Dict[str, Any]]:
    for m in st["room"]["members"]:
        if m.get("mid") == mid:
            return m
    return None


def _tr_claim_custid(st: Dict[str, Any], mid, custid) -> bool:
    """Lock an iRacing id onto a member, ONCE.

    Called from the heartbeat, which starts carrying my_id the moment Chief binds
    it from the SDK. Only fills a member whose custid is still None: a different id
    arriving on the same token later is ignored, otherwise a reconnect could
    silently take over a teammate's seat and their stints.

    On the claim, backfill every stint the member owns. The endurance engine
    matches stints on driver_custid, so a plan built in the garage -- when nobody
    had an id yet -- only starts working at exactly this moment.
    """
    if custid is None:
        return False
    m = _tr_find(st, mid)
    if m is None or m.get("custid") is not None:
        return False
    if any(x.get("custid") == custid for x in st["room"]["members"]):
        return False                      # that id already belongs to someone here
    m["custid"] = custid
    if st["room"].get("host_mid") == mid:
        st["room"]["host_custid"] = custid
    for s_ in (st.get("plan", {}) or {}).get("stints", []) or []:
        if s_.get("mid") == mid and s_.get("driver_custid") is None:
            s_["driver_custid"] = custid
    print("[team] room %s: %s claimed custid=%s" % (st["room"]["code"], mid, custid))
    return True


def _tr_touch(state: Dict[str, Any], mid) -> None:
    state["_touched"] = _now()
    for m in state["room"]["members"]:
        if m.get("mid") == mid:
            m["online"] = True
            m["last_seen"] = _now()
    for m in state["room"]["members"]:
        if (_now() - m.get("last_seen", 0)) > 10.0:
            m["online"] = False


def _tr_age_live(st: Dict[str, Any]) -> None:
    """Mark the live block stale once nobody is refreshing it.

    Nothing aged `live` out. The ACTIVE client writes it every 2s; if that client
    crashes, alt-F4s or loses the internet, this server kept serving the LAST
    payload it received, unchanged, for the rest of the race. Every pit wall in
    the room showed IN CAR, that driver's fuel and that driver's tyres, with no
    way to tell a steady stint from a dead connection.

    Flagged, not blanked. The numbers are still the last thing that was true, and
    a crew wants to see them -- they just need to know how old they are. Clients
    render this as NO SIGNAL / LAST KNOWN.

    Deliberately NOT a version bump: staleness is not a structural change, and
    bumping here would churn every client's UI on a timer, which is the exact
    problem the live/version split above exists to avoid.
    """
    lv = st.get("live")
    if not isinstance(lv, dict) or not lv:
        return
    ts = lv.get("server_ts")
    if ts is None:
        return
    try:
        stale = (_now() - float(ts)) > _TR_LIVE_STALE_S
    except (TypeError, ValueError):
        return
    if stale and not lv.get("stale"):
        lv["stale"] = True
        print("[team] room %s: live block went stale (%.0fs)"
              % (st["room"]["code"], _now() - float(ts)))


def _tr_public(state: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in state.items() if not k.startswith("_")}


def _tr_norm_id(v):
    """A driver id only counts once iRacing is running. Before that the client
    reports -1 (or 0/None), and that sentinel must NEVER be treated as a real
    identity: the join reconnect matched seats on custid, so two teammates who
    set up their room BEFORE firing up the sim both arrived as custid -1, matched
    each other's seat, and the second one silently took over the first's seat
    instead of being added (Kory + Amanda, Aug 22 -- confirmed from the live
    roster: two -1 members can never share a room). Normalise any non-positive
    id to None so all the existing None-handling (no seat match; custid locks on
    later from the heartbeat) does the right thing."""
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _tr_resolve(member_token):
    tok = _TR_TOKENS.get(member_token or "")
    if not tok:
        return None, None
    return _TR_ROOMS.get(tok["code"]), tok


# --------------------------------------------------------------- store actions
def tr_create_room(email, license_token, my_id, race_session_id=None, install_id=None,
                   name=None):
    my_id = _tr_norm_id(my_id)
    if not verify_pro_plus(email, license_token, install_id):
        return {"error": "pro_plus_required"}, 403
    with _TR_LOCK:
        _tr_reap()
        code = _tr_gen_code()
        st = _tr_new_state(code, my_id)
        st["room"]["race_session_id"] = race_session_id
        mid = _tr_next_mid(st)
        st["room"]["host_mid"] = mid
        st["room"]["members"].append(
            _tr_member(my_id, "host", name=_tr_unique_name(st, name or "Host"),
                       mid=mid, install_id=install_id))
        token = _tr_secrets.token_urlsafe(18)
        _TR_TOKENS[token] = {"code": code, "custid": my_id, "mid": mid}
        _TR_ROOMS[code] = st
        print(f"[team] room {code} created by {mid} custid={my_id} ({email})")
        return {"room_code": code, "member_token": token, "state": _tr_public(st)}, 200


def tr_join_room(email, license_token, my_id, room_code, install_id=None, name=None):
    my_id = _tr_norm_id(my_id)
    if not verify_pro_plus(email, license_token, install_id):
        return {"error": "pro_plus_required"}, 403
    with _TR_LOCK:
        _tr_reap()
        st = _TR_ROOMS.get((room_code or "").upper())
        if not st:
            return {"error": "no_such_room"}, 404
        if st["room"].get("locked"):
            return {"error": "room_locked"}, 403
        members = st["room"]["members"]

        # RECONNECT TO YOUR OWN SEAT. Setting up early means people close Chief and
        # come back at green; without this they would return as a SECOND member and
        # the stint plan would point at the seat they abandoned. Match the machine
        # first (install_id), then the iRacing id if we already have one.
        #
        # TWO PASSES, not one. Checking both keys inside a single loop makes the
        # answer depend on member ORDER: a seat matching on custid at index 0
        # wins over the seat matching on install_id at index 1, and the driver
        # ends up with two seats. install_id is the stronger claim -- it is the
        # machine -- so try it against everybody before falling back.
        seat = None
        if install_id:
            for m in members:
                if m.get("install_id") and m["install_id"] == install_id:
                    seat = m
                    break
        if seat is None and my_id is not None:
            for m in members:
                if m.get("custid") == my_id:
                    seat = m
                    break

        if seat is None:
            if len(members) >= _TR_MAX_MEMBERS:
                return {"error": "room_full"}, 403
            mid = _tr_next_mid(st)
            seat = _tr_member(my_id, "driver",
                              name=_tr_unique_name(st, name or "Driver"),
                              mid=mid, install_id=install_id)
            members.append(seat)
            st["version"] += 1
        else:
            mid = seat.get("mid") or _tr_next_mid(st)
            seat["mid"] = mid
            seat["online"] = True
            seat["last_seen"] = _now()
            if install_id and not seat.get("install_id"):
                seat["install_id"] = install_id
            # A name typed on this rejoin wins -- it is the most recent thing the
            # driver actually said about themselves.
            if name and (name or "").strip() != (seat.get("name") or ""):
                seat["name"] = _tr_unique_name(st, name)
                st["version"] += 1

        if my_id is not None:
            _tr_claim_custid(st, mid, my_id)

        token = _tr_secrets.token_urlsafe(18)
        _TR_TOKENS[token] = {"code": st["room"]["code"], "custid": my_id, "mid": mid}
        _tr_touch(st, mid)
        print(f"[team] {mid} ({seat.get('name')}) joined room {st['room']['code']}"
              f" custid={my_id}")
        return {"room_code": st["room"]["code"], "member_token": token,
                "state": _tr_public(st)}, 200


def tr_heartbeat(member_token, my_id, live=None, events=None):
    my_id = _tr_norm_id(my_id)
    with _TR_LOCK:
        st, tok = _tr_resolve(member_token)
        if not st:
            return {"error": "unknown_token"}, 401
        _tr_touch(st, tok.get("mid"))
        # THE LOCK. Chief binds the iRacing id as soon as the SDK reports it, and
        # the heartbeat starts carrying it from that moment. This is where a member
        # who joined from the garage -- name only, no id -- becomes a real driver,
        # and where their pre-built stints get their driver_custid backfilled.
        if my_id is not None and _tr_claim_custid(st, tok.get("mid"), my_id):
            tok["custid"] = my_id
            st["version"] += 1
        # `version` is the STRUCTURAL version: roster, plan, stint history. Clients
        # rebuild their UI on it, and update_plan uses it as a compare-and-swap token.
        #
        # A live telemetry payload must NOT bump it. The ACTIVE client pushes live
        # every 2s, so bumping version there churned it constantly, with two effects:
        #   1. the pit-wall tab tore down and rebuilt its roster/timeline every 2s —
        #      visible as flicker and the scroll position snapping back to the top;
        #   2. worse, base_version went stale within 2 seconds, so the host saving a
        #      stint plan would 409 version_conflict almost every time.
        # Live gets its own counter so a UI can still tell that the numbers moved.
        structural = False
        if isinstance(live, dict) and live:
            # Stamp on the SERVER clock. The client's own session_t comes from the
            # sim and stops moving with it, so it cannot answer "how long since
            # anyone told me anything" -- which is the only question that matters
            # when a client dies mid-stint. See _tr_age_live.
            live["server_ts"] = _now()
            live.pop("stale", None)
            st["live"] = live
            if live.get("active_custid") is not None:
                st["live"]["active_custid"] = live["active_custid"]
            st["live_seq"] = int(st.get("live_seq", 0)) + 1
        for ev in (events or []):
            # Events mutate stint_history, which IS structural -- but only if the
            # event was actually applied. A re-delivered one changes nothing, and
            # bumping version for it would churn every client's UI for no reason.
            if _tr_apply_event(st, ev.get("type"), tok.get("custid"),
                               ev.get("data"), eid=ev.get("eid")):
                structural = True
        if structural:
            st["version"] += 1
        _tr_age_live(st)
        return {"state": _tr_public(st)}, 200


def tr_get_state(room_code, member_token):
    with _TR_LOCK:
        st, _ = _tr_resolve(member_token)
        if not st:
            st = _TR_ROOMS.get((room_code or "").upper())
        if not st:
            return {"error": "no_such_room"}, 404
        # A late joiner's FIRST snapshot has to tell live data from a corpse too.
        _tr_age_live(st)
        return _tr_public(st), 200


def tr_update_plan(member_token, plan, base_version):
    with _TR_LOCK:
        st, tok = _tr_resolve(member_token)
        if not st:
            return {"error": "unknown_token"}, 401
        # Host by MID, not custid. Checking custid meant a host who opened the room
        # before loading into the sim (host_custid None) stopped being host of their
        # own room the moment their real id arrived -- and could no longer edit the
        # plan they had just built.
        _host_mid = st["room"].get("host_mid")
        _is_host = (tok.get("mid") == _host_mid) if _host_mid else \
                   (st["room"].get("host_custid") == tok.get("custid"))
        if not _is_host:
            return {"error": "host_only"}, 403
        if base_version is not None and int(base_version) != int(st["version"]):
            # Compare-and-swap miss: hand back the current state so the client
            # can rebase and retry rather than clobbering someone else's edit.
            return {"error": "version_conflict", "state": _tr_public(st)}, 409
        st["plan"] = plan or {}
        # RESOLVE mid -> custid ON EVERY SAVE, not just at claim time.
        # A plan built in the garage carries only member ids, and the engine matches
        # stints on driver_custid. _tr_claim_custid backfills once when a driver
        # connects -- but the host re-saving the plan afterwards ships the client's
        # copy, whose stints still say null, and that wiped the resolution. Doing it
        # here makes the plan self-healing whoever saves it and whenever.
        _by_mid = {m.get("mid"): m for m in st["room"]["members"] if m.get("mid")}
        for _s in (st["plan"].get("stints") or []):
            _m = _by_mid.get(_s.get("mid"))
            if _m is None:
                continue
            if _s.get("driver_custid") is None and _m.get("custid") is not None:
                _s["driver_custid"] = _m["custid"]
            if _m.get("name"):
                _s["driver_name"] = _m["name"]     # a rename propagates
        st["plan"]["version"] = st["plan"].get("version", 1) + 1
        st["version"] += 1
        return {"state": _tr_public(st)}, 200


def tr_add_event(member_token, event):
    with _TR_LOCK:
        st, tok = _tr_resolve(member_token)
        if not st:
            return {"error": "unknown_token"}, 401
        _tr_touch(st, tok.get("mid"))
        if _tr_apply_event(st, (event or {}).get("type"), tok.get("custid"),
                           (event or {}).get("data"),
                           eid=(event or {}).get("eid")):
            st["version"] += 1
        return {"ok": True, "seq": st["last_seq"]}, 200


def tr_leave(member_token):
    with _TR_LOCK:
        st, tok = _tr_resolve(member_token)
        _TR_TOKENS.pop(member_token or "", None)
        if st and tok:
            for m in st["room"]["members"]:
                if m.get("mid") == tok.get("mid"):
                    m["online"] = False
        return {"ok": True}, 200


# ------------------------------------------------------------------ HTTP glue
class TeamCreateIn(BaseModel):
    email: str = ""
    license_token: str = ""
    my_id: Optional[int] = None
    race_session_id: Optional[Any] = None
    install_id: Optional[str] = None
    name: Optional[str] = None


class TeamJoinIn(BaseModel):
    email: str = ""
    license_token: str = ""
    my_id: Optional[int] = None
    room_code: str = ""
    install_id: Optional[str] = None
    name: Optional[str] = None


class TeamHeartbeatIn(BaseModel):
    member_token: str = ""
    my_id: Optional[int] = None
    live: Optional[Dict[str, Any]] = None
    events: Optional[List[Dict[str, Any]]] = None


class TeamPlanIn(BaseModel):
    member_token: str = ""
    plan: Dict[str, Any] = {}
    base_version: Optional[int] = None


class TeamEventIn(BaseModel):
    member_token: str = ""
    event: Dict[str, Any] = {}


class TeamLeaveIn(BaseModel):
    member_token: str = ""


def _tr_reply(response: Response, pair):
    body, code = pair
    response.status_code = int(code)
    return body


@app.post("/team/create")
def team_create(body: TeamCreateIn, response: Response) -> Dict[str, Any]:
    return _tr_reply(response, tr_create_room(body.email, body.license_token, body.my_id,
                                              body.race_session_id, body.install_id,
                                              name=body.name))


@app.post("/team/join")
def team_join(body: TeamJoinIn, response: Response) -> Dict[str, Any]:
    return _tr_reply(response, tr_join_room(body.email, body.license_token, body.my_id,
                                            body.room_code, body.install_id,
                                            name=body.name))


@app.post("/team/heartbeat")
def team_heartbeat(body: TeamHeartbeatIn, response: Response) -> Dict[str, Any]:
    return _tr_reply(response, tr_heartbeat(body.member_token, body.my_id,
                                            body.live, body.events))


@app.post("/team/plan")
def team_plan(body: TeamPlanIn, response: Response) -> Dict[str, Any]:
    return _tr_reply(response, tr_update_plan(body.member_token, body.plan, body.base_version))


@app.post("/team/event")
def team_event(body: TeamEventIn, response: Response) -> Dict[str, Any]:
    return _tr_reply(response, tr_add_event(body.member_token, body.event))


@app.post("/team/leave")
def team_leave(body: TeamLeaveIn, response: Response) -> Dict[str, Any]:
    return _tr_reply(response, tr_leave(body.member_token))


@app.get("/team/state")
def team_state(response: Response, room_code: str = "", member_token: str = "") -> Dict[str, Any]:
    return _tr_reply(response, tr_get_state(room_code, member_token))


@app.get("/team/health")
def team_health() -> Dict[str, Any]:
    """Unauthenticated liveness only. Deliberately returns COUNTS, never room
    codes — a room code is the join credential."""
    with _TR_LOCK:
        _tr_reap()
        return {"ok": True, "rooms": len(_TR_ROOMS), "tokens": len(_TR_TOKENS),
                "enforce": TEAM_RELAY_ENFORCE}


@app.get("/admin/team/rooms")
def admin_team_rooms(
    x_aichief_key: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    control_api_key_hdr: Optional[str] = Header(default=None, alias="CONTROL_API_KEY"),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
    control_api_key: Optional[str] = Header(default=None, alias="control-api-key"),
) -> Dict[str, Any]:
    """Admin view of live rooms — for debugging a teammate test."""
    _require_admin(x_aichief_key, authorization, control_api_key_hdr, x_api_key, control_api_key)
    with _TR_LOCK:
        rooms = []
        for code, st in _TR_ROOMS.items():
            rm = st.get("room", {})
            rooms.append({
                "code": code,
                "age_s": round(_now() - st.get("_created", _now()), 1),
                "version": st.get("version"),
                "host_custid": rm.get("host_custid"),
                "members": [{"custid": m.get("custid"), "role": m.get("role"),
                             "online": m.get("online")} for m in rm.get("members", [])],
                "stints": len((st.get("plan") or {}).get("stints") or []),
                "events": len(st.get("events") or []),
                "stint_history": len(st.get("stint_history") or []),
                "active_custid": (st.get("live") or {}).get("active_custid"),
            })
        return {"ok": True, "enforce": TEAM_RELAY_ENFORCE, "rooms": rooms}
