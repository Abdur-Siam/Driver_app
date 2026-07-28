"""Data access + job-lifecycle state machine for the Driver App.

Ownership is enforced everywhere: a driver may only read/mutate jobs
whose ``driver_id`` is theirs. The lifecycle mirrors TOM's audited
driver-job flow and extends it with per-drop scan/POD for multidrop.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .db import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Job-level lifecycle: action -> (required prev, new state)
LIFECYCLE = {
    "acknowledge":     ("assigned",        "acknowledged"),
    "en_route_pickup": ("acknowledged",    "en_route_pickup"),
    "arrive_pickup":   ("en_route_pickup", "at_pickup"),
    "collected":       ("at_pickup",       "pob"),
    "en_route_drop":   ("pob",             "en_route_drop"),
}
# Aliases accepted from the client.
ACTION_ALIASES = {
    "accept": "acknowledge", "start": "en_route_pickup",
    "arrived_pickup": "arrive_pickup", "pob": "collected",
    "depart_pickup": "en_route_drop", "start_delivery": "en_route_drop",
}


# ── Audit ────────────────────────────────────────────────────────────

def audit(driver_id, action, docket=None, detail=None, request_id=None) -> None:
    conn = get_connection()
    conn.execute(
        "INSERT INTO audit (ts, driver_id, action, docket_number, request_id, detail_json) "
        "VALUES (?,?,?,?,?,?)",
        (_now(), driver_id, action, docket, request_id,
         json.dumps(detail) if detail is not None else None),
    )
    conn.commit()


# ── Reads ────────────────────────────────────────────────────────────

def get_driver(driver_id) -> Optional[Dict[str, Any]]:
    row = get_connection().execute(
        "SELECT driver_id, name, callsign, vehicle, is_subcontracted, active, "
        "home_postcode, phone, location_consent_at, avatar_url, vehicle_photo_url "
        "FROM drivers WHERE driver_id = ?",
        (driver_id,),
    ).fetchone()
    return dict(row) if row else None


_PHOTO_KINDS = {"avatar": "avatar_url", "vehicle": "vehicle_photo_url"}


def set_profile_photo(driver_id, kind: str, ref: Optional[str]) -> bool:
    """Store an avatar or vehicle photo ref on the driver. Audited."""
    col = _PHOTO_KINDS.get(kind)
    if not col:
        return False
    conn = get_connection()
    conn.execute(f"UPDATE drivers SET {col} = ? WHERE driver_id = ?", (ref, driver_id))
    conn.commit()
    audit(driver_id, "profile:photo", detail={"kind": kind})
    return True


# ── Consent / push / data-subject requests (commercial hardening) ────

def set_location_consent(driver_id, granted: bool) -> Optional[str]:
    """Record or withdraw consent for location tracking. Returns the consent
    timestamp (None when withdrawn). Audited either way."""
    ts = _now() if granted else None
    conn = get_connection()
    conn.execute("UPDATE drivers SET location_consent_at = ? WHERE driver_id = ?", (ts, driver_id))
    conn.commit()
    audit(driver_id, "consent:location:" + ("grant" if granted else "withdraw"), detail={"at": ts})
    return ts


def has_location_consent(driver_id) -> bool:
    row = get_connection().execute(
        "SELECT location_consent_at FROM drivers WHERE driver_id = ?", (driver_id,),
    ).fetchone()
    return bool(row and row["location_consent_at"])


def register_device_token(driver_id, token: str, platform: Optional[str]) -> None:
    """Upsert a push token for this device (one row per token). Audited."""
    conn = get_connection()
    conn.execute(
        "INSERT INTO device_tokens (driver_id, token, platform, created_at, last_seen) "
        "VALUES (?,?,?,?,?) "
        "ON CONFLICT(token) DO UPDATE SET driver_id=excluded.driver_id, "
        "platform=excluded.platform, last_seen=excluded.last_seen",
        (driver_id, token, platform, _now(), _now()),
    )
    conn.commit()
    audit(driver_id, "push:register", detail={"platform": platform})


def create_data_request(driver_id, kind: str) -> Dict[str, Any]:
    """File a GDPR access/erasure request for ops to action. Audited."""
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO data_requests (driver_id, kind, status, requested_at) VALUES (?,?, 'received', ?)",
        (driver_id, kind, _now()),
    )
    conn.commit()
    audit(driver_id, "data_request:" + kind)
    return {"id": cur.lastrowid, "kind": kind, "status": "received"}


def _drops_for(docket) -> List[Dict[str, Any]]:
    rows = get_connection().execute(
        "SELECT * FROM drops WHERE docket_number = ? ORDER BY seq",
        (docket,),
    ).fetchall()
    return [dict(r) for r in rows]


def _parcels_for(docket) -> List[Dict[str, Any]]:
    rows = get_connection().execute(
        "SELECT * FROM parcels WHERE docket_number = ? ORDER BY drop_seq, id",
        (docket,),
    ).fetchall()
    return [dict(r) for r in rows]


def _job_row(docket, driver_id=None) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    if driver_id is None:
        row = conn.execute("SELECT * FROM jobs WHERE docket_number = ?", (docket,)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM jobs WHERE docket_number = ? AND driver_id = ?",
            (docket, driver_id),
        ).fetchone()
    return dict(row) if row else None


def _project_job(job: Dict[str, Any], full: bool = False) -> Dict[str, Any]:
    docket = job["docket_number"]
    drops = _drops_for(docket)
    parcels = _parcels_for(docket)
    expected = [p for p in parcels if p["drop_seq"] == 0] or parcels
    on_board = sum(1 for p in parcels if p["state"] in ("on_board", "delivered"))
    out = {
        "docket_number": docket,
        "status": job["status"],
        "lifecycle_status": job["lifecycle_status"] or "assigned",
        "account": job["account"],
        "vehicle": job["vehicle"],
        "deadline": job["deadline"],
        "operational_date": job["operational_date"],
        "sequence_position": job["sequence_position"],
        "route_version": job["route_version"],
        "driver_pay_final": job["driver_pay_final"],
        "pickup": {
            "address": job["pickup_address"], "postcode": job["pickup_postcode"],
            "lat": job["pickup_lat"], "lng": job["pickup_lng"],
            "contact": job["pickup_contact"], "notes": job["pickup_notes"],
        },
        "special_instructions": job["special_instructions"],
        "requires_scan": bool(job.get("requires_scan", 1)),
        "parcel_count": len(parcels),
        "parcels_on_board": on_board,
        "drops": [
            {
                "seq": d["seq"], "address": d["address"], "postcode": d["postcode"],
                "lat": d["lat"], "lng": d["lng"], "contact": d["contact"],
                "instructions": d["instructions"], "status": d["status"],
                "arrived_at": d.get("arrived_at"),
                "parcels": [
                    {"barcode": p["barcode"], "description": p["description"], "state": p["state"]}
                    for p in parcels if p["drop_seq"] == d["seq"]
                ],
            }
            for d in drops
        ],
    }
    if full:
        out["pod"] = [_pod_entry(d) for d in drops if d["status"] in ("delivered", "failed")]
    return out


def _pod_entry(d: Dict[str, Any]) -> Dict[str, Any]:
    # Failure photos live in fail_photo; rows written before that column
    # existed misfiled them in pod_photo — fall back so old rows stay readable.
    photo = ((d.get("fail_photo") or d["pod_photo"]) if d["status"] == "failed"
             else d["pod_photo"])
    return {
        "seq": d["seq"], "recipient": d["pod_recipient"], "at": d["pod_at"],
        "signature": d["pod_signature"], "photo": photo,
        "photos": json.loads(d["pod_photos"]) if d.get("pod_photos") else (
            [photo] if photo else []),
        "note": d["pod_note"], "fail_reason": d["fail_reason"],
        "lat": d.get("pod_lat"), "lng": d.get("pod_lng"),
    }


def list_run(driver_id, date=None) -> List[Dict[str, Any]]:
    conn = get_connection()
    # Optional ISO-date filter (?date=YYYY-MM-DD). 'today'/blank/junk = no filter,
    # preserving the historic behaviour of returning all active jobs.
    where, params = "", [driver_id]
    if isinstance(date, str):
        d = date.strip()
        if len(d) == 10 and d[4] == "-" and d[7] == "-":
            where = "AND operational_date = ? "
            params.append(d)
    rows = conn.execute(
        "SELECT docket_number FROM jobs WHERE driver_id = ? "
        "AND status NOT IN ('COMPLETED','CANCELLED') " + where +
        "ORDER BY (sequence_position IS NULL), sequence_position, deadline, docket_number",
        params,
    ).fetchall()
    return [_project_job(_job_row(r["docket_number"]), full=False) for r in rows]


def get_job(docket, driver_id) -> Optional[Dict[str, Any]]:
    job = _job_row(docket, driver_id)
    return _project_job(job, full=True) if job else None


def get_job_admin(docket) -> Optional[Dict[str, Any]]:
    """Ops-side read: full job projection with no driver-ownership filter, plus
    the assigned driver_id (dispatch needs to see who owns it)."""
    job = _job_row(docket)
    if not job:
        return None
    out = _project_job(job, full=True)
    out["driver_id"] = job["driver_id"]
    out["created_at"] = job["created_at"]
    out["updated_at"] = job["updated_at"]
    return out


# ── Lifecycle ────────────────────────────────────────────────────────

def advance_lifecycle(docket, driver_id, action) -> Dict[str, Any]:
    """Returns {ok, reason?, previous?, new?}. Reasons:
    not_found (job missing or not this driver's), invalid_action,
    invalid_transition, parcels_outstanding."""
    act = ACTION_ALIASES.get(action, action)
    if act not in LIFECYCLE:
        return {"ok": False, "reason": "invalid_action"}
    job = _job_row(docket, driver_id)
    if not job:
        return {"ok": False, "reason": "not_found"}
    if job["status"] in ("COMPLETED", "CANCELLED"):
        return {"ok": False, "reason": "invalid_transition"}

    prev = job["lifecycle_status"] or "assigned"
    required, new_state = LIFECYCLE[act]
    if prev != required:
        return {"ok": False, "reason": "invalid_transition"}

    # POB precondition — only on scan-required jobs: every expected parcel
    # must be scanned on board. Jobs without the requirement go straight to POB
    # (scanning remains available but optional).
    if act == "collected" and job.get("requires_scan", 1):
        parcels = _parcels_for(docket)
        if parcels and any(p["state"] == "expected" for p in parcels):
            return {"ok": False, "reason": "parcels_outstanding"}

    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET lifecycle_status = ?, updated_at = ? WHERE docket_number = ?",
        (new_state, _now(), docket),
    )
    conn.commit()
    audit(driver_id, "lifecycle:" + act, docket, {"from": prev, "to": new_state})
    return {"ok": True, "previous": prev, "new": new_state}


def _maybe_complete_job(docket, driver_id) -> bool:
    """Mark a job COMPLETED once every drop is resolved (delivered/failed)."""
    drops = _drops_for(docket)
    if drops and all(d["status"] in ("delivered", "failed") for d in drops):
        conn = get_connection()
        ts = _now()
        conn.execute(
            "UPDATE jobs SET status = 'COMPLETED', lifecycle_status = 'completed', "
            "completed_at = ?, updated_at = ? WHERE docket_number = ?",
            (ts, ts, docket),
        )
        conn.commit()
        audit(driver_id, "job:completed", docket)
        return True
    return False


# ── Scanning ─────────────────────────────────────────────────────────

def scan_parcel(docket, driver_id, drop_seq, phase, barcode, entry="scan",
                lat=None, lng=None) -> Dict[str, Any]:
    """phase: 'collect' | 'deliver'. Returns {ok, match, parcel_state?,
    remaining_expected?, reason?}."""
    job = _job_row(docket, driver_id)
    if not job:
        return {"ok": False, "reason": "not_found"}
    if not isinstance(barcode, str) or not barcode.strip():
        return {"ok": False, "reason": "invalid_barcode"}
    barcode = barcode.strip()
    conn = get_connection()
    parcels = _parcels_for(docket)

    match = "unexpected"
    parcel_state = None
    target = next((p for p in parcels if p["barcode"] == barcode), None)

    if phase == "collect":
        if target is None:
            match = "unexpected"
        elif target["state"] in ("on_board", "delivered"):
            match = "duplicate"
            parcel_state = target["state"]
        else:
            conn.execute("UPDATE parcels SET state = 'on_board' WHERE id = ?", (target["id"],))
            match = "expected"
            parcel_state = "on_board"
    elif phase == "deliver":
        if target is None:
            match = "unexpected"
        elif drop_seq is not None and int(target["drop_seq"]) != int(drop_seq) and int(target["drop_seq"]) != 0:
            match = "wrong_drop"
        elif target["state"] == "delivered":
            match = "duplicate"
            parcel_state = "delivered"
        else:
            conn.execute("UPDATE parcels SET state = 'delivered' WHERE id = ?", (target["id"],))
            match = "expected"
            parcel_state = "delivered"
    else:
        return {"ok": False, "reason": "invalid_phase"}

    conn.execute(
        "INSERT INTO parcel_events (event_id, docket_number, drop_seq, phase, barcode, "
        "entry, match_result, driver_id, recorded_at, lat, lng) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), docket, drop_seq, phase, barcode,
         entry if entry in ("scan", "manual") else "scan", match, driver_id, _now(), lat, lng),
    )
    conn.commit()
    audit(driver_id, "scan:" + phase, docket,
          {"barcode": barcode, "match": match, "drop_seq": drop_seq})

    remaining = sum(1 for p in _parcels_for(docket) if p["state"] == "expected")
    return {"ok": True, "match": match, "parcel_state": parcel_state,
            "remaining_expected": remaining}


# ── POD / failure ────────────────────────────────────────────────────

def capture_pod(docket, driver_id, drop_seq, recipient, signature_ref,
                photo_ref, note, lat=None, lng=None, photo_refs=None) -> Dict[str, Any]:
    # photo_refs: full list of delivery photos; photo_ref kept for the first
    # (back-compat with the single pod_photo column + existing readers).
    photos = [r for r in (photo_refs or ([photo_ref] if photo_ref else [])) if r]
    photo_ref = photos[0] if photos else None
    job = _job_row(docket, driver_id)
    if not job:
        return {"ok": False, "reason": "not_found"}
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM drops WHERE docket_number = ? AND seq = ?", (docket, drop_seq),
    ).fetchone()
    if not row:
        return {"ok": False, "reason": "drop_not_found"}
    if row["status"] in ("delivered", "failed"):
        return {"ok": False, "reason": "already_resolved"}
    # Scan-required jobs: this drop's parcels must be deliver-scanned before POD.
    # (drop_seq 0 = job-level parcels deliverable at any drop; not gated per drop.)
    if job.get("requires_scan", 1):
        outstanding = [p for p in _parcels_for(docket)
                       if int(p["drop_seq"]) == int(drop_seq) and p["state"] != "delivered"]
        if outstanding:
            return {"ok": False, "reason": "parcels_outstanding"}
    # Persist where the POD was captured (validated — junk coords store NULL,
    # never a fake position on a legal proof-of-delivery record).
    pod_lat, pod_lng = _coord(lat, -90, 90), _coord(lng, -180, 180)
    if pod_lat is None or pod_lng is None:
        pod_lat = pod_lng = None
    conn.execute(
        "UPDATE drops SET status='delivered', pod_recipient=?, pod_signature=?, "
        "pod_photo=?, pod_photos=?, pod_note=?, pod_at=?, pod_lat=?, pod_lng=? WHERE id=?",
        (recipient, signature_ref, photo_ref, json.dumps(photos) if photos else None,
         note, _now(), pod_lat, pod_lng, row["id"]),
    )
    conn.commit()
    audit(driver_id, "pod", docket, {"drop_seq": drop_seq, "recipient": recipient})
    completed = _maybe_complete_job(docket, driver_id)
    return {"ok": True, "drop_seq": drop_seq, "job_completed": completed}


def fail_drop(docket, driver_id, drop_seq, reason, note=None, photo_ref=None) -> Dict[str, Any]:
    job = _job_row(docket, driver_id)
    if not job:
        return {"ok": False, "reason": "not_found"}
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM drops WHERE docket_number = ? AND seq = ?", (docket, drop_seq),
    ).fetchone()
    if not row:
        return {"ok": False, "reason": "drop_not_found"}
    if row["status"] in ("delivered", "failed"):
        return {"ok": False, "reason": "already_resolved"}
    # The failure photo has its own column (fail_photo). Rows written before it
    # existed carry the photo in pod_photo — readers fall back (see _project_job).
    conn.execute(
        "UPDATE drops SET status='failed', fail_reason=?, fail_note=?, fail_photo=?, "
        "pod_at=? WHERE id=?",
        (reason, note, photo_ref, _now(), row["id"]),
    )
    conn.commit()
    audit(driver_id, "fail", docket, {"drop_seq": drop_seq, "reason": reason})
    completed = _maybe_complete_job(docket, driver_id)
    return {"ok": True, "drop_seq": drop_seq, "job_completed": completed}


def arrive_at_drop(docket, driver_id, drop_seq, lat=None, lng=None) -> Dict[str, Any]:
    """Mark the driver arrived at a drop (pending → arrived). Arrival is an
    optional waypoint before the POD/failure flow: an 'arrived' drop is NOT
    resolved, so _maybe_complete_job semantics are untouched. Ordering is
    enforced — the job must be loaded (pob / en_route_drop) and the drop
    still unresolved. POD directly from 'pending' stays allowed (offline
    outboxes may replay out of order; arrival must never wedge a delivery)."""
    job = _job_row(docket, driver_id)
    if not job:
        return {"ok": False, "reason": "not_found"}
    if job["status"] in ("COMPLETED", "CANCELLED"):
        return {"ok": False, "reason": "invalid_state"}
    if (job["lifecycle_status"] or "assigned") not in ("pob", "en_route_drop"):
        return {"ok": False, "reason": "invalid_state"}
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM drops WHERE docket_number = ? AND seq = ?", (docket, drop_seq),
    ).fetchone()
    if not row:
        return {"ok": False, "reason": "drop_not_found"}
    if row["status"] in ("delivered", "failed"):
        return {"ok": False, "reason": "already_resolved"}
    if row["status"] == "arrived":
        return {"ok": False, "reason": "already_arrived"}
    ts = _now()
    conn.execute("UPDATE drops SET status='arrived', arrived_at=? WHERE id=?", (ts, row["id"]))
    conn.commit()
    audit(driver_id, "drop:arrived", docket,
          {"drop_seq": drop_seq, "lat": _coord(lat, -90, 90), "lng": _coord(lng, -180, 180)})
    return {"ok": True, "drop_seq": drop_seq, "arrived_at": ts}


# ── Location ─────────────────────────────────────────────────────────

def _coord(v, lo: float, hi: float) -> Optional[float]:
    """A finite number within [lo, hi], else None."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")) or f < lo or f > hi:
        return None
    return f


