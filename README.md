# TOM Driver App — Commercial Design Package

**System:** TOM — Courier Tracking & Operations Management (Xtra Mile Couriers)
**Component:** Driver mobile application (iOS + Android)
**Author role:** AI Developer
**Status:** Design — v0.1 (2026-06-26)
**Context repo:** `JEET_TOM_REBUILD-29-05-26`

---

## 1. What this is

This folder is the **commercial design package** for the TOM Driver App: the native mobile
app that owned-fleet and subcontractor drivers use to receive jobs, navigate optimised
multi-drop routes, scan barcodes at pickup/drop, capture proof-of-delivery, and stream their
live location back to the TOM operations board.

It is a **design and architecture deliverable** — specifications, contracts, data models,
screen flows and diagrams detailed enough to hand to an implementation team (or to drive
the next build phase) without further discovery. It is deliberately TOM-specific: every
integration point names the real TOM module, table, field and constraint it touches.

It is **not** itself a running app yet. Section 10 phases the actual build.

---

## 2. Design pillars (the brief, restated)

| Pillar | Commitment |
|--------|-----------|
| **Built for TOM** | Talks to the existing Flask backend, the driver-auth store, the spine/board, the multidrop and pricing models — no parallel source of truth. |
| **Live tracking** | Background GPS streamed to TOM, surfaced on the ops board and (optionally) to the booking customer, battery- and data-frugal. |
| **Barcode / QR scanning** | Parcel-level scan-on-collect and scan-on-deliver, with mismatch guards, offline queueing and full audit. |
| **Commercial-grade routing** | Google Maps Platform multi-stop optimisation; server-side, dispatch-aware, integrated with TOM's `sequence_position` / multidrop. |
| **Google Maps** | Maps SDK for the in-app map + Routes / Route Optimization / Distance Matrix / Geocoding for ETAs and ordering. |
| **Everything a commercial driver app needs** | Offline-first, push, POD, earnings, availability, compliance docs, messaging, in-app support, accessibility, store-ready CI/CD. |

---

## 3. Document index

Read in order for a full walkthrough; jump by topic otherwise.

| # | Document | Covers |
|---|----------|--------|
| 00 | [README.md](README.md) | This index + overview |
| 01 | [01_Product_Spec_PRD.md](01_Product_Spec_PRD.md) | Goals, personas, scope, feature catalogue, user journeys, success metrics, non-goals |
| 02 | [02_Architecture_and_Tech_Stack.md](02_Architecture_and_Tech_Stack.md) | Stack decision (React Native), system topology, offline-first model, how it plugs into TOM |
| 03 | [03_TOM_Integration_API_Contract.md](03_TOM_Integration_API_Contract.md) | The `/api/driver/v1/*` contract, auth, payloads, mapping to existing TOM routes/tables |
| 04 | [04_Live_Tracking_Design.md](04_Live_Tracking_Design.md) | GPS capture, ping cadence, geofencing, battery, server storage, board + customer surfacing |
| 05 | [05_Barcode_Scanning_Design.md](05_Barcode_Scanning_Design.md) | Scan flows, symbologies, parcel model, mismatch handling, offline, audit |
| 06 | [06_Route_Optimisation_Google_Maps.md](06_Route_Optimisation_Google_Maps.md) | Google Maps Platform usage, single-driver vs fleet optimisation, cost model, ETA truthfulness |
| 07 | [07_Screen_UX_Spec.md](07_Screen_UX_Spec.md) | Screen-by-screen spec, navigation map, states, components, design tokens |
| 08 | [08_Data_Model_and_Offline_Sync.md](08_Data_Model_and_Offline_Sync.md) | On-device schema, new TOM tables, sync engine, conflict resolution |
| 09 | [09_Security_Privacy_Compliance.md](09_Security_Privacy_Compliance.md) | Auth hardening, PII/GDPR, location-consent law, secrets, store privacy requirements |
| 10 | [10_Delivery_Roadmap_and_Estimates.md](10_Delivery_Roadmap_and_Estimates.md) | Phased build, milestones, effort estimate, risks, go-live gates |

