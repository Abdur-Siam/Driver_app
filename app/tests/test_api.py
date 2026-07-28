"""Standalone Driver App — backend API tests.

Run:
    cd Driver/app && PYTHONPATH=. python -m pytest tests/test_api.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_HERE)
if _APP not in sys.path:
    sys.path.insert(0, _APP)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from backend import config, db, seed
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(config, "MEDIA_DIR", str(tmp_path / "media"))
    # Limiter + lockout state is DB-backed (rate_events), so the fresh
    # per-test DB resets it — nothing in process memory to clear.
    db.close_connection()
    db.init_db()
    seed.seed_if_empty()
    from backend.server import create_app
    return create_app().test_client()


def _login(c, identifier="DRV001", password="test1234"):
    r = c.post("/api/driver/v1/auth/login", json={"identifier": identifier, "password": password})
    return r


def _auth(c, identifier="DRV001", password="test1234"):
    r = _login(c, identifier, password)
    assert r.status_code == 200, r.data
    return {"Authorization": "Bearer " + r.get_json()["token"]}


# ── auth ─────────────────────────────────────────────────────────────

def test_login_ok(client):
    r = _login(client)
    assert r.status_code == 200
    b = r.get_json()
    assert b["token"] and b["driver"]["driver_id"] == "DRV001"
    assert "password_hash" not in b["driver"]


def test_login_wrong_password(client):
    assert _login(client, password="nope").status_code == 401


def test_login_unknown(client):
    assert _login(client, identifier="GHOST").status_code == 401


def test_protected_requires_token(client):
    assert client.get("/api/driver/v1/run").status_code == 401


def test_logout_revokes(client):
    h = _auth(client)
    assert client.get("/api/driver/v1/run", headers=h).status_code == 200
    assert client.post("/api/driver/v1/auth/logout", headers=h).status_code == 200
    assert client.get("/api/driver/v1/run", headers=h).status_code == 401


# ── run / detail / ownership ─────────────────────────────────────────

def test_run_lists_owned_jobs(client):
    h = _auth(client)
    jobs = client.get("/api/driver/v1/run", headers=h).get_json()["jobs"]
    dockets = {j["docket_number"] for j in jobs}
    assert "XM-20260626-0042" in dockets
    assert "XM-20260626-0051" in dockets
    assert "XM-20260626-0067" not in dockets  # belongs to CX014


def test_job_detail_has_drops_and_parcels(client):
    h = _auth(client)
    job = client.get("/api/driver/v1/jobs/XM-20260626-0042", headers=h).get_json()["job"]
    assert len(job["drops"]) == 2
    assert job["parcel_count"] == 3


def test_ownership_enforced(client):
    h = _auth(client, "CX014")
    # CX014 cannot read DRV001's job
    assert client.get("/api/driver/v1/jobs/XM-20260626-0042", headers=h).status_code == 404


# ── lifecycle + scanning ─────────────────────────────────────────────

def test_collected_requires_all_parcels_scanned(client):
    h = _auth(client)
    d = "XM-20260626-0042"
    assert client.post(f"/api/driver/v1/jobs/{d}/status", json={"action": "acknowledge"}, headers=h).status_code == 200
    assert client.post(f"/api/driver/v1/jobs/{d}/status", json={"action": "en_route_pickup"}, headers=h).status_code == 200
    assert client.post(f"/api/driver/v1/jobs/{d}/status", json={"action": "arrive_pickup"}, headers=h).status_code == 200
    # Not all parcels scanned yet → 409
    r = client.post(f"/api/driver/v1/jobs/{d}/status", json={"action": "collected"}, headers=h)
    assert r.status_code == 409
    assert r.get_json()["error"]["code"] == "parcels_outstanding"
    # Scan all three on collect
    for bc in ("XM00420101", "XM00420201", "XM00420202"):
        sr = client.post(f"/api/driver/v1/jobs/{d}/scan",
                         json={"phase": "collect", "barcode": bc}, headers=h)
        assert sr.status_code == 200 and sr.get_json()["match"] == "expected"
    # Now POB succeeds
    assert client.post(f"/api/driver/v1/jobs/{d}/status", json={"action": "collected"}, headers=h).status_code == 200


def test_scan_wrong_drop_guard(client):
    h = _auth(client)
    d = "XM-20260626-0042"
    # XM00420201 belongs to drop 2; scanning it for drop 1 must flag wrong_drop
    r = client.post(f"/api/driver/v1/jobs/{d}/scan",
                    json={"phase": "deliver", "drop_seq": 1, "barcode": "XM00420201"}, headers=h)
    assert r.status_code == 200 and r.get_json()["match"] == "wrong_drop"


def test_invalid_transition(client):
    h = _auth(client)
    d = "XM-20260626-0042"
    # collected straight from assigned is invalid
    r = client.post(f"/api/driver/v1/jobs/{d}/status", json={"action": "collected"}, headers=h)
    assert r.status_code == 409


def test_unknown_action(client):
    h = _auth(client)
    r = client.post("/api/driver/v1/jobs/XM-20260626-0042/status",
                    json={"action": "teleport"}, headers=h)
    assert r.status_code == 400


def test_run_exposes_requires_scan_flag(client):
    h = _auth(client)
    jobs = {j["docket_number"]: j for j in client.get("/api/driver/v1/run", headers=h).get_json()["jobs"]}
    assert jobs["XM-20260626-0042"]["requires_scan"] is True     # fragile electronics
    assert jobs["XM-20260626-0051"]["requires_scan"] is False    # document run — no scan needed


def test_no_scan_job_skips_straight_to_pob_and_pod(client):
    h = _auth(client)
    d = "XM-20260626-0051"   # requires_scan = 0
    for action in ("acknowledge", "en_route_pickup", "arrive_pickup"):
        assert client.post(f"/api/driver/v1/jobs/{d}/status", json={"action": action}, headers=h).status_code == 200
    # POB allowed with zero scans
    assert client.post(f"/api/driver/v1/jobs/{d}/status", json={"action": "collected"}, headers=h).status_code == 200
    # POD allowed with zero deliver-scans
    pod = client.post(f"/api/driver/v1/jobs/{d}/pod",
                      json={"drop_seq": 1, "recipient_name": "Mailroom"}, headers=h)
    assert pod.status_code == 200 and pod.get_json()["job_completed"] is True


def test_no_scan_job_still_accepts_optional_scans(client):
    h = _auth(client)
    d = "XM-20260626-0051"
    r = client.post(f"/api/driver/v1/jobs/{d}/scan",
                    json={"phase": "collect", "barcode": "XM00510101"}, headers=h)
    assert r.status_code == 200 and r.get_json()["match"] == "expected"


def test_scan_required_job_blocks_pod_until_deliver_scan(client):
    h = _auth(client)
    d = "XM-20260626-0042"   # requires_scan = 1
    for action in ("acknowledge", "en_route_pickup", "arrive_pickup"):
        client.post(f"/api/driver/v1/jobs/{d}/status", json={"action": action}, headers=h)
    for bc in ("XM00420101", "XM00420201", "XM00420202"):
        client.post(f"/api/driver/v1/jobs/{d}/scan", json={"phase": "collect", "barcode": bc}, headers=h)
    client.post(f"/api/driver/v1/jobs/{d}/status", json={"action": "collected"}, headers=h)
    client.post(f"/api/driver/v1/jobs/{d}/status", json={"action": "en_route_drop"}, headers=h)
    # POD on drop 1 before its deliver-scan → 409
    r = client.post(f"/api/driver/v1/jobs/{d}/pod",
                    json={"drop_seq": 1, "recipient_name": "Mr Adeyemi"}, headers=h)
    assert r.status_code == 409 and r.get_json()["error"]["code"] == "parcels_outstanding"
    # Deliver-scan the drop-1 parcel, then POD succeeds
    sr = client.post(f"/api/driver/v1/jobs/{d}/scan",
                     json={"phase": "deliver", "drop_seq": 1, "barcode": "XM00420101"}, headers=h)
    assert sr.status_code == 200 and sr.get_json()["match"] == "expected"
    assert client.post(f"/api/driver/v1/jobs/{d}/pod",
                       json={"drop_seq": 1, "recipient_name": "Mr Adeyemi"}, headers=h).status_code == 200


# ── idempotency ──────────────────────────────────────────────────────

def test_idempotent_status_replay(client):
    h = dict(_auth(client))
    h["X-Idempotency-Key"] = "outbox-1"
    d = "XM-20260626-0042"
    r1 = client.post(f"/api/driver/v1/jobs/{d}/status", json={"action": "acknowledge"}, headers=h)
    r2 = client.post(f"/api/driver/v1/jobs/{d}/status", json={"action": "acknowledge"}, headers=h)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.get_json() == r2.get_json()
    # Fresh acknowledge without key proves single apply (state already moved)
    r3 = client.post(f"/api/driver/v1/jobs/{d}/status",
                     json={"action": "acknowledge"}, headers=_auth(client))
    assert r3.status_code == 409


def test_idempotent_scan_replay(client):
    # A scan is queued through the offline outbox with an idempotency key; a
    # lost response replayed on reconnect must NOT re-execute the handler (which
    # would insert a second parcel_events row). The keyed replay returns the
    # identical first response; a genuine re-scan (no key) is what reports
    # 'duplicate' — so the two are distinguishable, proving API-layer dedup.
    h = dict(_auth(client))
    h["X-Idempotency-Key"] = "outbox-scan-1"
    d = "XM-20260626-0042"
    r1 = client.post(f"/api/driver/v1/jobs/{d}/scan",
                     json={"phase": "collect", "barcode": "XM00420101"}, headers=h)
    r2 = client.post(f"/api/driver/v1/jobs/{d}/scan",
                     json={"phase": "collect", "barcode": "XM00420101"}, headers=h)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.get_json()["match"] == "expected"
    # The replay is the cached first response, NOT a fresh 'duplicate' apply.
    assert r2.get_json() == r1.get_json()
    # A genuine re-scan WITHOUT the key does re-hit the store → 'duplicate'.
    r3 = client.post(f"/api/driver/v1/jobs/{d}/scan",
                     json={"phase": "collect", "barcode": "XM00420101"}, headers=_auth(client))
    assert r3.status_code == 200 and r3.get_json()["match"] == "duplicate"


# ── POD completes the job ────────────────────────────────────────────

def test_pod_completes_single_drop_job(client):
    h = _auth(client)
    d = "XM-20260626-0051"   # one drop, one parcel
    for action in ("acknowledge", "en_route_pickup", "arrive_pickup"):
        client.post(f"/api/driver/v1/jobs/{d}/status", json={"action": action}, headers=h)
    client.post(f"/api/driver/v1/jobs/{d}/scan", json={"phase": "collect", "barcode": "XM00510101"}, headers=h)
    client.post(f"/api/driver/v1/jobs/{d}/status", json={"action": "collected"}, headers=h)
    pod = client.post(f"/api/driver/v1/jobs/{d}/pod",
                      json={"drop_seq": 1, "recipient_name": "Mailroom"}, headers=h)
    assert pod.status_code == 200
    assert pod.get_json()["job_completed"] is True
    # Completed jobs drop off the run
    jobs = client.get("/api/driver/v1/run", headers=h).get_json()["jobs"]
    assert d not in {j["docket_number"] for j in jobs}


# ── location + route ─────────────────────────────────────────────────

def test_location_batch_accepted(client):
    h = _auth(client)
    client.post("/api/driver/v1/consent/location", json={"granted": True}, headers=h)
    # Tracking is shift-gated: an on-shift driver's pings are accepted.
    client.post("/api/driver/v1/shift/start", json={"planned_end": "18:00"}, headers=h)
    r = client.post("/api/driver/v1/location/batch",
                    json={"pings": [{"t": "2026-06-26T08:00:00Z", "lat": 51.52, "lng": -0.1}]}, headers=h)
    assert r.status_code == 202 and r.get_json()["accepted"] == 1


def test_location_batch_dedups_replayed_pings(client):
    # The offline buffer re-sends a batch until it gets a 2xx, so a batch that
    # applied but whose response was lost gets replayed. Client ping ids must
    # make the replay a no-op — no duplicate location rows.
    h = _auth(client)
    client.post("/api/driver/v1/consent/location", json={"granted": True}, headers=h)
    client.post("/api/driver/v1/shift/start", json={"planned_end": "18:00"}, headers=h)
    batch = {"pings": [
        {"id": "p-1", "t": "2026-06-26T09:00:00Z", "lat": 51.50, "lng": -0.12},
        {"id": "p-2", "t": "2026-06-26T09:00:05Z", "lat": 51.51, "lng": -0.11},
    ]}
    r1 = client.post("/api/driver/v1/location/batch", json=batch, headers=h)
    assert r1.status_code == 202 and r1.get_json()["accepted"] == 2
    # Exact replay → 0 newly stored (both deduped on ping_id).
    r2 = client.post("/api/driver/v1/location/batch", json=batch, headers=h)
    assert r2.status_code == 202 and r2.get_json()["accepted"] == 0
    # A pingless legacy ping is NOT deduped (NULL ids are distinct) — still stores.
    r3 = client.post("/api/driver/v1/location/batch",
                     json={"pings": [{"t": "2026-06-26T09:00:09Z", "lat": 51.52, "lng": -0.10}]}, headers=h)
    assert r3.status_code == 202 and r3.get_json()["accepted"] == 1


def test_location_batch_blocked_without_consent(client):
    h = _auth(client)   # demo driver has NOT consented yet
    r = client.post("/api/driver/v1/location/batch",
                    json={"pings": [{"t": "2026-06-26T08:00:00Z", "lat": 51.52, "lng": -0.1}]}, headers=h)
    assert r.status_code == 403 and r.get_json()["error"]["code"] == "consent_required"


def test_route_optimise_fallback(client):
    h = _auth(client)
    r = client.post("/api/driver/v1/route/optimise",
                    json={"from": {"lat": 51.5237, "lng": -0.1075}}, headers=h)
    assert r.status_code == 200
    b = r.get_json()
    assert b["engine"] == "haversine_fallback"   # no Maps key in tests
    assert set(b["ordered_dockets"]) == {"XM-20260626-0042", "XM-20260626-0051"}


def test_config_no_maps_key(client):
    assert client.get("/api/driver/v1/config").get_json()["maps_enabled"] is False


# ── home ─────────────────────────────────────────────────────────────

def test_home_dashboard(client):
    h = _auth(client)
    b = client.get("/api/driver/v1/home", headers=h).get_json()
    assert b["run_count"] == 2
    assert b["driver"]["driver_id"] == "DRV001"
    assert "today" in b and "performance" in b


# ── profile ──────────────────────────────────────────────────────────

def test_profile_get_has_full_details(client):
    h = _auth(client)
    b = client.get("/api/driver/v1/profile", headers=h).get_json()
    p = b["profile"]
    assert p["email"] and p["address"] and p["utr_or_company_ref"]
    assert p["bank_account_number"].startswith("****")   # masked at rest


def test_profile_direct_edit_updates_live(client):
    h = _auth(client)
    r = client.post("/api/driver/v1/profile", json={"fields": {"phone": "07999 111222"}}, headers=h)
    assert r.status_code == 200 and "phone" in r.get_json()["updated"]
    assert client.get("/api/driver/v1/profile", headers=h).get_json()["profile"]["phone"] == "07999 111222"


def test_profile_sensitive_edit_goes_to_review(client):
    h = _auth(client)
    r = client.post("/api/driver/v1/profile",
                    json={"fields": {"bank_account_number": "12345678"}}, headers=h)
    body = r.get_json()
    assert any(p["field"] == "bank_account_number" for p in body["pending"])
    # live value unchanged (still masked seed value)
    assert client.get("/api/driver/v1/profile", headers=h).get_json()["profile"]["bank_account_number"].startswith("****")


def test_change_password(client):
    h = _auth(client)
    assert client.post("/api/driver/v1/profile/password",
                       json={"current": "test1234", "new": "newpass99"}, headers=h).status_code == 200
    # old password no longer works, new one does
    assert _login(client, password="test1234").status_code == 401
    assert _login(client, password="newpass99").status_code == 200


# ── earnings / statements / tax ──────────────────────────────────────

def test_earnings_summary(client):
    h = _auth(client)
    b = client.get("/api/driver/v1/earnings", headers=h).get_json()
    assert b["period"]["status"] == "processing"
    assert float(b["totals"]["ytd_gross"]) > 0
    assert len(b["week_chart"]) == 7


def test_statements_list_and_detail(client):
    h = _auth(client)
    stmts = client.get("/api/driver/v1/statements", headers=h).get_json()["statements"]
    assert len(stmts) == 3
    paid = [s for s in stmts if s["status"] == "paid"]
    assert len(paid) == 2
    detail = client.get(f"/api/driver/v1/statements/{stmts[0]['statement_id']}", headers=h).get_json()["statement"]
    assert detail["lines"] and float(detail["net"]) > 0


def test_statement_pdf_download(client):
    h = _auth(client)
    sid = client.get("/api/driver/v1/statements", headers=h).get_json()["statements"][0]["statement_id"]
    r = client.get(f"/api/driver/v1/statements/{sid}/pdf", headers=h)
    assert r.status_code == 200
    assert r.mimetype == "application/pdf"
    assert r.data[:4] == b"%PDF"
    assert "attachment" in r.headers.get("Content-Disposition", "")


def test_statement_pdf_ownership(client):
    h = _auth(client, "CX014")   # CX014 has no statements; DRV001's sid must 404
    drv_sid = None
    # fetch DRV001's sid via its own session
    h1 = _auth(client)
    drv_sid = client.get("/api/driver/v1/statements", headers=h1).get_json()["statements"][0]["statement_id"]
    assert client.get(f"/api/driver/v1/statements/{drv_sid}/pdf", headers=h).status_code == 404


def test_tax_summary(client):
    h = _auth(client)
    b = client.get("/api/driver/v1/tax", headers=h).get_json()
    assert float(b["ytd_gross"]) > 0 and b["suggested_set_aside_pct"] == 25


# ── expenses / payout ────────────────────────────────────────────────

def test_expenses_list_and_add(client):
    h = _auth(client)
    assert len(client.get("/api/driver/v1/expenses", headers=h).get_json()["expenses"]) >= 3
    r = client.post("/api/driver/v1/expenses",
                    json={"type": "parking", "amount": "3.20", "note": "N1 bay"}, headers=h)
    assert r.status_code == 200
    assert len(client.get("/api/driver/v1/expenses", headers=h).get_json()["expenses"]) >= 4


def test_payout_request_within_balance(client):
    h = _auth(client)
    avail = float(client.get("/api/driver/v1/earnings", headers=h).get_json()["totals"]["available_balance"])
    assert avail > 0
    r = client.post("/api/driver/v1/payout", json={"amount": "10.00"}, headers=h)
    assert r.status_code == 200 and float(r.get_json()["net"]) < 10.0   # fee applied
    # over-balance is rejected
    assert client.post("/api/driver/v1/payout", json={"amount": str(avail + 9999)}, headers=h).status_code == 400


# ── shift / availability / history (from the legacy app model) ───────

def test_shift_start_status_end(client):
    h = _auth(client)
    # No active shift to start with
    b = client.get("/api/driver/v1/shift", headers=h).get_json()
    assert b["shift"] is None and b["duty_status"] == "off"
    # Start a shift with an end time → available
    assert client.post("/api/driver/v1/shift/start", json={"planned_end": "18:15"}, headers=h).status_code == 200
    b = client.get("/api/driver/v1/shift", headers=h).get_json()
    assert b["shift"]["status"] == "active" and b["shift"]["planned_end"] == "18:15"
    assert b["duty_status"] == "available"
    # Going home
    assert client.post("/api/driver/v1/status", json={"status": "going_home"}, headers=h).status_code == 200
    assert client.get("/api/driver/v1/shift", headers=h).get_json()["duty_status"] == "going_home"
    # End shift → off
    assert client.post("/api/driver/v1/shift/end", headers=h).status_code == 200
    b = client.get("/api/driver/v1/shift", headers=h).get_json()
    assert b["shift"] is None and b["duty_status"] == "off"


def test_invalid_duty_status(client):
    h = _auth(client)
    assert client.post("/api/driver/v1/status", json={"status": "teleporting"}, headers=h).status_code == 400


def test_quick_message_with_category(client):
    h = _auth(client)
    r = client.post("/api/driver/v1/messages", json={"text": "EMERGENCY", "category": "emergency"}, headers=h)
    assert r.status_code == 200
    msgs = client.get("/api/driver/v1/messages", headers=h).get_json()["messages"]
    assert any(m["text"] == "EMERGENCY" and m["category"] == "emergency" for m in msgs)


def test_history_lists_completed(client):
    h = _auth(client)
    d = "XM-20260626-0051"   # single-drop job we can complete
    for action in ("acknowledge", "en_route_pickup", "arrive_pickup"):
        client.post(f"/api/driver/v1/jobs/{d}/status", json={"action": action}, headers=h)
    client.post(f"/api/driver/v1/jobs/{d}/scan", json={"phase": "collect", "barcode": "XM00510101"}, headers=h)
    client.post(f"/api/driver/v1/jobs/{d}/status", json={"action": "collected"}, headers=h)
    client.post(f"/api/driver/v1/jobs/{d}/pod", json={"drop_seq": 1, "recipient_name": "Mailroom"}, headers=h)
    hist = client.get("/api/driver/v1/history", headers=h).get_json()["jobs"]
    assert any(j["docket_number"] == d and j["status"] == "COMPLETED" for j in hist)


# ── commercial hardening: signed media, push, consent, data rights ────

def _complete_pod_with_signature(client, h):
    """Drive a job to a delivered POD that carries a signature image, and
    return the signed media URL the API hands back for it."""
    d = "XM-20260626-0051"
    for action in ("acknowledge", "en_route_pickup", "arrive_pickup"):
        client.post(f"/api/driver/v1/jobs/{d}/status", json={"action": action}, headers=h)
    client.post(f"/api/driver/v1/jobs/{d}/scan", json={"phase": "collect", "barcode": "XM00510101"}, headers=h)
    client.post(f"/api/driver/v1/jobs/{d}/status", json={"action": "collected"}, headers=h)
    png = ("data:image/png;base64,"
           "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
    client.post(f"/api/driver/v1/jobs/{d}/pod",
                json={"drop_seq": 1, "recipient_name": "Mailroom", "signature": png}, headers=h)
    job = client.get(f"/api/driver/v1/jobs/{d}", headers=h).get_json()["job"]
    pods = [p for p in job["pod"] if p.get("signature")]
    assert pods, "expected a signed POD signature URL"
    return pods[0]["signature"]


def test_pod_media_url_is_signed_and_serves(client):
    h = _auth(client)
    url = _complete_pod_with_signature(client, h)
    assert url.startswith("/media/") and "sig=" in url and "exp=" in url
    # A correctly signed URL serves the bytes, no auth header needed (for <img>).
    r = client.get(url)
    assert r.status_code == 200
    assert r.headers.get("Cache-Control", "").startswith("no-store")


def test_pod_media_forbidden_without_signature(client):
    h = _auth(client)
    url = _complete_pod_with_signature(client, h)
    bare = url.split("?")[0]                       # strip the signature
    assert client.get(bare).status_code == 403     # unsigned + no token → denied
    # Tampered signature is also rejected.
    assert client.get(bare + "?exp=9999999999&sig=deadbeef").status_code == 403
    # A valid bearer token is an alternative way in.
    assert client.get(bare, headers=h).status_code == 200


def test_push_register(client):
    h = _auth(client)
    r = client.post("/api/driver/v1/push/register",
                    json={"token": "fcm-abc-123", "platform": "android"}, headers=h)
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert client.post("/api/driver/v1/push/register", json={"token": ""}, headers=h).status_code == 400


def test_push_test_queues_durably_without_credentials(client):
    h = _auth(client)
    client.post("/api/driver/v1/push/register",
                json={"token": "fcm-dev-1", "platform": "android"}, headers=h)
    r = client.post("/api/driver/v1/push/test", headers=h)
    assert r.status_code == 200
    b = r.get_json()
    # No FCM credentials in tests → queued, not sent, and the API says so.
    assert b["configured"] is False and b["tokens"] == 1
    assert b["queued"] == 1 and b["sent"] == 0
    from backend.db import get_connection
    row = get_connection().execute(
        "SELECT status, kind FROM push_outbox WHERE driver_id='DRV001'").fetchone()
    assert row["status"] == "pending" and row["kind"] == "message"


def test_push_respects_notification_preference(client):
    h = _auth(client)
    client.post("/api/driver/v1/push/register",
                json={"token": "fcm-dev-2", "platform": "ios"}, headers=h)
    client.post("/api/driver/v1/profile", json={"fields": {"notify_msgs": 0}}, headers=h)
    b = client.post("/api/driver/v1/push/test", headers=h).get_json()
    assert b["skipped"] == "preference" and b["queued"] == 0 and b["sent"] == 0


def test_push_test_with_no_registered_device(client):
    h = _auth(client, "CX014")
    b = client.post("/api/driver/v1/push/test", headers=h).get_json()
    assert b["tokens"] == 0 and b["queued"] == 0


def test_ops_message_raises_push(client):
    h = _auth(client)
    client.post("/api/driver/v1/push/register",
                json={"token": "fcm-dev-3", "platform": "android"}, headers=h)
    from backend import store
    from backend.db import get_connection
    store.add_message("DRV001", "ops", "New job added to your run")
    row = get_connection().execute(
        "SELECT status, body FROM push_outbox WHERE driver_id='DRV001' ORDER BY id DESC").fetchone()
    assert row["status"] == "pending" and "New job" in row["body"]
    # Driver-sent messages must NOT push back at the driver.
    n_before = get_connection().execute("SELECT COUNT(*) AS n FROM push_outbox").fetchone()["n"]
    store.add_message("DRV001", "driver", "On my way")
    n_after = get_connection().execute("SELECT COUNT(*) AS n FROM push_outbox").fetchone()["n"]
    assert n_after == n_before


def test_location_consent_recorded(client):
    h = _auth(client)
    b = client.post("/api/driver/v1/consent/location", json={"granted": True}, headers=h).get_json()
    assert b["granted"] is True and b["location_consent_at"]
    me = client.get("/api/driver/v1/me", headers=h).get_json()["driver"]
    assert me["location_consent_at"]
    # Withdrawal clears it and re-blocks location.
    client.post("/api/driver/v1/consent/location", json={"granted": False}, headers=h)
    assert client.get("/api/driver/v1/me", headers=h).get_json()["driver"]["location_consent_at"] is None


def test_data_request(client):
    h = _auth(client)
    r = client.post("/api/driver/v1/account/data-request", json={"kind": "access"}, headers=h)
    assert r.status_code == 202 and r.get_json()["kind"] == "access"
    assert client.post("/api/driver/v1/account/data-request", json={"kind": "bogus"}, headers=h).status_code == 400


def test_config_exposes_version(client):
    assert client.get("/api/driver/v1/config").get_json()["app_version"]


# ── profile photos + multi-photo POD ─────────────────────────────────

_PNG = ("data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def test_profile_photo_upload(client):
    h = _auth(client)
    r = client.post("/api/driver/v1/profile/photo", json={"kind": "avatar", "image": _PNG}, headers=h)
    assert r.status_code == 200 and r.get_json()["url"].startswith("/media/")
    v = client.post("/api/driver/v1/profile/photo", json={"kind": "vehicle", "image": _PNG}, headers=h)
    assert v.status_code == 200
    prof = client.get("/api/driver/v1/profile", headers=h).get_json()["profile"]
    assert prof["avatar_url"].startswith("/media/") and "sig=" in prof["avatar_url"]
    assert prof["vehicle_photo_url"].startswith("/media/")
    # bad kind / bad image are rejected
    assert client.post("/api/driver/v1/profile/photo", json={"kind": "x", "image": _PNG}, headers=h).status_code == 400
    assert client.post("/api/driver/v1/profile/photo", json={"kind": "avatar", "image": "nope"}, headers=h).status_code == 400


def test_pod_accepts_multiple_photos(client):
    h = _auth(client)
    d = "XM-20260626-0051"
    for action in ("acknowledge", "en_route_pickup", "arrive_pickup"):
        client.post(f"/api/driver/v1/jobs/{d}/status", json={"action": action}, headers=h)
    client.post(f"/api/driver/v1/jobs/{d}/scan", json={"phase": "collect", "barcode": "XM00510101"}, headers=h)
    client.post(f"/api/driver/v1/jobs/{d}/status", json={"action": "collected"}, headers=h)
    client.post(f"/api/driver/v1/jobs/{d}/pod",
                json={"drop_seq": 1, "recipient_name": "Mailroom", "signature": _PNG,
                      "photos": [_PNG, _PNG]}, headers=h)
    job = client.get(f"/api/driver/v1/jobs/{d}", headers=h).get_json()["job"]
    pod = [p for p in job["pod"] if p["seq"] == 1][0]
    assert len(pod["photos"]) == 2
    assert all(u.startswith("/media/") and "sig=" in u for u in pod["photos"])


# ── security hardening: headers, lockout, scoped media, payload cap ───

def test_security_headers_present(client):
    r = client.get("/api/driver/v1/config")
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert "frame-ancestors 'none'" in r.headers.get("Content-Security-Policy", "")
    assert "object-src 'none'" in r.headers.get("Content-Security-Policy", "")
    assert "geolocation=(self)" in r.headers.get("Permissions-Policy", "")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"


def test_cors_preflight_allows_native_app_origin(client):
    r = client.options("/api/driver/v1/auth/login", headers={
        "Origin": "capacitor://localhost",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type"})
    assert r.status_code == 204
    assert r.headers["Access-Control-Allow-Origin"] == "capacitor://localhost"
    assert "Authorization" in r.headers["Access-Control-Allow-Headers"]
    assert "X-Idempotency-Key" in r.headers["Access-Control-Allow-Headers"]


def test_cors_actual_response_carries_allow_origin(client):
    # iOS native (capacitor://localhost) and Android native (https://localhost)
    for origin in ("capacitor://localhost", "https://localhost"):
        r = client.get("/api/driver/v1/config", headers={"Origin": origin})
        assert r.headers.get("Access-Control-Allow-Origin") == origin


def test_cors_rejects_unknown_origin(client):
    r = client.get("/api/driver/v1/config", headers={"Origin": "https://evil.example"})
    assert r.headers.get("Access-Control-Allow-Origin") is None
    # Preflight from an unknown origin gets no CORS grant either.
    p = client.options("/api/driver/v1/auth/login", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert p.headers.get("Access-Control-Allow-Origin") is None


def test_account_lockout_after_failures(client):
    from backend import config
    for _ in range(config.LOGIN_LOCK_MAX):
        assert _login(client, password="wrong").status_code == 401
    # Now locked — even the CORRECT password is refused with 429.
    r = _login(client, password="test1234")
    assert r.status_code == 429 and r.get_json()["error"]["code"] == "account_locked"


def test_media_url_is_driver_scoped(client):
    h = _auth(client)
    url = _complete_pod_with_signature(client, h)   # /media/..?exp=&did=DRV001&sig=
    assert "did=DRV001" in url
    # Stripping/forging the did breaks the signature → denied.
    import re
    tampered = re.sub(r"did=DRV001", "did=CX014", url)
    assert client.get(tampered).status_code == 403
    assert client.get(url).status_code == 200       # untouched still serves


def test_payload_too_large(tmp_path, monkeypatch):
    from backend import config, db, seed
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(config, "MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 1)     # 1 MB cap for this test
    db.close_connection(); db.init_db(); seed.seed_if_empty()
    from backend.server import create_app
    c = create_app().test_client()
    big = "a" * (1_500_000)                              # ~1.5 MB > 1 MB cap
    r = c.post("/api/driver/v1/auth/login", json={"identifier": big, "password": "x"})
    assert r.status_code == 413


# ── production hardening: multi-worker abuse state, proxy trust, boot guards ──

def test_lockout_survives_restart_and_other_workers(client, tmp_path):
    """Lockout state is in the DB, so a second worker/process (a fresh app
    over the same DB) still refuses the account — and a restart can't wipe it."""
    from backend import config
    from backend.server import create_app
    for _ in range(config.LOGIN_LOCK_MAX):
        assert _login(client, password="wrong").status_code == 401
    other_worker = create_app().test_client()           # fresh app, same DB
    r = _login(other_worker, password="test1234")
    assert r.status_code == 429 and r.get_json()["error"]["code"] == "account_locked"


