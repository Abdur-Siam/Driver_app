"""Route optimisation.

Server-side by design — the Google Maps key never reaches the device.
When GOOGLE_MAPS_SERVER_KEY is set (requires the DRIVER_MAPS_ENABLED
master switch — see config.py), this calls the Google Routes API to
order the stops; otherwise it falls back to a nearest-neighbour
haversine heuristic so the app is fully functional without Google.

Cost barriers (28 Jul 2026, after the £415 Places bill on the TOM side):
  * This module is the app's ONLY outbound Google Maps call site — the
    app uses no Places/Geocoding/Place Details anywhere (guard-tested).
  * The field mask is pinned to three Essentials-tier route fields.
    Google bills at the highest SKU tier among requested fields, so
    adding a field here can multiply the per-call price — change it
    only with a deliberate cost decision.
  * Results are cached (maps_route_cache) keyed on the rounded
    coordinates + docket set: re-optimising the same run is free.
  * Calls are hard-capped per UTC day (maps_usage / MAPS_DAILY_CAP) and
    the cap FAILS CLOSED — if the counter cannot be read or advanced,
    the Google call is not made and the haversine order is used.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from . import config, db

# Essentials-tier fields only. Do NOT add fields casually: Routes/Places
# bill the whole call at the tier of the most expensive field requested.
ROUTES_FIELD_MASK = (
    "routes.optimizedIntermediateWaypointIndex,routes.distanceMeters,routes.duration"
)


def haversine_km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    R = 6371.0
    lat1, lon1, lat2, lon2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def _nearest_neighbour(origin, stops: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    remaining = [s for s in stops if s.get("lat") is not None and s.get("lng") is not None]
    no_coords = [s for s in stops if s.get("lat") is None or s.get("lng") is None]
    ordered: List[Dict[str, Any]] = []
    cur = origin
    total_km = 0.0
    while remaining:
        nxt = min(remaining, key=lambda s: haversine_km(cur, (s["lat"], s["lng"])))
        total_km += haversine_km(cur, (nxt["lat"], nxt["lng"]))
        ordered.append(nxt)
        cur = (nxt["lat"], nxt["lng"])
        remaining.remove(nxt)
    ordered.extend(no_coords)  # unknown-coord stops appended in booked order
    return ordered, total_km


# ── cost barriers ────────────────────────────────────────────────────

def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _today() -> str:
    return _utcnow().strftime("%Y-%m-%d")


def _cache_key(origin: Tuple[float, float], stops: List[Dict[str, Any]]) -> str:
    """Coordinates rounded to 5 dp (~1 m) + dockets in booked order — the
    same run asked twice produces the same key."""
    basis = [round(origin[0], 5), round(origin[1], 5)] + [
        [s["docket_number"], round(s["lat"], 5), round(s["lng"], 5)] for s in stops
    ]
    return hashlib.sha256(json.dumps(basis, separators=(",", ":")).encode()).hexdigest()


def _cache_get(key: str) -> Optional[Tuple[List[str], float]]:
    try:
        row = db.get_connection().execute(
            "SELECT ordered_json, distance_km, created_at FROM maps_route_cache"
            " WHERE cache_key = ?", (key,)).fetchone()
        if not row:
            return None
        created = _dt.datetime.fromisoformat(row["created_at"])
        if _utcnow() - created > _dt.timedelta(hours=config.MAPS_CACHE_TTL_HOURS):
            return None
        return json.loads(row["ordered_json"]), row["distance_km"] or 0.0
    except Exception:
        return None


def _cache_put(key: str, dockets: List[str], km: float) -> None:
    try:
        conn = db.get_connection()
        conn.execute(
            "INSERT OR REPLACE INTO maps_route_cache"
            " (cache_key, ordered_json, distance_km, created_at) VALUES (?,?,?,?)",
            (key, json.dumps(dockets), km, _utcnow().isoformat()))
        conn.commit()
    except Exception:
        pass  # a failed cache write must never break routing


def _budget_take() -> bool:
    """Reserve one Google call from today's budget. FAIL-CLOSED: any error
    (missing table, locked DB) refuses the call — the worst outcome is a
    haversine order, never an unmetered Google bill. The counter is
    per-connection-atomic; concurrent workers can overshoot the cap by at
    most the worker count, which is acceptable for a cost ceiling."""
    if config.MAPS_DAILY_CAP <= 0:
        return False
    try:
        conn = db.get_connection()
        day = _today()
        conn.execute("INSERT OR IGNORE INTO maps_usage (day, calls) VALUES (?, 0)", (day,))
        cur = conn.execute(
            "UPDATE maps_usage SET calls = calls + 1 WHERE day = ? AND calls < ?",
            (day, config.MAPS_DAILY_CAP))
        conn.commit()
        return cur.rowcount == 1
    except Exception:
        return False


def usage_today() -> Dict[str, Any]:
    """Read-only view of today's Google-call spend (for ops/diagnostics)."""
    try:
        row = db.get_connection().execute(
            "SELECT calls FROM maps_usage WHERE day = ?", (_today(),)).fetchone()
        calls = row["calls"] if row else 0
    except Exception:
        calls = None
    return {"day": _today(), "calls": calls, "cap": config.MAPS_DAILY_CAP,
            "enabled": bool(config.GOOGLE_MAPS_SERVER_KEY)}