def _numeric(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (f != f or f in (float("inf"), float("-inf"))) else f


def record_locations(driver_id, pings: List[Dict[str, Any]]) -> int:
    if not isinstance(pings, list):
        return 0
    conn = get_connection()
    n = 0
    recv = _now()
    for p in pings:
        if not isinstance(p, dict):
            continue
        # Validate coordinates: a ping with a missing/non-numeric/out-of-range
        # lat or lng is junk — skip it rather than store an unusable row.
        lat, lng = _coord(p.get("lat"), -90, 90), _coord(p.get("lng"), -180, 180)
        if lat is None or lng is None:
            continue
        # INSERT OR IGNORE on the (driver_id, ping_id) unique index: a batch
        # replayed after a flaky reconnect (the offline buffer re-sends until it
        # gets a 2xx) never double-inserts a point. A ping with no id (legacy)
        # is not deduped — NULL ping_ids are distinct — so it still stores.
        cur = conn.execute(
            "INSERT OR IGNORE INTO locations (driver_id, ping_id, docket_number, "
            "recorded_at, received_at, lat, lng, speed, heading, accuracy, battery) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (driver_id, p.get("id"), p.get("job") or p.get("docket"), p.get("t"),
             recv, lat, lng, _numeric(p.get("spd")), _numeric(p.get("hdg")),
             _numeric(p.get("acc")), _numeric(p.get("bat"))),
        )
        n += max(cur.rowcount, 0)  # count only rows actually inserted (0 if deduped)
    conn.commit()
    return n