def test_lockout_is_case_insensitive(client):
    """Identifiers resolve case-insensitively, so the lockout counter must
    too — else varying the case multiplies the brute-force budget."""
    from backend import config
    for _ in range(config.LOGIN_LOCK_MAX):
        assert _login(client, identifier="drv001", password="wrong").status_code == 401
    r = _login(client, identifier="DRV001", password="test1234")
    assert r.status_code == 429 and r.get_json()["error"]["code"] == "account_locked"


def test_successful_login_clears_failure_count(client):
    from backend import config
    for _ in range(config.LOGIN_LOCK_MAX - 1):          # one below the lockout
        assert _login(client, password="wrong").status_code == 401
    assert _login(client).status_code == 200            # success resets the counter
    for _ in range(config.LOGIN_LOCK_MAX - 1):          # a full fresh budget again
        assert _login(client, password="wrong").status_code == 401
    assert _login(client).status_code == 200


def test_forged_xff_cannot_dodge_login_limiter(client):
    """TRUST_PROXY=0 (default): X-Forwarded-For is ignored, so rotating it
    doesn't give an attacker a fresh per-IP window."""
    from backend import config
    got_429 = False
    for i in range(config.LOGIN_RATE_MAX + 1):
        r = client.post("/api/driver/v1/auth/login",
                        json={"identifier": f"GHOST{i}", "password": "x"},
                        headers={"X-Forwarded-For": f"10.0.0.{i}"})
        if r.status_code == 429:
            got_429 = True
    assert got_429


