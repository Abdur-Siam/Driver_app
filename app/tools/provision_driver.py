"""Provision real drivers on a deployed Driver App.

This script is intended for administering driver accounts on a deployed,
commercial Driver App instance. Production deployments do not seed demo
drivers, so operator tooling like this is used to create and manage
real driver accounts until the TOM merge (after which TOM's driver
roster + driver_auth_store own this data).

Usage (run on the server or against the same DB the app uses):

    cd Driver/app
    python3 tools/provision_driver.py add DRV010 "Sam Patel" S10 --vehicle "Small Van" --phone 07700900123
    python3 tools/provision_driver.py password DRV010          # (re)set — prompts, no echo
    python3 tools/provision_driver.py deactivate DRV010        # driver left — keep audit rows
    python3 tools/provision_driver.py activate DRV010
    python3 tools/provision_driver.py list

Passwords are prompted interactively (never accepted as a CLI argument,
to avoid shell history / process-list leakage) and are stored as scrypt
hashes (see backend.auth for implementation details). Resetting a
password also revokes active tokens for that driver to force re-login.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import auth, db  # noqa: E402

MIN_PASSWORD_LEN = 8


def _prompt_password(driver_id: str) -> str:
    pw = getpass.getpass(f"New password for {driver_id}: ")
    if len(pw) < MIN_PASSWORD_LEN:
        sys.exit(f"Refused: password must be at least {MIN_PASSWORD_LEN} characters")
    if pw != getpass.getpass("Repeat to confirm: "):
        sys.exit("Refused: passwords did not match")
    return pw


def _require(conn, driver_id: str) -> None:
    row = conn.execute("SELECT driver_id FROM drivers WHERE driver_id = ?", (driver_id,)).fetchone()
    if not row:
        sys.exit(f"No such driver: {driver_id}")


def cmd_add(conn, a) -> None:
    # basic callsign validation
    if not a.callsign or not a.callsign.strip():
        sys.exit("Refused: callsign cannot be empty")

    # Case-insensitive callsign uniqueness check inside SQL for correctness across DB collations
    if conn.execute(
        "SELECT 1 FROM drivers WHERE driver_id = ? OR lower(callsign) = lower(?)",
        (a.driver_id, a.callsign),
    ).fetchone():
        sys.exit(f"Refused: driver id {a.driver_id} or callsign {a.callsign} already exists")

    pw = _prompt_password(a.driver_id)

    conn.execute(
        "INSERT INTO drivers (driver_id, name, callsign, password_hash, vehicle, "
        "is_subcontracted, active, phone) VALUES (?,?,?,?,?,?,1,?)",
        (
            a.driver_id,
            a.name,
            a.callsign,
            auth.hash_password(pw),
            a.vehicle,
            1 if a.subcontractor else 0,
            a.phone,
        ),
    )
    conn.commit()
    print(f"Created {a.driver_id} ({a.name}, callsign {a.callsign})")


def cmd_password(conn, a) -> None:
    _require(conn, a.driver_id)
    auth.set_password(a.driver_id, _prompt_password(a.driver_id))
    # Force re-login everywhere: a password reset must invalidate stolen sessions.
    conn.execute("UPDATE tokens SET revoked = 1 WHERE driver_id = ? AND revoked = 0", (a.driver_id,))
    conn.commit()
    print(f"Password set for {a.driver_id}; active sessions revoked")


def _set_active(conn, driver_id: str, active: int) -> None:
    _require(conn, driver_id)
    conn.execute("UPDATE drivers SET active = ? WHERE driver_id = ?", (active, driver_id))
    if not active:
        conn.execute("UPDATE tokens SET revoked = 1 WHERE driver_id = ? AND revoked = 0", (driver_id,))
    conn.commit()
    print(f"{driver_id} {'activated' if active else 'deactivated (sessions revoked)'}")


def cmd_list(conn, _a) -> None:
    rows = conn.execute(
        "SELECT driver_id, name, callsign, vehicle, is_subcontracted, active, phone "
        "FROM drivers ORDER BY driver_id"
    ).fetchall()
    for r in rows:
        kind = "sub" if r["is_subcontracted"] else "own"
        state = "active" if r["active"] else "INACTIVE"
        print(
            f"{r['driver_id']:<8} {r['callsign']:<6} {kind:<4} {state:<8} "
            f"{r['name']}  ({r['vehicle'] or 'no vehicle'}) phone={r.get('phone') or 'n/a'}"
        )
    print(f"{len(rows)} driver(s)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add", help="create a driver (prompts for password)")
    add.add_argument("driver_id")
    add.add_argument("name")
    add.add_argument("callsign")
    add.add_argument("--vehicle", default=None)
    add.add_argument("--phone", default=None)
    add.add_argument("--subcontractor", action="store_true")

    pw = sub.add_parser("password", help="(re)set a password + revoke sessions")
    pw.add_argument("driver_id")

    for name in ("activate", "deactivate"):
        s = sub.add_parser(name, help=f"{name} a driver")
        s.add_argument("driver_id")

    sub.add_parser("list", help="list drivers")

    a = p.parse_args()
    db.init_db()
    conn = db.get_connection()
    if a.cmd == "add":
        cmd_add(conn, a)
    elif a.cmd == "password":
        cmd_password(conn, a)
    elif a.cmd == "activate":
        _set_active(conn, a.driver_id, 1)
    elif a.cmd == "deactivate":
        _set_active(conn, a.driver_id, 0)
    elif a.cmd == "list":
        cmd_list(conn, a)


if __name__ == "__main__":
    main()
