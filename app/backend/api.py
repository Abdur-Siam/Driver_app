"""The /api/driver/v1 surface for the standalone Driver App.

Token-authenticated JSON. Mirrors the contract in
Driver/03_TOM_Integration_API_Contract.md. Mutating calls honour
X-Idempotency-Key. Every state change is audited.
"""
from __future__ import annotations

import base64
import os
import time
import uuid
from functools import wraps

from flask import Blueprint, Response, g, jsonify, request

from . import auth, config, finance, pdf, push, routing, storage, store
from .db import get_connection

api = Blueprint("driver_api_v1", __name__, url_prefix="/api/driver/v1")

# ── throttles (per IP, per account, per driver) ──────────────────────
# DB-backed (rate_events): under multi-worker gunicorn an in-process dict
# would give every worker its own window (lockout N becomes N × workers)
# and forget lockouts on restart. Single-statement writes, so the pattern
# survives the TOM merge onto Postgres/PgBouncer unchanged.


def _rate_ok(key, limit, window):
    """Sliding-window limiter. Records the hit, prunes the expired ones, and
    answers whether this hit is within the allowance."""
    conn = get_connection()
    now = time.time()
    conn.execute("DELETE FROM rate_events WHERE bucket = ? AND ts <= ?", (key, now - window))
    conn.execute("INSERT INTO rate_events (bucket, ts) VALUES (?, ?)", (key, now))
    conn.commit()
    n = conn.execute("SELECT COUNT(*) AS n FROM rate_events WHERE bucket = ?",
                     (key,)).fetchone()["n"]
    return n <= limit


def _fail_bucket(identifier):
    # Lowercased: login identifiers are case-insensitive, so the lockout
    # counter must be too (else varying case multiplies the attempt budget).
    return "fail:" + str(identifier).strip().lower()


def _account_locked(identifier):
    conn = get_connection()
    cutoff = time.time() - config.LOGIN_LOCK_WINDOW_S
    n = conn.execute("SELECT COUNT(*) AS n FROM rate_events WHERE bucket = ? AND ts > ?",
                     (_fail_bucket(identifier), cutoff)).fetchone()["n"]
    return n >= config.LOGIN_LOCK_MAX


def _note_login_failure(identifier):
    conn = get_connection()
    bucket = _fail_bucket(identifier)
    conn.execute("DELETE FROM rate_events WHERE bucket = ? AND ts <= ?",
                 (bucket, time.time() - config.LOGIN_LOCK_WINDOW_S))
    conn.execute("INSERT INTO rate_events (bucket, ts) VALUES (?, ?)", (bucket, time.time()))
    conn.commit()


def _clear_login_failures(identifier):
    conn = get_connection()
    conn.execute("DELETE FROM rate_events WHERE bucket = ?", (_fail_bucket(identifier),))
    conn.commit()


def rate_limited(fn):
    """Per-driver sliding-window throttle on heavy/mutating endpoints. Generous
    so legitimate offline-outbox replay bursts pass; blocks abusive floods."""
    @wraps(fn)
    def w(*a, **k):
        key = "mut:" + str(getattr(g, "driver_id", "?")) + ":" + request.path
        if not _rate_ok(key, config.MUTATION_RATE_MAX, config.MUTATION_RATE_WINDOW_S):
            return _err("rate_limited", "Too many requests", 429, retryable=True)
        return fn(*a, **k)
    return w


def _err(code, message, status, retryable=False):
    return jsonify({"error": {"code": code, "message": message, "retryable": retryable}}), status


def _bearer():
    a = request.headers.get("Authorization", "") or ""
    return a[7:].strip() if a.startswith("Bearer ") else ""


def require_token(fn):
    @wraps(fn)
    def w(*a, **k):
        did = auth.resolve_token(_bearer())
        if not did:
            return _err("unauthorized", "Authentication required", 401)
        g.driver_id = did
        return fn(*a, **k)
    return w


