"""Push notification delivery for the Driver App (FCM HTTP v1).

One transport covers both platforms: Android tokens go to FCM directly and
iOS tokens are relayed by FCM to APNs (the APNs key is uploaded to Firebase —
see native/README.md). Design:

  notify_driver() ── preference gate (notify_jobs/pay/msgs)
                  ── fan-out to the driver's registered device_tokens
                  ── durable push_outbox row per token (audit + retry)
                  ── live send when credentials are configured, else the row
                     stays 'pending' and flush_pending() delivers it later.

Credentials (deploy-time, env):
  FCM_PROJECT_ID        Firebase project id
  FCM_CREDENTIALS_JSON  path to a service-account JSON key (falls back to
                        GOOGLE_APPLICATION_CREDENTIALS)
plus `pip install google-auth` (OAuth2 signing for the v1 API). Until those
exist every push is queued, so the whole pipeline is testable now and
deploy-day is credentials only.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from . import config
from .db import get_connection

# kind -> driver preference column; unknown kinds send unconditionally.
_PREF_FOR_KIND = {"job": "notify_jobs", "pay": "notify_pay", "message": "notify_msgs"}

_FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"


def _now() -> str:
    from .store import _now as store_now
    return store_now()


def configured() -> bool:
    """True when live FCM delivery is possible (project + key + google-auth)."""
    if not (config.FCM_PROJECT_ID and config.FCM_CREDENTIALS_JSON):
        return False
    try:
        import google.auth  # noqa: F401
        return True
    except ImportError:
        return False


def _access_token() -> Optional[str]:
    """OAuth2 access token for the FCM v1 API from the service-account key."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_file(
            config.FCM_CREDENTIALS_JSON, scopes=[_FCM_SCOPE])
        creds.refresh(Request())
        return creds.token
    except Exception:
        return None


def _fcm_send(token: str, title: str, body: str,
              data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Send one message via FCM v1. Returns {ok, unregistered?, detail?}."""
    bearer = _access_token()
    if not bearer:
        return {"ok": False, "detail": "credentials_error"}
    msg = {
        "message": {
            "token": token,
            "notification": {"title": title, "body": body},
            "data": {k: str(v) for k, v in (data or {}).items()},
            "android": {"priority": "HIGH"},
            "apns": {"headers": {"apns-priority": "10"}},
        }
    }
    req = urllib.request.Request(
        f"https://fcm.googleapis.com/v1/projects/{config.FCM_PROJECT_ID}/messages:send",
        data=json.dumps(msg).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + bearer},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            return {"ok": True}
    except urllib.error.HTTPError as e:
        # 404 UNREGISTERED / 410 = stale token → caller prunes it.
        return {"ok": False, "unregistered": e.code in (404, 410), "detail": f"http_{e.code}"}
    except Exception as e:
        return {"ok": False, "detail": type(e).__name__}


def _tokens_for(driver_id) -> List[Dict[str, Any]]:
    rows = get_connection().execute(
        "SELECT token, platform FROM device_tokens WHERE driver_id = ?", (driver_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _pref_allows(driver_id, kind: str) -> bool:
    col = _PREF_FOR_KIND.get(kind)
    if not col:
        return True
    row = get_connection().execute(
        f"SELECT {col} AS v FROM drivers WHERE driver_id = ?", (driver_id,),
    ).fetchone()
    return bool(row is None or row["v"] is None or row["v"])


def _queue(conn, driver_id, token, platform, kind, title, body, data, status, detail=None) -> None:
    conn.execute(
        "INSERT INTO push_outbox (driver_id, token, platform, kind, title, body, "
        "data_json, status, detail, created_at, sent_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (driver_id, token, platform, kind, title, body,
         json.dumps(data) if data else None, status, detail, _now(),
         _now() if status == "sent" else None),
    )


def notify_driver(driver_id, kind: str, title: str, body: str,
                  data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Deliver (or durably queue) a push to every device this driver has
    registered, honouring their notification preferences.
    Returns {sent, queued, failed, tokens, skipped?}."""
    if not _pref_allows(driver_id, kind):
        return {"sent": 0, "queued": 0, "failed": 0, "tokens": 0, "skipped": "preference"}
    tokens = _tokens_for(driver_id)
    conn = get_connection()
    live = configured()
    sent = queued = failed = 0
    for t in tokens:
        if not live:
            _queue(conn, driver_id, t["token"], t["platform"], kind, title, body, data, "pending")
            queued += 1
            continue
        r = _fcm_send(t["token"], title, body, data)
        if r["ok"]:
            _queue(conn, driver_id, t["token"], t["platform"], kind, title, body, data, "sent")
            sent += 1
        else:
            if r.get("unregistered"):
                conn.execute("DELETE FROM device_tokens WHERE token = ?", (t["token"],))
            _queue(conn, driver_id, t["token"], t["platform"], kind, title, body, data,
                   "failed", r.get("detail"))
            failed += 1
    conn.commit()
    return {"sent": sent, "queued": queued, "failed": failed, "tokens": len(tokens)}


# A claim older than this is presumed orphaned (worker died mid-send) and
# is re-queued on the next flush. Comfortably above the 10 s send timeout.
_CLAIM_STALE_S = 120


def flush_pending(limit: int = 200) -> Dict[str, Any]:
    """Deliver queued pushes once credentials exist (boot / cron seam).

    Multi-worker safe: every gunicorn worker calls this on boot, so each row
    is claimed (pending → sending) with a compare-and-set before delivery —
    whoever wins the claim sends; everyone else skips. Orphaned claims from a
    dead worker are re-queued after _CLAIM_STALE_S."""
    if not configured():
        return {"sent": 0, "failed": 0, "pending": _pending_count()}
    conn = get_connection()
    stale = (datetime.now(timezone.utc) - timedelta(seconds=_CLAIM_STALE_S)
             ).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("UPDATE push_outbox SET status='pending', claimed_at=NULL "
                 "WHERE status='sending' AND claimed_at < ?", (stale,))
    conn.commit()
    rows = conn.execute(
        "SELECT id, token, title, body, data_json FROM push_outbox "
        "WHERE status = 'pending' ORDER BY id LIMIT ?", (limit,),
    ).fetchall()
    sent = failed = 0
    for r in rows:
        cur = conn.execute(
            "UPDATE push_outbox SET status='sending', claimed_at=? "
            "WHERE id = ? AND status = 'pending'", (_now(), r["id"]))
        conn.commit()   # claim must be visible to other workers before the send
        if cur.rowcount != 1:
            continue    # another worker got there first
        res = _fcm_send(r["token"], r["title"], r["body"],
                        json.loads(r["data_json"]) if r["data_json"] else None)
        if res["ok"]:
            conn.execute("UPDATE push_outbox SET status='sent', sent_at=? WHERE id=?",
                         (_now(), r["id"]))
            sent += 1
        else:
            if res.get("unregistered"):
                conn.execute("DELETE FROM device_tokens WHERE token = ?", (r["token"],))
            conn.execute("UPDATE push_outbox SET status='failed', detail=? WHERE id=?",
                         (res.get("detail"), r["id"]))
            failed += 1
        conn.commit()
    return {"sent": sent, "failed": failed, "pending": _pending_count()}


def _pending_count() -> int:
    return get_connection().execute(
        "SELECT COUNT(*) AS n FROM push_outbox WHERE status = 'pending'").fetchone()["n"]
