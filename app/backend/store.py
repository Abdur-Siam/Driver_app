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

from . import bridge
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

# Internal lifecycle action → TOM bridge event name ('acknowledge' is
# app-internal and deliberately absent from the bridge enum).
_BRIDGE_LIFECYCLE_EVENTS = {
    "en_route_pickup": "en_route_pickup", "arrive_pickup": "arrived_pickup",
    "collected": "pob", "en_route_drop": "en_route_drop",
}


def _callsign(driver_id) -> str:
    row = get_connection().execute(
        "SELECT callsign FROM drivers WHERE driver_id = ?", (driver_id,)).fetchone()
    return row["callsign"] if row else str(driver_id)


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
    if bridge.enabled() and act in _BRIDGE_LIFECYCLE_EVENTS:
        bridge.enqueue_status(docket, _callsign(driver_id), _BRIDGE_LIFECYCLE_EVENTS[act])
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

    scanned_at = _now()
    conn.execute(
        "INSERT INTO parcel_events (event_id, docket_number, drop_seq, phase, barcode, "
        "entry, match_result, driver_id, recorded_at, lat, lng) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), docket, drop_seq, phase, barcode,
         entry if entry in ("scan", "manual") else "scan", match, driver_id, scanned_at, lat, lng),
    )
    conn.commit()
    audit(driver_id, "scan:" + phase, docket,
          {"barcode": barcode, "match": match, "drop_seq": drop_seq})
    if bridge.enabled():
        bridge.enqueue_scans(docket, [{
            "barcode": barcode, "event_type": phase,
            "lat": _coord(lat, -90, 90), "lng": _coord(lng, -180, 180),
            "scanned_at": scanned_at,
        }])

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
    ts = _now()
    conn.execute(
        "UPDATE drops SET status='delivered', pod_recipient=?, pod_signature=?, "
        "pod_photo=?, pod_photos=?, pod_note=?, pod_at=?, pod_lat=?, pod_lng=? WHERE id=?",
        (recipient, signature_ref, photo_ref, json.dumps(photos) if photos else None,
         note, ts, pod_lat, pod_lng, row["id"]),
    )
    conn.commit()
    audit(driver_id, "pod", docket, {"drop_seq": drop_seq, "recipient": recipient})
    completed = _maybe_complete_job(docket, driver_id)
    if bridge.enabled():
        cs = _callsign(driver_id)
        bridge.enqueue_status(docket, cs, "delivered", at=ts,
                              meta={"drop_seq": drop_seq, "job_completed": completed})
        bridge.enqueue_pod(docket, recipient, ts, pod_lat, pod_lng, signature_ref, photos)
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
    ts = _now()
    conn.execute(
        "UPDATE drops SET status='failed', fail_reason=?, fail_note=?, fail_photo=?, "
        "pod_at=? WHERE id=?",
        (reason, note, photo_ref, ts, row["id"]),
    )
    conn.commit()
    audit(driver_id, "fail", docket, {"drop_seq": drop_seq, "reason": reason})
    completed = _maybe_complete_job(docket, driver_id)
    if bridge.enabled():
        bridge.enqueue_status(docket, _callsign(driver_id), "failed", at=ts,
                              meta={"drop_seq": drop_seq, "reason": reason,
                                    "job_completed": completed})
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
    if bridge.enabled():
        bridge.enqueue_status(docket, _callsign(driver_id), "arrived_drop", at=ts,
                              meta={"drop_seq": drop_seq})
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
    bridging = bridge.enabled()
    accepted = []          # newly-stored points, batched onward to TOM
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
        if bridging and cur.rowcount > 0:
            accepted.append({"lat": lat, "lng": lng,
                             "recorded_at": p.get("t"), "ping_id": p.get("id")})
    conn.commit()
    if accepted:
        # One bridge event per accepted batch (deduped replays enqueue nothing).
        bridge.enqueue_locations(_callsign(driver_id), accepted)
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


# ── Job offers (driver side) ─────────────────────────────────────────
# Dispatch can OFFER a job instead of direct-assigning it: the driver gets a
# countdown to accept or decline. Poll-driven by design (no FCM dependency):
# expiry is applied lazily on every read/mutation, never by a daemon.