def _save_data_url(data_url, kind):
    """Persist a base64 data URL (signature/photo) to data/media, return its ref."""
    if not isinstance(data_url, str) or "," not in data_url or ";base64" not in data_url:
        return None
    try:
        header, b64 = data_url.split(",", 1)
        ext = "png" if "png" in header else ("jpg" if ("jpeg" in header or "jpg" in header) else "bin")
        os.makedirs(config.MEDIA_DIR, exist_ok=True)
        name = f"{kind}_{uuid.uuid4().hex}.{ext}"
        with open(os.path.join(config.MEDIA_DIR, name), "wb") as f:
            f.write(base64.b64decode(b64))
        return "media/" + name
    except Exception:
        return None


# ── meta / config ────────────────────────────────────────────────────

@api.route("/config", methods=["GET"])
def cfg():
    """Public bootstrap config for the frontend (no secrets — only whether
    a browser Maps key is present, and the key itself if so)."""
    return jsonify({
        "maps_browser_key": config.GOOGLE_MAPS_BROWSER_KEY or None,
        "maps_enabled": bool(config.GOOGLE_MAPS_BROWSER_KEY),
        "app_version": config.APP_VERSION,
        # Demo environment (seeded demo credentials exist) — the login screen
        # only shows the DRV001/test1234 hint when this is true.
        "demo": bool(config.SEED_DEMO),
    })


@api.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})


# ── auth ─────────────────────────────────────────────────────────────

@api.route("/auth/login", methods=["POST"])
def login():
    # remote_addr is the socket peer; behind a proxy, ProxyFix (server.py,
    # DRIVER_APP_TRUST_PROXY) has already resolved the real client from the
    # trusted hop count. X-Forwarded-For is never read raw — it's forgeable.
    ip = request.remote_addr or "?"
    if not _rate_ok("login:" + ip, config.LOGIN_RATE_MAX, config.LOGIN_RATE_WINDOW_S):
        return _err("rate_limited", "Too many attempts", 429, retryable=True)
    body = request.get_json(silent=True) or {}
    identifier = body.get("identifier") or body.get("driver_id")
    password = body.get("password")
    device = body.get("device") if isinstance(body.get("device"), dict) else {}
    # Per-account lockout (brute-force defence, complements the per-IP limiter).
    if identifier and _account_locked(identifier):
        return _err("account_locked", "Too many failed attempts — try again later", 429, retryable=True)
    did = auth.verify_credentials(identifier, password)
    if not did:
        if identifier:
            _note_login_failure(identifier)
        return _err("invalid_credentials", "Invalid login", 401)
    _clear_login_failures(identifier)      # clear failures on success
    label = " ".join(str(device.get(k)) for k in ("platform", "model")
                     if device.get(k)) or None
    issued = auth.issue_token(did, device.get("id"), label)
    drv = _sign_driver_photos(store.get_driver(did), did)
    store.audit(did, "auth:login", detail={"device": label})
    return jsonify({"token": issued["token"], "expires_at": issued["expires_at"], "driver": drv})


@api.route("/auth/logout", methods=["POST"])
@require_token
def logout():
    auth.revoke_token(_bearer())
    store.audit(g.driver_id, "auth:logout")
    return jsonify({"success": True})


def _sign_driver_photos(drv, did):
    if not drv:
        return drv
    for k in ("avatar_url", "vehicle_photo_url"):
        if drv.get(k):
            drv[k] = storage.sign_media_ref(drv[k], did)
    return drv


@api.route("/me", methods=["GET"])
@require_token
def me():
    return jsonify({"driver": _sign_driver_photos(store.get_driver(g.driver_id), g.driver_id)})


# ── run / jobs ───────────────────────────────────────────────────────

@api.route("/run", methods=["GET"])
@require_token
def run():
    return jsonify({
        "date": request.args.get("date", "today"),
        "jobs": store.list_run(g.driver_id, request.args.get("date")),
    })


def _sign_pod_media(job, did):
    """Replace stored POD refs with short-lived signed URLs before returning."""
    for p in (job.get("pod") or []):
        if p.get("signature"):
            p["signature"] = storage.sign_media_ref(p["signature"], did)
        if p.get("photo"):
            p["photo"] = storage.sign_media_ref(p["photo"], did)
        if p.get("photos"):
            p["photos"] = [storage.sign_media_ref(r, did) for r in p["photos"] if r]
    return job


