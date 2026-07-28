"""Google Maps disconnect + cost barriers (28 Jul 2026).

Context: a £415/month Places API (New) bill on the shared Google account.
This app's Google surface is deliberately reduced to ONE server-side
Routes call (routing.py) plus the browser Maps JS — and the whole
connection is behind the DRIVER_MAPS_ENABLED master switch, OFF by
default. These tests pin:

  1. The disconnect — with the switch off, keys resolve blank even when
     GOOGLE_MAPS_API_KEY is in the environment; /config ships no key;
     the CSP stays closed to Google hosts; optimise() never opens a
     socket to Google.
  2. The barriers — pinned Essentials-only field mask, per-day
     fail-closed call cap, route-result cache, and a repo guard that no
     Places/Geocoding usage exists anywhere.

Run:
    cd Driver/app && PYTHONPATH=. python -m pytest tests/test_maps_barriers.py -q
"""
from __future__ import annotations

import importlib
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_HERE)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

_ENV_KEYS = ("DRIVER_MAPS_ENABLED", "GOOGLE_MAPS_API_KEY",
             "GOOGLE_MAPS_SERVER_KEY", "GOOGLE_MAPS_BROWSER_KEY")


@pytest.fixture()
def clean_env():
    """Save/clear the maps env vars, reload config after the test."""
    from backend import config as cfg
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    for k in _ENV_KEYS:
        os.environ.pop(k, None)
    yield cfg
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    importlib.reload(cfg)


@pytest.fixture()
def app_db(tmp_path, monkeypatch):
    from backend import config, db
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(config, "MEDIA_DIR", str(tmp_path / "media"))
    db.close_connection()
    db.init_db()
    return db


def _no_network(monkeypatch):
    """Any urllib call = test failure. Proves zero Google traffic."""
    from backend import routing

    def boom(*a, **k):  # pragma: no cover - reaching this IS the failure
        raise AssertionError("outbound HTTP attempted while maps are disconnected")
    monkeypatch.setattr(routing.urllib.request, "urlopen", boom)


# ── 1. the disconnect (master switch off by default) ─────────────────

def test_switch_off_blanks_all_keys_even_with_env_key(clean_env):
    cfg = clean_env
    os.environ["GOOGLE_MAPS_API_KEY"] = "AIza-tom-shared"
    os.environ["GOOGLE_MAPS_SERVER_KEY"] = "AIza-server"
    os.environ["GOOGLE_MAPS_BROWSER_KEY"] = "AIza-browser"
    importlib.reload(cfg)          # DRIVER_MAPS_ENABLED unset → disconnected
    assert cfg.DRIVER_MAPS_ENABLED is False
    assert cfg.GOOGLE_MAPS_API_KEY == ""
    assert cfg.GOOGLE_MAPS_SERVER_KEY == ""
    assert cfg.GOOGLE_MAPS_BROWSER_KEY == ""


def test_switch_explicit_zero_stays_off(clean_env):
    cfg = clean_env
    os.environ["DRIVER_MAPS_ENABLED"] = "0"
    os.environ["GOOGLE_MAPS_API_KEY"] = "AIza-tom-shared"
    importlib.reload(cfg)
    assert cfg.GOOGLE_MAPS_SERVER_KEY == "" and cfg.GOOGLE_MAPS_BROWSER_KEY == ""


def test_switch_on_restores_key_resolution(clean_env):
    cfg = clean_env
    os.environ["DRIVER_MAPS_ENABLED"] = "1"
    os.environ["GOOGLE_MAPS_API_KEY"] = "AIza-tom-shared"
    importlib.reload(cfg)
    assert cfg.GOOGLE_MAPS_SERVER_KEY == "AIza-tom-shared"
    assert cfg.GOOGLE_MAPS_BROWSER_KEY == "AIza-tom-shared"


def test_config_endpoint_ships_no_key_when_disconnected(app_db, monkeypatch):
    """/config (driver) answers maps_enabled false + null key with the
    switch off, so the frontend renders the fallback panel and never
    loads maps.googleapis.com."""
    from backend import config as cfg, seed
    monkeypatch.setattr(cfg, "GOOGLE_MAPS_BROWSER_KEY", "")
    seed.seed_if_empty()
    from backend.server import create_app
    c = create_app().test_client()
    b = c.get("/api/driver/v1/config").get_json()
    assert b["maps_enabled"] is False and b["maps_browser_key"] is None


def test_csp_stays_closed_to_google_when_disconnected(app_db, monkeypatch):
    from backend import config as cfg, seed
    monkeypatch.setattr(cfg, "GOOGLE_MAPS_BROWSER_KEY", "")
    seed.seed_if_empty()
    from backend.server import create_app
    c = create_app().test_client()
    csp = c.get("/").headers.get("Content-Security-Policy", "")
    assert "maps.googleapis.com" not in csp


def test_optimise_makes_zero_google_calls_when_disconnected(app_db, monkeypatch):
    from backend import routing, config as cfg
    monkeypatch.setattr(cfg, "GOOGLE_MAPS_SERVER_KEY", "")
    _no_network(monkeypatch)
    stops = [{"docket_number": "A", "lat": 51.50, "lng": -0.10},
             {"docket_number": "B", "lat": 51.51, "lng": -0.09}]
    res = routing.optimise((51.52, -0.10), stops)
    assert res["engine"] == "haversine_fallback"
    assert sorted(res["ordered_dockets"]) == ["A", "B"]


# ── 2. the barriers (apply once the switch is flipped later) ─────────

_STOPS = [{"docket_number": "A", "lat": 51.50, "lng": -0.10},
          {"docket_number": "B", "lat": 51.51, "lng": -0.09},
          {"docket_number": "C", "lat": 51.49, "lng": -0.12}]
