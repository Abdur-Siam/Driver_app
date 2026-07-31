"""Data layer for the ops / dispatch console.

Read side: driver roster with live duty/shift/location, a tracking
snapshot for the map, and job monitoring. Write side: create + assign +
cancel jobs and message drivers. Every mutation writes an ops_audit row
(the console mirror of the driver audit invariant) and reuses the audited
driver-side primitives in `store` where they exist, so behaviour stays
consistent across both surfaces and the TOM merge is mechanical.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from . import config, store
from .auth import parse_iso
from .db import get_connection

_ACTIVE_JOB_STATUSES = ("IN PROGRESS", "ASSIGNED")


def _now() -> str:
    return store._now()


# ── Audit ────────────────────────────────────────────────────────────

def ops_audit(actor: str, action: str, target: Optional[str] = None,
              detail: Optional[dict] = None) -> None:
    conn = get_connection()
    conn.execute(
        "INSERT INTO ops_audit (ts, actor, action, target, detail_json) VALUES (?,?,?,?,?)",
        (_now(), actor, action, target, json.dumps(detail) if detail is not None else None),
    )
    conn.commit()


def _age_seconds(ts: Optional[str]) -> Optional[int]:
    dt = parse_iso(ts)
    if not dt:
        return None
    return max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))


# ── Drivers roster + live status ─────────────────────────────────────

def _active_job_count(driver_id) -> int:
    return get_connection().execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE driver_id = ? "
        "AND status NOT IN ('COMPLETED','CANCELLED')", (driver_id,),
    ).fetchone()["n"]


def _current_docket(driver_id) -> Optional[str]:
    row = get_connection().execute(
        "SELECT docket_number FROM jobs WHERE driver_id = ? "
        "AND status NOT IN ('COMPLETED','CANCELLED') "
        "ORDER BY (sequence_position IS NULL), sequence_position, deadline LIMIT 1",
        (driver_id,),
    ).fetchone()
    return row["docket_number"] if row else None


def list_drivers() -> List[Dict[str, Any]]:
    """Roster with live duty, shift, active-job count, latest fix and unread
    driver→ops messages — the dispatcher's at-a-glance view of the fleet."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT driver_id, name, callsign, vehicle, is_subcontracted, active, "
        "duty_status, phone, home_postcode FROM drivers ORDER BY name",
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        did = d["driver_id"]
        loc = store.latest_location(did)
        shift = store.get_active_shift(did)
        unread = conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE driver_id = ? "
            "AND direction = 'driver' AND read = 0", (did,)).fetchone()["n"]
        d["on_shift"] = bool(shift)
        d["shift"] = shift
        d["active_jobs"] = _active_job_count(did)
        d["current_docket"] = _current_docket(did)
        d["unread_from_driver"] = unread
        d["last_fix"] = loc
        # Guard against missing recorded_at in the latest_location result
        d["last_fix_age_s"] = _age_seconds(loc.get("recorded_at")) if loc else None
        out.append(d)
    return out


def driver_detail(driver_id) -> Optional[Dict[str, Any]]:
    drv = store.get_profile(driver_id)
    if not drv:
        return None
    # Never surface bank/PII into the tracking console — dispatch doesn't need it.
    for k in ("bank_sort_code", "bank_account_number", "bank_account_name",
              "utr_or_company_ref", "vat_number", "dob"):
        drv.pop(k, None)
    jobs = store.list_run(driver_id)
    return {
        "driver": drv,
        "shift": store.get_active_shift(driver_id),
        "jobs": jobs,
        "last_fix": store.latest_location(driver_id),
        "trail": location_trail(driver_id, 60),
    }


# ── Live tracking ────────────────────────────────────────────────────

# A fix older than this is flagged stale on the map snapshot — the marker is
# still shown (dispatch wants last-known position) but must not read as live.
STALE_FIX_S = 900


