# TOM Driver App — standalone, runnable build

A complete, self-contained driver application for Xtra Mile Couriers:
**login → today's optimised run → navigate → barcode scan on collect/deliver →
proof of delivery (signature + photo) → live location → offline-safe sync.**

It runs **on its own**, with no dependency on the TOM codebase, so it can be
developed and commercially tested independently and then merged into TOM as a
deliberate step (see [Merge & handoff](../HANDOFF_Driver_App.md)).

```
Driver/app
├── backend/          Flask API + SQLite (self-contained)
│   ├── config.py     env-driven config (DB path, Maps keys, TTLs)
│   ├── db.py         schema + WAL connection
│   ├── auth.py       password verify, bearer tokens, idempotency
│   ├── store.py      jobs/drops/parcels + lifecycle state machine
│   ├── routing.py    Google Routes API + haversine fallback
│   ├── seed.py       demo drivers + London multidrop jobs + ops user
│   ├── api.py        /api/driver/v1/* blueprint (driver app)
│   ├── ops_auth.py   ops-console account verify + bearer tokens (separate)
│   ├── ops_store.py  dispatch reads/writes: roster, tracking, jobs, chat
│   ├── ops_api.py    /api/ops/v1/* blueprint (dispatch console)
│   ├── bridge.py     TOM bridge — durable driver-event push (env-gated OFF)
│   └── server.py     app factory (serves both APIs + PWA + console + media)
├── frontend/         installable PWA (vanilla JS — no build step)
│   ├── index.html  app.js  styles.css        driver app
│   ├── ops.html  ops.js  ops.css             ops / dispatch console (served at /ops)
│   ├── native-bridge.js   native capability shim (no-op in a browser)
│   ├── sw.js  manifest.webmanifest  icon.svg
│   └── icons/         PNG app icons (iOS/Android home screen + maskable)
├── native/           Capacitor wrapper → real iOS + Android store apps
│   └── capacitor.config.json  package.json  README.md  android/  ios/
├── tools/make_icons.py        pure-Python PNG icon generator (no libs)
├── tools/provision_driver.py  create/manage real driver accounts (production)
├── tools/provision_ops.py     create/manage ops-console operators (production)
├── tests/  test_api.py 98 driver · test_ops.py 32 console · test_bridge.py 9 bridge
├── serve.py  run.sh  requirements.txt          local/dev entry
├── wsgi.py  gunicorn.conf.py  requirements-prod.txt   production entry
└── data/             created at runtime (driver_app.db, media/)
```

## Android & iOS

The app runs on **both platforms two ways**:

1. **Installable PWA (works today, no build).** Open the URL in **Chrome (Android)**
   or **Safari (iOS)** and install it: Android shows a one-tap *Install* prompt;
   iOS shows an in-app *Add to Home Screen* guide. It then runs full-screen with
   an app icon, safe-area/notch handling, offline shell, and a theme-coloured
   status bar. Pinch-zoom stays enabled (accessibility), and there is an in-app
   **text-size setting** (normal / large / extra) under Me → App & display.
