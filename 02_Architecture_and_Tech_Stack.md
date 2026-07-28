# 02 — Architecture & Tech Stack

**TOM Driver App** · v0.1 · 2026-06-26

---

## 1. Stack decision

### 1.1 Client framework — **React Native + TypeScript** (recommended)

| Option | Background GPS | Camera/Barcode | One codebase | Team fit | Verdict |
|--------|----------------|----------------|--------------|----------|---------|
| **React Native + TS** | ✅ mature (`react-native-background-geolocation`) | ✅ (`vision-camera` + code-scanner) | ✅ iOS+Android | ✅ TIA already uses React/TS | **Chosen** |
| Flutter | ✅ good | ✅ good | ✅ | ⚠️ new language (Dart) for the team | Strong alt |
| Native iOS+Android | ✅ best | ✅ best | ❌ two codebases | ❌ 2× cost | Overkill |
| PWA / web | ❌ **no reliable background location**, throttled, no APNs background | ⚠️ limited, browser-gated | ✅ | ✅ | **Rejected for v1** |

**Why React Native wins for TOM:** the two hardest requirements — *dependable background
location* and *fast camera barcode scanning* — are first-class in RN's native-module ecosystem,
while a PWA fundamentally cannot stream location when backgrounded on iOS. RN also reuses the
React/TypeScript competency already proven in TIA (`apps/web`, Next.js 14), shortening ramp.

**Concrete library shortlist:**
- Navigation/UI: `expo` (dev-client / bare workflow), `react-navigation`, `react-native-reanimated`.
- Location: `react-native-background-geolocation` (Transistorsoft) — best-in-class battery/geofence;
  or Expo `expo-location` + `expo-task-manager` if avoiding paid licence in early phases.
- Camera/scan: `react-native-vision-camera` + `vision-camera-code-scanner` (MLKit barcode).
- Maps: `react-native-maps` (Google provider) for display; Google **Routes/Route Optimization**
  called **server-side** (see Doc 06), not from the device.
- Local store: `op-sqlite` or `expo-sqlite` (on-device DB) + `react-native-mmkv` (fast KV/secrets cache).
- Secure storage: `expo-secure-store` / Keychain / Keystore for tokens.
- Push: `@react-native-firebase/messaging` (FCM) + APNs.
- Signature/photo: `@react-native-community/...` signature canvas + `vision-camera` capture.
- State/data: `@tanstack/react-query` for server cache + a small offline mutation queue (Doc 08).

### 1.2 Why not a second backend
TIA stands as a cautionary example in this repo: a separate Node stack that now needs a 5-phase
plan to re-converge with TOM's database. The Driver App therefore **extends TOM's existing Flask
backend** rather than introducing a new service. One source of truth, one audit trail, one set of
dual-backend tests.

## 2. System topology

```
┌──────────────────────────────────────────────────────────────────────────┐
│                         DRIVER DEVICE (React Native)                       │
│                                                                            │
│  UI layer (screens, Doc 07)                                                │
│  ├─ Run/Job store (react-query cache + local SQLite)                       │
│  ├─ Location service ──────────► background GPS sampler + geofences        │
│  ├─ Scanner service ───────────► VisionCamera + MLKit barcode              │
│  ├─ POD service ───────────────► signature + photo capture                 │
│  ├─ Offline outbox (ordered, durable event queue)  ◄── Doc 08             │
│  └─ Secure store (token, device id)                                        │
└───────────────┬───────────────────────────────────┬──────────────────────┘
                │ HTTPS (TLS) JSON                    │ FCM/APNs push
                ▼                                     ▲
┌──────────────────────────────────────────────────────────────────────────┐
│                       TOM BACKEND (Flask / Gunicorn)                       │
│                                                                            │
│  NEW: driver_api_v1 blueprint  /api/driver/v1/*   (Doc 03)                 │
│   ├─ auth (uses drivers_core/driver_auth_store.py)                         │
│   ├─ run/jobs read  (spine board: _merged_job_list, multidrop, sequence)  │
│   ├─ status/scan/POD writes → jobs_core.mutations (+ driver_actions audit) │
│   ├─ location ingest → NEW driver_locations table                         │
│   ├─ route optimise proxy → Google Maps Platform (server-side, keyed)      │
│   └─ messages/docs/availability  (reuse existing driver_* stores)          │
│                                                                            │
│  Spine / board  ◄── live driver position enriches ops board + ETAs        │
│  Push dispatcher (NEW) → FCM/APNs on job/route/message events             │
└───────────────┬───────────────────────────────────┬──────────────────────┘
                │                                     │
                ▼ Postgres via PgBouncer (txn pool)   ▼ Google Maps Platform
        jobs · drivers · driver_actions ·     Routes API · Route Optimization API
        driver_locations(NEW) · parcel_events(NEW)   Distance Matrix · Geocoding · Maps SDK
```

