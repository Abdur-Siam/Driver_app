"""TOM bridge — pushes driver-side events to the TOM platform.

Env-gated OFF by default. When BRIDGE_ENABLED=1 and TOM_BRIDGE_URL +
TOM_BRIDGE_KEY are all set, store-layer hooks enqueue events into the
durable bridge_outbox table and a drain delivers them to TOM:

    POST {TOM_BRIDGE_URL}/api/driver-bridge/v1/status     lifecycle events
    POST {TOM_BRIDGE_URL}/api/driver-bridge/v1/pod        proof of delivery
    POST {TOM_BRIDGE_URL}/api/driver-bridge/v1/locations  GPS point batches
    POST {TOM_BRIDGE_URL}/api/driver-bridge/v1/scans      barcode scan events

status events ∈ en_route_pickup | arrived_pickup | pob | en_route_drop |
arrived_drop | delivered | failed ('acknowledge' is app-internal, not bridged).

Delivery contract (stdlib urllib only; 5 s timeout; X-TOM-Bridge-Key header):
  2xx          → sent
  4xx          → dead immediately (a payload TOM will never accept; the
                 response body is kept in last_error for the post-mortem)
  5xx/network  → retried with exponential backoff via next_attempt,
                 dead after MAX_ATTEMPTS

Multi-worker safe: rows are claimed pending→sending with a compare-and-set
(the push.py outbox pattern); claims orphaned by a dead worker re-queue after
_CLAIM_STALE_S. The per-process drainer is a daemon thread started from
create_app() only when the bridge is enabled — tests call drain() directly.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from . import config
from .db import get_connection

MAX_ATTEMPTS = 8
BACKOFF_BASE_S = 5          # retry gaps: 5, 10, 20, 40, 80, 160, 320 s
_CLAIM_STALE_S = 120        # orphaned 'sending' claims re-queue after this

_PATHS = {
    "status": "/api/driver-bridge/v1/status",
    "pod": "/api/driver-bridge/v1/pod",
    "locations": "/api/driver-bridge/v1/locations",
    "scans": "/api/driver-bridge/v1/scans",
}


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def enabled() -> bool:
    """Live only when the flag AND the endpoint AND the key are all set.
    Read from config at call time so tests (and ops) can flip it."""
    return bool(config.BRIDGE_ENABLED and config.TOM_BRIDGE_URL and config.TOM_BRIDGE_KEY)


# ── enqueue (durable outbox writes; no-ops when disabled) ─────────────

def enqueue(kind: str, payload: Dict[str, Any]) -> Optional[int]:
    if not enabled() or kind not in _PATHS:
        return None
    conn = get_connection()
    now = _iso(_now_dt())
    cur = conn.execute(
        "INSERT INTO bridge_outbox (kind, payload_json, status, attempts, next_attempt, created_at) "
        "VALUES (?,?,'pending',0,?,?)", (kind, json.dumps(payload), now, now))
    conn.commit()
    return cur.lastrowid


def enqueue_status(docket, driver_callsign, event, at=None, meta=None) -> Optional[int]:
    return enqueue("status", {
        "docket_number": docket, "driver_callsign": driver_callsign,
        "event": event, "at": at or _iso(_now_dt()), "meta": meta or {}})


def enqueue_pod(docket, recipient, signed_at, lat, lng,
                signature_ref, photo_refs) -> Optional[int]:
    return enqueue("pod", {
        "docket_number": docket, "recipient": recipient, "signed_at": signed_at,
        "lat": lat, "lng": lng, "signature_ref": signature_ref,
        "photo_refs": list(photo_refs or [])})


def enqueue_locations(driver_callsign, points: List[Dict[str, Any]]) -> Optional[int]:
    if not points:
        return None
    return enqueue("locations", {"driver_callsign": driver_callsign, "points": points})


def enqueue_scans(docket, events: List[Dict[str, Any]]) -> Optional[int]:
    if not events:
        return None
    return enqueue("scans", {"docket_number": docket, "events": events})


# ── delivery ──────────────────────────────────────────────────────────

def _post(kind: str, payload_json: str):
    """POST one payload to TOM. Returns (outcome, detail);
    outcome ∈ 'sent' | 'dead' | 'retry'."""
    url = config.TOM_BRIDGE_URL + _PATHS[kind]
    req = urllib.request.Request(
        url, data=payload_json.encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "X-TOM-Bridge-Key": config.TOM_BRIDGE_KEY},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5):
            return "sent", None                      # any 2xx
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = (e.read() or b"")[:500].decode("utf-8", "replace")
        except Exception:
            pass
        if 400 <= e.code < 500:
            return "dead", f"http_{e.code}: {body}"  # TOM will never take it
        return "retry", f"http_{e.code}"
    except Exception as e:                            # URLError / timeout / DNS…
        return "retry", type(e).__name__


def drain(limit: int = 50) -> Dict[str, int]:
    """Deliver due outbox rows once. Safe to call from any worker/thread."""
    out = {"sent": 0, "dead": 0, "retried": 0}
    if not enabled():
        return out
    conn = get_connection()
    now = _iso(_now_dt())
    stale = _iso(_now_dt() - timedelta(seconds=_CLAIM_STALE_S))
    conn.execute("UPDATE bridge_outbox SET status='pending', claimed_at=NULL "
                 "WHERE status='sending' AND claimed_at < ?", (stale,))
    conn.commit()
    rows = conn.execute(
        "SELECT id, kind, payload_json, attempts FROM bridge_outbox "
        "WHERE status='pending' AND next_attempt <= ? ORDER BY id LIMIT ?",
        (now, limit)).fetchall()
    for r in rows:
        cur = conn.execute(
            "UPDATE bridge_outbox SET status='sending', claimed_at=? "
            "WHERE id = ? AND status = 'pending'", (_iso(_now_dt()), r["id"]))
        conn.commit()                    # claim must be visible to other workers
        if cur.rowcount != 1:
            continue                     # another worker won the row
        outcome, detail = _post(r["kind"], r["payload_json"])
        if outcome == "sent":
            conn.execute("UPDATE bridge_outbox SET status='sent', sent_at=?, "
                         "last_error=NULL WHERE id=?", (_iso(_now_dt()), r["id"]))
            out["sent"] += 1
        elif outcome == "dead":
            conn.execute("UPDATE bridge_outbox SET status='dead', attempts=attempts+1, "
                         "last_error=? WHERE id=?", (detail, r["id"]))
            out["dead"] += 1
        else:
            attempts = int(r["attempts"]) + 1
            if attempts >= MAX_ATTEMPTS:
                conn.execute(
                    "UPDATE bridge_outbox SET status='dead', attempts=?, last_error=? WHERE id=?",
                    (attempts, f"{detail or 'retry'} (gave up after {attempts} attempts)", r["id"]))
                out["dead"] += 1
            else:
                nxt = _iso(_now_dt() + timedelta(seconds=BACKOFF_BASE_S * (2 ** (attempts - 1))))
                conn.execute(
                    "UPDATE bridge_outbox SET status='pending', attempts=?, next_attempt=?, "
                    "last_error=?, claimed_at=NULL WHERE id=?",
                    (attempts, nxt, detail, r["id"]))
                out["retried"] += 1
        conn.commit()
    return out


def pending_count() -> int:
    return get_connection().execute(
        "SELECT COUNT(*) AS n FROM bridge_outbox WHERE status = 'pending'").fetchone()["n"]


# ── background drainer (one daemon thread per process) ────────────────

_drainer_lock = threading.Lock()
_drainer_started = False


def start_drainer(interval_s: int = 10) -> bool:
    """Start the per-process drain loop. No-op when disabled or already
    running. Daemon thread — it dies with the worker, and any in-flight row
    it orphans re-queues via the stale-claim sweep."""
    global _drainer_started
    if not enabled():
        return False
    with _drainer_lock:
        if _drainer_started:
            return False
        _drainer_started = True

    def _loop():
        while True:
            time.sleep(interval_s)
            try:
                drain()
            except Exception:
                pass    # never let the drainer die; the next tick retries

    threading.Thread(target=_loop, name="tom-bridge-drain", daemon=True).start()
    return True