2. **Native store apps (when you're ready to ship).** `native/` wraps the *same*
   frontend with **Capacitor** into Xcode (iOS) and Android Studio projects for
   the App Store / Play Store, unlocking **background GPS, push (FCM/APNs),
   hardware barcode scanning and Face/Touch ID** via `native-bridge.js`. The web
   app calls these only when running natively, so one codebase serves both.
   Build steps + required OS permission strings: [native/README.md](native/README.md).

## Run it

```bash
cd Driver/app
pip install -r requirements.txt      # Flask + Werkzeug
./run.sh                             # → http://127.0.0.1:5179
```
Open the URL on a phone (same network) or in Chrome. **Demo login: `DRV001` / `test1234`**
(a second driver, `CX014` / `test1234`, is a subcontractor with a medical job).
"Add to Home Screen" installs it as a standalone app.

## Ops / dispatch console (`/ops`)

The same backend serves a **desktop dispatch console** at
[http://127.0.0.1:5179/ops](http://127.0.0.1:5179/ops) — the operations side of
the driver app. **Demo login: `ops` / `ops1234`.** It provides:

* **Live map** — every driver plotted from their streamed GPS, colour-coded by
  duty status, click a driver to see their route trail + current job (self-contained
  canvas map, zero external dependencies; a Google/OSM tile layer is a later config add).
* **Jobs** — create + assign/reassign/unassign + cancel jobs, monitor lifecycle and
  per-drop progress. Creating/assigning a job pushes it straight onto the driver's run
  and notifies them; cancelling removes it and messages the driver. Unassigned jobs can
  also be **offered**: the driver gets a countdown (default 120 s) to accept or decline,
  and a decline/expiry returns the job to the unassigned board flagged with the outcome
  and the driver's reason.
* **Drivers** — roster with live duty/shift/last-fix/active-job/unread-message status
  (bank/PII deliberately never surfaced to dispatch).
* **Chat** — two-way ops↔driver messaging, optionally tagged to a job (per-job chat).
* **Dashboard** — drivers on shift, active/unassigned jobs, exceptions, unread messages,
  pending profile-change and GDPR reviews.

The console has its **own account table, tokens, login limiter and lockout** — a driver
token can never reach `/api/ops/v1`, and an ops token can never act as a driver. Every
console mutation writes an `ops_audit` row.

## Test it

```bash
cd Driver/app
PYTHONPATH=. python3 -m pytest tests/ -q     # 139 passed (98 driver + 32 console + 9 bridge)
```

## Run it in production

```bash
pip install -r requirements-prod.txt
DRIVER_APP_ENV=production \
DRIVER_APP_SECRET=$(openssl rand -hex 32) \
DRIVER_APP_DATA_DIR=/opt/driverapp/data \
DRIVER_APP_TRUST_PROXY=1 \
gunicorn -c gunicorn.conf.py wsgi:app
```

Production mode is guard-railed: boot **fails** without an env-supplied
`DRIVER_APP_SECRET`, demo seeding is refused (no `DRV001/test1234` **or the demo
`ops` account** on a real deploy — create drivers with `tools/provision_driver.py`
and console operators with `tools/provision_ops.py add <user> "<name>" --role admin`), and
`X-Forwarded-For` is only honoured behind a declared proxy hop count.
Abuse-control state (login limiter + account lockout) and the push outbox
are DB-backed, so any number of gunicorn workers share them safely. Full
steps (VM, TLS, systemd, backups, go-live credentials):
[../../TOM_DriverApp_Commercial_Deploy_Runbook_2026-07-13.md](../../TOM_DriverApp_Commercial_Deploy_Runbook_2026-07-13.md).

## Google Maps — DISCONNECTED by default (master switch)

**As of 28 Jul 2026 the Google Maps connection is OFF by default** (Jeet's
directive after a £415/month Places API bill on the shared Google account).
The master switch is `DRIVER_MAPS_ENABLED`: unless it is explicitly `1`,
every Google key resolves blank — even when `GOOGLE_MAPS_API_KEY` is present
in the host environment — so the app makes **zero** Google calls, ships no
key to the browser, and the CSP stays closed to Google hosts. Do not flip
the switch until Jeet says so.

The app is **fully functional disconnected** — routing falls back to a
nearest-neighbour haversine order and the run screen shows a map placeholder.

Cost barriers (active whenever the switch is later flipped on):

* **One Google surface only.** The server-side Routes `computeRoutes` call in
  `backend/routing.py` is the app's only outbound Google API; there is no
  Places/Geocoding/Place Details usage anywhere, and `tests/test_maps_barriers.py`
  guards against any creeping in (Places is the expensive SKU family —
  requesting one Pro/Enterprise-tier field bills the whole call at that tier).
* **Pinned Essentials field mask.** `ROUTES_FIELD_MASK` requests exactly three
  route fields; the mask is test-pinned so a casual field addition (= a price
  tier jump) fails the suite.
* **Per-day hard cap.** `DRIVER_MAPS_DAILY_CAP` (default 200) is a DB-backed
  counter (`maps_usage`) that survives restarts and **fails closed** — if the
  counter can't be advanced, the Google call is not made.
* **Route cache.** Results are cached 24 h (`maps_route_cache`,
  `DRIVER_MAPS_CACHE_TTL_HOURS`) keyed on coordinates+dockets, so
  re-optimising the same run is free. Coordinates/orderings only — no Places
  content, so caching is within Google's terms.
* **No per-render calls.** The run map is pins-only by design: no route
  polyline (each would be a Directions call), no `libraries=` param on the
  Maps JS loader (test-pinned).
* **Recommended on reconnect:** set a per-API daily quota cap + billing budget
  alert in the Cloud Console as the backstop above the app-level cap.

When the switch IS on, it uses **the same key as TOM**: the single,
HTTP-referrer-restricted `GOOGLE_MAPS_API_KEY` (see `app/modules/google_maps.py`):

```bash
DRIVER_MAPS_ENABLED=1                             \
GOOGLE_MAPS_API_KEY=<the TOM Maps Platform key>   \
TOM_PUBLIC_ORIGIN=https://<driver-app-origin>/    \
./run.sh
```
* **`DRIVER_MAPS_ENABLED`** → the master switch (default OFF = disconnected).
* **`GOOGLE_MAPS_API_KEY`** → one key for Routes (server-side) + Maps JS (run screen).
* **`TOM_PUBLIC_ORIGIN`** → the `Referer` sent on server-side Routes calls (TOM's key is
  referrer-restricted, so Google only honours REST calls whose Referer matches the key's
  allow-list — same var/behaviour as TOM). **Deploy step:** add the driver app's own public
  origin to the key's *allowed HTTP referrers* so the browser Maps JS is accepted too.
* Legacy split vars `GOOGLE_MAPS_SERVER_KEY` / `GOOGLE_MAPS_BROWSER_KEY` still take
  precedence if explicitly set (dedicated IP-restricted server / app-restricted browser key).

See [../06_Route_Optimisation_Google_Maps.md](../06_Route_Optimisation_Google_Maps.md).

## The API (token-authenticated)

`POST /api/driver/v1/auth/login` · `/auth/logout` · `GET /me` · `GET /run` ·
`GET /jobs/<docket>` · `POST /jobs/<docket>/status` · `/scan` · `/pod` · `/fail` ·
`POST /jobs/<docket>/drops/<seq>/arrive` · `GET /offers` · `POST /offers/<id>/accept` ·
`POST /offers/<id>/decline` · `POST /location/batch` · `POST /route/optimise` ·
`GET|POST /messages` · `POST /shift/vehicle-check` · `GET /config`.

All mutating calls accept `X-Idempotency-Key` (offline-outbox replay). Auth is a
bearer token (`Authorization: Bearer …`), one active device per driver, SHA-256
hashed at rest. Full contract: [../03_TOM_Integration_API_Contract.md](../03_TOM_Integration_API_Contract.md).

## What's real vs. what the merge/hardening adds

**Real and working now:**
- **Operations:** token auth + device binding, Home dashboard, today's run, the full
  job lifecycle, barcode scan-match with wrong-drop/duplicate/unexpected guards,
  signature + photo POD (**with capture GPS persisted per drop**), a per-drop
  **Arrived** stage (audited waypoint before the POD flow; ordering enforced
  server-side), failure capture (photo in its own `fail_photo` column; legacy rows
  stay readable), live geolocation streaming, offline outbox with idempotent replay,
  installable offline PWA, audit trail, **job history with tappable detail** —
  status, stops and the full POD (signature/photos via signed URLs) re-accessible
  per completed job. The run list **self-refreshes** (gentle 30 s poll while on
  shift + on app-visibility change) and surfaces incoming **job offers** with a
  live accept/decline countdown.
- **Per-job barcode scanning (`requires_scan`):** scanning is not universal — each job
  declares whether it needs it. Scan-required jobs are hard-gated server-side (POB blocked
  until all parcels collect-scanned; each drop's POD blocked until its parcels are
  deliver-scanned) and badged **📷 Scan** in the UI. Non-scan jobs go straight to
  "parcels on board" and signature/photo POD; optional scans are still accepted and audited.
- **Route optimisation (consumer + edge model):** the optimised order is owned by the
  central engine (TOM; the standalone backend stands in for it and holds the Google
  **server** key). The **device** consumes that order, renders **per-stop ETAs**, and
  hands off turn-by-turn to Google / Waze / HERE WeGo. If signal drops mid-route it
  re-sequences its own stops with an **on-device nearest-neighbour** (no Google call,
  no key on the device) and **re-syncs to the dispatch order on reconnect**. The device
  feeds GPS, scan and drop events back so the next re-optimisation has fresh inputs.
- **Shift & availability** (modelled on the legacy ECHO driver app): **start shift with an
  end-time** so dispatch can pre-allocate, live shift **countdown**, **Available / Going
  home / On break** status (break time is accumulated on the shift and excluded from
  worked-time maths), a **vehicle inspection checklist** at shift start (odometer + six
  pass/fail items + defect note/photo; defects flag to ops via the message channel but
  never block the shift), and an **end-of-shift summary** (duty/break/worked time, jobs
  completed, drops delivered/failed, GPS distance, earnings); **quick canned messages**
  to ops (Heavy traffic, Where should I plot?, **EMERGENCY**, Accident/breakdown — with
  confirm) plus free text.
- **App preferences:** **Day / Night / Auto** theme, **navigation app** choice (Google /
  Android native / Waze / HERE WeGo), **connection-lost sound alert**, notification
  toggles, fingerprint-login toggle.
- **Pay (advanced):** earnings dashboard (period, weekly chart, component breakdown,
  pending vs paid, projected payout, per-job breakdown); **downloadable PDF pay
  statements** (pure-Python generator); **instant pay / early payout** with fee;
  **tax centre** (YTD gross, HMRC mileage allowance, allowable expenses, estimated
  taxable + suggested set-aside); **expense logging** with receipt capture;
  **performance** metrics (rating, on-time, completion, jobs).
- **Account self-service:** full driver profile with editing + **profile photo and vehicle
  photo upload** (access-controlled, signed URLs); **sensitive bank/tax changes routed to ops
  review** (not changed live); notification preferences; change password; Settings hub.
- **Proof of delivery per drop:** signature capture **plus multiple delivery photos** (camera
  or gallery) for each drop/job, viewable back on the delivered drop; consistent **back button**
  on every sub-page.
- **Security headers & abuse controls:** strict response headers on every request
  (**Content-Security-Policy**, `X-Frame-Options: DENY`, `Permissions-Policy`, HSTS, no
  `Server` version banner); a **request-size cap** (DoS guard on base64 uploads); **per-account
  login lockout** (brute-force) on top of the per-IP limiter; a **per-driver throttle** on
  heavy/mutating endpoints; POD/receipt media URLs are **driver-scoped** and short-lived.
  *(Known follow-up: the CSP keeps `'unsafe-inline'` because the UI uses inline handlers;
  moving to nonces is a later tightening. TLS/HSTS enforcement, WAF and the pen-test are
  deploy-time.)*
- **Security & privacy (commercial hardening):** POD/receipt media is **access-controlled**
  — served only via **short-lived signed URLs** (HMAC) or a valid token, never world-readable;
  **explicit GDPR location consent** is required before any GPS is accepted (server enforces
  403 without it), withdrawable in-app; **Privacy & data** screen with **data-access / erasure**
  requests routed to ops; **push device-token registration**; optional **biometric app-lock**
  and **native hardware scanner** (engage in the native build); **About/version** + Terms/Privacy.

Endpoints added for the above: `/home /profile (GET/POST) /profile/password /earnings
/statements /statements/<id> /statements/<id>/pdf /tax /performance /payout /expenses
/offers /offers/<id>/accept|decline /jobs/<docket>/drops/<seq>/arrive /shift/vehicle-check`.

**Production hardening (documented, not in this local build):**
* **Native wrapper (Capacitor)** — *scaffolded* in [native/](native/) for *background*
  GPS, push, hardware barcode scanning and biometric unlock. Needs Xcode /
  Android Studio to build the store binaries (can't be compiled in this sandbox),
  but the config, plugins, JS bridge and per-OS permission strings are all in place.
* **TOM merge** — point auth at `driver_auth_store`, the run at the spine board, and
  status at `update_driver_job_lifecycle` (seam already proven on SQLite + Postgres;
  mapping in [../HANDOFF_Driver_App.md](../HANDOFF_Driver_App.md)).
* **Push delivery (built; credentials pending):** the full server-side pipeline is in
  `backend/push.py` — preference-gated fan-out to registered devices, a durable
  `push_outbox` (audit + retry), stale-token pruning, an FCM HTTP v1 transport that covers
  Android *and* iOS (APNs relayed via Firebase), a driver-facing **test notification**
  button (Settings → App & display), and `POST /push/test`. Ops→driver messages raise a
  push automatically. To go live: create a Firebase project, `pip install google-auth`,
  set `FCM_PROJECT_ID` + `FCM_CREDENTIALS_JSON` — queued pushes flush on boot.
* **TOM bridge (built; env-gated OFF):** `backend/bridge.py` pushes driver events to
  TOM — lifecycle transitions, POD, failed deliveries, GPS batches and barcode scans —
  through a durable `bridge_outbox` (attempts / backoff / dead-letter, multi-worker-safe
  claims, stdlib-only HTTP). Enable with `BRIDGE_ENABLED=1 TOM_BRIDGE_URL=… TOM_BRIDGE_KEY=…`;
  events POST to `{TOM_BRIDGE_URL}/api/driver-bridge/v1/{status,pod,locations,scans}` with
  an `X-TOM-Bridge-Key` header. TOM's receiving endpoints are the merge-side counterpart.
* **Deploy-time only:** repoint signed-media storage at a cloud bucket (S3/GCS pre-signed
  URLs — `storage.sign_media_ref` is the single seam), TLS/cert-pinning, and the formal
  DPIA. The in-app consent flow, access-controlled media, data-rights requests and version
  surface are already built.