def expire_offers() -> int:
    """Flag pending offers past their deadline as expired. Returns count."""
    conn = get_connection()
    now = _now()
    cur = conn.execute(
        "UPDATE offers SET status='expired', responded_at=? "
        "WHERE status='pending' AND expires_at <= ?", (now, now))
    conn.commit()
    return cur.rowcount


def _offer_row(offer_id, driver_id) -> Optional[Dict[str, Any]]:
    row = get_connection().execute(
        "SELECT * FROM offers WHERE id = ? AND driver_id = ?", (offer_id, driver_id),
    ).fetchone()
    return dict(row) if row else None


def list_offers(driver_id) -> List[Dict[str, Any]]:
    """Live (pending, unexpired) offers with the job summary the offer card
    shows. expires_in_s is server-computed so client clock skew can't lie."""
    from .auth import parse_iso
    expire_offers()
    rows = get_connection().execute(
        "SELECT * FROM offers WHERE driver_id = ? AND status = 'pending' ORDER BY id",
        (driver_id,),
    ).fetchall()
    out = []
    for r in rows:
        o = dict(r)
        job = _job_row(o["docket_number"])
        if not job:
            continue
        exp = parse_iso(o["expires_at"])
        left = max(0, int((exp - datetime.now(timezone.utc)).total_seconds())) if exp else 0
        out.append({
            "offer_id": o["id"], "docket_number": o["docket_number"],
            "offered_at": o["offered_at"], "expires_at": o["expires_at"],
            "expires_in_s": left,
            "account": job["account"], "vehicle": job["vehicle"],
            "deadline": job["deadline"],
            "pickup_postcode": job["pickup_postcode"], "pickup_address": job["pickup_address"],
            "drops": len(_drops_for(o["docket_number"])),
            "driver_pay_final": job["driver_pay_final"],
        })
    return out


def accept_offer(driver_id, offer_id) -> Dict[str, Any]:
    expire_offers()
    o = _offer_row(offer_id, driver_id)
    if not o:
        return {"ok": False, "reason": "not_found"}
    if o["status"] != "pending":
        return {"ok": False, "reason": "offer_closed", "status": o["status"]}
    conn = get_connection()
    job = _job_row(o["docket_number"])
    if (not job or job["status"] in ("COMPLETED", "CANCELLED")
            or (job["driver_id"] and job["driver_id"] != driver_id)):
        # Ops moved the job on while the offer was open — the offer is dead.
        conn.execute("UPDATE offers SET status='withdrawn', responded_at=? WHERE id=?",
                     (_now(), offer_id))
        conn.commit()
        return {"ok": False, "reason": "job_taken"}
    conn.execute(
        "UPDATE jobs SET driver_id=?, lifecycle_status='assigned', sequence_position=NULL, "
        "updated_at=? WHERE docket_number=?", (driver_id, _now(), o["docket_number"]))
    conn.execute("UPDATE offers SET status='accepted', responded_at=? WHERE id=?",
                 (_now(), offer_id))
    conn.commit()
    audit(driver_id, "offer:accept", o["docket_number"], {"offer_id": offer_id})
    return {"ok": True, "docket_number": o["docket_number"]}


def decline_offer(driver_id, offer_id, reason=None) -> Dict[str, Any]:
    expire_offers()
    o = _offer_row(offer_id, driver_id)
    if not o:
        return {"ok": False, "reason": "not_found"}
    if o["status"] != "pending":
        return {"ok": False, "reason": "offer_closed", "status": o["status"]}
    reason = (str(reason).strip()[:200] or None) if reason else None
    conn = get_connection()
    conn.execute(
        "UPDATE offers SET status='declined', decline_reason=?, responded_at=? WHERE id=?",
        (reason, _now(), offer_id))
    conn.commit()
    audit(driver_id, "offer:decline", o["docket_number"],
          {"offer_id": offer_id, "reason": reason})
    return {"ok": True, "docket_number": o["docket_number"]}


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
        "SELECT id, start_at, planned_end, status, break_started_at, break_minutes "
        "FROM shifts WHERE driver_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
        (driver_id,),
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
    # Close any stale active shift first (folding in its open break, if any).
    stale = get_active_shift(driver_id)
    if stale:
        _close_open_break(conn, stale)
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


def _break_minutes_since(started_at) -> int:
    from .auth import parse_iso
    dt = parse_iso(started_at)
    if not dt:
        return 0
    return max(0, int(round((datetime.now(timezone.utc) - dt).total_seconds() / 60.0)))


