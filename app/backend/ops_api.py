"""The /api/ops/v1 surface for the dispatch console.

Token-authenticated JSON, kept entirely separate from the driver API:
different account table, different token table, its own login limiter and
lockout. Every mutating call writes an ops_audit row (in ops_store). The
console is a desktop web app served at /ops.
"""
from __future__ import annotations

import time
from functools import wraps

from flask import Blueprint, g, jsonify, request

from . import config, ops_auth, ops_store, store
from .db import get_connection

ops_api = Blueprint("ops_api_v1", __name__, url_prefix="/api/ops/v1")


# ── errors ───────────────────────────────────────────────────────────

def _err(code, message, status, retryable=False):
    return jsonify({"error": {"code": code, "message": message, "retryable": retryable}}), status


def _bearer():
    a = request.headers.get("Authorization", "") or ""
    return a[7:].strip() if a.startswith("Bearer ") else ""


# ── abuse control (DB-backed, shared across workers — same table/pattern
#    as the driver API so a leaked worker can't reset a lockout) ────────

def _rate_ok(key, limit, window):
    conn = get_connection()
    now = time.time()
    conn.execute("DELETE FROM rate_events WHERE bucket = ? AND ts <= ?", (key, now - window))
    conn.execute("INSERT INTO rate_events (bucket, ts) VALUES (?, ?)", (key, now))
    conn.commit()
    n = conn.execute("SELECT COUNT(*) AS n FROM rate_events WHERE bucket = ?", (key,)).fetchone()["n"]
    return n <= limit


def _fail_bucket(username):
    return "ops_fail:" + str(username).strip().lower()


def _account_locked(username):
    conn = get_connection()
    cutoff = time.time() - config.LOGIN_LOCK_WINDOW_S
    n = conn.execute("SELECT COUNT(*) AS n FROM rate_events WHERE bucket = ? AND ts > ?",
                     (_fail_bucket(username), cutoff)).fetchone()["n"]
    return n >= config.LOGIN_LOCK_MAX


def _note_login_failure(username):
    conn = get_connection()
    bucket = _fail_bucket(username)
    conn.execute("DELETE FROM rate_events WHERE bucket = ? AND ts <= ?",
                 (bucket, time.time() - config.LOGIN_LOCK_WINDOW_S))
    conn.execute("INSERT INTO rate_events (bucket, ts) VALUES (?, ?)", (bucket, time.time()))
    conn.commit()


def _clear_login_failures(username):
    conn = get_connection()
    conn.execute("DELETE FROM rate_events WHERE bucket = ?", (_fail_bucket(username),))
    conn.commit()


def require_ops(fn):
    @wraps(fn)
    def w(*a, **k):
        who = ops_auth.resolve_token(_bearer())
        if not who:
            return _err("unauthorized", "Authentication required", 401)
        g.ops_user = who["username"]
        g.ops_role = who["role"]
        return fn(*a, **k)
    return w


# ── meta / auth ──────────────────────────────────────────────────────

@ops_api.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})


@ops_api.route("/config", methods=["GET"])
def cfg():
    return jsonify({
        "app_version": config.APP_VERSION,
        "maps_browser_key": config.GOOGLE_MAPS_BROWSER_KEY or None,
        "maps_enabled": bool(config.GOOGLE_MAPS_BROWSER_KEY),
    })


@ops_api.route("/auth/login", methods=["POST"])
def login():
    ip = request.remote_addr or "?"
    if not _rate_ok("ops_login:" + ip, config.LOGIN_RATE_MAX, config.LOGIN_RATE_WINDOW_S):
        return _err("rate_limited", "Too many attempts", 429, retryable=True)
    body = request.get_json(silent=True) or {}
    username = body.get("username") or body.get("identifier")
    password = body.get("password")
    if username and _account_locked(username):
        return _err("account_locked", "Too many failed attempts — try again later", 429, retryable=True)
    who = ops_auth.verify_credentials(username, password)
    if not who:
        if username:
            _note_login_failure(username)
        return _err("invalid_credentials", "Invalid login", 401)
    _clear_login_failures(username)
    issued = ops_auth.issue_token(who, label=(request.headers.get("User-Agent") or "")[:120] or None)
    user = ops_auth.get_user(who)
    ops_store.ops_audit(who, "auth:login")
    return jsonify({"token": issued["token"], "expires_at": issued["expires_at"], "user": user})


@ops_api.route("/auth/logout", methods=["POST"])
@require_ops
def logout():
    ops_auth.revoke_token(_bearer())
    ops_store.ops_audit(g.ops_user, "auth:logout")
    return jsonify({"success": True})


@ops_api.route("/me", methods=["GET"])
@require_ops
def me():
    return jsonify({"user": ops_auth.get_user(g.ops_user)})


# ── dashboard ────────────────────────────────────────────────────────

@ops_api.route("/dashboard", methods=["GET"])
@require_ops
def dashboard():
    return jsonify(ops_store.dashboard())


# ── drivers + tracking ───────────────────────────────────────────────

@ops_api.route("/drivers", methods=["GET"])
@require_ops
def drivers():
    return jsonify({"drivers": ops_store.list_drivers()})


