# 01 — Product Spec / PRD

**TOM Driver App** · v0.1 · 2026-06-26

---

## 1. Problem & opportunity

TOM's operations board, pricing engine and dispatch brain are feature-complete, but the
**driver edge** is still a web surface designed for a browser. For commercial operation Xtra Mile
needs a true mobile app that a courier can run all day, one-handed, in poor signal, on a £150
Android phone, while driving. The three capabilities the business cannot ship without — and that
TOM has no client for today — are **live location**, **barcode/parcel scanning**, and
**optimised multi-drop navigation**. This app closes that gap and becomes the data source that
makes the ops board's ETAs and the customer's tracking promises *true*.

## 2. Goals & success metrics

| Goal | Metric | Target (commercial) |
|------|--------|---------------------|
| Drivers self-serve their whole day | % runs completed with zero ops phone calls | > 85% |
| Honest ETAs | Median |predicted − actual| arrival error | < 8 min |
| No lost proof | POD + scan capture rate of completed jobs | 100% (offline-safe) |
| Battery acceptable | Battery drain over a 9-hr shift with tracking on | < 35% incremental |
| Routing earns its keep | Avg miles saved per multi-drop run vs naive order | ≥ 8% |
| Adoption | Active drivers on app / total active drivers | > 90% within 60 days |
| Reliability | Crash-free sessions | > 99.5% |

## 3. Personas

1. **Owned-fleet driver (primary).** Employed/contracted to Xtra Mile, callsign without a
   subcontractor prefix (`drivers.is_subcontracted = 0`). Full feature set, including earnings and
   compliance. Highest trust.
2. **Subcontractor driver.** Callsign begins `CX / SWIFT / AGENCY / SUB / Z00`
   (`is_subcontracted = 1`, see `app/subcontractor.py`). Same operational features; **self-billing**
   figures and document requirements differ; may be VAT-registered (`drivers.vat_registered`).
3. **Recruit / onboarding driver.** `pipeline_stage` in {applicant…}; `can_receive_jobs = 0`.
   App shows onboarding checklist + document upload only — no job allocation until approved.
4. **Operations controller (indirect persona).** Not an app user, but every design choice is
   judged by "does this make the board more truthful and the controller's job easier?"

## 4. Feature catalogue

Priority: **P0** = v1 launch-blocking · **P1** = v1 if time · **P2** = fast-follow.

### 4.1 Identity & device
- **P0** Driver login via TOM credentials (driver-id + password) → token; backed by
  `drivers_core/driver_auth_store.py` (lockout/failed-count already exist).
- **P0** Device binding (one active device per driver; re-bind requires re-auth) + remote sign-out.
- **P0** Biometric unlock (FaceID/fingerprint) for app re-entry after first login.
- **P1** Forced password reset / first-login password set (store already models `password_set_at`).

### 4.2 The run (job lifecycle)
- **P0** **Today's run** list — assigned jobs for `operational_date = today`, in optimised order.
- **P0** **Job card** — pickup, drops (ordered), vehicle, deadline, account, notes, special
  instructions, parcel count.
- **P0** **Status lifecycle** mapped to TOM statuses: `ACCEPTED → ON ROUTE TO PU → ARRIVED →
  POB (parcel on board) → ON ROUTE - POB → POD → COMPLETED`, plus `COA / CANCELLED ON ARRIVAL`.
  (These are the live values already in `_JOBS_ACTIVE_STATUSES` / completed sets in `app/web/app.py`.)
- **P0** Accept / decline an offered job (with decline reason) where ops offers rather than hard-assigns.
- **P1** "Running late" self-flag that posts a `driver_issues` entry to the job.

### 4.3 Navigation & routing
- **P0** Optimised multi-drop **sequence** received from TOM (server computes; see Doc 06).
- **P0** In-app map (Google Maps SDK) showing the run, current leg, next stop, live ETA.
- **P0** **Hand-off to Google Maps / Waze** for turn-by-turn for the active leg.
- **P1** In-app turn-by-turn (Navigation SDK) — premium tier; gated behind cost sign-off.
- **P1** Re-optimise on the fly when a stop is added/removed or a drop fails.

### 4.4 Live tracking
- **P0** Background location streaming while on an active job (Doc 04).
- **P0** Foreground "share trip" so the board (and optionally the customer) sees driver position + ETA.
- **P0** Auto start/stop tracking on job accept/complete; manual off honoured with consent record.