def _close_open_break(conn, shift) -> int:
    """Fold an open break into the shift's accumulated break_minutes.
    Returns the minutes added (0 when no break was open)."""
    if not shift or not shift.get("break_started_at"):
        return 0
    mins = _break_minutes_since(shift["break_started_at"])
    conn.execute(
        "UPDATE shifts SET break_minutes = COALESCE(break_minutes, 0) + ?, "
        "break_started_at = NULL WHERE id = ?", (mins, shift["id"]))
    return mins


def end_shift(driver_id) -> Optional[Dict[str, Any]]:
    """End the active shift (closing any open break first) and return the
    shift summary — None when there was no active shift."""
    conn = get_connection()
    shift = get_active_shift(driver_id)
    if not shift:
        conn.execute("UPDATE drivers SET duty_status='off' WHERE driver_id=?", (driver_id,))
        conn.commit()
        return None
    _close_open_break(conn, shift)
    conn.execute("UPDATE shifts SET status='ended', ended_at=? WHERE id=?",
                 (_now(), shift["id"]))
    conn.execute("UPDATE drivers SET duty_status='off' WHERE driver_id=?", (driver_id,))
    conn.commit()
    audit(driver_id, "shift:end")
    return shift_summary(driver_id, shift["id"])


def _haversine_km(lat1, lng1, lat2, lng2) -> float:
    from math import asin, cos, radians, sin, sqrt
    if None in (lat1, lng1, lat2, lng2):
        return 0.0
    dlat, dlng = radians(lat2 - lat1), radians(lng2 - lng1)
    h = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return 2 * 6371.0 * asin(sqrt(h))


def shift_summary(driver_id, shift_id) -> Optional[Dict[str, Any]]:
    """What the shift added up to: duty/break/worked minutes, jobs completed,
    drops delivered/failed, GPS distance (honest 0.0 when tracking was off)
    and the earnings on jobs completed inside the shift window."""
    from .auth import parse_iso
    conn = get_connection()
    s = conn.execute("SELECT * FROM shifts WHERE id = ? AND driver_id = ?",
                     (shift_id, driver_id)).fetchone()
    if not s:
        return None
    s = dict(s)
    start, end = s["start_at"], s["ended_at"] or _now()
    t0, t1 = parse_iso(start), parse_iso(end)
    duration = max(0, int(round((t1 - t0).total_seconds() / 60.0))) if t0 and t1 else 0
    breaks = int(s.get("break_minutes") or 0)
    # Timestamps are "%Y-%m-%dT%H:%M:%SZ" strings — lexicographic == chronological.
    jobs = conn.execute(
        "SELECT driver_pay_final FROM jobs WHERE driver_id = ? AND status = 'COMPLETED' "
        "AND completed_at >= ? AND completed_at <= ?", (driver_id, start, end)).fetchall()
    earnings = sum(float(j["driver_pay_final"] or 0) for j in jobs)
    drops = conn.execute(
        "SELECT d.status FROM drops d JOIN jobs j ON j.docket_number = d.docket_number "
        "WHERE j.driver_id = ? AND d.pod_at >= ? AND d.pod_at <= ? "
        "AND d.status IN ('delivered','failed')", (driver_id, start, end)).fetchall()
    pts = conn.execute(
        "SELECT lat, lng FROM locations WHERE driver_id = ? "
        "AND recorded_at >= ? AND recorded_at <= ? ORDER BY id",
        (driver_id, start, end)).fetchall()
    km = sum(_haversine_km(a["lat"], a["lng"], b["lat"], b["lng"])
             for a, b in zip(pts, pts[1:]))
    return {
        "shift_id": s["id"], "start_at": start, "ended_at": s["ended_at"],
        "duration_minutes": duration, "break_minutes": breaks,
        "worked_minutes": max(0, duration - breaks),
        "jobs_completed": len(jobs),
        "drops_delivered": sum(1 for d in drops if d["status"] == "delivered"),
        "drops_failed": sum(1 for d in drops if d["status"] == "failed"),
        "distance_km": round(km, 1),
        "earnings": "%.2f" % (earnings + 1e-9),
    }