def test_trusted_proxy_separates_clients_by_xff(tmp_path, monkeypatch):
    """TRUST_PROXY=1 (behind App Service/nginx): ProxyFix resolves the real
    client from X-Forwarded-For, so different clients get separate windows."""
    from backend import config, db, seed
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(config, "MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setattr(config, "TRUST_PROXY", 1)
    db.close_connection(); db.init_db(); seed.seed_if_empty()
    from backend.server import create_app
    c = create_app().test_client()
    for i in range(config.LOGIN_RATE_MAX):              # exhaust client A's window
        c.post("/api/driver/v1/auth/login", json={"identifier": f"GHOST{i}", "password": "x"},
               headers={"X-Forwarded-For": "203.0.113.1"})
    ra = c.post("/api/driver/v1/auth/login", json={"identifier": "GHOSTA", "password": "x"},
                headers={"X-Forwarded-For": "203.0.113.1"})
    rb = c.post("/api/driver/v1/auth/login", json={"identifier": "DRV001", "password": "test1234"},
                headers={"X-Forwarded-For": "203.0.113.2"})
    assert ra.status_code == 429                        # A is throttled…
    assert rb.status_code == 200                        # …B is not collateral damage


def test_production_boot_requires_secret_and_refuses_demo_seed(tmp_path, monkeypatch):
    from backend import config, db
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(config, "IS_PRODUCTION", True)
    db.close_connection()
    from backend.server import create_app
    monkeypatch.delenv("DRIVER_APP_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="DRIVER_APP_SECRET"):
        create_app()
    monkeypatch.setenv("DRIVER_APP_SECRET", "x" * 64)
    monkeypatch.setenv("DRIVER_APP_SEED_DEMO", "1")
    with pytest.raises(RuntimeError, match="SEED_DEMO"):
        create_app()
    monkeypatch.delenv("DRIVER_APP_SEED_DEMO")
    create_app()                                        # boots clean


def test_push_flush_claims_rows_once(client, monkeypatch):
    """Two workers flushing the same outbox must not double-send: the claim
    (pending → sending) is a compare-and-set, so a row already claimed by
    another worker is skipped."""
    from backend import push
    from backend.db import get_connection
    conn = get_connection()
    conn.execute(
        "INSERT INTO push_outbox (driver_id, token, platform, kind, title, body, "
        "data_json, status, created_at) VALUES ('DRV001','tok1','android','job',"
        "'T','B',NULL,'pending','2026-07-13T00:00:00Z')")
    conn.commit()
    monkeypatch.setattr(push, "configured", lambda: True)
    sends = []
    monkeypatch.setattr(push, "_fcm_send", lambda tok, *a, **k: (sends.append(tok) or {"ok": True}))
    first = push.flush_pending()
    second = push.flush_pending()                       # the "other worker"
    assert first["sent"] == 1 and second["sent"] == 0
    assert sends == ["tok1"]                            # exactly one delivery


def test_push_flush_requeues_orphaned_claims(client, monkeypatch):
    """A row stuck in 'sending' (worker died mid-send) is re-queued once its
    claim goes stale, so no push is silently lost."""
    from backend import push
    from backend.db import get_connection
    conn = get_connection()
    conn.execute(
        "INSERT INTO push_outbox (driver_id, token, platform, kind, title, body, "
        "data_json, status, created_at, claimed_at) VALUES ('DRV001','tok2','ios',"
        "'job','T','B',NULL,'sending','2026-07-13T00:00:00Z','2026-07-13T00:00:00Z')")
    conn.commit()
    monkeypatch.setattr(push, "configured", lambda: True)
    monkeypatch.setattr(push, "_fcm_send", lambda *a, **k: {"ok": True})
    assert push.flush_pending()["sent"] == 1


# ── shared Google Maps key (same key as TOM: GOOGLE_MAPS_API_KEY) ────

def test_shared_google_maps_key_fallback():
    """When the master switch is ON and only the shared TOM key is set, BOTH
    the server-side and browser keys resolve to it (split vars unset)."""
    import importlib
    from backend import config as cfg
    saved = {k: os.environ.get(k) for k in
             ("GOOGLE_MAPS_API_KEY", "GOOGLE_MAPS_SERVER_KEY", "GOOGLE_MAPS_BROWSER_KEY",
              "TOM_PUBLIC_ORIGIN", "DRIVER_MAPS_ENABLED")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        os.environ["DRIVER_MAPS_ENABLED"] = "1"
        os.environ["GOOGLE_MAPS_API_KEY"] = "AIza-shared-tom-key"
        os.environ["TOM_PUBLIC_ORIGIN"] = "https://driver.example/"
        importlib.reload(cfg)
        assert cfg.GOOGLE_MAPS_API_KEY == "AIza-shared-tom-key"
        assert cfg.GOOGLE_MAPS_SERVER_KEY == "AIza-shared-tom-key"
        assert cfg.GOOGLE_MAPS_BROWSER_KEY == "AIza-shared-tom-key"
        assert cfg.GOOGLE_MAPS_REFERER == "https://driver.example/"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(cfg)


def test_split_keys_take_precedence_over_shared():
    """An operator can still supply dedicated split keys; they win (switch on)."""
    import importlib
    from backend import config as cfg
    keys = ("GOOGLE_MAPS_API_KEY", "GOOGLE_MAPS_SERVER_KEY", "GOOGLE_MAPS_BROWSER_KEY",
            "DRIVER_MAPS_ENABLED")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        os.environ["DRIVER_MAPS_ENABLED"] = "1"
        os.environ["GOOGLE_MAPS_API_KEY"] = "AIza-shared"
        os.environ["GOOGLE_MAPS_SERVER_KEY"] = "AIza-server-only"
        os.environ["GOOGLE_MAPS_BROWSER_KEY"] = "AIza-browser-only"
        importlib.reload(cfg)
        assert cfg.GOOGLE_MAPS_SERVER_KEY == "AIza-server-only"
        assert cfg.GOOGLE_MAPS_BROWSER_KEY == "AIza-browser-only"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(cfg)


def test_routing_sends_referer_for_restricted_key(tmp_path, monkeypatch):
    """The server-side Routes call must send the Referer that TOM's
    referrer-restricted key requires, plus the shared key itself.
    (Needs a real DB: the fail-closed budget counter refuses the call
    otherwise — which is the intended barrier behaviour.)"""
    from backend import routing, db, config as cfg
    monkeypatch.setattr(cfg, "DB_PATH", str(tmp_path / "t.db"))
    db.close_connection()
    db.init_db()
    monkeypatch.setattr(cfg, "GOOGLE_MAPS_SERVER_KEY", "AIza-shared")
    monkeypatch.setattr(cfg, "GOOGLE_MAPS_REFERER", "https://driver.example/")
    captured = {}

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return (b'{"routes":[{"optimizedIntermediateWaypointIndex":[0],'
                    b'"distanceMeters":1500,"duration":"120s"}]}')

    def fake_urlopen(req, timeout=8):
        captured["headers"] = dict(req.headers)
        return FakeResp()

    monkeypatch.setattr(routing.urllib.request, "urlopen", fake_urlopen)
    stops = [{"docket_number": "A", "lat": 51.50, "lng": -0.10},
             {"docket_number": "B", "lat": 51.51, "lng": -0.09}]
    res = routing.optimise((51.52, -0.10), stops)
    assert res["engine"] == "google_routes"
    # urllib.request.Request title-cases header keys.
    assert captured["headers"].get("Referer") == "https://driver.example/"
    assert captured["headers"].get("X-goog-api-key") == "AIza-shared"

# ── QA-audit hardening fixes (July 2026) ──────────────────────────────

def test_finance_today_is_live_with_env_override(monkeypatch):
    """No more frozen demo date: finance.today() is the real current date,
    evaluated per call, with DRIVER_APP_TODAY as a deterministic override."""
    from datetime import date
    from backend import finance
    monkeypatch.delenv("DRIVER_APP_TODAY", raising=False)
    assert finance.today() == date.today()
    monkeypatch.setenv("DRIVER_APP_TODAY", "2026-06-26")
    assert finance.today() == date(2026, 6, 26)
    monkeypatch.setenv("DRIVER_APP_TODAY", "not-a-date")
    assert finance.today() == date.today()          # bad override ignored


def test_todays_earnings_follow_the_real_date(client, monkeypatch):
    """Seed data is anchored to 2026-06-26. Pinned there, the seeded jobs are
    today's work; unpinned (the real date), they are not."""
    h = _auth(client)
    monkeypatch.setenv("DRIVER_APP_TODAY", "2026-06-26")
    b = client.get("/api/driver/v1/earnings", headers=h).get_json()
    assert b["today"]["jobs_total"] == 2
    monkeypatch.delenv("DRIVER_APP_TODAY")
    b = client.get("/api/driver/v1/earnings", headers=h).get_json()
    assert b["today"]["jobs_total"] == 0


def test_driver_reply_docket_requires_ownership(client):
    h = _auth(client)
    # Someone else's job → refused.
    r = client.post("/api/driver/v1/messages",
                    json={"text": "On my way", "docket_number": "XM-20260626-0067"}, headers=h)
    assert r.status_code == 404
    # Own job → stored with the docket tag.
    r = client.post("/api/driver/v1/messages",
                    json={"text": "At pickup now", "docket_number": "XM-20260626-0042"}, headers=h)
    assert r.status_code == 200
    assert r.get_json()["message"]["docket_number"] == "XM-20260626-0042"


def test_driver_mark_read_clears_unread_badge(client):
    h = _auth(client)   # seed leaves one unread ops message
    assert client.get("/api/driver/v1/home", headers=h).get_json()["unread_messages"] >= 1
    r = client.post("/api/driver/v1/messages/read", headers=h)
    assert r.status_code == 200 and r.get_json()["marked"] >= 1
    assert client.get("/api/driver/v1/home", headers=h).get_json()["unread_messages"] == 0
    msgs = client.get("/api/driver/v1/messages", headers=h).get_json()["messages"]
    assert all(m["read"] for m in msgs if m["direction"] == "ops")


def test_location_batch_rejected_off_shift(client):
    """Never been on shift → live pings are refused with 409 so the device
    knows to stop sending."""
    h = _auth(client)
    client.post("/api/driver/v1/consent/location", json={"granted": True}, headers=h)
    r = client.post("/api/driver/v1/location/batch",
                    json={"pings": [{"t": "2026-06-26T08:00:00Z", "lat": 51.5, "lng": -0.1}]}, headers=h)
    assert r.status_code == 409
    assert r.get_json()["error"]["code"] == "no_active_shift"


def test_location_batch_accepts_late_flush_of_onshift_pings(client):
    """The offline queue may flush AFTER shift end: pings recorded during the
    shift still store; pings timestamped after shift end are refused."""
    h = _auth(client)
    client.post("/api/driver/v1/consent/location", json={"granted": True}, headers=h)
    client.post("/api/driver/v1/shift/start", json={"planned_end": "18:00"}, headers=h)
    client.post("/api/driver/v1/shift/end", headers=h)
    during = "2000-01-01T00:00:00Z"                 # before ended_at → on-shift
    after = "2999-01-01T00:00:00Z"                  # after ended_at → off-shift
    r = client.post("/api/driver/v1/location/batch", json={"pings": [
        {"id": "on-1", "t": during, "lat": 51.5, "lng": -0.1},
        {"id": "off-1", "t": after, "lat": 51.5, "lng": -0.1},
    ]}, headers=h)
    assert r.status_code == 202 and r.get_json()["accepted"] == 1
    r2 = client.post("/api/driver/v1/location/batch",
                     json={"pings": [{"id": "off-2", "t": after, "lat": 51.5, "lng": -0.1}]}, headers=h)
    assert r2.status_code == 409


def test_location_ping_coordinate_validation(client):
    """Junk coordinates never reach the locations table; a bad numeric extra
    (speed etc.) is nulled but the fix itself is kept."""
    h = _auth(client)
    client.post("/api/driver/v1/consent/location", json={"granted": True}, headers=h)
    client.post("/api/driver/v1/shift/start", json={"planned_end": "18:00"}, headers=h)
    r = client.post("/api/driver/v1/location/batch", json={"pings": [
        {"t": "2026-06-26T08:00:00Z", "lat": 91.0, "lng": -0.1},     # lat out of range
        {"t": "2026-06-26T08:00:01Z", "lat": "junk", "lng": -0.1},   # non-numeric
        {"t": "2026-06-26T08:00:02Z", "lng": -0.1},                  # missing lat
        {"t": "2026-06-26T08:00:03Z", "lat": 51.5, "lng": -200.0},   # lng out of range
        {"t": "2026-06-26T08:00:04Z", "lat": 51.5, "lng": -0.1, "spd": "fast"},
    ]}, headers=h)
    assert r.status_code == 202 and r.get_json()["accepted"] == 1
    from backend.db import get_connection
    row = get_connection().execute(
        "SELECT speed FROM locations WHERE driver_id='DRV001' AND recorded_at='2026-06-26T08:00:04Z'"
    ).fetchone()
    assert row is not None and row["speed"] is None


def test_completed_job_stamps_completed_at(client):
    h = _auth(client)
    _complete_pod_with_signature(client, h)     # single-drop job → COMPLETED
    from backend.db import get_connection
    row = get_connection().execute(
        "SELECT status, completed_at FROM jobs WHERE docket_number = 'XM-20260626-0051'").fetchone()
    assert row["status"] == "COMPLETED"
    assert row["completed_at"]


def test_media_bearer_fallback_bound_to_owner(client):
    """A valid bearer token is no longer enough for /media/* — the media must
    BELONG to that driver. Cross-driver access is denied."""
    h = _auth(client)
    url = _complete_pod_with_signature(client, h)
    bare = url.split("?")[0]                        # unsigned → bearer fallback
    other = _auth(client, "CX014")
    assert client.get(bare, headers=other).status_code == 403
    assert client.get(bare, headers=h).status_code == 200


def test_config_exposes_demo_flag(client, monkeypatch):
    from backend import config
    assert client.get("/api/driver/v1/config").get_json()["demo"] is True
    monkeypatch.setattr(config, "SEED_DEMO", False)
    assert client.get("/api/driver/v1/config").get_json()["demo"] is False


def test_undeclared_env_under_gunicorn_is_production_like():
    """DRIVER_APP_ENV forgotten + gunicorn serving → production-like: demo
    credentials are never seeded. Declaring the env always wins."""
    import importlib
    from backend import config as cfg
    saved = {k: os.environ.get(k) for k in
             ("DRIVER_APP_ENV", "SERVER_SOFTWARE", "DRIVER_APP_SEED_DEMO")}
    try:
        os.environ.pop("DRIVER_APP_ENV", None)
        os.environ.pop("DRIVER_APP_SEED_DEMO", None)
        os.environ["SERVER_SOFTWARE"] = "gunicorn/21.2.0"
        importlib.reload(cfg)
        assert cfg.IS_PRODUCTION_LIKE is True
        assert cfg.SEED_DEMO is False
        os.environ["DRIVER_APP_ENV"] = "development"    # declared env wins
        importlib.reload(cfg)
        assert cfg.IS_PRODUCTION_LIKE is False
        assert cfg.SEED_DEMO is True
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(cfg)


def test_run_date_filter(client):
    h = _auth(client)
    assert len(client.get("/api/driver/v1/run?date=2026-06-26", headers=h).get_json()["jobs"]) == 2
    assert client.get("/api/driver/v1/run?date=1999-01-01", headers=h).get_json()["jobs"] == []
    # Non-ISO values ('today', junk) keep the historic no-filter behaviour.
    assert len(client.get("/api/driver/v1/run?date=today", headers=h).get_json()["jobs"]) == 2


# ── POD GPS + arrived-at-drop + failure photo (commercial wave) ───────

def _drive_to_pob(client, h, d="XM-20260626-0051", barcodes=("XM00510101",)):
    """Take a seeded job through acknowledge → … → pob."""
    for action in ("acknowledge", "en_route_pickup", "arrive_pickup"):
        client.post(f"/api/driver/v1/jobs/{d}/status", json={"action": action}, headers=h)
    for bc in barcodes:
        client.post(f"/api/driver/v1/jobs/{d}/scan", json={"phase": "collect", "barcode": bc}, headers=h)
    r = client.post(f"/api/driver/v1/jobs/{d}/status", json={"action": "collected"}, headers=h)
    assert r.status_code == 200, r.data
    return d


def test_pod_persists_gps_coordinates(client):
    h = _auth(client)
    d = _drive_to_pob(client, h)
    r = client.post(f"/api/driver/v1/jobs/{d}/pod",
                    json={"drop_seq": 1, "recipient_name": "Mailroom",
                          "lat": 51.532, "lng": -0.119}, headers=h)
    assert r.status_code == 200
    pod = client.get(f"/api/driver/v1/jobs/{d}", headers=h).get_json()["job"]["pod"]
    assert pod[0]["lat"] == 51.532 and pod[0]["lng"] == -0.119


def test_pod_junk_gps_stored_as_null(client):
    h = _auth(client)
    d = _drive_to_pob(client, h)
    r = client.post(f"/api/driver/v1/jobs/{d}/pod",
                    json={"drop_seq": 1, "recipient_name": "Mailroom",
                          "lat": "nonsense", "lng": 999}, headers=h)
    assert r.status_code == 200
    pod = client.get(f"/api/driver/v1/jobs/{d}", headers=h).get_json()["job"]["pod"]
    assert pod[0]["lat"] is None and pod[0]["lng"] is None


def test_arrive_at_drop_then_pod(client):
    h = _auth(client)
    d = _drive_to_pob(client, h)
    r = client.post(f"/api/driver/v1/jobs/{d}/drops/1/arrive",
                    json={"lat": 51.53, "lng": -0.12}, headers=h)
    assert r.status_code == 200 and r.get_json()["arrived_at"]
    job = client.get(f"/api/driver/v1/jobs/{d}", headers=h).get_json()["job"]
    assert job["drops"][0]["status"] == "arrived"
    assert job["drops"][0]["arrived_at"]
    # An arrived drop is NOT resolved: job stays active and on the run.
    assert job["status"] == "IN PROGRESS"
    runs = client.get("/api/driver/v1/run", headers=h).get_json()["jobs"]
    assert d in {j["docket_number"] for j in runs}
    # POD from arrived completes the (single-drop) job as normal.
    pr = client.post(f"/api/driver/v1/jobs/{d}/pod",
                     json={"drop_seq": 1, "recipient_name": "Mailroom"}, headers=h)
    assert pr.status_code == 200 and pr.get_json()["job_completed"] is True


def test_arrive_requires_loaded_job(client):
    h = _auth(client)
    # Job still 'assigned' — not loaded, so arrival is out of order.
    r = client.post("/api/driver/v1/jobs/XM-20260626-0051/drops/1/arrive", json={}, headers=h)
    assert r.status_code == 409 and r.get_json()["error"]["code"] == "invalid_state"


def test_arrive_conflicts(client):
    h = _auth(client)
    # Two-drop job: resolving drop 1 leaves the JOB open, isolating the
    # drop-level conflict answers from the job-closed guard.
    d = _drive_to_pob(client, h, d="XM-20260626-0042",
                      barcodes=("XM00420101", "XM00420201", "XM00420202"))
    assert client.post(f"/api/driver/v1/jobs/{d}/drops/1/arrive", json={}, headers=h).status_code == 200
    # Double arrival → conflict.
    r = client.post(f"/api/driver/v1/jobs/{d}/drops/1/arrive", json={}, headers=h)
    assert r.status_code == 409 and r.get_json()["error"]["code"] == "already_arrived"
    # After resolution → conflict too (deliver-scan drop 1's parcel, then POD it).
    client.post(f"/api/driver/v1/jobs/{d}/scan",
                json={"phase": "deliver", "drop_seq": 1, "barcode": "XM00420101"}, headers=h)
    pr = client.post(f"/api/driver/v1/jobs/{d}/pod",
                     json={"drop_seq": 1, "recipient_name": "M"}, headers=h)
    assert pr.status_code == 200 and pr.get_json()["job_completed"] is False
    r2 = client.post(f"/api/driver/v1/jobs/{d}/drops/1/arrive", json={}, headers=h)
    assert r2.status_code == 409 and r2.get_json()["error"]["code"] == "already_resolved"
    # A closed job refuses arrival outright.
    assert client.post(f"/api/driver/v1/jobs/{d}/drops/2/arrive", json={}, headers=h).status_code == 200
    # Unknown drop and unowned job stay 404.
    assert client.post(f"/api/driver/v1/jobs/{d}/drops/9/arrive", json={}, headers=h).status_code == 404
    h2 = _auth(client, "CX014")
    assert client.post(f"/api/driver/v1/jobs/{d}/drops/1/arrive", json={}, headers=h2).status_code == 404


def test_fail_photo_lands_in_fail_photo_column(client):
    h = _auth(client)
    d = _drive_to_pob(client, h)
    png = ("data:image/png;base64,"
           "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
    r = client.post(f"/api/driver/v1/jobs/{d}/fail",
                    json={"drop_seq": 1, "reason_code": "no_access", "photo": png}, headers=h)
    assert r.status_code == 200
    from backend.db import get_connection
    row = get_connection().execute(
        "SELECT fail_photo, pod_photo FROM drops WHERE docket_number = ? AND seq = 1", (d,),
    ).fetchone()
    assert row["fail_photo"] and row["fail_photo"].startswith("media/")
    assert row["pod_photo"] is None            # no longer misfiled
    pod = client.get(f"/api/driver/v1/jobs/{d}", headers=h).get_json()["job"]["pod"]
    assert pod[0]["fail_reason"] == "no_access"
    assert pod[0]["photo"] and "/media/" in pod[0]["photo"] and "sig=" in pod[0]["photo"]


def test_legacy_fail_photo_in_pod_photo_still_readable(client):
    h = _auth(client)
    d = _drive_to_pob(client, h)
    r = client.post(f"/api/driver/v1/jobs/{d}/fail",
                    json={"drop_seq": 1, "reason_code": "refused"}, headers=h)
    assert r.status_code == 200
    # Simulate a pre-migration row: failure photo sitting in pod_photo.
    from backend.db import get_connection
    conn = get_connection()
    conn.execute("UPDATE drops SET pod_photo = 'media/legacy_fail.png', fail_photo = NULL "
                 "WHERE docket_number = ? AND seq = 1", (d,))
    conn.commit()
    pod = client.get(f"/api/driver/v1/jobs/{d}", headers=h).get_json()["job"]["pod"]
    assert pod[0]["photo"] and pod[0]["photo"].startswith("/media/legacy_fail.png")


# ── breaks, shift-end summary, vehicle check, history POD re-access ──

def _minutes_ago(mins):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_break_flow(client):
    h = _auth(client)
    client.post("/api/driver/v1/shift/start", json={"planned_end": "18:00"}, headers=h)
    # Start a break → duty on_break, break clock open.
    r = client.post("/api/driver/v1/status", json={"status": "on_break"}, headers=h)
    assert r.status_code == 200
    b = client.get("/api/driver/v1/shift", headers=h).get_json()
    assert b["duty_status"] == "on_break" and b["shift"]["break_started_at"]
    # Backdate the break start, then end the break → minutes accumulate.
    from backend.db import get_connection
    conn = get_connection()
    conn.execute("UPDATE shifts SET break_started_at = ? WHERE id = ?",
                 (_minutes_ago(10), b["shift"]["id"]))
    conn.commit()
    assert client.post("/api/driver/v1/status", json={"status": "available"}, headers=h).status_code == 200
    b2 = client.get("/api/driver/v1/shift", headers=h).get_json()
    assert b2["shift"]["break_started_at"] is None
    assert b2["shift"]["break_minutes"] == 10


def test_break_requires_active_shift(client):
    h = _auth(client)
    r = client.post("/api/driver/v1/status", json={"status": "on_break"}, headers=h)
    assert r.status_code == 409 and r.get_json()["error"]["code"] == "no_active_shift"


def test_shift_end_summary(client):
    h = _auth(client)
    # Drop the seeded demo trail — its newest point lands in the same second
    # the shift starts, which would pollute the distance assertion.
    from backend.db import get_connection as _gc
    _gc().execute("DELETE FROM locations WHERE driver_id = 'DRV001'")
    _gc().commit()
    client.post("/api/driver/v1/consent/location", json={"granted": True}, headers=h)
    client.post("/api/driver/v1/shift/start", json={"planned_end": "18:00"}, headers=h)
    # Complete a job during the shift.
    d = _drive_to_pob(client, h)
    assert client.post(f"/api/driver/v1/jobs/{d}/pod",
                       json={"drop_seq": 1, "recipient_name": "Mailroom"}, headers=h).status_code == 200
    # GPS points inside the window (~1.3 km apart). Stamp them with the
    # shift's own start_at so they sit inside [start, end] even though the
    # whole test runs in well under a second.
    start_at = client.get("/api/driver/v1/shift", headers=h).get_json()["shift"]["start_at"]
    client.post("/api/driver/v1/location/batch", json={"pings": [
        {"id": "s-1", "t": start_at, "lat": 51.50, "lng": -0.12},
        {"id": "s-2", "t": start_at, "lat": 51.51, "lng": -0.11},
    ]}, headers=h)
    # Pretend a 10-minute break was taken.
    from backend.db import get_connection
    conn = get_connection()
    conn.execute("UPDATE shifts SET break_minutes = 10 WHERE driver_id = 'DRV001' AND status = 'active'")
    conn.commit()
    r = client.post("/api/driver/v1/shift/end", headers=h)
    assert r.status_code == 200
    s = r.get_json()["summary"]
    assert s["jobs_completed"] == 1
    assert s["drops_delivered"] == 1 and s["drops_failed"] == 0
    assert s["break_minutes"] == 10
    assert s["worked_minutes"] == max(0, s["duration_minutes"] - 10)
    assert s["earnings"] == "19.80"           # 16.30 base + 3.50 waiting
    assert 1.0 < s["distance_km"] < 1.7       # haversine over the two pings
    assert s["ended_at"]


def test_end_shift_closes_open_break(client):
    h = _auth(client)
    client.post("/api/driver/v1/shift/start", json={"planned_end": "18:00"}, headers=h)
    client.post("/api/driver/v1/status", json={"status": "on_break"}, headers=h)
    from backend.db import get_connection
    conn = get_connection()
    conn.execute("UPDATE shifts SET break_started_at = ? WHERE driver_id = 'DRV001' AND status = 'active'",
                 (_minutes_ago(7),))
    conn.commit()
    s = client.post("/api/driver/v1/shift/end", headers=h).get_json()["summary"]
    assert s["break_minutes"] == 7            # open break folded in at shift end


def test_shift_end_without_shift_returns_null_summary(client):
    h = _auth(client)
    r = client.post("/api/driver/v1/shift/end", headers=h)
    assert r.status_code == 200 and r.get_json()["summary"] is None


def test_vehicle_check_flow(client):
    h = _auth(client)
    items = {"tyres": True, "lights": True, "bodywork": True,
             "load_area": True, "oil_coolant": True, "wipers_washers": True}
    # Requires an active shift.
    r = client.post("/api/driver/v1/shift/vehicle-check",
                    json={"odometer": 48200, "items": items}, headers=h)
    assert r.status_code == 409 and r.get_json()["error"]["code"] == "no_active_shift"
    client.post("/api/driver/v1/shift/start", json={"planned_end": "18:00"}, headers=h)
    # Validation: missing item / junk odometer.
    incomplete = dict(items); incomplete.pop("tyres")
    assert client.post("/api/driver/v1/shift/vehicle-check",
                       json={"odometer": 48200, "items": incomplete}, headers=h).status_code == 400
    assert client.post("/api/driver/v1/shift/vehicle-check",
                       json={"odometer": "junk", "items": items}, headers=h).status_code == 400
    # All-pass check stores and surfaces on GET /shift.
    r = client.post("/api/driver/v1/shift/vehicle-check",
                    json={"odometer": 48200, "items": items}, headers=h)
    assert r.status_code == 200 and r.get_json()["failed_items"] == []
    vc = client.get("/api/driver/v1/shift", headers=h).get_json()["vehicle_check"]
    assert vc["odometer"] == 48200 and vc["items"]["tyres"] is True


def test_vehicle_check_defect_flags_ops_not_blocks(client):
    h = _auth(client)
    client.post("/api/driver/v1/shift/start", json={"planned_end": "18:00"}, headers=h)
    items = {"tyres": True, "lights": False, "bodywork": True,
             "load_area": True, "oil_coolant": True, "wipers_washers": False}
    r = client.post("/api/driver/v1/shift/vehicle-check",
                    json={"items": items, "defects": "nearside brake light out"}, headers=h)
    assert r.status_code == 200
    assert r.get_json()["failed_items"] == ["lights", "wipers_washers"]
    # Shift stays active (advisory, never blocking).
    assert client.get("/api/driver/v1/shift", headers=h).get_json()["shift"]["status"] == "active"
    # The defect rode the existing message channel to ops.
    msgs = client.get("/api/driver/v1/messages", headers=h).get_json()["messages"]
    flag = next(m for m in msgs if m["category"] == "vehicle_check")
    assert "lights" in flag["text"] and "nearside brake light out" in flag["text"]
    assert flag["direction"] == "driver"     # counts on the ops unread badge


def test_history_job_detail_returns_pod_for_reaccess(client):
    h = _auth(client)
    d = _drive_to_pob(client, h)
    png = ("data:image/png;base64,"
           "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
    client.post(f"/api/driver/v1/jobs/{d}/pod",
                json={"drop_seq": 1, "recipient_name": "Mailroom", "signature": png,
                      "photos": [png], "lat": 51.53, "lng": -0.12}, headers=h)
    # Job is now COMPLETED and in history…
    hist = client.get("/api/driver/v1/history", headers=h).get_json()["jobs"]
    assert any(j["docket_number"] == d for j in hist)
    # …and the detail read still serves the full POD with signed media URLs.
    job = client.get(f"/api/driver/v1/jobs/{d}", headers=h).get_json()["job"]
    assert job["status"] == "COMPLETED"
    pod = job["pod"][0]
    assert pod["recipient"] == "Mailroom" and pod["at"]
    assert pod["signature"].startswith("/media/") and "sig=" in pod["signature"]
    assert pod["photos"] and all("sig=" in p for p in pod["photos"])
    assert pod["lat"] == 51.53 and pod["lng"] == -0.12


def test_text_size_persists_like_theme(client):
    h = _auth(client)
    assert client.get("/api/driver/v1/profile", headers=h).get_json()["profile"]["text_size"] == "normal"
    r = client.post("/api/driver/v1/profile", json={"fields": {"text_size": "large"}}, headers=h)
    assert r.status_code == 200 and "text_size" in r.get_json()["updated"]
    assert client.get("/api/driver/v1/profile", headers=h).get_json()["profile"]["text_size"] == "large"
