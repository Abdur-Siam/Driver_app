# 06 — Route Optimisation & Google Maps Platform

**TOM Driver App** · v0.1 · 2026-06-26

---

## 1. Goal

Give each driver a **commercial-grade, deadline-aware, optimised multi-drop order** and honest live
ETAs, using **Google Maps Platform**, while keeping dispatch authority inside TOM and the API key off
the device.

## 2. Two distinct problems — two Google products

| Problem | Scale | Google product |
|---------|-------|----------------|
| **A. Single-driver stop ordering** — given the stops already assigned to one driver, what's the best visiting order? | ≤ ~25 waypoints / driver / day | **Routes API** with `optimizeWaypointOrder` (waypoint optimisation) |
| **B. Fleet assignment + sequencing** — given N jobs and M drivers with capacities, time windows, vehicle types, who does what and in what order? | whole depot, many vehicles | **Route Optimization API** (Google's commercial fleet-routing / VRP engine, formerly Cloud Fleet Routing) |
| **C. ETAs & late-risk** — live time/distance between current position and the next stop(s) | continuous | **Distance Matrix API** (+ Routes API `computeRoutes` for the active leg) |
| **D. Address → coordinates** | on booking / on demand | **Geocoding API** |
| **E. In-app map display** | per session | **Maps SDK for Android / iOS** (`react-native-maps`, Google provider) |
| **F. (Optional, P1) In-app turn-by-turn** | per leg | **Navigation SDK** (premium; cost-gated) |

**v1 default:** A + C + D + E (single-driver optimisation + ETAs + map). **B** (full fleet VRP) is the
commercial scale-up, recommended once volume justifies it (TOM already dispatches at a
1,000–1,500 jobs/day baseline per the productisation plan, which is squarely in fleet-routing
territory). **F** is hand-off to the Google Maps app in v1 (free), Navigation SDK as a paid upgrade.

## 3. Where optimisation runs — **server-side, in TOM**

The device **never** calls Google routing/optimisation directly. Reasons:
1. **Key safety** — the Maps key stays server-side, IP/app-restricted (Doc 09); a key shipped in an
   app binary is a billing-fraud magnet.
2. **Dispatch authority** — TOM owns assignment; optimisation must respect existing
   `sequence_position`, `multidrop_status`, deadlines (`deadline_minutes`) and ops overrides.
3. **Caching & cost control** — server dedupes/caches matrix calls across drivers and reuses results,
   cutting Google spend dramatically vs per-device calls.
4. **Consistency** — board and app read the *same* computed sequence (persisted to `sequence_position`).

Flow (Doc 03 §8 `POST /route/optimise`):
```
App (current position) ──► TOM driver_api_v1 ──► route_optimiser (server)
                                                   ├─ Geocode any missing drop coords
                                                   ├─ build distance/time matrix (Distance Matrix)
                                                   ├─ A: Routes API optimizeWaypointOrder  (per driver)
                                                   │   or B: Route Optimization API         (fleet)
                                                   ├─ apply TOM constraints (deadlines, locked first,
                                                   │   vehicle, multidrop "preserve order" flags)
                                                   └─ persist ordered_dockets → jobs.sequence_position
App ◄── ordered sequence + legs + ETAs ◄──────────┘   (board reads same data)
```

## 4. Constraints the optimiser must honour (TOM-specific)

- **Deadlines:** `deadline_minutes` per job → time-window constraints; never order a stop past its
  promised time if avoidable, surface infeasibility to ops.
- **Locked-first / in-progress:** a job already **POB** or with a started leg is pinned at the front;
  re-optimisation only reorders *remaining* stops.
- **Multidrop preserve:** jobs whose `multidrop_status = "preserved"` keep the customer's drop order
  (customer order is sacred in TOM — never silently reordered); only `review_suggested` ones are free
  to reorder.
- **Vehicle/capacity:** vehicle type and parcel counts feed fleet-mode (B) capacity constraints.
- **Subcontractor vs owned:** assignment policy (B) can prefer owned fleet before subbies, or honour
  cost-model tiers — a TOM business rule, not a Google one.

## 5. Re-optimisation triggers
- New job added to a driver mid-shift.
- A drop fails (removed → re-sequence the rest).
- Significant deviation (driver far off predicted route for N minutes).
- Ops manual override (push corrected sequence down).
Each re-optimisation bumps the `route.version`; the app reconciles and re-renders, the board agrees.

## 6. ETA truthfulness
- ETAs come from **live** Distance Matrix / Routes calls using the driver's **current** position
  (from live tracking, Doc 04) + real-time traffic (`departure_time=now`, `traffic_model`).
- Compared against `deadline_minutes` to drive the existing late/risk signals
  (`modules/tom/signal_layer.py` already models vehicle-specific delay allowances).
- Success metric: median arrival error **< 8 min** (Doc 01). This directly retires the
  "map/ETA truthfulness — placeholder coordinates" risk flagged in TOM's commercial readiness plan.

## 7. Cost model (Google Maps Platform)

Google bills per API call/element. Approximate control levers (illustrative — confirm live pricing at
build time, costs change):

| API | Cost driver | Control |
|-----|-------------|---------|
| Distance Matrix | per origin×destination **element** | server-side cache + reuse matrix across a driver's run; batch; cap re-opt frequency |
| Routes API (optimise) | per request (more for advanced/traffic) | once per run + on real triggers only, not continuously |
| Route Optimization API (fleet) | per shipment/vehicle in the model | run on the dispatch cadence (e.g. wave planning), not per device |
| Geocoding | per address | geocode **once at booking**, store in `drop_coords` (already a field), never re-geocode |
| Maps SDK | per map load (mobile) | one map instance per session; SDK mobile loads are comparatively cheap |
| Navigation SDK (P1) | premium per-use | gated behind explicit cost sign-off |

**Budget guardrails:** server-side request quotas + alerting; a monthly Maps spend cap; daily
optimisation budget per driver; cache TTLs. All keys usage-capped in Google Cloud console (Doc 09).

## 8. Fallbacks & resilience
- **Google unavailable / quota hit:** fall back to a **nearest-neighbour heuristic** on cached
  `drop_coords` (haversine) so the driver still gets a sane order — degraded, not broken. TOM already
  computes haversine distances for ETAs in the dispatch path, so the primitive exists.
- **Missing coordinates:** geocode on demand; if that fails, present stops in booked order and flag.
- **Offline:** the app uses the last server-computed sequence from the cached run; hand-off
  navigation still works (Google Maps app has its own offline maps).

## 9. Build phasing for routing
1. **v1:** Geocode-at-booking + Distance Matrix ETAs + Routes API single-driver waypoint
   optimisation + Maps SDK display + hand-off navigation. Heuristic fallback.
2. **v1.1:** Live re-optimisation on triggers; customer ETA link (P1).
3. **v2 (scale):** Route Optimization API fleet VRP integrated with TOM dispatch wave-planning
   (capacities, time windows, owned-vs-subbie preference, cost-model tiers).
4. **v2+ (premium):** In-app Navigation SDK turn-by-turn if the per-use cost is signed off.