def set_duty_status(driver_id, status) -> Dict[str, Any]:
    if status not in ("available", "going_home", "off", "on_break"):
        return {"ok": False, "reason": "invalid_status"}
    conn = get_connection()
    row = conn.execute("SELECT duty_status FROM drivers WHERE driver_id = ?",
                       (driver_id,)).fetchone()
    prev = row["duty_status"] if row else "off"
    shift = get_active_shift(driver_id)
    if status == "on_break":
        # Breaks are shift-time bookkeeping — without a shift there's nothing
        # to pause, and no row to accumulate the minutes on.
        if not shift:
            return {"ok": False, "reason": "no_active_shift"}
        if not shift.get("break_started_at"):
            conn.execute("UPDATE shifts SET break_started_at = ? WHERE id = ?",
                         (_now(), shift["id"]))
    elif prev == "on_break":
        _close_open_break(conn, shift)
    conn.execute("UPDATE drivers SET duty_status=? WHERE driver_id=?", (status, driver_id))
    conn.commit()
    audit(driver_id, "duty:status", detail={"status": status, "from": prev})
    return {"ok": True, "status": status}


# ── Vehicle inspection checklist (shift-start walkaround) ─────────────

VEHICLE_CHECK_ITEMS = ("tyres", "lights", "bodywork", "load_area",
                       "oil_coolant", "wipers_washers")


def save_vehicle_check(driver_id, odometer, items, defects=None,
                       photo_ref=None) -> Dict[str, Any]:
    """Record the walkaround for the ACTIVE shift. Advisory by design: a
    failed item never blocks the shift, but it does flag to ops through the
    existing message channel (unread badge + thread)."""
    shift = get_active_shift(driver_id)
    if not shift:
        return {"ok": False, "reason": "no_active_shift"}
    if not isinstance(items, dict):
        return {"ok": False, "reason": "invalid_items"}
    clean = {}
    for k in VEHICLE_CHECK_ITEMS:
        if k not in items:
            return {"ok": False, "reason": "invalid_items"}
        clean[k] = bool(items[k])
    odo = None
    if odometer not in (None, ""):
        try:
            odo = int(float(odometer))
        except (TypeError, ValueError):
            return {"ok": False, "reason": "invalid_odometer"}
        if not (0 <= odo <= 2_000_000):
            return {"ok": False, "reason": "invalid_odometer"}
    defects = (str(defects).strip()[:500] or None) if defects else None
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO vehicle_checks (driver_id, shift_id, odometer, items_json, "
        "defects, photo_ref, created_at) VALUES (?,?,?,?,?,?,?)",
        (driver_id, shift["id"], odo, json.dumps(clean), defects, photo_ref, _now()))
    conn.commit()
    failed = sorted(k for k, v in clean.items() if not v)
    audit(driver_id, "vehicle:check",
          detail={"shift_id": shift["id"], "failed": failed, "odometer": odo})
    if failed:
        labels = ", ".join(k.replace("_", " ") for k in failed)
        add_message(driver_id, "driver",
                    f"Vehicle check: defect(s) reported — {labels}."
                    + (f" Note: {defects}" if defects else ""),
                    "vehicle_check")
    return {"ok": True, "id": cur.lastrowid, "shift_id": shift["id"],
            "failed_items": failed}


def latest_vehicle_check(driver_id, shift_id=None) -> Optional[Dict[str, Any]]:
    if shift_id is None:
        shift = get_active_shift(driver_id)
        if not shift:
            return None
        shift_id = shift["id"]
    row = get_connection().execute(
        "SELECT * FROM vehicle_checks WHERE driver_id = ? AND shift_id = ? "
        "ORDER BY id DESC LIMIT 1", (driver_id, shift_id)).fetchone()
    if not row:
        return None
    v = dict(row)
    v["items"] = json.loads(v.pop("items_json") or "{}")
    return v


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
    "duty_status", "theme", "text_size", "sound_alert_conn", "biometric", "location_consent_at",
    "avatar_url", "vehicle_photo_url",
)
# Fields a driver can change live; everything sensitive goes via review.
_DIRECT_EDIT = {"phone", "email", "address", "emergency_name", "emergency_phone",
                "vehicle_reg", "vehicle_make", "vehicle_model",
                "notify_jobs", "notify_pay", "notify_msgs", "nav_app",
                "theme", "text_size", "sound_alert_conn", "biometric"}
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