Supporting:
- [diagrams/](diagrams/) — architecture and flow diagrams (SVG/Mermaid sources)

**Runnable build (the actual app):**
- [app/](app/) — a complete, self-contained, locally-runnable driver app (Flask backend +
  installable PWA) plus the `/ops` dispatch console. `cd Driver/app && ./run.sh` →
  http://127.0.0.1:5179 (demo: `DRV001` / `test1234`; console `ops` / `ops1234`).
  139 backend tests pass (98 driver + 32 console + 9 TOM-bridge); verified end-to-end
  (login → run → arrive → scan → POB → deliver → signature POD → shift summary).
  See [app/README.md](app/README.md).
- [../HANDOFF_Driver_App.md](../HANDOFF_Driver_App.md) — merge-into-TOM mapping, the proven
  integration seam, the `web/app.py` SHA-pin governance note, and the production-hardening list.

---

## 4. How it fits the existing TOM system (one-paragraph orientation)

TOM today already exposes a **driver-facing web surface** — `app/web/driver_auth_routes.py`,
`driver_availability_routes.py`, `driver_documents_routes.py`, `driver_expenses_routes.py`,
`driver_messages_routes.py`, `driver_insights_routes.py`, `driver_notifications_routes.py` — backed
by `drivers_core/driver_auth_store.py` (per-driver password/lockout, separate from `drivers.json`),
the append-only `driver_actions` audit log, and the `drivers` table. Jobs live on the spine/board
with `drops`, `drop_coords`, `sequence_position`, `multidrop_status` and the durable Step-3 pricing
fields. The Driver App does **not** replace any of that — it adds a **native client** plus a thin,
versioned **`/api/driver/v1/*` JSON contract** in front of the existing stores, and **three new
capabilities** that have no home today: live location streaming, barcode/parcel events, and
server-side route optimisation. Every new write respects the project's hard rules — dual
SQLite/Postgres, PgBouncer transaction-pooling, decimal-string money, the three-whitelist persist
discipline, and "no driver record mutated without a `driver_actions` entry".

---

## 5. Headline technical decisions

| Decision | Choice | Why (short) |
|----------|--------|-------------|
| App framework | **React Native + TypeScript** (Expo dev-client / bare) | Reliable background GPS + camera/barcode, one codebase, reuses TIA's React/TS skill base. PWA can't do dependable background location. |
| Maps & routing | **Google Maps Platform** — Maps SDK + Routes API (waypoint optimisation) + Route Optimization API (fleet) + Distance Matrix + Geocoding | Brief mandates Google; Routes API covers per-driver ordering, Route Optimization API is the commercial fleet-grade VRP engine. |
| Optimisation locus | **Server-side, inside TOM** | Keeps dispatch authoritative, protects the API key, integrates with multidrop/`sequence_position`, lets ops override. |
| Offline | **Offline-first**, on-device SQLite + outbound event queue | Drivers work in lifts, basements, dead spots; scans and PODs must never be lost. |
| Backend | **Extend TOM Flask** with `/api/driver/v1/*` + new tables | Single source of truth; no second backend to reconcile (contrast TIA's separate stack). |
| Push | **FCM (Android) + APNs (iOS)** via one provider abstraction | Standard, free tier ample for fleet size. |

---

## 6. Scope at a glance

**In scope (commercial v1):** auth & device binding · today's run · optimised multi-drop navigation
· turn-by-turn handoff to Google Maps · live location streaming · barcode scan on collect/deliver
· proof-of-delivery (signature + photo + notes) · job status lifecycle · availability/shift · earnings &
self-billing visibility · compliance document upload & expiry · two-way messaging with ops · push
notifications · offline queue · in-app help/SOS.

**Out of scope (v1):** in-app payments/tipping · driver-to-driver chat · gamification/leaderboards ·
public consumer parcel-tracking page (TOM/ops side, separate) · marketplace job bidding.

See [01_Product_Spec_PRD.md](01_Product_Spec_PRD.md) for the full catalogue and rationale.