@api.route("/jobs/<docket>", methods=["GET"])
@require_token
def job_detail(docket):
    job = store.get_job(docket, g.driver_id)
    if not job:
        return _err("job_not_found", "Job not found", 404)
    return jsonify({"job": _sign_pod_media(job, g.driver_id)})


def _idempotent(route, fn):
    key = (request.headers.get("X-Idempotency-Key") or "").strip()
    if key:
        prior = auth.get_idempotent(key)
        if prior is not None:
            import json as _j
            try:
                body = _j.loads(prior.get("result_json") or "{}")
            except Exception:
                body = {}
            return jsonify(body), int(prior.get("status_code") or 200)
    resp, status = fn()
    if key:
        import json as _j
        try:
            auth.save_idempotent(key, g.driver_id, route, status,
                                 _j.dumps(resp.get_json()))
        except Exception:
            pass
    return resp, status


@api.route("/jobs/<docket>/status", methods=["POST"])
@require_token
def job_status(docket):
    def do():
        body = request.get_json(silent=True) or {}
        action = (body.get("action") or body.get("status") or "")
        action = action.strip().lower() if isinstance(action, str) else ""
        r = store.advance_lifecycle(docket, g.driver_id, action)
        if r["ok"]:
            return jsonify({"success": True, "docket_number": docket,
                            "previous_status": r["previous"], "new_status": r["new"]}), 200
        reason = r["reason"]
        if reason == "not_found":
            return _err("job_not_found", "Job not found", 404)
        if reason == "invalid_action":
            return _err("invalid_action", "Unknown status action", 400)
        if reason == "parcels_outstanding":
            return _err("parcels_outstanding", "Scan all parcels before marking on board", 409)
        return _err("invalid_transition", "Status change not allowed from current state", 409)
    return _idempotent("status", do)


@api.route("/jobs/<docket>/scan", methods=["POST"])
@require_token
@rate_limited
def job_scan(docket):
    # Idempotent like status/pod/fail: a scan is queued through the app's
    # offline outbox with an X-Idempotency-Key, so a lost response that gets
    # replayed on reconnect MUST NOT insert a second parcel_events row. Without
    # this the parcel STATE is safe (a re-scan returns match=duplicate) but the
    # scan/GPS event trail double-inserts on every replayed scan.
    def do():
        body = request.get_json(silent=True) or {}
        r = store.scan_parcel(
            docket, g.driver_id, body.get("drop_seq"), body.get("phase"),
            body.get("barcode"), body.get("entry", "scan"),
            body.get("lat"), body.get("lng"),
        )
        if not r["ok"]:
            reason = r["reason"]
            if reason == "not_found":
                return _err("job_not_found", "Job not found", 404)
            return _err(reason, "Scan rejected", 400)
        return jsonify(r), 200
    return _idempotent("scan", do)


@api.route("/jobs/<docket>/pod", methods=["POST"])
@require_token
@rate_limited
def job_pod(docket):
    def do():
        body = request.get_json(silent=True) or {}
        sig = _save_data_url(body.get("signature"), "sig")
        # Accept one or many POD photos (gallery upload or camera), persist all.
        raw = body.get("photos") if isinstance(body.get("photos"), list) else [body.get("photo")]
        photos = [ref for ref in (_save_data_url(d, "pod") for d in (raw or [])) if ref]
        r = store.capture_pod(docket, g.driver_id, body.get("drop_seq"),
                              body.get("recipient_name"), sig, photos[0] if photos else None,
                              body.get("note"), body.get("lat"), body.get("lng"),
                              photo_refs=photos)
        if not r["ok"]:
            reason = r["reason"]
            if reason == "not_found":
                return _err("job_not_found", "Job not found", 404)
            if reason == "drop_not_found":
                return _err("drop_not_found", "Drop not found", 404)
            if reason == "parcels_outstanding":
                return _err("parcels_outstanding",
                            "This job requires barcode scanning — scan this drop's parcels before proof of delivery", 409)
            return _err(reason, "POD rejected", 409)
        return jsonify({"success": True, **r}), 200
    return _idempotent("pod", do)


