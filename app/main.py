from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from fastapi import Response
from fastapi.responses import RedirectResponse, FileResponse
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

    _init_prime_table()
    _init_affiliate_table()
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
AFFILIATE_PROFILES_PATH = DATA_DIR / "affiliate_profiles.json"
TESTER_OVERRIDES_PATH   = DATA_DIR / "tester_overrides.json"

CONTROL_API_KEY = (os.getenv("CONTROL_API_KEY") or "").strip()

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

class KillIn(BaseModel):
    version: str

class UnkillIn(BaseModel):
    version: str

class LicenseCheckIn(BaseModel):
    email: str
    
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
        return {"tier": "pro_plus", "email": email, "is_dev": True}
        
    # ── Tester override (checked before Stripe) ───────────────
    _overrides = _load_json(TESTER_OVERRIDES_PATH, {})
    if email in _overrides:
        _tier = _overrides[email]
        print(f"[license] {email} → {_tier} (tester override)")
        return {"tier": _tier, "email": email, "is_dev": False}
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
                        print(f"[lictrace] email={email!r} -> tier=pro_plus code={code} price={price_id} prod={product_id}")
                        return {"tier": "pro_plus", "email": email, "affiliate_code": code, "is_dev": email in DEV_EMAILS}

                    if price_id in STRIPE_PRO_IDS or product_id in STRIPE_PRO_IDS:
                        code = _extract_promo_code(sub, customer)
                        _record_affiliate(email, code, "pro")
                        print(f"[lictrace] email={email!r} -> tier=pro code={code} price={price_id} prod={product_id}")
                        return {"tier": "pro", "email": email, "affiliate_code": code, "is_dev": email in DEV_EMAILS}

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
            f"metadata['dlc']:'spotter_daisy' AND "
            f"metadata['email']:'{email}' AND status:'succeeded'"
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