## 3. Backend integration principles (TOM hard rules honoured)

1. **Versioned, isolated surface.** All app traffic enters through one new blueprint,
   `driver_api_v1`, mounted at `/api/driver/v1`. Existing `/driver/*` web routes are untouched.
   Registered alongside the other ~92 blueprints in `app/web/app.py`.
2. **Dual SQLite/Postgres.** Every new table ships in both `database/schema.py` and
   `database/pg_schema.py`; every query goes through the `database/stores/*` layer and the
   `?`/`%s` translation in `pg_engine.py`. New tests run green on both backends.
3. **PgBouncer transaction-pooling safe.** Location ingest is the hot path — short, single-statement
   inserts, no long-lived transactions, explicit commit/rollback so no idle-in-transaction pins a
   pooled slot. No `FOR UPDATE` held across I/O.
4. **Decimal-string money.** Any earnings/expense field the app reads or writes uses the
   `_to_decimal_str` convention; the app never does float arithmetic on money.
5. **Audit invariant.** Every driver-originated mutation (status change, scan, POD, doc upload)
   writes a `driver_actions` entry — the existing "no driver record updated without an action log"
   rule extends to job/parcel events with a `request_id` (the `web/request_id.py` correlation id).
6. **Three-whitelist discipline.** New persisted job fields (e.g. `pod_captured_at`,
   `last_scan_event_id`) are added to all three persist whitelists (serializers,
   `jobs_core.mutations._PERSISTABLE_FIELDS`, `database/stores/jobs._PERSISTABLE_FIELDS`) with a
   round-trip test — the trap documented in ADR-003.
7. **Department policy.** The driver API is its own section in `web/dept_policy.py`; driver tokens
   are not admin sessions and never traverse admin/sales/accounts sections.

## 4. Offline-first model (summary; full detail Doc 08)

- **Read model:** today's run is fetched once, cached in local SQLite, and re-validated via
  react-query. The app is fully usable read-side with zero signal.
- **Write model:** every state-changing action (arrive, scan, POD, status, fail) is written first to
  a local **outbox** with a client-generated UUID (idempotency key), then drained to the server in
  order. The server treats writes as idempotent on that key.
- **Location:** sampled continuously to a local ring buffer; flushed in batches; lossy by design
  (old pings can be dropped, business events never).
- **Conflict rule:** server is authoritative for *assignment* (which jobs are yours); device is
  authoritative for *field events* (what physically happened, timestamped on device). Ops can
  override via the board, which pushes a corrective state down.

## 5. Environments & config

| Env | App build | TOM backend | Maps keys | Data |
|-----|-----------|-------------|-----------|------|
| Dev | Expo dev-client | local Flask + SQLite | dev key, IP-restricted | seed/demo |
| Staging | internal TestFlight / Play internal | staging Flask + managed Postgres | staging key | sanitised |
| Prod | App Store / Play | prod Flask + managed Postgres + PgBouncer | prod key, app-restricted | live |

Config is build-time (`API_BASE_URL`, Maps key alias) + runtime feature flags pulled from TOM
(mirrors TOM's `runtime_adapter_feature_flags` philosophy — ship dark, enable per-flag).

## 6. Observability
- Client: crash reporting (Sentry RN), structured event logs, "send logs to ops" support action.
- Server: every `/api/driver/v1/*` call carries `X-Request-ID`; location/scan/POD volumes and
  latencies are dashboarded; battery/upload telemetry sampled.
- SLO targets in Doc 10.

## 7. Why this is commercially safe
The app adds a thin, well-bounded surface to a backend that already enforces the hard parts
(audit, dual-backend, decimal money, multi-worker coherence). The risky new physics — background
location and offline event durability — are isolated in two device services and two new server
tables, each independently testable, each fail-safe (lose pings, never lose proof).