@api.route("/jobs/<docket>/drops/<int:seq>/arrive", methods=["POST"])
@require_token
def drop_arrive(docket, seq):
    """Mark arrival at a drop (pending → arrived) — an audited waypoint before
    the POD/failure flow. Idempotency-keyed like the rest of the outbox verbs."""
    def do():
        body = request.get_json(silent=True) or {}
        r = store.arrive_at_drop(docket, g.driver_id, seq, body.get("lat"), body.get("lng"))
        if not r["ok"]:
            reason = r["reason"]
            if reason == "not_found":
                return _err("job_not_found", "Job not found", 404)
            if reason == "drop_not_found":
                return _err("drop_not_found", "Drop not found", 404)
            if reason == "already_arrived":
                return _err("already_arrived", "Already marked as arrived", 409)
            if reason == "already_resolved":
                return _err("already_resolved", "Drop already delivered or failed", 409)
            return _err("invalid_state", "Arrival not allowed from the job's current state", 409)
        return jsonify({"success": True, **r}), 200
    return _idempotent("arrive", do)


@api.route("/jobs/<docket>/fail", methods=["POST"])
@require_token
def job_fail(docket):
    def do():
        body = request.get_json(silent=True) or {}
        photo = _save_data_url(body.get("photo"), "fail")
        r = store.fail_drop(docket, g.driver_id, body.get("drop_seq"),
                           body.get("reason_code") or body.get("reason"),
                           body.get("note"), photo)
        if not r["ok"]:
            reason = r["reason"]
            if reason == "not_found":
                return _err("job_not_found", "Job not found", 404)
            if reason == "drop_not_found":
                return _err("drop_not_found", "Drop not found", 404)
            return _err(reason, "Failure rejected", 409)
        return jsonify({"success": True, **r}), 200
    return _idempotent("fail", do)


# ── job offers ───────────────────────────────────────────────────────

@api.route("/offers", methods=["GET"])
@require_token
def offers_get():
    """Live offers for this driver (poll-driven; expiry applied lazily)."""
    return jsonify({"offers": store.list_offers(g.driver_id), "now": store._now()})


@api.route("/offers/<int:offer_id>/accept", methods=["POST"])
@require_token
def offer_accept(offer_id):
    r = store.accept_offer(g.driver_id, offer_id)
    if not r["ok"]:
        if r["reason"] == "not_found":
            return _err("offer_not_found", "Offer not found", 404)
        if r["reason"] == "job_taken":
            return _err("job_taken", "This job is no longer available", 409)
        return _err("offer_closed", "This offer has expired or was already answered", 409)
    return jsonify({"success": True, **r})


@api.route("/offers/<int:offer_id>/decline", methods=["POST"])
@require_token
def offer_decline(offer_id):
    body = request.get_json(silent=True) or {}
    r = store.decline_offer(g.driver_id, offer_id, body.get("reason"))
    if not r["ok"]:
        if r["reason"] == "not_found":
            return _err("offer_not_found", "Offer not found", 404)
        return _err("offer_closed", "This offer has expired or was already answered", 409)
    return jsonify({"success": True, **r})


# ── location ─────────────────────────────────────────────────────────

@api.route("/location/batch", methods=["POST"])
@require_token
def location_batch():
    # GDPR: don't accept location data without recorded consent.
    if not store.has_location_consent(g.driver_id):
        return _err("consent_required", "Location consent not granted", 403)
    body = request.get_json(silent=True) or {}
    pings = body.get("pings") or []
    # Tracking is a shift-time activity: with no open shift, only pings whose
    # recorded timestamp falls before the last shift ended (the offline queue
    # legitimately flushes late) are accepted; anything newer is rejected with
    # 409 so the device knows to stop sending.
    if isinstance(pings, list) and pings and not store.get_active_shift(g.driver_id):
        cutoff = store.last_shift_ended_at(g.driver_id)
        pings = [p for p in pings
                 if isinstance(p, dict) and cutoff and p.get("t") and str(p["t"]) <= cutoff]
        if not pings:
            return _err("no_active_shift", "Not on shift — location not accepted", 409)
    n = store.record_locations(g.driver_id, pings)
    return jsonify({"accepted": n}), 202