@ops_api.route("/drivers/<driver_id>", methods=["GET"])
@require_ops
def driver_detail(driver_id):
    d = ops_store.driver_detail(driver_id)
    if not d:
        return _err("not_found", "Driver not found", 404)
    return jsonify(d)


@ops_api.route("/tracking", methods=["GET"])
@require_ops
def tracking():
    return jsonify({"drivers": ops_store.tracking_snapshot(), "now": ops_store._now()})


@ops_api.route("/tracking/<driver_id>/trail", methods=["GET"])
@require_ops
def trail(driver_id):
    return jsonify({"trail": ops_store.location_trail(driver_id, request.args.get("n", 60))})


# ── jobs ─────────────────────────────────────────────────────────────

@ops_api.route("/jobs", methods=["GET"])
@require_ops
def jobs_list():
    return jsonify({"jobs": ops_store.list_jobs(request.args.get("status", "active"))})


@ops_api.route("/jobs", methods=["POST"])
@require_ops
def jobs_create():
    body = request.get_json(silent=True) or {}
    r = ops_store.create_job(body, g.ops_user)
    if not r["ok"]:
        codes = {
            "invalid_docket": ("invalid_docket", "Docket format is invalid", 400),
            "docket_exists": ("docket_exists", "That docket already exists", 409),
            "driver_not_found": ("driver_not_found", "Assigned driver not found", 404),
            "driver_inactive": ("driver_inactive", "That driver is not active", 409),
            "no_drops": ("no_drops", "Add at least one delivery drop", 400),
        }
        c, m, s = codes.get(r["reason"], (r["reason"], "Could not create job", 400))
        return _err(c, m, s)
    return jsonify({"success": True, **r}), 201


@ops_api.route("/jobs/<docket>", methods=["GET"])
@require_ops
def job_detail(docket):
    j = ops_store.job_full(docket)
    if not j:
        return _err("job_not_found", "Job not found", 404)
    return jsonify({"job": j})


@ops_api.route("/jobs/<docket>/assign", methods=["POST"])
@require_ops
def job_assign(docket):
    body = request.get_json(silent=True) or {}
    r = ops_store.assign_job(docket, body.get("driver_id"), g.ops_user)
    if not r["ok"]:
        codes = {
            "job_not_found": ("job_not_found", "Job not found", 404),
            "job_closed": ("job_closed", "Job is completed or cancelled", 409),
            "driver_not_found": ("driver_not_found", "Driver not found", 404),
            "driver_inactive": ("driver_inactive", "That driver is not active", 409),
        }
        c, m, s = codes.get(r["reason"], (r["reason"], "Could not assign", 400))
        return _err(c, m, s)
    return jsonify({"success": True, **r})


@ops_api.route("/jobs/<docket>/offer", methods=["POST"])
@require_ops
def job_offer(docket):
    """Offer an unassigned job to a driver (countdown accept/decline) instead
    of direct-assigning it. Decline/expiry returns it to the unassigned board
    flagged with the outcome."""
    body = request.get_json(silent=True) or {}
    r = ops_store.offer_job(docket, body.get("driver_id"), g.ops_user, body.get("expires_in_s"))
    if not r["ok"]:
        codes = {
            "job_not_found": ("job_not_found", "Job not found", 404),
            "job_closed": ("job_closed", "Job is completed or cancelled", 409),
            "job_assigned": ("job_assigned", "Unassign the job before offering it", 409),
            "driver_not_found": ("driver_not_found", "Driver not found", 404),
            "driver_inactive": ("driver_inactive", "That driver is not active", 409),
        }
        c, m, s = codes.get(r["reason"], (r["reason"], "Could not offer", 400))
        return _err(c, m, s)
    return jsonify({"success": True, **r}), 201


@ops_api.route("/jobs/<docket>/cancel", methods=["POST"])
@require_ops
def job_cancel(docket):
    r = ops_store.cancel_job(docket, g.ops_user)
    if not r["ok"]:
        codes = {
            "job_not_found": ("job_not_found", "Job not found", 404),
            "job_closed": ("job_closed", "Job is already completed or cancelled", 409),
        }
        c, m, s = codes.get(r["reason"], (r["reason"], "Could not cancel", 400))
        return _err(c, m, s)
    return jsonify({"success": True, **r})


# ── messaging (ops ↔ driver, optional per-job) ───────────────────────

@ops_api.route("/messages/<driver_id>", methods=["GET"])
@require_ops
def messages_get(driver_id):
    if not store.get_driver(driver_id):
        return _err("driver_not_found", "Driver not found", 404)
    return jsonify({"messages": ops_store.thread(driver_id, request.args.get("docket"))})


@ops_api.route("/messages/<driver_id>", methods=["POST"])
@require_ops
def messages_post(driver_id):
    body = request.get_json(silent=True) or {}
    r = ops_store.send_message(driver_id, body.get("text"), g.ops_user, body.get("docket"))
    if not r["ok"]:
        if r["reason"] == "driver_not_found":
            return _err("driver_not_found", "Driver not found", 404)
        return _err("invalid_request", "Message text required", 400)
    return jsonify({"success": True, **r})


@ops_api.route("/messages/<driver_id>/read", methods=["POST"])
@require_ops
def messages_read(driver_id):
    return jsonify({"success": True, "marked": ops_store.mark_thread_read(driver_id)})
