"""Provision ops / dispatch console users on a commercial deploy.

Production never seeds the demo `ops` account, so this is how console
operators are created and managed (until the TOM merge, after which TOM's
operator accounts own this). Run on the server, or against the same
DRIVER_APP_DATA_DIR / DRIVER_APP_DB the app uses:

    cd Driver/app
    python3 tools/provision_ops.py add jsmith "Jane Smith" --role admin
    python3 tools/provision_ops.py password jsmith        # (re)set — prompts, no echo
    python3 tools/provision_ops.py deactivate jsmith      # operator left — keep audit rows
    python3 tools/provision_ops.py activate jsmith
    python3 tools/provision_ops.py list

Passwords are prompted (never an argument — they'd land in shell history /
process lists) and stored as scrypt hashes. Roles: admin | dispatcher.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import db, ops_auth  # noqa: E402

MIN_PASSWORD_LEN = 8


def _prompt_password(username: str) -> str:
    pw = getpass.getpass(f"New password for {username}: ")
    if len(pw) < MIN_PASSWORD_LEN:
        sys.exit(f"Refused: password must be at least {MIN_PASSWORD_LEN} characters")
    if pw != getpass.getpass("Repeat to confirm: "):
        sys.exit("Refused: passwords did not match")
    return pw


def cmd_add(a) -> None:
    username = a.username.strip().lower()
    if ops_auth.get_user(username):
        sys.exit(f"Refused: ops user {username} already exists (use `password` to reset)")
    if a.role not in ("admin", "dispatcher"):
        sys.exit("Refused: role must be admin or dispatcher")
    pw = _prompt_password(username)
    ops_auth.create_user(username, a.name, pw, a.role)
    print(f"Created ops user {username} ({a.name}, role {a.role})")


def cmd_password(a) -> None:
    username = a.username.strip().lower()
    if not ops_auth.get_user(username):
        sys.exit(f"No such ops user: {username}")
    ops_auth.set_password(username, _prompt_password(username))
    # Force re-login everywhere: a password reset must invalidate stolen sessions.
    conn = db.get_connection()
    conn.execute("UPDATE ops_tokens SET revoked = 1 WHERE username = ? AND revoked = 0", (username,))
    conn.commit()
    print(f"Password set for {username}; active sessions revoked")


def _set_active(username: str, active: bool) -> None:
    username = username.strip().lower()
    if not ops_auth.get_user(username):
        sys.exit(f"No such ops user: {username}")
    ops_auth.set_active(username, active)
    print(f"{username} {'activated' if active else 'deactivated (sessions revoked)'}")


def cmd_list(_a) -> None:
    rows = ops_auth.list_users()
    for r in rows:
        state = "active" if r["active"] else "INACTIVE"
        last = r.get("last_login_at") or "never"
        print(f"{r['username']:<14} {r['role']:<11} {state:<9} last-login {last}  ({r.get('name') or ''})")
    print(f"{len(rows)} ops user(s)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add", help="create an ops user (prompts for password)")
    add.add_argument("username")
    add.add_argument("name")
    add.add_argument("--role", default="dispatcher", choices=("admin", "dispatcher"))

    pw = sub.add_parser("password", help="(re)set a password + revoke sessions")
    pw.add_argument("username")

    for name in ("activate", "deactivate"):
        s = sub.add_parser(name, help=f"{name} an ops user")
        s.add_argument("username")

    sub.add_parser("list", help="list ops users")

    a = p.parse_args()
    db.init_db()
    if a.cmd == "add":
        cmd_add(a)
    elif a.cmd == "password":
        cmd_password(a)
    elif a.cmd == "activate":
        _set_active(a.username, True)
    elif a.cmd == "deactivate":
        _set_active(a.username, False)
    elif a.cmd == "list":
        cmd_list(a)


if __name__ == "__main__":
    main()