# ── consent / push / data rights (commercial hardening) ───────────────

@api.route("/consent/location", methods=["POST"])
@require_token
def consent_location():
    body = request.get_json(silent=True) or {}
    granted = bool(body.get("granted"))
    ts = store.set_location_consent(g.driver_id, granted)
    return jsonify({"granted": granted, "location_consent_at": ts})


@api.route("/push/register", methods=["POST"])
@require_token
def push_register():
    body = request.get_json(silent=True) or {}
    token = (body.get("token") or "").strip()
    if not token:
        return _err("invalid_token", "Missing device token", 400)
    store.register_device_token(g.driver_id, token, body.get("platform"))
    return jsonify({"ok": True})


@api.route("/push/test", methods=["POST"])
@require_token
@rate_limited
def push_test():
    """Send this driver a test notification — lets a driver (or ops during
    device setup) confirm the push pipeline end-to-end. Without FCM
    credentials the message queues durably and the response says so."""
    r = push.notify_driver(g.driver_id, "message", "TOM Driver",
                           "Test notification — this device is registered for alerts.")
    store.audit(g.driver_id, "push:test", detail=r)
    return jsonify({"ok": True, "configured": push.configured(), **r})


@api.route("/account/data-request", methods=["POST"])
@require_token
@rate_limited
def account_data_request():
    body = request.get_json(silent=True) or {}
    kind = (body.get("kind") or "").strip().lower()
    if kind not in ("access", "erasure"):
        return _err("invalid_kind", "kind must be 'access' or 'erasure'", 400)
    return jsonify({"success": True, **store.create_data_request(g.driver_id, kind)}), 202


# ── route optimise ───────────────────────────────────────────────────

@api.route("/route/optimise", methods=["POST"])
@require_token
def route_optimise():
    body = request.get_json(silent=True) or {}
    frm = body.get("from") or {}
    drv = store.get_driver(g.driver_id) or {}
    # Origin: device position if given, else a sensible default (central London).
    origin = (frm.get("lat") if frm.get("lat") is not None else 51.5237,
              frm.get("lng") if frm.get("lng") is not None else -0.1075)
    stops = store.stops_for_routing(g.driver_id)
    if not stops:
        return jsonify({"ordered_dockets": [], "total_distance_km": 0, "engine": "none", "version": 1})
    result = routing.optimise(origin, stops)
    version = (max((s.get("sequence_position") or 0) for s in stops) or 0) + 1
    store.set_route_order(g.driver_id, result["ordered_dockets"], version)
    result["version"] = version
    return jsonify(result)


# ── messages ─────────────────────────────────────────────────────────

@api.route("/messages", methods=["GET"])
@require_token
def messages_get():
    return jsonify({"messages": store.list_messages(g.driver_id)})


@api.route("/messages", methods=["POST"])
@require_token
def messages_post():
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return _err("invalid_request", "Message text required", 400)
    category = body.get("category")
    # Optional per-job tag: a reply sent with a docket joins that job's thread
    # in the ops console. The driver must own the job (any status — a query
    # about completed/cancelled work is legitimate).
    docket = (body.get("docket_number") or body.get("docket") or "").strip() or None
    if docket and not store._job_row(docket, g.driver_id):
        return _err("job_not_found", "Job not found", 404)
    msg = store.add_message(g.driver_id, "driver", text, category, docket)
    store.audit(g.driver_id, "message:sent", detail={"category": category, "docket": docket})
    return jsonify({"success": True, "message": msg})


@api.route("/messages/read", methods=["POST"])
@require_token
def messages_read():
    """Mark every ops→driver message read (the inbox was opened)."""
    return jsonify({"success": True, "marked": store.mark_messages_read(g.driver_id)})


# ── shift & availability ─────────────────────────────────────────────

