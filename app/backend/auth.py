"""Authentication: password verification, bearer tokens, idempotency.

Self-contained equivalent of the TOM driver_auth_store + the P0
driver_api token store. Passwords are hashed with werkzeug (scrypt /
pbkdf2 fallback). Tokens are 256-bit random, stored only as SHA-256
hashes, one active token per driver (device binding).
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from werkzeug.security import check_password_hash, generate_password_hash

from . import config
from .db import get_connection


def hash_password(plain: str) -> str:
    try:
        return generate_password_hash(plain, method="scrypt")
    except (ValueError, AttributeError):
        return generate_password_hash(plain, method="pbkdf2:sha256")


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        s = value.replace("Z", "+00:00") if value.endswith("Z") else value
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


# ── Credentials ──────────────────────────────────────────────────────

def verify_credentials(identifier: str, password: str) -> Optional[str]:
    """Resolve identifier (driver_id or callsign, case-insensitive) and
    verify the password. Returns the driver_id on success, else None.
    Indistinguishable timing/branch for unknown driver vs wrong password
    is approximated by always running a hash check."""
    if not isinstance(identifier, str) or not identifier.strip():
        return None
    if not isinstance(password, str) or not password:
        return None
    needle = identifier.strip().lower()
    conn = get_connection()
    row = conn.execute(
        "SELECT driver_id, password_hash, active FROM drivers "
        "WHERE lower(driver_id) = ? OR lower(callsign) = ?",
        (needle, needle),
    ).fetchone()
    stored = row["password_hash"] if row else None
    # Always compare against *something* to flatten timing.
    probe = stored or "scrypt:dummy$x$y"
    try:
        ok = check_password_hash(probe, password)
    except Exception:
        ok = False
    if not row or not stored or not ok or not row["active"]:
        return None
    return row["driver_id"]


def set_password(driver_id: str, plain: str) -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE drivers SET password_hash = ? WHERE driver_id = ?",
        (hash_password(plain), driver_id),
    )
    conn.commit()


# ── Tokens ───────────────────────────────────────────────────────────

def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue_token(driver_id: str, device_id=None, device_label=None) -> Dict[str, str]:
    raw = secrets.token_urlsafe(32)
    th = _hash_token(raw)
    n = now()
    exp = n + timedelta(hours=config.TOKEN_TTL_HOURS)
    conn = get_connection()
    # One active device per driver.
    conn.execute(
        "UPDATE tokens SET revoked = 1 WHERE driver_id = ? AND revoked = 0",
        (driver_id,),
    )
    conn.execute(
        "INSERT INTO tokens (token_hash, driver_id, device_id, device_label, "
        "created_at, expires_at, last_used_at, revoked) VALUES (?,?,?,?,?,?,?,0)",
        (th, driver_id, device_id, device_label, iso(n), iso(exp), None),
    )
    conn.commit()
    return {"token": raw, "expires_at": iso(exp)}


def resolve_token(raw) -> Optional[str]:
    if not isinstance(raw, str) or not raw.strip():
        return None
    th = _hash_token(raw.strip())
    conn = get_connection()
    row = conn.execute(
        "SELECT driver_id, expires_at, revoked FROM tokens WHERE token_hash = ?",
        (th,),
    ).fetchone()
    if not row or row["revoked"]:
        return None
    exp = parse_iso(row["expires_at"])
    if exp is None or exp <= now():
        return None
    try:
        conn.execute(
            "UPDATE tokens SET last_used_at = ? WHERE token_hash = ?",
            (iso(now()), th),
        )
        conn.commit()
    except Exception:
        pass
    return row["driver_id"]


def revoke_token(raw) -> None:
    if not isinstance(raw, str) or not raw.strip():
        return
    conn = get_connection()
    conn.execute(
        "UPDATE tokens SET revoked = 1 WHERE token_hash = ?",
        (_hash_token(raw.strip()),),
    )
    conn.commit()


# ── Idempotency ──────────────────────────────────────────────────────

def get_idempotent(key) -> Optional[Dict]:
    if not isinstance(key, str) or not key.strip():
        return None
    conn = get_connection()
    row = conn.execute(
        "SELECT status_code, result_json FROM idempotency WHERE idempotency_key = ?",
        (key.strip(),),
    ).fetchone()
    return dict(row) if row else None


def save_idempotent(key, driver_id, route, status_code, result_json) -> None:
    if not isinstance(key, str) or not key.strip():
        return
    conn = get_connection()
    conn.execute(
        "INSERT OR IGNORE INTO idempotency "
        "(idempotency_key, driver_id, route, status_code, result_json, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (key.strip(), driver_id, route, int(status_code), result_json, iso(now())),
    )
    conn.commit()
