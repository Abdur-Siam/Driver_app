"""TOM bridge tests — enqueue hooks, delivery contract, gating.

Run:
    cd Driver/app && PYTHONPATH=. python -m pytest tests/test_bridge.py -q
"""
from __future__ import annotations

import io
import json
import os
import sys
import urllib.error

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_HERE)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

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


@pytest.fixture()
def bridged(client, monkeypatch):
    """Client with the bridge switched ON. No drainer thread — tests call
    bridge.drain() directly so delivery is deterministic."""
    from backend import config
    monkeypatch.setattr(config, "BRIDGE_ENABLED", True)
    monkeypatch.setattr(config, "TOM_BRIDGE_URL", "https://tom.example")
    monkeypatch.setattr(config, "TOM_BRIDGE_KEY", "test-bridge-key")
    return client


def _auth(c, identifier="DRV001", password="test1234"):
    r = c.post(DRV + "/auth/login", json={"identifier": identifier, "password": password})
    assert r.status_code == 200, r.data
    return {"Authorization": "Bearer " + r.get_json()["token"]}


def _rows():
    from backend.db import get_connection
    return [dict(r) for r in get_connection().execute(
        "SELECT * FROM bridge_outbox ORDER BY id").fetchall()]


def _drive_to_pob(c, h, d="XM-20260626-0051", barcodes=("XM00510101",)):
    for action in ("acknowledge", "en_route_pickup", "arrive_pickup"):
        c.post(f"{DRV}/jobs/{d}/status", json={"action": action}, headers=h)
    for bc in barcodes:
        c.post(f"{DRV}/jobs/{d}/scan", json={"phase": "collect", "barcode": bc}, headers=h)
    r = c.post(f"{DRV}/jobs/{d}/status", json={"action": "collected"}, headers=h)
    assert r.status_code == 200, r.data
    return d