@api.route("/shift", methods=["GET"])
@require_token
def shift_get():
    drv = store.get_profile(g.driver_id) or {}
    return jsonify({"shift": store.get_active_shift(g.driver_id),
                    "duty_status": drv.get("duty_status", "off"),
                    "vehicle_check": store.latest_vehicle_check(g.driver_id),
                    "now": store._now()})


@api.route("/shift/start", methods=["POST"])
@require_token
def shift_start():
    body = request.get_json(silent=True) or {}
    pe = (body.get("planned_end") or "").strip()
    if not pe:
        return _err("invalid_request", "Shift end time required", 400)
    return jsonify({"success": True, "shift": store.start_shift(g.driver_id, pe)})


@api.route("/shift/end", methods=["POST"])
@require_token
def shift_end():
    """Ends the shift and returns its summary (None when no shift was open)."""
    summary = store.end_shift(g.driver_id)
    return jsonify({"success": True, "summary": summary})


@api.route("/shift/vehicle-check", methods=["POST"])
@require_token
@rate_limited
def shift_vehicle_check():
    """Shift-start walkaround: odometer + pass/fail per checklist item.
    Defects are advisory — they flag to ops but never block the shift."""
    body = request.get_json(silent=True) or {}
    photo = _save_data_url(body.get("photo"), "vcheck")
    r = store.save_vehicle_check(g.driver_id, body.get("odometer"),
                                 body.get("items"), body.get("defects"), photo)
    if not r["ok"]:
        reason = r["reason"]
        if reason == "no_active_shift":
            return _err("no_active_shift", "Start your shift first", 409)
        if reason == "invalid_odometer":
            return _err("invalid_odometer", "Enter a valid odometer reading", 400)
        return _err("invalid_items",
                    "Every checklist item needs a pass/fail answer", 400)
    return jsonify({"success": True, **r})


@api.route("/status", methods=["POST"])
@require_token
def duty_status():
    body = request.get_json(silent=True) or {}
    r = store.set_duty_status(g.driver_id, (body.get("status") or "").strip())
    if not r["ok"]:
        if r["reason"] == "no_active_shift":
            return _err("no_active_shift", "Start a shift before taking a break", 409)
        return _err("invalid_status", "Unknown status", 400)
    return jsonify({"success": True, **r})


@api.route("/history", methods=["GET"])
@require_token
def history():
    return jsonify({"jobs": store.list_history(g.driver_id)})


# ── home dashboard ───────────────────────────────────────────────────

@api.route("/home", methods=["GET"])
@require_token
def home():
    did = g.driver_id
    run = store.list_run(did)
    nxt = run[0] if run else None
    msgs = store.list_messages(did)
    drv = store.get_profile(did) or {}
    return jsonify({
        "driver": store.get_driver(did),
        "shift": store.get_active_shift(did),
        "duty_status": drv.get("duty_status", "off"),
        "today": finance.earnings_summary(did)["today"],
        "run_count": len(run),
        "next_stop": nxt and {"docket_number": nxt["docket_number"], "account": nxt["account"],
                              "deadline": nxt["deadline"],
                              "where": nxt["pickup"]["postcode"] if nxt["lifecycle_status"] in ("assigned", "acknowledged", "en_route_pickup") else (nxt["drops"][0]["postcode"] if nxt["drops"] else "")},
        "unread_messages": sum(1 for m in msgs if m["direction"] == "ops" and not m["read"]),
        "performance": finance.performance(did),
    })


# ── profile ──────────────────────────────────────────────────────────

@api.route("/profile", methods=["GET"])
@require_token
def profile_get():
    p = store.get_profile(g.driver_id)
    if not p:
        return _err("not_found", "Profile not found", 404)
    return jsonify({"profile": _sign_driver_photos(p, g.driver_id), "pending": store.get_pending_changes(g.driver_id)})


@api.route("/profile/photo", methods=["POST"])
@require_token
@rate_limited
def profile_photo():
    body = request.get_json(silent=True) or {}
    kind = (body.get("kind") or "").strip().lower()
    if kind not in ("avatar", "vehicle"):
        return _err("invalid_kind", "kind must be 'avatar' or 'vehicle'", 400)
    ref = _save_data_url(body.get("image"), kind)
    if not ref:
        return _err("invalid_image", "Expected a base64 image data URL", 400)
    store.set_profile_photo(g.driver_id, kind, ref)
    return jsonify({"success": True, "kind": kind, "url": storage.sign_media_ref(ref, g.driver_id)})