def _google_optimise(origin, stops: List[Dict[str, Any]]) -> Optional[List[Dict[str, Any]]]:
    """Call Google Routes API computeRoutes with optimizeWaypointOrder.
    Returns the reordered stop list, or None on any failure (caller falls
    back). Kept dependency-free (urllib) so the app has no extra installs."""
    coord_stops = [s for s in stops if s.get("lat") is not None and s.get("lng") is not None]
    if len(coord_stops) < 2:
        return None
    body = {
        "origin": {"location": {"latLng": {"latitude": origin[0], "longitude": origin[1]}}},
        "destination": {"location": {"latLng": {
            "latitude": coord_stops[-1]["lat"], "longitude": coord_stops[-1]["lng"]}}},
        "intermediates": [
            {"location": {"latLng": {"latitude": s["lat"], "longitude": s["lng"]}}}
            for s in coord_stops[:-1]
        ],
        "travelMode": "DRIVE",
        "optimizeWaypointOrder": True,
    }
    req = urllib.request.Request(
        "https://routes.googleapis.com/directions/v2:computeRoutes",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": config.GOOGLE_MAPS_SERVER_KEY,
            "X-Goog-FieldMask": ROUTES_FIELD_MASK,
            # TOM's shared key is referrer-restricted: server-side REST calls are
            # only honoured when the Referer matches the key's allow-list (proven
            # against Routes on 21 Jul 2026 — see app/modules/google_maps.py).
            "Referer": config.GOOGLE_MAPS_REFERER,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        route = (data.get("routes") or [None])[0]
        if not route:
            return None
        order = route.get("optimizedIntermediateWaypointIndex", [])
        reordered = [coord_stops[i] for i in order] + [coord_stops[-1]]
        no_coords = [s for s in stops if s.get("lat") is None or s.get("lng") is None]
        return reordered + no_coords, (route.get("distanceMeters") or 0) / 1000.0
    except Exception:
        return None


def optimise(origin: Tuple[float, float], stops: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Return {ordered_dockets, total_distance_km, engine}.

    engine: google_routes | google_routes_cached | haversine_capped |
    haversine_fallback. Anything starting "google_routes" came from Google
    (the frontend keys its label off that prefix)."""
    engine = "haversine_fallback"
    result = None
    if config.GOOGLE_MAPS_SERVER_KEY:
        coord_stops = [s for s in stops if s.get("lat") is not None and s.get("lng") is not None]
        key = _cache_key(origin, coord_stops) if len(coord_stops) >= 2 else None
        if key:
            hit = _cache_get(key)
            if hit is not None:
                dockets, km = hit
                by_docket = {s["docket_number"]: s for s in stops}
                if all(d in by_docket for d in dockets) and len(dockets) == len(stops):
                    return {"ordered_dockets": dockets,
                            "total_distance_km": round(km, 1),
                            "engine": "google_routes_cached"}
        if key and _budget_take():
            result = _google_optimise(origin, stops)
            if result is not None:
                engine = "google_routes"
                _cache_put(key, [s["docket_number"] for s in result[0]], result[1])
        elif key:
            engine = "haversine_capped"   # budget refused (cap hit or counter down)
    if result is None:
        result = _nearest_neighbour(origin, stops)
    ordered, total_km = result
    return {
        "ordered_dockets": [s["docket_number"] for s in ordered],
        "total_distance_km": round(total_km, 1),
        "engine": engine,
    }
