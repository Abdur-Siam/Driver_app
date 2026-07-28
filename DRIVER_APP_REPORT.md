# TOM Driver App — Full Report

**Xtra Mile Couriers · driver-facing application for the TOM platform**
Status: feature-complete standalone build, v1.0.0 · 46 backend tests green · not yet merged/deployed
Report date: 26 June 2026

---

## 1. Executive summary

The TOM Driver App is a commercial-grade, driver-facing application covering the
**entire working day**: log in → see today's optimised run → navigate → scan
parcels on collection and delivery → capture proof of delivery → stream live
location → manage shift, pay, tax and account — all **offline-tolerant**.

It is built as an **installable PWA** that runs today on Android and iOS browsers,
with a **Capacitor native wrapper scaffolded** for App Store / Play Store
delivery (background GPS, push, hardware scanning, biometrics). It runs entirely
**standalone** (its own backend + database + demo data) so it can be tested and
proven in isolation, then merged into the TOM fleet system as a deliberate step.

**What it is not yet:** merged into TOM's live PostgreSQL/auth, compiled as a
native binary, or deployed (no TLS termination, push send keys, cloud storage,
or pen-test — all deploy-time work). It depends on TOM's Driver-authorisation
layer (C2c) existing before it connects to real fleet data.

---

## 2. Technology stack

| Layer | Technology | Notes |
|-------|-----------|-------|
| Backend | **Python 3 + Flask** (Werkzeug only) | No heavy framework; mirrors TOM's server-rendered, no-Node ethos |
| Database | **SQLite (WAL mode)** | Self-contained; mirrors TOM's WAL setup; swaps to TOM Postgres at merge |
| Frontend | **Vanilla JS PWA — no build step** | Single `app.js` SPA, hash router, `index.html`, `styles.css` |
| Offline | **Service worker** + `localStorage` outbox | Caches app shell; queues mutations with idempotency keys |
| Native shell | **Capacitor 6** (scaffolded) | Wraps the *same* web assets into iOS/Android projects |
| PDF | **Hand-rolled pure-Python PDF writer** | No reportlab/fpdf; produces valid PDF-1.4 statements |
| Icons | **Pure-Python PNG encoder** | `tools/make_icons.py` (zlib only) — apple-touch + maskable |
| Routing | **Google Routes API** + haversine fallback (server); haversine on device | See §5 |
| Auth | Bearer tokens, **SHA-256 hashed at rest**, one device per driver | scrypt/pbkdf2 passwords |
| Tests | **pytest — 46 backend tests** | Run on SQLite; designed to pass on Postgres after merge |

**Dependencies:** Flask + Werkzeug only on the backend; **zero** third-party JS
on the frontend. This keeps the attack surface small and the merge clean.

**Run:** `cd Driver/app && ./run.sh` → `http://127.0.0.1:5179` (demo `DRV001` / `test1234`).

---

## 3. Feature set

