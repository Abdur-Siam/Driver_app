"""Authentication for the ops / dispatch console.

Deliberately separate from the driver `auth` module: ops accounts live in
their own table (`ops_users`) with their own bearer tokens (`ops_tokens`),
so a leaked driver token can never authenticate against the console, and a
leaked ops token can never act as a driver. Passwords are hashed with
werkzeug (scrypt / pbkdf2 fallback); tokens are 256-bit random, stored only
as SHA-256 hashes. Mirrors the shape of `backend.auth` for the merge.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Dict, Optional

from werkzeug.security import check_password_hash, generate_password_hash

from . import config
from .auth import iso, now, parse_iso
from .db import get_connection


def hash_password(plain: str) -> str:
    try:
        return generate_password_hash(plain, method="scrypt")
    except (ValueError, AttributeError):
        return generate_password_hash(plain, method="pbkdf2:sha256")


# ── Accounts ─────────────────────────────────────────────────────────

def create_user(username: str, name: str, password: str, role: str = "dispatcher") -> None:
    """Create or overwrite an ops account. Role is admin | dispatcher."""
    if role not in ("admin", "dispatcher"):
        role = "dispatcher"
    conn = get_connection()
    conn.execute(
        "INSERT INTO ops_users (username, name, password_hash, role, active, created_at) "
        "VALUES (?,?,?,?,1,?) "
        "ON CONFLICT(username) DO UPDATE SET name=excluded.name, "
        "password_hash=excluded.password_hash, role=excluded.role, active=1",
        (username.strip().lower(), name, hash_password(password), role, iso(now())),
    )
    conn.commit()


def get_user(username) -> Optional[Dict]:
    if not isinstance(username, str) or not username.strip():
        return None
    row = get_connection().execute(
        "SELECT username, name, role, active, last_login_at FROM ops_users WHERE username = ?",
        (username.strip().lower(),),
    ).fetchone()
    return dict(row) if row else None


def verify_credentials(username: str, password: str) -> Optional[str]:
    """Return the username on success, else None. Runs a hash check even for
    unknown users to flatten the timing/branch difference."""
    if not isinstance(username, str) or not username.strip():
        return None
    if not isinstance(password, str) or not password:
        return None
    needle = username.strip().lower()
    row = get_connection().execute(
        "SELECT username, password_hash, active FROM ops_users WHERE username = ?",
        (needle,),
    ).fetchone()
    stored = row["password_hash"] if row else None
    probe = stored or "scrypt:dummy$x$y"
    try:
        ok = check_password_hash(probe, password)
    except Exception:
        ok = False
    if not row or not stored or not ok or not row["active"]:
        return None
    return row["username"]


def set_password(username: str, plain: str) -> None:
    conn = get_connection()
    conn.execute("UPDATE ops_users SET password_hash = ? WHERE username = ?",
                 (hash_password(plain), username.strip().lower()))
    conn.commit()


def set_active(username: str, active: bool) -> None:
    conn = get_connection()
    conn.execute("UPDATE ops_users SET active = ? WHERE username = ?",
                 (1 if active else 0, username.strip().lower()))
    if not active:   # deactivating an account kills its live sessions
        conn.execute("UPDATE ops_tokens SET revoked = 1 WHERE username = ? AND revoked = 0",
                     (username.strip().lower(),))
    conn.commit()


def list_users() -> list:
    rows = get_connection().execute(
        "SELECT username, name, role, active, created_at, last_login_at "
        "FROM ops_users ORDER BY username").fetchall()
    return [dict(r) for r in rows]


# ── Tokens ───────────────────────────────────────────────────────────

def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue_token(username: str, label=None) -> Dict[str, str]:
    raw = secrets.token_urlsafe(32)
    th = _hash_token(raw)
    n = now()
    exp = n + timedelta(hours=config.OPS_TOKEN_TTL_HOURS)
    conn = get_connection()
    conn.execute(
        "INSERT INTO ops_tokens (token_hash, username, label, created_at, expires_at, "
        "last_used_at, revoked) VALUES (?,?,?,?,?,?,0)",
        (th, username, label, iso(n), iso(exp), None),
    )
    conn.execute("UPDATE ops_users SET last_login_at = ? WHERE username = ?", (iso(n), username))
    conn.commit()
    return {"token": raw, "expires_at": iso(exp)}


def resolve_token(raw) -> Optional[Dict]:
    """Return {username, role} for a live token, else None. Rejects tokens
    whose account has since been deactivated."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    th = _hash_token(raw.strip())
    conn = get_connection()
    row = conn.execute(
        "SELECT t.username AS username, t.expires_at AS expires_at, t.revoked AS revoked, "
        "u.role AS role, u.active AS active "
        "FROM ops_tokens t JOIN ops_users u ON u.username = t.username "
        "WHERE t.token_hash = ?",
        (th,),
    ).fetchone()
    if not row or row["revoked"] or not row["active"]:
        return None
    exp = parse_iso(row["expires_at"])
    if exp is None or exp <= now():
        return None
    try:
        conn.execute("UPDATE ops_tokens SET last_used_at = ? WHERE token_hash = ?", (iso(now()), th))
        conn.commit()
    except Exception:
        pass
    return {"username": row["username"], "role": row["role"]}


def revoke_token(raw) -> None:
    if not isinstance(raw, str) or not raw.strip():
        return
    conn = get_connection()
    conn.execute("UPDATE ops_tokens SET revoked = 1 WHERE token_hash = ?",
                 (_hash_token(raw.strip()),))
    conn.commit()