def latest_location(driver_id) -> Optional[Dict[str, Any]]:
    row = get_connection().execute(
        "SELECT lat, lng, recorded_at FROM locations WHERE driver_id = ? "
        "ORDER BY id DESC LIMIT 1", (driver_id,),
    ).fetchone()
    return dict(row) if row else None


# ── Media ownership ──────────────────────────────────────────────────

def media_owner(name: str) -> Optional[str]:
    """Which driver a stored media file belongs to (None if unknown). Media
    refs are recorded as 'media/<name>' on expenses receipts, driver profile
    photos and drop POD fields (owner = the job's driver)."""
    ref = "media/" + name
    conn = get_connection()
    r = conn.execute("SELECT driver_id FROM expenses WHERE receipt = ? LIMIT 1", (ref,)).fetchone()
    if r:
        return r["driver_id"]
    r = conn.execute(
        "SELECT driver_id FROM drivers WHERE avatar_url = ? OR vehicle_photo_url = ? LIMIT 1",
        (ref, ref),
    ).fetchone()
    if r:
        return r["driver_id"]
    r = conn.execute(
        "SELECT j.driver_id AS driver_id FROM drops d "
        "JOIN jobs j ON j.docket_number = d.docket_number "
        "WHERE d.pod_signature = ? OR d.pod_photo = ? OR d.fail_photo = ? "
        "OR d.pod_photos LIKE ? LIMIT 1",
        (ref, ref, ref, '%"' + ref + '"%'),
    ).fetchone()
    if r:
        return r["driver_id"]
    r = conn.execute(
        "SELECT driver_id FROM vehicle_checks WHERE photo_ref = ? LIMIT 1", (ref,),
    ).fetchone()
    return r["driver_id"] if r else None


