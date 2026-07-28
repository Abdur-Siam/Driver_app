"""Route optimisation.

Server-side by design — the Google Maps key never reaches the device.
When GOOGLE_MAPS_SERVER_KEY is set, this calls the Google Routes API to
order the stops; otherwise it falls back to a nearest-neighbour
haversine heuristic so the app is fully functional before the operator
provisions a key (the key is supplied later — see config.py / README).
"""
from __future__ import annotations

import json
import math
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from . import config


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
            "X-Goog-FieldMask":
                "routes.optimizedIntermediateWaypointIndex,routes.distanceMeters,routes.duration",
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
    """Return {ordered_dockets, total_distance_km, engine}."""
    engine = "haversine_fallback"
    result = None
    if config.GOOGLE_MAPS_SERVER_KEY:
        result = _google_optimise(origin, stops)
        if result is not None:
            engine = "google_routes"
    if result is None:
        result = _nearest_neighbour(origin, stops)
    ordered, total_km = result
    return {
        "ordered_dockets": [s["docket_number"] for s in ordered],
        "total_distance_km": round(total_km, 1),
        "engine": engine,
    }