_ORIGIN = (51.52, -0.10)


def _fake_google(monkeypatch, counter):
    from backend import routing

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return (b'{"routes":[{"optimizedIntermediateWaypointIndex":[1,0],'
                    b'"distanceMeters":4200,"duration":"600s"}]}')

    def fake_urlopen(req, timeout=8):
        counter.append(req)
        return FakeResp()
    monkeypatch.setattr(routing.urllib.request, "urlopen", fake_urlopen)


def test_field_mask_is_pinned_to_essentials_fields():
    """Google bills the whole call at the tier of the priciest requested
    field. This mask is the cost contract — change it only deliberately."""
    from backend import routing
    assert routing.ROUTES_FIELD_MASK == (
        "routes.optimizedIntermediateWaypointIndex,routes.distanceMeters,routes.duration")


def test_route_result_is_cached_second_call_is_free(app_db, monkeypatch):
    from backend import routing, config as cfg
    monkeypatch.setattr(cfg, "GOOGLE_MAPS_SERVER_KEY", "AIza-x")
    calls = []
    _fake_google(monkeypatch, calls)
    r1 = routing.optimise(_ORIGIN, _STOPS)
    assert r1["engine"] == "google_routes" and len(calls) == 1
    r2 = routing.optimise(_ORIGIN, _STOPS)
    assert r2["engine"] == "google_routes_cached"
    assert len(calls) == 1                      # no second Google call
    assert r2["ordered_dockets"] == r1["ordered_dockets"]


def test_daily_cap_hard_stops_google_calls(app_db, monkeypatch):
    from backend import routing, config as cfg
    monkeypatch.setattr(cfg, "GOOGLE_MAPS_SERVER_KEY", "AIza-x")
    monkeypatch.setattr(cfg, "MAPS_DAILY_CAP", 2)
    calls = []
    _fake_google(monkeypatch, calls)
    # Three DISTINCT runs (cache can't answer) against a cap of 2.
    for i in range(3):
        stops = [{"docket_number": f"S{i}A", "lat": 51.0 + i, "lng": -0.1},
                 {"docket_number": f"S{i}B", "lat": 51.1 + i, "lng": -0.2}]
        res = routing.optimise(_ORIGIN, stops)
        if i < 2:
            assert res["engine"] == "google_routes"
        else:
            assert res["engine"] == "haversine_capped"
    assert len(calls) == 2
    u = routing.usage_today()
    assert u["calls"] == 2 and u["cap"] == 2


def test_cap_zero_disables_google_even_with_key(app_db, monkeypatch):
    from backend import routing, config as cfg
    monkeypatch.setattr(cfg, "GOOGLE_MAPS_SERVER_KEY", "AIza-x")
    monkeypatch.setattr(cfg, "MAPS_DAILY_CAP", 0)
    _no_network(monkeypatch)
    res = routing.optimise(_ORIGIN, _STOPS)
    assert res["engine"] == "haversine_capped"


def test_budget_fails_closed_without_db(tmp_path, monkeypatch):
    """Counter unavailable → NO Google call (a haversine order, never an
    unmetered bill)."""
    from backend import routing, db, config as cfg
    monkeypatch.setattr(cfg, "DB_PATH", str(tmp_path / "empty" / "t.db"))
    db.close_connection()               # fresh connection, no init_db → no tables
    monkeypatch.setattr(cfg, "GOOGLE_MAPS_SERVER_KEY", "AIza-x")
    _no_network(monkeypatch)
    res = routing.optimise(_ORIGIN, _STOPS)
    assert res["engine"] == "haversine_capped"
    db.close_connection()


def test_cap_counter_survives_reconnect(app_db, monkeypatch):
    from backend import routing, db, config as cfg
    monkeypatch.setattr(cfg, "GOOGLE_MAPS_SERVER_KEY", "AIza-x")
    monkeypatch.setattr(cfg, "MAPS_DAILY_CAP", 5)
    calls = []
    _fake_google(monkeypatch, calls)
    routing.optimise(_ORIGIN, _STOPS)
    db.close_connection()               # simulate process restart
    assert routing.usage_today()["calls"] == 1


# ── 3. repo guards — no Places, no library creep ─────────────────────

def _read(rel):
    with open(os.path.join(_APP, rel), encoding="utf-8") as f:
        return f.read()


def test_no_places_or_geocoding_usage_anywhere():
    """The £415 bill class (Places API New) must not enter this app by
    accident. Only routes.googleapis.com (server) and the Maps JS loader
    (browser) are allowed Google surfaces."""
    banned = ("places.googleapis.com", "place/details", "place/autocomplete",
              "maps/api/geocode", "PlacesService", "AutocompleteService",
              "google.maps.places")
    for root in ("backend", "frontend"):
        for dirpath, _dirs, files in os.walk(os.path.join(_APP, root)):
            for fn in files:
                if not fn.endswith((".py", ".js", ".html")):
                    continue
                src = open(os.path.join(dirpath, fn), encoding="utf-8",
                           errors="ignore").read()
                for b in banned:
                    assert b not in src, f"{fn} references banned Google surface: {b}"


def test_maps_js_loader_requests_no_extra_libraries():
    """The Maps JS URL must not grow a libraries= param (places library =
    Places billing) — the map is pins-only by design."""
    src = _read("frontend/app.js")
    assert "maps.googleapis.com/maps/api/js" in src
    assert "libraries=" not in src


def test_no_route_polyline_directions_calls():
    """drawMap deliberately draws no road polyline (each render would be a
    Directions/Routes call). Pin the absence."""
    src = _read("frontend/app.js")
    assert "DirectionsService" not in src and "DirectionsRenderer" not in src