# ── Route order ──────────────────────────────────────────────────────

def set_route_order(driver_id, ordered_dockets: List[str], version: int) -> None:
    conn = get_connection()
    for pos, docket in enumerate(ordered_dockets, start=1):
        conn.execute(
            "UPDATE jobs SET sequence_position = ?, route_version = ? "
            "WHERE docket_number = ? AND driver_id = ?",
            (pos, version, docket, driver_id),
        )
    conn.commit()
    audit(driver_id, "route:optimise", None, {"order": ordered_dockets, "version": version})


def stops_for_routing(driver_id) -> List[Dict[str, Any]]:
    """Pickup + first-drop coordinates per active job, for optimisation."""
    out = []
    for j in list_run(driver_id):
        pu = j["pickup"]
        out.append({
            "docket_number": j["docket_number"],
            "lat": pu.get("lat"), "lng": pu.get("lng"),
            "deadline": j.get("deadline"),
        })
    return out


# ── Messages ─────────────────────────────────────────────────────────

def list_messages(driver_id) -> List[Dict[str, Any]]:
    rows = get_connection().execute(
        "SELECT id, ts, direction, text, category, read, docket_number FROM messages "
        "WHERE driver_id = ? ORDER BY ts DESC, id DESC LIMIT 100", (driver_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_messages_read(driver_id) -> int:
    """Driver-side mark-read: flag every ops→driver message as read (the
    mirror of ops_store.mark_thread_read, which covers driver→ops)."""
    conn = get_connection()
    cur = conn.execute(
        "UPDATE messages SET read = 1 WHERE driver_id = ? AND direction = 'ops' AND read = 0",
        (driver_id,))
    conn.commit()
    return cur.rowcount


def add_message(driver_id, direction, text, category=None, docket=None) -> Dict[str, Any]:
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO messages (driver_id, ts, direction, text, category, read, docket_number) "
        "VALUES (?,?,?,?,?,0,?)",
        (driver_id, _now(), direction, text, category, docket),
    )
    conn.commit()
    if direction == "ops":
        # Ops → driver messages raise a push (delivered live or durably queued).
        from . import push
        push.notify_driver(driver_id, "message", "Message from ops", text)
    return {"id": cur.lastrowid, "ts": _now(), "direction": direction, "text": text,
            "category": category, "docket_number": docket}


# ── Shift & availability (legacy ECHO app model) ─────────────────────

def get_active_shift(driver_id) -> Optional[Dict[str, Any]]:
    r = get_connection().execute(
        "SELECT id, start_at, planned_end, status FROM shifts "
        "WHERE driver_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1", (driver_id,),
    ).fetchone()
    return dict(r) if r else None


def last_shift_ended_at(driver_id) -> Optional[str]:
    """When the driver's most recent shift ended (None if never on shift).
    Used to accept late-flushed GPS pings recorded during that shift."""
    r = get_connection().execute(
        "SELECT ended_at FROM shifts WHERE driver_id = ? AND status = 'ended' "
        "ORDER BY id DESC LIMIT 1", (driver_id,),
    ).fetchone()
    return r["ended_at"] if r else None


def start_shift(driver_id, planned_end) -> Dict[str, Any]:
    conn = get_connection()
    # Close any stale active shift first.
    conn.execute("UPDATE shifts SET status='ended', ended_at=? WHERE driver_id=? AND status='active'",
                 (_now(), driver_id))
    cur = conn.execute(
        "INSERT INTO shifts (driver_id, start_at, planned_end, status) VALUES (?,?,?,'active')",
        (driver_id, _now(), planned_end),
    )
    conn.execute("UPDATE drivers SET duty_status='available' WHERE driver_id=?", (driver_id,))
    conn.commit()
    audit(driver_id, "shift:start", detail={"planned_end": planned_end})
    return {"id": cur.lastrowid, "start_at": _now(), "planned_end": planned_end, "status": "active"}


def end_shift(driver_id) -> bool:
    conn = get_connection()
    cur = conn.execute("UPDATE shifts SET status='ended', ended_at=? WHERE driver_id=? AND status='active'",
                       (_now(), driver_id))
    conn.execute("UPDATE drivers SET duty_status='off' WHERE driver_id=?", (driver_id,))
    conn.commit()
    if cur.rowcount:
        audit(driver_id, "shift:end")
    return cur.rowcount > 0


def set_duty_status(driver_id, status) -> Dict[str, Any]:
    if status not in ("available", "going_home", "off"):
        return {"ok": False, "reason": "invalid_status"}
    conn = get_connection()
    conn.execute("UPDATE drivers SET duty_status=? WHERE driver_id=?", (status, driver_id))
    conn.commit()
    audit(driver_id, "duty:status", detail={"status": status})
    return {"ok": True, "status": status}


# ── Job history ──────────────────────────────────────────────────────

def list_history(driver_id, limit=50) -> List[Dict[str, Any]]:
    rows = get_connection().execute(
        "SELECT docket_number, account, vehicle, operational_date, driver_pay_final, status, updated_at "
        "FROM jobs WHERE driver_id = ? AND status IN ('COMPLETED','CANCELLED') "
        "ORDER BY updated_at DESC LIMIT ?", (driver_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Profile ──────────────────────────────────────────────────────────

_PROFILE_FIELDS = (
    "driver_id", "name", "callsign", "vehicle", "is_subcontracted", "active", "home_postcode",
    "phone", "email", "address", "dob", "emergency_name", "emergency_phone",
    "vehicle_reg", "vehicle_make", "vehicle_model",
    "utr_or_company_ref", "vat_registered", "vat_number", "pay_cycle",
    "bank_sort_code", "bank_account_number", "bank_account_name", "bank_status",
    "rating", "acceptance_pct", "completion_pct", "on_time_pct",
    "notify_jobs", "notify_pay", "notify_msgs", "nav_app",
    "duty_status", "theme", "sound_alert_conn", "biometric", "location_consent_at",
    "avatar_url", "vehicle_photo_url",
)
# Fields a driver can change live; everything sensitive goes via review.
_DIRECT_EDIT = {"phone", "email", "address", "emergency_name", "emergency_phone",
                "vehicle_reg", "vehicle_make", "vehicle_model",
                "notify_jobs", "notify_pay", "notify_msgs", "nav_app",
                "theme", "sound_alert_conn", "biometric"}
_SENSITIVE_EDIT = {"bank_sort_code", "bank_account_number", "bank_account_name",
                   "vat_number", "vat_registered", "utr_or_company_ref"}


def get_profile(driver_id) -> Optional[Dict[str, Any]]:
    row = get_connection().execute(
        "SELECT " + ", ".join(_PROFILE_FIELDS) + " FROM drivers WHERE driver_id = ?", (driver_id,),
    ).fetchone()
    return dict(row) if row else None


def get_pending_changes(driver_id) -> List[Dict[str, Any]]:
    rows = get_connection().execute(
        "SELECT field, new_value, status, requested_at FROM profile_change_requests "
        "WHERE driver_id = ? AND status = 'pending' ORDER BY id DESC", (driver_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def update_profile(driver_id, fields: Dict[str, Any]) -> Dict[str, Any]:
    """Direct fields update live + audit; sensitive fields raise a pending
    review request (live value unchanged). Returns {updated, pending}."""
    conn = get_connection()
    cur = get_profile(driver_id) or {}
    updated, pending = [], []
    for k, v in (fields or {}).items():
        if k in _DIRECT_EDIT:
            conn.execute(f"UPDATE drivers SET {k} = ? WHERE driver_id = ?", (v, driver_id))
            updated.append(k)
        elif k in _SENSITIVE_EDIT:
            if str(cur.get(k)) == str(v):
                continue
            conn.execute(
                "INSERT INTO profile_change_requests (driver_id, field, old_value, new_value, status, requested_at) "
                "VALUES (?,?,?,?,'pending',?)",
                (driver_id, k, str(cur.get(k)), str(v), _now()),
            )
            pending.append(k)
    conn.commit()
    if updated:
        audit(driver_id, "profile:update", detail={"fields": updated})
    if pending:
        audit(driver_id, "profile:change_requested", detail={"fields": pending})
    return {"updated": updated, "pending": pending}