### 3.1 Operations (the working day)
- **Token login** with device binding (one active device per driver).
- **Home dashboard** — next job, run summary, shift state, unread messages, performance snapshot.
- **Today's run** — jobs in dispatch-optimised order, sequence numbers, deadlines, **per-stop ETAs**.
- **Full job lifecycle** — assigned → acknowledged → en-route pickup → at pickup → POB → en-route drop → delivered, each transition audited.
- **Barcode scanning** with **scan-match guards**: expected / duplicate / unexpected / **wrong-drop** detection on both collect and deliver; proof-of-board enforced before POB.
- **Proof of delivery per drop** — recipient name, **on-canvas signature**, and **multiple delivery photos** (camera or gallery), viewable back on the delivered drop.
- **Failure capture** — reason codes (no access, refused, damaged, wrong address, other) + note + photo.
- **Live location streaming** — batched GPS pings (consent-gated, see §6).
- **Turn-by-turn hand-off** to Google Maps / Waze / HERE WeGo / Android native (driver's choice).
- **Job history** of completed work.

### 3.2 Shift & availability (modelled on the legacy ECHO app)
- **Start shift with an end-time** so dispatch can pre-allocate pre-bookings, with duration presets.
- **Live shift countdown** in the header.
- **Available / Going home** status (Going home signals dispatch to send only homeward jobs).
- **Quick canned messages** to ops — Heavy traffic, "Where should I plot?", **EMERGENCY**, Accident/breakdown (with confirm) — plus free text.

### 3.3 Pay (advanced, driver-pay focused)
- **Earnings dashboard** — period view, weekly chart, component breakdown (base/waiting/tolls/extras/bonus/deductions), pending vs paid, projected payout, per-job breakdown.
- **Downloadable PDF pay statements** — issued after the company processes payment; viewable + downloadable.
- **Instant pay / early payout** with a configurable fee.
- **Tax centre** — YTD gross, HMRC mileage allowance, allowable expenses, estimated taxable income, suggested set-aside %.
- **Expense logging** with receipt capture (access-controlled).
- **Performance metrics** — rating, acceptance %, completion %, on-time %.

### 3.4 Account self-service
- **Full profile** with editing; **profile photo + vehicle photo upload**.
- **Sensitive bank/tax changes routed to ops review** (not changed live — three-whitelist discipline mirrored).
- **Change password**, notification preferences.
- **Privacy & data** screen with GDPR access / erasure requests (see §6).
- **About / version / legal** (Terms, Privacy policy, support).

### 3.5 App experience
- **Day / Night / Auto theme.**
- **Connection-lost sound alert** + offline banner.
- **Installable** with home-screen icon, full-screen, safe-area/notch handling, no pinch-zoom.
- **Consistent back button** on every sub-page; 5-tab bottom nav (Home / Run / Pay / Inbox / Me).

---

## 4. Offline-first design

A courier loses signal constantly, so the app is built to keep working:
- **Service worker** caches the app shell — it launches with no signal.
- **Offline outbox** — every mutating action (status, scan, POD, fail, messages) queues to
  `localStorage` with a unique **idempotency key** and replays automatically on reconnect.
- **Idempotency ledger** on the backend dedupes replays so a queued action applies exactly once.
- **Last-known run** is cached so the run screen is never blank offline.
- **Lossy vs durable** by design: location pings are lossy (drop on failure); job actions are durable (queued).

---

## 5. Route optimisation

The app follows a **consumer + edge-fallback** model — the heavy optimisation is
owned centrally (TOM), the device consumes and degrades gracefully.

**Authoritative engine (server-side; TOM owns it after merge):**
- **Fleet allocation (VRP)** — which jobs go to which driver (TOM only; needs whole-fleet state).
- **Per-driver stop sequencing (TSP)** via the **Google Routes API** (`computeRoutes` with
  `optimizeWaypointOrder`), called **server-side** so the Google key never reaches the device.
- The optimised order is **persisted, audited and costable**.

**On the device (no Google key, no Google call):**
- **Consumes** TOM's order via `/run`, renders the sequence + **per-stop ETAs**.
- **Offline fallback** — a client-side **nearest-neighbour haversine** re-sequence so the driver
  keeps moving if signal drops mid-route; clearly flagged as an on-device order.
- **Re-syncs to dispatch** automatically on reconnect (discards the local order; TOM is authoritative).
- **Feeds the engine** — streams GPS, scan and drop events back so the next re-optimisation has fresh inputs.

Without a Google key the app is still fully functional (haversine fallback + map placeholder);
keys are added later by the operator (one IP-restricted server key, one app-restricted browser key).

---

## 6. Security & privacy

### 6.1 Authentication & access
- Bearer tokens, **SHA-256 hashed at rest**, **one active device per driver** (login revokes prior).
- **Per-IP login rate limiting** + **per-account lockout** (5 failures → 15-min lock) against brute force.
- **Ownership enforced everywhere** — a driver can only read/mutate their own jobs.
- **Per-driver throttle** on heavy/mutating endpoints (scan, POD, photo, data-request).

### 6.2 Data protection
- **POD/receipt/profile media is access-controlled** — never world-readable. Served only via
  **short-lived (10-min), driver-scoped, HMAC-signed URLs** or a valid bearer token.
- **Request-size cap** (16MB) + 413 handler — DoS guard on base64 uploads.
- **Full audit trail** — no driver record or job event changes without an audit row (TOM invariant mirrored).
- **Penny-exact decimal-string money** convention (no float drift) for all pay figures.

### 6.3 Browser/transport hardening
- Strict response headers on every request: **Content-Security-Policy**
  (`object-src 'none'`, `frame-ancestors 'none'`, locked script/img/connect sources),
  **X-Frame-Options: DENY**, **Permissions-Policy** (camera/geo self-only; mic/payment/usb off),
  **HSTS**, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, **Server banner removed**.

### 6.4 GDPR / UK data rights
- **Explicit location consent** required before any GPS is accepted (server returns 403 without it);
  withdrawable in-app, recorded with timestamp + audit.
- **Privacy & data** screen: plain-English "what we collect" + **data-access** and **erasure**
  requests filed to ops.
- Consent and data-subject requests are auditable.

### 6.5 Known follow-ups (honest)
- CSP currently allows `'unsafe-inline'` for scripts (UI uses inline handlers) — tightening to
  nonces is a planned refactor.
- TLS/HSTS *enforcement*, cert-pinning (native), secrets vault for the signing key, DB encryption
  at rest, WAF/DDoS, and an independent pen-test are **deploy-time** items.

---

## 7. Android & iOS compatibility

**Two delivery modes, one codebase:**
1. **Installable PWA (works today):** Android shows a one-tap Install prompt; iOS shows an
   Add-to-Home-Screen guide. Runs full-screen with an app icon, notch/safe-area handling,
   no zoom, offline shell, themed status bar — native-feeling for daily use.
2. **Native store apps (scaffolded):** `Driver/app/native/` wraps the same frontend with
   **Capacitor** into Xcode/Android Studio projects, unlocking **background GPS, push (FCM/APNs),
   hardware barcode scanning and Face/Touch ID** via `native-bridge.js`. The web app calls these
   only when running natively — no branching in the screens. Build steps and the required OS
   permission strings are documented in `native/README.md`. Compiling the binaries needs
   Xcode/Android Studio (not done in the sandbox).

---

## 8. API surface (`/api/driver/v1`)

Token-authenticated JSON; mutating calls honour `X-Idempotency-Key`; every state change audited.

`config · health · auth/login · auth/logout · me · run · jobs/<docket> ·
jobs/<docket>/status · jobs/<docket>/scan · jobs/<docket>/pod · jobs/<docket>/fail ·
location/batch · route/optimise · messages (GET/POST) · home · profile (GET/POST) ·
profile/password · profile/photo · earnings · statements · statements/<id> ·
statements/<id>/pdf · tax · performance · payout · expenses (GET/POST) · shift (GET/start/end) ·
status · history · consent/location · push/register · account/data-request`

---

## 9. Testing & verification

- **46 backend tests (pytest), all green** — auth/lockout, ownership, lifecycle, scan guards,
  idempotency, POD completion + multi-photo, location consent gate, route fallback, earnings,
  statements + PDF, tax, expenses, payout, shift, history, signed/driver-scoped media,
  push register, data requests, security headers, payload cap.
- **Verified live in-browser** throughout: login, run with ETAs, scan, signature+photo POD,
  offline→fallback→reconnect routing, earnings, PDF statements, settings, photo upload,
  consent flow, security headers + lockout (curl), CSP non-breaking, no console errors.
- Designed to pass on **both SQLite and Postgres** (TOM's dual-backend requirement).

---

## 10. Commercial readiness

| Area | Assessment |
|------|-----------|
| Feature breadth / UX | **High** — covers the full driver day + pay + account |
| Frontend engineering | **High** — installable PWA, offline outbox, cross-platform |
| Backend (standalone) | **High** — clean API, audited, 46 tests; stand-in for TOM |
| App-side security/GDPR | **High** — headers, consent, scoped media, lockout, audit |
| Integration with fleet | **Low** — not yet wired to TOM PostgreSQL/auth (depends on TOM C2c) |
| Native binaries | **Scaffolded** — needs Xcode/Android Studio to compile |
| Production/deploy | **Pending** — TLS, push send, cloud storage, monitoring, pen-test |

**Bottom line:** commercial-grade on everything the app itself owns. Remaining gates are
(a) TOM's Driver-authorisation layer landing, (b) the merge, and (c) native compile +
deploy-time infrastructure — most of which is not blocked on the app's own code.

---

## 11. Path to production

1. TOM ships **C2c Driver authorisation** (the dependency).
2. **Merge** the proven seam into TOM — auth → `driver_auth_store`, run → spine board,
   status → `update_driver_job_lifecycle`; move route engine + Google key to TOM; retire the
   standalone `routing.py` (device keeps the offline fallback). Mapping in `HANDOFF_Driver_App.md`.
3. **Compile native** (Capacitor → Xcode/Android Studio) with push + background GPS.
4. **Deploy-time:** TLS/HSTS enforcement, cloud media bucket (repoint `storage.sign_media_ref`),
   FCM/APNs send keys, secrets vault, DB encryption/backups, monitoring, DPIA, pen-test.
5. Supervised commercial pilot → full rollout.

---

*Repo locations: app `Driver/app/` · design docs `Driver/01–10_*.md` · native wrapper
`Driver/app/native/` · merge notes `HANDOFF_Driver_App.md`.*