def tracking_snapshot() -> List[Dict[str, Any]]:
    """One row per driver that has a location fix — the map markers. Includes
    duty status and the job they're currently working so a dispatcher can see
    who is where and on what."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT driver_id, name, callsign, vehicle, duty_status FROM drivers",
    ).fetchall()
    out = []
    for r in rows:
        did = r["driver_id"]
        loc = store.latest_location(did)
        if not loc or loc.get("lat") is None or loc.get("lng") is None:
            continue
        age = _age_seconds(loc.get("recorded_at"))
        out.append({
            "driver_id": did, "name": r["name"], "callsign": r["callsign"],
            "vehicle": r["vehicle"], "duty_status": r["duty_status"],
            "on_shift": bool(store.get_active_shift(did)),
            "lat": loc.get("lat"), "lng": loc.get("lng"),
            "recorded_at": loc.get("recorded_at"), "age_s": age,
            "stale": age is None or age > STALE_FIX_S,
            "current_docket": _current_docket(did),
        })
    return out


def location_trail(driver_id, n: int = 60) -> List[Dict[str, Any]]:
    n = max(1, min(int(n or 60), 500))
    rows = get_connection().execute(
        "SELECT lat, lng, recorded_at, speed, heading FROM locations "
        "WHERE driver_id = ? AND lat IS NOT NULL AND lng IS NOT NULL "
        "ORDER BY id DESC LIMIT ?", (driver_id, n),
    ).fetchall()
    return list(reversed([dict(r) for r in rows]))


# ── Jobs (monitor + dispatch) ────────────────────────────────────────

def _job_progress(docket) -> Dict[str, Any]:
    conn = get_connection()
    drops = conn.execute(
        "SELECT status FROM drops WHERE docket_number = ?", (docket,)).fetchall()
    total = len(drops)
    done = sum(1 for d in drops if d["status"] == "delivered")
    failed = sum(1 for d in drops if d["status"] == "failed")
    parcels = conn.execute(
        "SELECT state FROM parcels WHERE docket_number = ?", (docket,)).fetchall()
    return {
        "drops_total": total, "drops_delivered": done, "drops_failed": failed,
        "parcels_total": len(parcels),
        "parcels_on_board": sum(1 for p in parcels if p["state"] in ("on_board", "delivered")),
        "parcels_delivered": sum(1 for p in parcels if p["state"] == "delivered"),
        "has_failure": failed > 0,
    }


def list_jobs(status: str = "active", limit: int = 200) -> List[Dict[str, Any]]:
    """status: active | unassigned | completed | all."""
    store.expire_offers()   # keep offer flags honest on every board read
    conn = get_connection()
    where = ""
    status = (status or "active").lower()
    if status == "active":
        where = "WHERE status NOT IN ('COMPLETED','CANCELLED')"
    elif status == "unassigned":
        where = "WHERE (driver_id IS NULL OR driver_id = '') AND status NOT IN ('COMPLETED','CANCELLED')"
    elif status == "completed":
        where = "WHERE status IN ('COMPLETED','CANCELLED')"
    rows = conn.execute(
        f"SELECT docket_number, driver_id, status, lifecycle_status, account, vehicle, "
        f"pickup_postcode, deadline, operational_date, driver_pay_final, requires_scan, "
        f"updated_at FROM jobs {where} "
        f"ORDER BY (status IN ('COMPLETED','CANCELLED')), (driver_id IS NULL) DESC, "
        f"deadline, docket_number LIMIT ?", (max(1, min(int(limit), 500)),),
    ).fetchall()
    out = []
    for r in rows:
        j = dict(r)
        # DB may store requires_scan as 1/0 or "1"/"0" or True/False; normalize to proper bool
        rs_val = j.get("requires_scan", 1)
        try:
            j["requires_scan"] = bool(int(rs_val))
        except (TypeError, ValueError):
            j["requires_scan"] = bool(rs_val)
        drv = store.get_driver(j["driver_id"]) if j.get("driver_id") else None
        j["driver_name"] = drv["name"] if drv else None
        j["progress"] = _job_progress(j["docket_number"])
        # Offer state only matters on the unassigned board (declined/expired
        # offers are exactly what a dispatcher must see and re-route).
        j["offer"] = latest_offer(j["docket_number"]) if not j.get("driver_id") else None
        out.append(j)
    return out


def job_full(docket) -> Optional[Dict[str, Any]]:
    out = store.get_job_admin(docket)
    if out is not None:
        store.expire_offers()
        out["offer"] = latest_offer(docket)
    return out


# ── Job offers (ops side) ────────────────────────────────────────────

def offer_job(docket, driver_id, actor: str, expires_in_s=None) -> Dict[str, Any]:
    """Offer an UNASSIGNED job to a driver with an accept-by countdown.
    One live offer per job — re-offering withdraws the previous one."""
    store.expire_offers()
    conn = get_connection()
    job = conn.execute("SELECT status, driver_id FROM jobs WHERE docket_number = ?",
                       (docket,)).fetchone()
    if not job:
        return {"ok": False, "reason": "job_not_found"}
    if job["status"] in ("COMPLETED", "CANCELLED"):
        return {"ok": False, "reason": "job_closed"}
    if job["driver_id"]:
        return {"ok": False, "reason": "job_assigned"}
    driver_id = (driver_id or "").strip()
    drv = store.get_driver(driver_id) if driver_id else None
    if not drv:
        return {"ok": False, "reason": "driver_not_found"}
    if not drv.get("active"):
        return {"ok": False, "reason": "driver_inactive"}
    try:
        ttl = int(expires_in_s)
    except (TypeError, ValueError):
        ttl = config.OFFER_TTL_S
    ttl = max(15, min(ttl if ttl > 0 else config.OFFER_TTL_S, 3600))
    expires = (datetime.now(timezone.utc) + timedelta(seconds=ttl)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("UPDATE offers SET status='withdrawn', responded_at=? "
                 "WHERE docket_number=? AND status='pending'", (_now(), docket))
    cur = conn.execute(
        "INSERT INTO offers (docket_number, driver_id, status, offered_by, offered_at, expires_at) "
        "VALUES (?,?,'pending',?,?,?)", (docket, driver_id, actor, _now(), expires))
    conn.commit()
    ops_audit(actor, "job:offer", docket,
              {"driver_id": driver_id, "expires_at": expires, "ttl_s": ttl})
    # Poll-driven by design; the push is a best-effort nudge (queues durably
    # without FCM credentials, never a dependency).
    from . import push
    push.notify_driver(driver_id, "job", "New job offer",
                       f"Job {docket} is on offer — open the app to accept before it expires.")
    return {"ok": True, "offer_id": cur.lastrowid, "docket": docket,
            "driver_id": driver_id, "expires_at": expires, "ttl_s": ttl}


def latest_offer(docket) -> Optional[Dict[str, Any]]:
    """Most recent offer for a docket (any state) — the board flag."""
    row = get_connection().execute(
        "SELECT * FROM offers WHERE docket_number = ? ORDER BY id DESC LIMIT 1", (docket,),
    ).fetchone()
    if not row:
        return None
    o = dict(row)
    drv = store.get_driver(o["driver_id"])
    return {"driver_id": o["driver_id"], "driver_name": drv["name"] if drv else None,
            "status": o["status"], "offered_at": o["offered_at"],
            "expires_at": o["expires_at"], "responded_at": o["responded_at"],
            "decline_reason": o["decline_reason"]}


_DOCKET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-]{2,39}$")


def _f2(v, default="0.00") -> str:
    try:
        return "%.2f" % float(v)
    except (TypeError, ValueError):
        return default


def _next_docket(conn) -> str:
    """XM-YYYYMMDD-NNNN, sequential within the operational day."""
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    prefix = f"XM-{day}-"
    row = conn.execute(
        "SELECT docket_number FROM jobs WHERE docket_number LIKE ? "
        "ORDER BY docket_number DESC LIMIT 1", (prefix + "%",)).fetchone()
    n = 1
    if row:
        try:
            n = int(row["docket_number"].rsplit("-", 1)[1]) + 1
        except (ValueError, IndexError):
            n = 1
    return f"{prefix}{n:04d}"


def create_job(payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
    """Create a dispatch job (optionally pre-assigned). Returns {ok, docket?,
    reason?}. Validates docket uniqueness, driver existence and at least one
    drop. Pay components are optional decimal strings; the final is their sum."""
    conn = get_connection()
    docket = (payload.get("docket_number") or "").strip()
    if docket:
        if not _DOCKET_RE.match(docket):
            return {"ok": False, "reason": "invalid_docket"}
        if conn.execute("SELECT 1 FROM jobs WHERE docket_number = ?", (docket,)).fetchone():
            return {"ok": False, "reason": "docket_exists"}
    else:
        docket = _next_docket(conn)

    driver_id = (payload.get("driver_id") or "").strip() or None
    if driver_id:
        drv = store.get_driver(driver_id)
        if not drv:
            return {"ok": False, "reason": "driver_not_found"}
        if not drv.get("active"):
            return {"ok": False, "reason": "driver_inactive"}

    drops = payload.get("drops") or []
    if not isinstance(drops, list) or not drops:
        return {"ok": False, "reason": "no_drops"}

    pickup = payload.get("pickup") or {}
    pay = payload.get("pay") or {}
    comp = {k: _f2(pay.get(k)) for k in ("base", "waiting", "toll", "extras", "bonus", "deduction")}
    final = "%.2f" % (sum(float(comp[k]) for k in ("base", "waiting", "toll", "extras", "bonus"))
                      - float(comp["deduction"]))
    requires_scan = 1 if payload.get("requires_scan", True) else 0
    lifecycle = "assigned" if driver_id else "unassigned"
    op_date = (payload.get("operational_date") or datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    conn.execute(
        "INSERT INTO jobs (docket_number, driver_id, status, lifecycle_status, account, vehicle, "
        "pickup_address, pickup_postcode, pickup_lat, pickup_lng, pickup_contact, pickup_notes, "
        "deadline, operational_date, special_instructions, requires_scan, sequence_position, "
        "route_version, driver_pay_final, base_pay, waiting_pay, toll_pay, extras_pay, bonus_pay, "
        "deduction, miles, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (docket, driver_id, "IN PROGRESS", lifecycle, payload.get("account"),
         payload.get("vehicle"), pickup.get("address"), pickup.get("postcode"),
         pickup.get("lat"), pickup.get("lng"), pickup.get("contact"), pickup.get("notes"),
         payload.get("deadline"), op_date, payload.get("special_instructions"), requires_scan,
         None, 1, final, comp["base"], comp["waiting"], comp["toll"], comp["extras"],
         comp["bonus"], comp["deduction"], payload.get("miles"), _now(), _now()),
    )
    for i, dr in enumerate(drops, start=1):
        if not isinstance(dr, dict):
            continue
        conn.execute(
            "INSERT INTO drops (docket_number, seq, address, postcode, lat, lng, contact, "
            "instructions, status) VALUES (?,?,?,?,?,?,?,?,'pending')",
            (docket, i, dr.get("address"), dr.get("postcode"), dr.get("lat"), dr.get("lng"),
             dr.get("contact"), dr.get("instructions")),
        )
        for pc in (dr.get("parcels") or []):
            bc = (pc.get("barcode") if isinstance(pc, dict) else str(pc)) or ""
            if not bc.strip():
                continue
            desc = pc.get("description") if isinstance(pc, dict) else None
            conn.execute(
                "INSERT INTO parcels (docket_number, drop_seq, barcode, description, state) "
                "VALUES (?,?,?,?,'expected')", (docket, i, bc.strip(), desc),
            )
    conn.commit()
    ops_audit(actor, "job:create", docket, {"driver_id": driver_id, "drops": len(drops)})
    if driver_id:
        _notify_assignment(driver_id, docket)
    return {"ok": True, "docket": docket, "driver_id": driver_id}


def assign_job(docket, driver_id, actor: str) -> Dict[str, Any]:
    """Assign / reassign / unassign a job. driver_id None or '' unassigns."""
    conn = get_connection()
    job = conn.execute("SELECT status, driver_id FROM jobs WHERE docket_number = ?",
                       (docket,)).fetchone()
    if not job:
        return {"ok": False, "reason": "job_not_found"}
    if job["status"] in ("COMPLETED", "CANCELLED"):
        return {"ok": False, "reason": "job_closed"}
    driver_id = (driver_id or "").strip() or None
    if driver_id:
        drv = store.get_driver(driver_id)
        if not drv:
            return {"ok": False, "reason": "driver_not_found"}
        if not drv.get("active"):
            return {"ok": False, "reason": "driver_inactive"}
    lifecycle = "assigned" if driver_id else "unassigned"
    conn.execute(
        "UPDATE jobs SET driver_id = ?, lifecycle_status = ?, sequence_position = NULL, "
        "updated_at = ? WHERE docket_number = ?", (driver_id, lifecycle, _now(), docket),
    )
    conn.commit()
    ops_audit(actor, "job:assign", docket, {"from": job["driver_id"], "to": driver_id})
    if driver_id:
        _notify_assignment(driver_id, docket)
    return {"ok": True, "docket": docket, "driver_id": driver_id}


def cancel_job(docket, actor: str) -> Dict[str, Any]:
    conn = get_connection()
    job = conn.execute("SELECT status, driver_id FROM jobs WHERE docket_number = ?",
                       (docket,)).fetchone()
    if not job:
        return {"ok": False, "reason": "job_not_found"}
    if job["status"] in ("COMPLETED", "CANCELLED"):
        return {"ok": False, "reason": "job_closed"}
    conn.execute(
        "UPDATE jobs SET status = 'CANCELLED', lifecycle_status = 'cancelled', updated_at = ? "
        "WHERE docket_number = ?", (_now(), docket),
    )
    conn.commit()
    ops_audit(actor, "job:cancel", docket, {"driver_id": job["driver_id"]})
    if job["driver_id"]:
        store.add_message(job["driver_id"], "ops",
                          f"Job {docket} has been cancelled by dispatch.", "system", docket)
    return {"ok": True, "docket": docket}


def _notify_assignment(driver_id, docket) -> None:
    """Tell the driver a job is theirs — an in-app message (which also raises a
    push via store.add_message) so the assignment surfaces on the device."""
    store.add_message(driver_id, "ops", f"New job assigned: {docket}. Tap to view your run.",
                      "job", docket)


# ── Messaging (ops ↔ driver, optionally per-job) ─────────────────────

def send_message(driver_id, text: str, actor: str, docket: Optional[str] = None) -> Dict[str, Any]:
    if not store.get_driver(driver_id):
        return {"ok": False, "reason": "driver_not_found"}
    text = (text or "").strip()
    if not text:
        return {"ok": False, "reason": "empty"}
    msg = store.add_message(driver_id, "ops", text, "chat", docket)
    ops_audit(actor, "message:send", driver_id, {"docket": docket})
    return {"ok": True, "message": msg}


def thread(driver_id, docket: Optional[str] = None) -> List[Dict[str, Any]]:
    msgs = store.list_messages(driver_id)
    if docket:
        # Support both possible message key names for docket ("docket_number" or "docket")
        msgs = [m for m in msgs if (m.get("docket_number") or m.get("docket")) == docket]
    return list(reversed(msgs))   # oldest-first for a chat view


def mark_thread_read(driver_id) -> int:
    conn = get_connection()
    cur = conn.execute(
        "UPDATE messages SET read = 1 WHERE driver_id = ? AND direction = 'driver' AND read = 0",
        (driver_id,))
    conn.commit()
    return cur.rowcount


# ── Dashboard ───────────────────────────────────────────────────────

def dashboard() -> Dict[str, Any]:
    conn = get_connection()

    def _n(sql, *p):
        return conn.execute(sql, p).fetchone()["n"]

    drivers_total = _n("SELECT COUNT(*) AS n FROM drivers WHERE active = 1")
    on_shift = _n("SELECT COUNT(*) AS n FROM shifts WHERE status = 'active'")
    active_jobs = _n("SELECT COUNT(*) AS n FROM jobs WHERE status NOT IN ('COMPLETED','CANCELLED')")
    unassigned = _n("SELECT COUNT(*) AS n FROM jobs WHERE (driver_id IS NULL OR driver_id = '') "
                    "AND status NOT IN ('COMPLETED','CANCELLED')")
    failed = _n("SELECT COUNT(DISTINCT docket_number) AS n FROM drops WHERE status = 'failed'")
    unread_msgs = _n("SELECT COUNT(*) AS n FROM messages WHERE direction = 'driver' AND read = 0")
    pending_changes = _n("SELECT COUNT(*) AS n FROM profile_change_requests WHERE status = 'pending'")
    data_reqs = _n("SELECT COUNT(*) AS n FROM data_requests WHERE status = 'received'")
    return {
        "drivers_total": drivers_total, "drivers_on_shift": on_shift,
        "active_jobs": active_jobs, "unassigned_jobs": unassigned,
        "jobs_with_failure": failed, "unread_driver_messages": unread_msgs,
        "pending_profile_changes": pending_changes, "open_data_requests": data_reqs,
        "now": _now(),
    }
