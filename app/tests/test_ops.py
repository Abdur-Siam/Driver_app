"""Standalone Driver App — ops / dispatch console API tests.

Run:
    cd Driver/app && PYTHONPATH=. python -m pytest tests/test_ops.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_HERE)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

OPS = "/api/ops/v1"
DRV = "/api/driver/v1"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from backend import config, db, seed
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(config, "MEDIA_DIR", str(tmp_path / "media"))
    db.close_connection()
    db.init_db()
    seed.seed_if_empty()
    from backend.server import create_app
    return create_app().test_client()


def _ops(c, username="ops", password="ops1234"):
    r = c.post(OPS + "/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.data
    return {"Authorization": "Bearer " + r.get_json()["token"]}


def _driver(c, identifier="DRV001", password="test1234"):
    r = c.post(DRV + "/auth/login", json={"identifier": identifier, "password": password})
    assert r.status_code == 200, r.data
    return {"Authorization": "Bearer " + r.get_json()["token"]}


# ── auth / isolation ─────────────────────────────────────────────────

def test_ops_login_ok(client):
    h = _ops(client)
    me = client.get(OPS + "/me", headers=h).get_json()["user"]
    assert me["username"] == "ops" and me["role"] == "admin"


def test_ops_login_bad_password(client):
    assert client.post(OPS + "/auth/login", json={"username": "ops", "password": "no"}).status_code == 401


def test_ops_protected_requires_token(client):
    assert client.get(OPS + "/dashboard").status_code == 401
    assert client.get(OPS + "/tracking").status_code == 401


def test_driver_token_cannot_use_ops(client):
    """A driver bearer token must not authenticate against the console."""
    h = _driver(client)
    assert client.get(OPS + "/dashboard", headers=h).status_code == 401


def test_ops_token_cannot_use_driver(client):
    """An ops bearer token must not authenticate as a driver."""
    h = _ops(client)
    assert client.get(DRV + "/run", headers=h).status_code == 401


def test_ops_logout_revokes(client):
    h = _ops(client)
    assert client.get(OPS + "/me", headers=h).status_code == 200
    assert client.post(OPS + "/auth/logout", headers=h).status_code == 200
    assert client.get(OPS + "/me", headers=h).status_code == 401


def test_ops_account_lockout(client):
    for _ in range(5):
        client.post(OPS + "/auth/login", json={"username": "ops", "password": "wrong"})
    # Even the correct password is locked out for the window.
    r = client.post(OPS + "/auth/login", json={"username": "ops", "password": "ops1234"})
    assert r.status_code == 429


# ── dashboard / drivers / tracking ───────────────────────────────────

def test_dashboard_counts(client):
    h = _ops(client)
    d = client.get(OPS + "/dashboard", headers=h).get_json()
    assert d["drivers_total"] == 2
    assert d["active_jobs"] >= 3
    assert d["unassigned_jobs"] == 0


def test_drivers_roster(client):
    h = _ops(client)
    drivers = client.get(OPS + "/drivers", headers=h).get_json()["drivers"]
    ids = {d["driver_id"] for d in drivers}
    assert {"DRV001", "CX014"} <= ids
    drv1 = next(d for d in drivers if d["driver_id"] == "DRV001")
    assert drv1["active_jobs"] == 2 and drv1["current_docket"]


def test_tracking_snapshot_and_trail(client):
    h = _ops(client)
    snap = client.get(OPS + "/tracking", headers=h).get_json()["drivers"]
    assert len(snap) == 2  # both demo drivers have a seeded fix
    m = next(x for x in snap if x["driver_id"] == "DRV001")
    assert m["lat"] and m["lng"] and m["current_docket"]
    trail = client.get(OPS + "/tracking/DRV001/trail", headers=h).get_json()["trail"]
    assert len(trail) >= 2


def test_driver_detail_hides_bank(client):
    h = _ops(client)
    d = client.get(OPS + "/drivers/DRV001", headers=h).get_json()
    assert "bank_account_number" not in d["driver"]
    assert "utr_or_company_ref" not in d["driver"]
    assert len(d["jobs"]) == 2


# ── job dispatch ─────────────────────────────────────────────────────

def _make_job(client, h, **over):
    payload = {
        "account": "TESTCO", "vehicle": "Small Van", "deadline": "17:00",
        "pickup": {"address": "1 A St", "postcode": "EC1A 1AA", "lat": 51.52, "lng": -0.1},
        "requires_scan": True,
        "pay": {"base": "20.00", "extras": "5.00", "deduction": "1.00"},
        "drops": [{"address": "9 B Rd", "postcode": "N1 1AA", "lat": 51.53, "lng": -0.1,
                   "parcels": [{"barcode": "TB001", "description": "box"}]}],
    }
    payload.update(over)
    return client.post(OPS + "/jobs", json=payload, headers=h)


def test_create_job_unassigned(client):
    h = _ops(client)
    r = _make_job(client, h)
    assert r.status_code == 201, r.data
    docket = r.get_json()["docket"]
    job = client.get(OPS + "/jobs/" + docket, headers=h).get_json()["job"]
    assert job["driver_id"] is None
    assert job["lifecycle_status"] == "unassigned"
    assert job["driver_pay_final"] == "24.00"   # 20 + 5 - 1
    assert job["parcel_count"] == 1 and len(job["drops"]) == 1


def test_create_job_requires_a_drop(client):
    h = _ops(client)
    assert _make_job(client, h, drops=[]).status_code == 400


def test_create_job_rejects_duplicate_docket(client):
    h = _ops(client)
    r1 = _make_job(client, h, docket_number="XM-DUP-0001")
    assert r1.status_code == 201
    assert _make_job(client, h, docket_number="XM-DUP-0001").status_code == 409


def test_create_and_assign_reaches_driver_run(client):
    h = _ops(client)
    docket = _make_job(client, h, driver_id="DRV001").get_json()["docket"]
    # It shows up on the driver's run.
    dh = _driver(client, "DRV001")
    dockets = {j["docket_number"] for j in client.get(DRV + "/run", headers=dh).get_json()["jobs"]}
    assert docket in dockets
    # And the driver got an assignment message.
    msgs = client.get(DRV + "/messages", headers=dh).get_json()["messages"]
    assert any(docket in m["text"] for m in msgs)


def test_reassign_and_unassign(client):
    h = _ops(client)
    docket = _make_job(client, h).get_json()["docket"]
    assert client.post(OPS + f"/jobs/{docket}/assign", json={"driver_id": "CX014"}, headers=h).status_code == 200
    assert client.get(OPS + "/jobs/" + docket, headers=h).get_json()["job"]["driver_id"] == "CX014"
    # Unassign.
    assert client.post(OPS + f"/jobs/{docket}/assign", json={"driver_id": ""}, headers=h).status_code == 200
    assert client.get(OPS + "/jobs/" + docket, headers=h).get_json()["job"]["driver_id"] is None


def test_assign_unknown_driver_rejected(client):
    h = _ops(client)
    docket = _make_job(client, h).get_json()["docket"]
    assert client.post(OPS + f"/jobs/{docket}/assign", json={"driver_id": "NOPE"}, headers=h).status_code == 404


def test_cancel_job_removes_from_driver_run(client):
    h = _ops(client)
    docket = _make_job(client, h, driver_id="DRV001").get_json()["docket"]
    assert client.post(OPS + f"/jobs/{docket}/cancel", headers=h).status_code == 200
    dh = _driver(client, "DRV001")
    dockets = {j["docket_number"] for j in client.get(DRV + "/run", headers=dh).get_json()["jobs"]}
    assert docket not in dockets
    # Cancelling twice is a 409.
    assert client.post(OPS + f"/jobs/{docket}/cancel", headers=h).status_code == 409


def test_job_filters(client):
    h = _ops(client)
    _make_job(client, h)                       # unassigned
    _make_job(client, h, driver_id="DRV001")   # active/assigned
    unassigned = client.get(OPS + "/jobs?status=unassigned", headers=h).get_json()["jobs"]
    assert unassigned and all(j["driver_id"] in (None, "") for j in unassigned)
    completed = client.get(OPS + "/jobs?status=completed", headers=h).get_json()["jobs"]
    assert all(j["status"] in ("COMPLETED", "CANCELLED") for j in completed)


# ── messaging (ops ↔ driver, per-job) ────────────────────────────────

def test_ops_message_reaches_driver_and_read_flow(client):
    h = _ops(client)
    r = client.post(OPS + "/messages/DRV001", json={"text": "Head to depot", "docket": "XM-20260626-0042"}, headers=h)
    assert r.status_code == 200
    # Driver sees it in their inbox with the job tag.
    dh = _driver(client, "DRV001")
    msgs = client.get(DRV + "/messages", headers=dh).get_json()["messages"]
    assert any(m["text"] == "Head to depot" and m["docket_number"] == "XM-20260626-0042" for m in msgs)
    # Driver replies; ops sees it and unread count rises, then clears on read.
    client.post(DRV + "/messages", json={"text": "On my way"}, headers=dh)
    assert client.get(OPS + "/dashboard", headers=h).get_json()["unread_driver_messages"] >= 1
    thread = client.get(OPS + "/messages/DRV001", headers=h).get_json()["messages"]
    assert any(m["direction"] == "driver" and m["text"] == "On my way" for m in thread)
    client.post(OPS + "/messages/DRV001/read", headers=h)
    assert client.get(OPS + "/dashboard", headers=h).get_json()["unread_driver_messages"] == 0


def test_message_unknown_driver_404(client):
    h = _ops(client)
    assert client.post(OPS + "/messages/GHOST", json={"text": "hi"}, headers=h).status_code == 404
    assert client.get(OPS + "/messages/GHOST", headers=h).status_code == 404


def test_per_job_thread_filter(client):
    h = _ops(client)
    client.post(OPS + "/messages/DRV001", json={"text": "job A note", "docket": "XM-20260626-0042"}, headers=h)
    client.post(OPS + "/messages/DRV001", json={"text": "general note"}, headers=h)
    tagged = client.get(OPS + "/messages/DRV001?docket=XM-20260626-0042", headers=h).get_json()["messages"]
    assert tagged and all(m["docket_number"] == "XM-20260626-0042" for m in tagged)


# ── console shell served ─────────────────────────────────────────────

def test_ops_console_page_served(client):
    r = client.get("/ops")
    assert r.status_code == 200
    assert b"TOM Dispatch" in r.data

# ── QA-audit hardening fixes (July 2026) ──────────────────────────────

def test_driver_reply_joins_per_job_thread(client):
    """The driver can tag a reply with a docket, and the ops per-job thread
    shows it — the chat round-trips both ways."""
    oh = _ops(client)
    dh = _driver(client)
    d = "XM-20260626-0042"
    client.post(OPS + "/messages/DRV001", json={"text": "Any ETA on this one?", "docket": d}, headers=oh)
    r = client.post(DRV + "/messages", json={"text": "10 minutes out", "docket_number": d}, headers=dh)
    assert r.status_code == 200
    thread = client.get(OPS + f"/messages/DRV001?docket={d}", headers=oh).get_json()["messages"]
    assert any(m["direction"] == "driver" and m["text"] == "10 minutes out" for m in thread)
    # An untagged reply stays out of the per-job thread but is in the full one.
    client.post(DRV + "/messages", json={"text": "general note"}, headers=dh)
    thread = client.get(OPS + f"/messages/DRV001?docket={d}", headers=oh).get_json()["messages"]
    assert not any(m["text"] == "general note" for m in thread)
    full = client.get(OPS + "/messages/DRV001", headers=oh).get_json()["messages"]
    assert any(m["text"] == "general note" for m in full)


def test_tracking_snapshot_flags_stale_fixes(client):
    """Seeded fixes are timestamped 'now' → fresh; a driver whose last fix is
    old is flagged stale so the map never presents it as live."""
    h = _ops(client)
    snap = client.get(OPS + "/tracking", headers=h).get_json()["drivers"]
    assert snap, "expected seeded fixes on the map"
    fresh = {s["driver_id"]: s for s in snap}
    assert fresh["DRV001"]["stale"] is False
    from backend.db import get_connection
    get_connection().execute(
        "UPDATE locations SET recorded_at = '2026-01-01T00:00:00Z' WHERE driver_id = 'DRV001'")
    get_connection().commit()
    snap = client.get(OPS + "/tracking", headers=h).get_json()["drivers"]
    assert {s["driver_id"]: s for s in snap}["DRV001"]["stale"] is True


# ── job offers (offer / accept / decline / expiry) ───────────────────

def _offer(client, h, docket, driver_id="DRV001", **body):
    return client.post(OPS + f"/jobs/{docket}/offer",
                       json={"driver_id": driver_id, **body}, headers=h)


def test_offer_accept_assigns_job(client):
    h = _ops(client)
    docket = _make_job(client, h).get_json()["docket"]
    r = _offer(client, h, docket)
    assert r.status_code == 201, r.data
    b = r.get_json()
    assert b["offer_id"] and b["expires_at"] and b["ttl_s"] == 120   # default TTL
    # The driver sees the offer with a live countdown and the job summary.
    dh = _driver(client, "DRV001")
    offers = client.get(DRV + "/offers", headers=dh).get_json()["offers"]
    assert len(offers) == 1
    o = offers[0]
    assert o["docket_number"] == docket and 0 < o["expires_in_s"] <= 120
    assert o["account"] == "TESTCO" and o["drops"] == 1
    # Accept → normal assigned flow.
    assert client.post(DRV + f"/offers/{o['offer_id']}/accept", headers=dh).status_code == 200
    run = {j["docket_number"] for j in client.get(DRV + "/run", headers=dh).get_json()["jobs"]}
    assert docket in run
    job = client.get(OPS + "/jobs/" + docket, headers=h).get_json()["job"]
    assert job["driver_id"] == "DRV001" and job["lifecycle_status"] == "assigned"
    # No live offers left.
    assert client.get(DRV + "/offers", headers=dh).get_json()["offers"] == []


def test_offer_decline_returns_job_with_reason(client):
    h = _ops(client)
    docket = _make_job(client, h).get_json()["docket"]
    oid = _offer(client, h, docket).get_json()["offer_id"]
    dh = _driver(client, "DRV001")
    r = client.post(DRV + f"/offers/{oid}/decline", json={"reason": "too far"}, headers=dh)
    assert r.status_code == 200
    # Back on the unassigned board, flagged declined with the reason visible.
    rows = client.get(OPS + "/jobs?status=unassigned", headers=h).get_json()["jobs"]
    row = next(j for j in rows if j["docket_number"] == docket)
    assert row["offer"]["status"] == "declined"
    assert row["offer"]["decline_reason"] == "too far"
    assert row["offer"]["driver_id"] == "DRV001"
    # The job stays unassigned; the driver's offer list is empty.
    assert row["driver_id"] is None
    assert client.get(DRV + "/offers", headers=dh).get_json()["offers"] == []
    # Answering the same offer again is a conflict.
    assert client.post(DRV + f"/offers/{oid}/decline", headers=dh).status_code == 409
    assert client.post(DRV + f"/offers/{oid}/accept", headers=dh).status_code == 409


def test_offer_expiry_flags_board(client):
    h = _ops(client)
    docket = _make_job(client, h).get_json()["docket"]
    oid = _offer(client, h, docket).get_json()["offer_id"]
    # Force the deadline into the past (lazy expiry runs on the next read).
    from backend.db import get_connection
    conn = get_connection()
    conn.execute("UPDATE offers SET expires_at = '2000-01-01T00:00:00Z' WHERE id = ?", (oid,))
    conn.commit()
    dh = _driver(client, "DRV001")
    assert client.get(DRV + "/offers", headers=dh).get_json()["offers"] == []
    rows = client.get(OPS + "/jobs?status=unassigned", headers=h).get_json()["jobs"]
    row = next(j for j in rows if j["docket_number"] == docket)
    assert row["offer"]["status"] == "expired"
    # Accepting after expiry is refused.
    assert client.post(DRV + f"/offers/{oid}/accept", headers=dh).status_code == 409


def test_offer_validation(client):
    h = _ops(client)
    # Assigned job cannot be offered.
    assigned = _make_job(client, h, driver_id="DRV001").get_json()["docket"]
    r = _offer(client, h, assigned)
    assert r.status_code == 409 and r.get_json()["error"]["code"] == "job_assigned"
    # Unknown driver / unknown job.
    docket = _make_job(client, h).get_json()["docket"]
    assert _offer(client, h, docket, driver_id="NOPE").status_code == 404
    assert _offer(client, h, "XM-GHOST-0001").status_code == 404
    # TTL override is clamped to a sane window.
    b = _offer(client, h, docket, expires_in_s=999999).get_json()
    assert b["ttl_s"] == 3600


def test_offer_withdrawn_when_job_taken(client):
    h = _ops(client)
    docket = _make_job(client, h).get_json()["docket"]
    oid = _offer(client, h, docket).get_json()["offer_id"]
    # Ops direct-assigns to someone else while the offer is open.
    assert client.post(OPS + f"/jobs/{docket}/assign", json={"driver_id": "CX014"}, headers=h).status_code == 200
    dh = _driver(client, "DRV001")
    r = client.post(DRV + f"/offers/{oid}/accept", headers=dh)
    assert r.status_code == 409 and r.get_json()["error"]["code"] == "job_taken"
    # CX014 keeps the job.
    assert client.get(OPS + "/jobs/" + docket, headers=h).get_json()["job"]["driver_id"] == "CX014"


def test_reoffer_withdraws_previous_offer(client):
    h = _ops(client)
    docket = _make_job(client, h).get_json()["docket"]
    first = _offer(client, h, docket, driver_id="DRV001").get_json()["offer_id"]
    _offer(client, h, docket, driver_id="CX014")
    # DRV001's offer is gone (withdrawn); CX014 holds the live one.
    dh1 = _driver(client, "DRV001")
    assert client.get(DRV + "/offers", headers=dh1).get_json()["offers"] == []
    assert client.post(DRV + f"/offers/{first}/accept", headers=dh1).status_code == 409
    dh2 = _driver(client, "CX014")
    offers = client.get(DRV + "/offers", headers=dh2).get_json()["offers"]
    assert len(offers) == 1 and offers[0]["docket_number"] == docket


def test_offer_ownership_enforced(client):
    h = _ops(client)
    docket = _make_job(client, h).get_json()["docket"]
    oid = _offer(client, h, docket, driver_id="DRV001").get_json()["offer_id"]
    # Another driver cannot see or answer it.
    dh2 = _driver(client, "CX014")
    assert client.get(DRV + "/offers", headers=dh2).get_json()["offers"] == []
    assert client.post(DRV + f"/offers/{oid}/accept", headers=dh2).status_code == 404
    assert client.post(DRV + f"/offers/{oid}/decline", headers=dh2).status_code == 404
