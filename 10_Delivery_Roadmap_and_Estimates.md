# 10 — Delivery Roadmap & Estimates

**TOM Driver App** · v0.1 · 2026-06-26

---

## 1. Phasing overview

Built **TOM-side dark-first** (mirrors the project's existing discipline: ship the backend behind
flags, enable per-flag). Each phase is independently shippable and testable on both SQLite and Postgres.

| Phase | Theme | Outcome | Gate |
|-------|-------|---------|------|
| **P0** | Backend foundations | `driver_api_v1` blueprint, new tables, auth/run/status endpoints, idempotency, audit, dual-backend tests green | API testable via curl/Postman; no app yet |
| **P1** | App skeleton + auth | RN app boots, login (device-bound), today's run read from TOM, offline run cache | Driver can see their run on a phone |
| **P2** | Status + offline outbox | Full status lifecycle, durable outbox, optimistic UI, idempotent sync | A job can be driven start→complete offline |
| **P3** | Scanning + POD | VisionCamera barcode scan (collect/deliver), parcel match guards, signature+photo POD, failure capture | Proof captured end-to-end, offline-safe |
| **P4** | Live tracking | Background GPS, geofence arrival, `driver_locations`, board "latest position", consent flow | Board shows drivers moving live |
| **P5** | Routing | Geocode-at-booking, Distance Matrix ETAs, Routes API single-driver optimisation, Maps SDK display, nav hand-off | Optimised order + honest ETAs in app and on board |
| **P6** | Comms + commercial | Push (FCM/APNs), messages, documents wallet, availability, earnings (P1 features) | Feature-complete v1 |
| **P7** | Harden + store launch | Security review, DPIA, store submissions, pilot with a driver cohort | Commercial pilot live |
| **P8** | Scale (post-v1) | Route Optimization API fleet VRP, customer ETA link, in-app Navigation SDK | Volume scale-up |

## 2. Milestones & rough effort

Estimates are **planning-grade ranges** for a small team (≈2 mobile + 1 backend + part-time design/QA),
to be refined at build kickoff. Not a commitment.

| Phase | Indicative effort | Key risks |
|-------|-------------------|-----------|
| P0 Backend foundations | 2–3 wks | three-whitelist discipline; PgBouncer-safe hot paths |
| P1 App skeleton + auth | 2 wks | device binding, secure token storage |
| P2 Status + offline | 2–3 wks | offline correctness, idempotency edge cases |
| P3 Scanning + POD | 2–3 wks | camera perf on cheap Android; offline media staging |
| P4 Live tracking | 3–4 wks | **background location reliability + battery** (highest-risk) |
| P5 Routing | 3–4 wks | Google cost control; ETA truthfulness; fallback heuristic |
| P6 Comms + commercial | 2–3 wks | push delivery reliability; earnings accuracy (decimal) |
| P7 Harden + store | 2–4 wks | store review (background-location justification), DPIA |
| **v1 total** | **~18–26 wks** | sequential-ish; some overlap possible |
| P8 Scale | 4–6 wks | fleet VRP modelling + dispatch integration |

## 3. Critical-path risks (and mitigations)

| Risk | Severity | Mitigation |
|------|----------|-----------|
| Background location unreliable / battery-heavy | **High** | Use a proven native module; adaptive sampling; pilot battery test early in P4; degrade gracefully. |
| Google Maps cost overrun | High | Server-side caching, geocode-once, budget caps + alerts, heuristic fallback (Doc 06 §7–8). |
| Offline data loss (lost POD/scan) | **High** | Durable outbox + idempotency keys; lossy only for location; explicit conflict handling (Doc 08). |
| Store rejection (background location) | Medium | Prepare prominent-disclosure + justification video; on-duty-only scope; privacy labels (Doc 09 §8). |
| Three-whitelist persist trap on new job fields | Medium | Round-trip tests for every new persisted field (ADR-003 lesson). |
| Shipping ahead of TOM backend hardening | Medium | Don't go-live before secrets→Key Vault, TLS, MFA, log rotation land (Doc 09 §9). |
| Scope creep into payments/marketplace | Low | Held as explicit non-goals (Doc 01 §6). |

## 4. Definition of done — commercial v1
- Driver completes a full multi-drop run (navigate → scan → POD) **offline-safe**, nothing lost.
- Board shows live position + honest ETAs (median error < 8 min) for every on-duty driver.
- All money displayed is decimal-exact from durable `driver_pay_final`.
- Every driver mutation audited; every new table green on SQLite + Postgres; PgBouncer-safe.
- DPIA signed; consent flow live; Maps keys restricted + capped; tokens in secure storage.
- Crash-free sessions > 99.5%; battery drain < 35%/shift in pilot.
- App approved on App Store + Play; pilot cohort running real jobs.

## 5. Dependencies to line up before P0
1. Google Maps Platform billing account + restricted keys (server + SDK).
2. Firebase project (FCM) + APNs key.
3. Apple Developer + Google Play org accounts.
4. Object storage for POD media (e.g. Azure Blob) + signed-URL policy.
5. Product sign-off on the four open questions in Doc 01 §8 (offer-vs-assign, customer tracking,
   in-app nav, subbie earnings depth).
6. Confirmation the managed Postgres + PgBouncer staging environment is available for the new tables.

## 6. Suggested immediate next step
Stand up **P0** as a thin vertical slice: the `driver_api_v1` blueprint with `auth/login`, `GET /run`
(reading the spine board for one driver), and `POST /jobs/{docket}/status` with idempotency + audit +
dual-backend tests. That proves the integration seam end-to-end against real TOM internals before any
mobile code is written — the same "prove the seam, ship dark, enable per-flag" pattern the rest of TOM
already follows.