_PNG = ("data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


# ── gating ────────────────────────────────────────────────────────────

def test_disabled_mode_enqueues_nothing(client):
    h = _auth(client)
    d = _drive_to_pob(client, h)
    client.post(f"{DRV}/jobs/{d}/drops/1/arrive", json={}, headers=h)
    client.post(f"{DRV}/jobs/{d}/pod", json={"drop_seq": 1, "recipient_name": "M"}, headers=h)
    assert _rows() == []


def test_partial_config_stays_disabled(client, monkeypatch):
    from backend import bridge, config
    assert bridge.enabled() is False
    monkeypatch.setattr(config, "BRIDGE_ENABLED", True)     # flag alone: no
    assert bridge.enabled() is False
    monkeypatch.setattr(config, "TOM_BRIDGE_URL", "https://tom.example")
    assert bridge.enabled() is False                        # key still missing
    monkeypatch.setattr(config, "TOM_BRIDGE_KEY", "k")
    assert bridge.enabled() is True
    assert bridge.drain() == {"sent": 0, "dead": 0, "retried": 0}


# ── enqueue hooks ─────────────────────────────────────────────────────

def test_lifecycle_and_scan_events_enqueue(bridged):
    h = _auth(bridged)
    d = "XM-20260626-0051"
    for action in ("acknowledge", "en_route_pickup", "arrive_pickup"):
        assert bridged.post(f"{DRV}/jobs/{d}/status", json={"action": action},
                            headers=h).status_code == 200
    bridged.post(f"{DRV}/jobs/{d}/scan",
                 json={"phase": "collect", "barcode": "XM00510101", "lat": 51.52, "lng": -0.1},
                 headers=h)
    bridged.post(f"{DRV}/jobs/{d}/status", json={"action": "collected"}, headers=h)
    bridged.post(f"{DRV}/jobs/{d}/status", json={"action": "en_route_drop"}, headers=h)
    bridged.post(f"{DRV}/jobs/{d}/drops/1/arrive", json={"lat": 51.53, "lng": -0.12}, headers=h)
    rows = _rows()
    events = [json.loads(r["payload_json"])["event"] for r in rows if r["kind"] == "status"]
    # 'acknowledge' is app-internal — never bridged. The rest map 1:1.
    assert events == ["en_route_pickup", "arrived_pickup", "pob", "en_route_drop", "arrived_drop"]
    first = json.loads([r for r in rows if r["kind"] == "status"][0]["payload_json"])
    assert first["docket_number"] == d and first["driver_callsign"] == "DRV001"
    assert first["at"] and first["meta"] == {}
    arrived = json.loads(rows[-1]["payload_json"])
    assert arrived["event"] == "arrived_drop" and arrived["meta"] == {"drop_seq": 1}
    scans = [r for r in rows if r["kind"] == "scans"]
    assert len(scans) == 1
    sp = json.loads(scans[0]["payload_json"])
    assert sp["docket_number"] == d
    assert sp["events"] == [{"barcode": "XM00510101", "event_type": "collect",
                             "lat": 51.52, "lng": -0.1,
                             "scanned_at": sp["events"][0]["scanned_at"]}]
    assert sp["events"][0]["scanned_at"]
    # Every row starts pending with 0 attempts and an immediate next_attempt.
    assert all(r["status"] == "pending" and r["attempts"] == 0 and r["next_attempt"]
               for r in rows)


def test_pod_and_fail_enqueue(bridged):
    h = _auth(bridged)
    d = _drive_to_pob(bridged, h)
    r = bridged.post(f"{DRV}/jobs/{d}/pod",
                     json={"drop_seq": 1, "recipient_name": "Mailroom", "signature": _PNG,
                           "photos": [_PNG], "lat": 51.53, "lng": -0.12}, headers=h)
    assert r.status_code == 200
    rows = _rows()
    pods = [x for x in rows if x["kind"] == "pod"]
    assert len(pods) == 1
    pp = json.loads(pods[0]["payload_json"])
    assert pp["docket_number"] == d and pp["recipient"] == "Mailroom"
    assert pp["lat"] == 51.53 and pp["lng"] == -0.12 and pp["signed_at"]
    assert pp["signature_ref"].startswith("media/")
    assert len(pp["photo_refs"]) == 1 and pp["photo_refs"][0].startswith("media/")
    delivered = [json.loads(x["payload_json"]) for x in rows if x["kind"] == "status"
                 and json.loads(x["payload_json"])["event"] == "delivered"]
    assert delivered and delivered[0]["meta"] == {"drop_seq": 1, "job_completed": True}
    # Failed delivery raises a 'failed' status event with the reason.
    d2 = _drive_to_pob(bridged, h, d="XM-20260626-0042",
                       barcodes=("XM00420101", "XM00420201", "XM00420202"))
    bridged.post(f"{DRV}/jobs/{d2}/fail",
                 json={"drop_seq": 1, "reason_code": "no_access"}, headers=h)
    failed = [json.loads(x["payload_json"]) for x in _rows() if x["kind"] == "status"
              and json.loads(x["payload_json"])["event"] == "failed"]
    assert failed and failed[0]["docket_number"] == d2
    assert failed[0]["meta"]["reason"] == "no_access"
    assert failed[0]["meta"]["drop_seq"] == 1 and failed[0]["meta"]["job_completed"] is False


def test_location_batches_enqueue(bridged):
    h = _auth(bridged)
    bridged.post(DRV + "/consent/location", json={"granted": True}, headers=h)
    bridged.post(DRV + "/shift/start", json={"planned_end": "18:00"}, headers=h)
    r = bridged.post(DRV + "/location/batch", json={"pings": [
        {"id": "b-1", "t": "2026-06-26T09:00:00Z", "lat": 51.50, "lng": -0.12},
        {"id": "b-2", "t": "2026-06-26T09:00:05Z", "lat": 51.51, "lng": -0.11},
        {"id": "b-3", "t": "junk", "lat": "junk", "lng": -0.11},   # invalid → skipped
    ]}, headers=h)
    assert r.status_code == 202 and r.get_json()["accepted"] == 2
    rows = [x for x in _rows() if x["kind"] == "locations"]
    assert len(rows) == 1
    lp = json.loads(rows[0]["payload_json"])
    assert lp["driver_callsign"] == "DRV001"
    assert lp["points"] == [
        {"lat": 51.50, "lng": -0.12, "recorded_at": "2026-06-26T09:00:00Z", "ping_id": "b-1"},
        {"lat": 51.51, "lng": -0.11, "recorded_at": "2026-06-26T09:00:05Z", "ping_id": "b-2"},
    ]
    # A fully-deduped replay stores nothing → enqueues nothing.
    bridged.post(DRV + "/location/batch", json={"pings": [
        {"id": "b-1", "t": "2026-06-26T09:00:00Z", "lat": 51.50, "lng": -0.12}]}, headers=h)
    assert len([x for x in _rows() if x["kind"] == "locations"]) == 1


# ── drain / delivery contract ─────────────────────────────────────────

class _Resp2xx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b"{}"


def test_drain_delivers_and_marks_sent(bridged, monkeypatch):
    from backend import bridge
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append((req.full_url, dict(req.header_items()), req.data, timeout))
        return _Resp2xx()

    monkeypatch.setattr(bridge.urllib.request, "urlopen", fake_urlopen)
    bridge.enqueue_status("XM-1", "DRV001", "pob")
    bridge.enqueue_pod("XM-1", "Bob", "2026-06-26T10:00:00Z", 51.5, -0.1,
                       "media/sig.png", ["media/p1.jpg"])
    res = bridge.drain()
    assert res == {"sent": 2, "dead": 0, "retried": 0}
    # Exact endpoints, key header, 5 s timeout, JSON body.
    assert [c[0] for c in calls] == [
        "https://tom.example/api/driver-bridge/v1/status",
        "https://tom.example/api/driver-bridge/v1/pod",
    ]
    hdrs = {k.lower(): v for k, v in calls[0][1].items()}
    assert hdrs["x-tom-bridge-key"] == "test-bridge-key"
    assert hdrs["content-type"] == "application/json"
    assert calls[0][3] == 5
    body = json.loads(calls[0][2].decode())
    assert body["docket_number"] == "XM-1" and body["event"] == "pob"
    pod_body = json.loads(calls[1][2].decode())
    assert pod_body["signature_ref"] == "media/sig.png"
    assert pod_body["photo_refs"] == ["media/p1.jpg"]
    rows = _rows()
    assert all(r["status"] == "sent" and r["sent_at"] and r["last_error"] is None
               for r in rows)
    # Nothing left to do.
    assert bridge.drain() == {"sent": 0, "dead": 0, "retried": 0}


def test_drain_4xx_is_dead_with_body_logged(bridged, monkeypatch):
    from backend import bridge

    def reject(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 422, "Unprocessable",
                                     None, io.BytesIO(b'{"error":"unknown docket"}'))

    monkeypatch.setattr(bridge.urllib.request, "urlopen", reject)
    bridge.enqueue_status("XM-BAD", "DRV001", "pob")
    assert bridge.drain() == {"sent": 0, "dead": 1, "retried": 0}
    row = _rows()[0]
    assert row["status"] == "dead"
    assert "http_422" in row["last_error"] and "unknown docket" in row["last_error"]
    # Dead rows are never retried.
    assert bridge.drain() == {"sent": 0, "dead": 0, "retried": 0}


def test_drain_5xx_retries_with_backoff_then_dead(bridged, monkeypatch):
    from backend import bridge

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 503, "boom", None, None)

    monkeypatch.setattr(bridge.urllib.request, "urlopen", boom)
    bridge.enqueue_status("XM-1", "DRV001", "pob")
    assert bridge.drain()["retried"] == 1
    row = _rows()[0]
    assert row["status"] == "pending" and row["attempts"] == 1
    assert row["last_error"] == "http_503"
    assert row["next_attempt"] > row["created_at"]      # backoff pushed it out
    # Not due yet → drain leaves it alone.
    assert bridge.drain() == {"sent": 0, "dead": 0, "retried": 0}
    # At the attempt ceiling the row dies instead of retrying forever.
    from backend.db import get_connection
    conn = get_connection()
    conn.execute("UPDATE bridge_outbox SET attempts = ?, next_attempt = '2000-01-01T00:00:00Z'",
                 (bridge.MAX_ATTEMPTS - 1,))
    conn.commit()
    assert bridge.drain()["dead"] == 1
    row = _rows()[0]
    assert row["status"] == "dead" and row["attempts"] == bridge.MAX_ATTEMPTS
    assert "gave up" in row["last_error"]


def test_drain_network_error_retries(bridged, monkeypatch):
    from backend import bridge

    def net_fail(req, timeout=None):
        raise urllib.error.URLError("dns exploded")

    monkeypatch.setattr(bridge.urllib.request, "urlopen", net_fail)
    bridge.enqueue_status("XM-1", "DRV001", "pob")
    assert bridge.drain()["retried"] == 1
    assert _rows()[0]["last_error"] == "URLError"
    assert _rows()[0]["status"] == "pending"