### 4.5 Scanning & proof
- **P0** **Barcode/QR scan on collect** — verify each parcel against the job's expected parcel set.
- **P0** **Barcode/QR scan on deliver** — confirm the right parcel at the right drop (multi-drop guard).
- **P0** **Proof of delivery** — captured name, on-glass **signature**, **photo**, optional notes.
- **P0** **Failure capture** — reason codes (no-access, refused, damaged, wrong address) + photo.
- **P1** Manual entry fallback when a barcode is unreadable (keys the code, flagged "manual").

### 4.6 Earnings & commercial
- **P1** Per-job and per-day **earnings** view, sourced from durable `driver_pay_final` (Step 3).
- **P1** **Self-billing statement** visibility for subcontractors (aligns with TIA driverpay model).
- **P2** Expense submission (mirrors `driver_expenses_routes.py`): tolls, parking, congestion.

### 4.7 Availability & compliance
- **P0** **Availability / shift** toggle (on-duty/off-duty) + working-area — feeds dispatch
  eligibility (`active`, availability routes already exist).
- **P0** **Document wallet** — insurance, goods-in-transit, MOT, licence — upload + expiry status
  (mirrors `driver_documents` table). Block job offers if a mandatory doc is expired.
- **P1** Vehicle check / daily walk-around checklist (medical: red box, spill kit, DBS flags exist).

### 4.8 Communication & support
- **P0** **Two-way messaging** with ops (mirrors `driver_messages_routes.py`).
- **P0** **Push notifications** — new job, route change, message, document-expiry warning.
- **P0** **In-app SOS / help** — one-tap call ops, plus incident report.
- **P2** Broadcast / company announcements.

### 4.9 Cross-cutting
- **P0** **Offline-first** — view today's run, scan, capture POD, change status with no signal; sync later.
- **P0** Accessibility (large tap targets, high-contrast/dark, dynamic type, glove/sunlight legibility).
- **P0** Multilingual-ready (i18n scaffold; English first).
- **P1** In-app diagnostics / "send logs to ops" for support.

## 5. Primary user journeys

### J1 — Start of shift
Open app → biometric unlock → go **On Duty** → app pulls today's run (optimised) → reviews route →
push received when ops finalises allocation.

### J2 — Collect
Tap first job → "Navigate" hands to Google Maps → arrives, geofence auto-prompts **Arrived** →
**Scan parcels** (each barcode checked against expected set) → all matched → status **POB** →
location streaming continues.

### J3 — Multi-drop delivery
App shows next optimised drop → navigate → arrive → **scan the parcel for this drop** (guard against
delivering drop-3's parcel at drop-2) → capture **signature + photo** → status **POD** for that drop →
app advances to next drop → on last drop, job **COMPLETED**.

### J4 — Failed delivery
Arrive → recipient absent → **Failure** → reason `no-access` + photo → ops notified in real time →
job routed per ops decision (redeliver/return); cost fields (`failed_job_cost`, `redelivery_cost`) flow
into TOM's Financial Truth Layer.

### J5 — Dead-spot resilience
Driver in a basement loading bay: scans 6 parcels, captures POD with no signal. All events persist to
the on-device queue and replay in order when signal returns; nothing re-keyed, nothing lost.

## 6. Non-goals (v1)
In-app card payments/tipping · driver↔driver chat · gamified leaderboards · public consumer tracking
page (lives on TOM/ops side) · marketplace bidding for jobs · multi-tenant white-label theming
(TOM productisation handles tenancy separately).

## 7. Assumptions & dependencies
- TOM backend gains the `/api/driver/v1/*` surface and new tables (Docs 03, 08).
- A Google Maps Platform billing account + restricted API keys are provisioned (Doc 06, 09).
- Apple Developer + Google Play org accounts exist for store distribution.
- Managed Postgres is the production backend (the app's writes assume PgBouncer transaction-pooling).
- Push: a Firebase project (FCM) and APNs key.

## 8. Open product questions (for sign-off)
1. **Offer vs assign:** does ops hard-assign jobs or offer-and-accept? (Affects J1/4.2.) Default
   assumed: hard-assign with a decline-with-reason escape hatch.
2. **Customer-facing live tracking:** in v1 scope, or board-only? (Doc 04 supports both; legal/consent
   differ.) Default assumed: board-only in v1, customer link P1.
3. **In-app turn-by-turn** (Navigation SDK, premium cost) vs **hand-off** to Google Maps (free)?
   Default assumed: hand-off in v1.
4. Earnings visibility for subcontractors — show full self-billing in-app, or summary only pending
   TIA integration? Default assumed: summary in v1.