@api.route("/profile", methods=["POST", "PATCH"])
@require_token
def profile_update():
    body = request.get_json(silent=True) or {}
    res = store.update_profile(g.driver_id, body.get("fields") or body)
    return jsonify({"success": True, **res, "pending": store.get_pending_changes(g.driver_id)})


@api.route("/profile/password", methods=["POST"])
@require_token
def profile_password():
    body = request.get_json(silent=True) or {}
    current, new = body.get("current"), body.get("new")
    if not isinstance(new, str) or len(new) < 8:
        return _err("weak_password", "New password must be at least 8 characters", 400)
    if auth.verify_credentials(g.driver_id, current) != g.driver_id:
        return _err("invalid_credentials", "Current password is incorrect", 401)
    auth.set_password(g.driver_id, new)
    store.audit(g.driver_id, "auth:password_changed")
    return jsonify({"success": True})


# ── earnings / statements / tax / payout ─────────────────────────────

@api.route("/earnings", methods=["GET"])
@require_token
def earnings():
    return jsonify(finance.earnings_summary(g.driver_id))


@api.route("/statements", methods=["GET"])
@require_token
def statements():
    return jsonify({"statements": finance.list_statements(g.driver_id)})


@api.route("/statements/<sid>", methods=["GET"])
@require_token
def statement_detail(sid):
    s = finance.get_statement(g.driver_id, sid)
    if not s:
        return _err("not_found", "Statement not found", 404)
    return jsonify({"statement": s})


@api.route("/statements/<sid>/pdf", methods=["GET"])
@require_token
def statement_pdf(sid):
    s = finance.get_statement(g.driver_id, sid)
    if not s:
        return _err("not_found", "Statement not found", 404)
    drv = store.get_profile(g.driver_id) or {}
    data = pdf.render_statement_pdf(s, drv)
    store.audit(g.driver_id, "statement:download", detail={"statement_id": sid})
    fname = f"XtraMile_Statement_{(s.get('reference') or sid)}.pdf".replace(" ", "_")
    return Response(data, mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@api.route("/tax", methods=["GET"])
@require_token
def tax():
    return jsonify(finance.tax_summary(g.driver_id))


@api.route("/performance", methods=["GET"])
@require_token
def perf():
    return jsonify(finance.performance(g.driver_id))


@api.route("/payout", methods=["POST"])
@require_token
def payout():
    body = request.get_json(silent=True) or {}
    r = finance.request_payout(g.driver_id, body.get("amount"))
    if not r["ok"]:
        if r["reason"] == "exceeds_available":
            return _err("exceeds_available", f"Only {r['available']} available", 400)
        return _err("invalid_amount", "Enter a valid amount", 400)
    store.audit(g.driver_id, "payout:requested", detail={"amount": r["amount"]})
    return jsonify({"success": True, **r})


# ── expenses ─────────────────────────────────────────────────────────

@api.route("/expenses", methods=["GET"])
@require_token
def expenses_get():
    rows = finance.list_expenses(g.driver_id)
    for e in rows:
        if e.get("receipt"):
            e["receipt"] = storage.sign_media_ref(e["receipt"], g.driver_id)
    return jsonify({"expenses": rows})


@api.route("/expenses", methods=["POST"])
@require_token
def expenses_post():
    body = request.get_json(silent=True) or {}
    if finance._f(body.get("amount")) <= 0:
        return _err("invalid_amount", "Enter an amount", 400)
    receipt = _save_data_url(body.get("receipt"), "rcpt")
    r = finance.add_expense(g.driver_id, body.get("type") or "other", body.get("amount"),
                           body.get("date"), body.get("note"), receipt)
    store.audit(g.driver_id, "expense:added", detail={"type": body.get("type")})
    return jsonify({"success": True, **r})
