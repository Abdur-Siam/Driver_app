"""Configuration for the standalone TOM Driver App backend.

Self-contained: no TOM imports. Everything is overridable via environment
variables so the same code runs locally, in CI, and (after the TOM merge)
against a real backend. See README.md.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP_ROOT = os.path.dirname(_HERE)              # Driver/app

# development (default) | production. Production refuses to boot without an
# env-supplied secret and never seeds demo data (see server.create_app).
_APP_ENV_RAW = os.environ.get("DRIVER_APP_ENV", "").strip().lower()
APP_ENV_DECLARED = bool(_APP_ENV_RAW)           # was the environment set explicitly?
APP_ENV = _APP_ENV_RAW or "development"
IS_PRODUCTION = APP_ENV == "production"


def _under_gunicorn() -> bool:
    """True when this process is being served by gunicorn — a strong signal
    of a real deployment even if DRIVER_APP_ENV was forgotten."""
    if "gunicorn" in (os.environ.get("SERVER_SOFTWARE", "") or "").lower():
        return True
    if os.environ.get("GUNICORN_CMD_ARGS"):
        return True
    argv0 = (sys.argv[0] if sys.argv else "") or ""
    return "gunicorn" in os.path.basename(argv0).lower()


# Fail-safe: an UNDECLARED environment running under gunicorn is treated as
# production-like — demo credentials are never seeded there. Declaring
# DRIVER_APP_ENV (either value) always wins over the heuristic.
IS_PRODUCTION_LIKE = IS_PRODUCTION or (not APP_ENV_DECLARED and _under_gunicorn())

# Data root — override in production so the DB + POD media land on persistent
# storage (e.g. /home on Azure App Service; container-local disk is ephemeral).
DATA_DIR = os.environ.get("DRIVER_APP_DATA_DIR", "").strip() or os.path.join(_APP_ROOT, "data")
MEDIA_DIR = os.path.join(DATA_DIR, "media")     # POD signatures + photos
FRONTEND_DIR = os.path.join(_APP_ROOT, "frontend")

# App version surfaced to the client (About screen + update gating).
APP_VERSION = os.environ.get("DRIVER_APP_VERSION", "1.1.0")


def _app_secret() -> str:
    """Server secret for signing media URLs. From env in production; otherwise
    persisted once under data/ so signed URLs survive a restart in local use."""
    env = os.environ.get("DRIVER_APP_SECRET", "").strip()
    if env:
        return env
    path = os.path.join(DATA_DIR, ".app_secret")
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        os.makedirs(DATA_DIR, exist_ok=True)
        secret = os.urandom(32).hex()
        with open(path, "w") as fh:
            fh.write(secret)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return secret


APP_SECRET = _app_secret()
# How long a signed POD/receipt media URL stays valid (short — it's a bearer capability).
MEDIA_URL_TTL_S = int(os.environ.get("DRIVER_APP_MEDIA_TTL_S", "600"))

# Database — a single self-contained SQLite file (mirrors TOM's WAL setup).
DB_PATH = os.environ.get("DRIVER_APP_DB", os.path.join(DATA_DIR, "driver_app.db"))

# Token session lifetime before re-auth.
TOKEN_TTL_HOURS = int(os.environ.get("DRIVER_APP_TOKEN_TTL_HOURS", "12"))

# Ops/dispatch console session lifetime (a dispatcher works a full shift at a
# desk, so a longer default than the driver device token).
OPS_TOKEN_TTL_HOURS = int(os.environ.get("DRIVER_APP_OPS_TOKEN_TTL_HOURS", "12"))

# Google Maps Platform — DISCONNECTED BY DEFAULT (Jeet, 28 Jul 2026).
#
# MASTER SWITCH: DRIVER_MAPS_ENABLED. Unless it is explicitly "1"/"true"/
# "yes", every Google Maps key below resolves to "" — even when
# GOOGLE_MAPS_API_KEY is present in the environment (it will be, on any
# host that shares TOM's app settings). With the switch off the app makes
# ZERO Google Maps calls, ships no key to the browser, and the CSP stays
# closed to Google hosts; routing falls back to haversine and the frontend
# renders the non-Google map panel. Flip the switch only on Jeet's say-so.
#
# Key resolution (only when the switch is on) is unchanged from before:
# TOM's single referrer-restricted GOOGLE_MAPS_API_KEY drives both the
# server-side Routes call and the in-app Maps JS; the two split vars take
# precedence when explicitly set (dedicated IP-restricted server key /
# app-restricted browser key).
#
# Cost barriers (see routing.py): the server-side Routes call is the ONLY
# Google API this app uses (there is no Places/Geocoding usage anywhere —
# a guard test pins that). It is capped per day and cached; both knobs
# below apply only once the master switch is on.
DRIVER_MAPS_ENABLED = os.environ.get(
    "DRIVER_MAPS_ENABLED", "").strip().lower() in ("1", "true", "yes")
_RAW_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
if DRIVER_MAPS_ENABLED:
    GOOGLE_MAPS_API_KEY = _RAW_MAPS_API_KEY
    GOOGLE_MAPS_SERVER_KEY = os.environ.get("GOOGLE_MAPS_SERVER_KEY", "").strip() or GOOGLE_MAPS_API_KEY
    GOOGLE_MAPS_BROWSER_KEY = os.environ.get("GOOGLE_MAPS_BROWSER_KEY", "").strip() or GOOGLE_MAPS_API_KEY
else:
    GOOGLE_MAPS_API_KEY = ""
    GOOGLE_MAPS_SERVER_KEY = ""
    GOOGLE_MAPS_BROWSER_KEY = ""

# Hard daily ceiling on server-side Google Routes calls (fail-closed: when
# the counter cannot be read/advanced, the call is NOT made). 0 disables
# Google routing outright even with the switch on.
MAPS_DAILY_CAP = int(os.environ.get("DRIVER_MAPS_DAILY_CAP", "200"))

# Route-result cache TTL. Route orders for a fixed set of coordinates are
# near-static — re-optimising the same run must not cost another call.
MAPS_CACHE_TTL_HOURS = int(os.environ.get("DRIVER_MAPS_CACHE_TTL_HOURS", "24"))

# TOM's key is referrer-restricted, so Google only honours server-side REST calls
# whose Referer header matches the key's allow-list. We send this origin on the
# Routes call (mirrors app/modules/google_maps.server_referer). Same env var name
# as TOM — TOM_PUBLIC_ORIGIN — so a single value drives both systems; default
# matches TOM's. At deploy, add the driver app's own public origin to the key's
# allowed HTTP referrers so the in-app Maps JS (loaded from the device/app
# origin) is accepted too.
GOOGLE_MAPS_REFERER = (os.environ.get("TOM_PUBLIC_ORIGIN", "").strip()
                       or "https://tom-dispatch-xmc.azurewebsites.net/")

# Firebase Cloud Messaging (push to Android + iOS via one API) — supplied
# LATER by the operator. Until both are set (plus `pip install google-auth`),
# pushes queue durably in push_outbox and flush once credentials appear.
FCM_PROJECT_ID = os.environ.get("FCM_PROJECT_ID", "").strip()
FCM_CREDENTIALS_JSON = (os.environ.get("FCM_CREDENTIALS_JSON", "").strip()
                        or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip())

# Job-offer countdown: how long a dispatched offer stays open before it
# expires back to the ops board (seconds; per-offer override clamped 15–3600).
OFFER_TTL_S = int(os.environ.get("DRIVER_APP_OFFER_TTL_S", "120"))

# TOM bridge (driver events → TOM), env-gated OFF by default. All three must
# be set for the bridge to enqueue/deliver anything — see backend/bridge.py.
BRIDGE_ENABLED = os.environ.get("BRIDGE_ENABLED", "").strip() == "1"
TOM_BRIDGE_URL = os.environ.get("TOM_BRIDGE_URL", "").strip().rstrip("/")
TOM_BRIDGE_KEY = os.environ.get("TOM_BRIDGE_KEY", "").strip()

# Login throttle (per-IP sliding window) + per-account lockout (brute-force).
LOGIN_RATE_MAX = int(os.environ.get("DRIVER_APP_LOGIN_RATE_MAX", "10"))
LOGIN_RATE_WINDOW_S = int(os.environ.get("DRIVER_APP_LOGIN_RATE_WINDOW_S", "60"))
LOGIN_LOCK_MAX = int(os.environ.get("DRIVER_APP_LOGIN_LOCK_MAX", "5"))          # failures before lockout
LOGIN_LOCK_WINDOW_S = int(os.environ.get("DRIVER_APP_LOGIN_LOCK_WINDOW_S", "900"))  # lockout window (15m)

# Per-driver throttle on heavy/mutating endpoints (generous — must not break
# legitimate offline-outbox replay bursts).
MUTATION_RATE_MAX = int(os.environ.get("DRIVER_APP_MUTATION_RATE_MAX", "300"))
MUTATION_RATE_WINDOW_S = int(os.environ.get("DRIVER_APP_MUTATION_RATE_WINDOW_S", "60"))

# Max request body (POD photos are base64 → inflate ~33%). Caps memory-exhaustion DoS.
MAX_UPLOAD_MB = int(os.environ.get("DRIVER_APP_MAX_UPLOAD_MB", "16"))

# Send HSTS (only honoured by browsers over HTTPS; harmless on local http).
SECURITY_HSTS = os.environ.get("DRIVER_APP_HSTS", "1") == "1"

# Number of reverse-proxy hops in front of the app (Azure App Service / nginx
# = 1). 0 (default) means directly exposed: X-Forwarded-* headers are IGNORED,
# because a raw client can forge them to dodge or poison the login limiter.
# When > 0, ProxyFix rewrites remote_addr/scheme from the trusted hop count.
TRUST_PROXY = int(os.environ.get("DRIVER_APP_TRUST_PROXY", "0") or "0")

# CORS allowlist for the NATIVE app's WebView origins (the packaged Capacitor
# app calls the API cross-origin: iOS from capacitor://localhost, Android from
# https://localhost). The browser PWA is same-origin and never needs CORS.
# Extend/override per deployment (comma-separated) via env.
CORS_ORIGINS = [o.strip() for o in os.environ.get(
    "DRIVER_APP_CORS_ORIGINS",
    "capacitor://localhost,https://localhost,http://localhost,ionic://localhost",
).split(",") if o.strip()]

# Reseed the demo data on boot if the DB is empty. Hard-off in production and
# in production-like contexts (undeclared env under gunicorn) — demo
# credentials (DRV001/test1234) must never exist on a commercial deploy.
# Default stays on for plain local runs (run.sh / serve.py dev ergonomics).
SEED_DEMO = (os.environ.get("DRIVER_APP_SEED_DEMO", "1") == "1") and not IS_PRODUCTION_LIKE
