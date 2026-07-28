# 05 — Barcode / QR Scanning Design

**TOM Driver App** · v0.1 · 2026-06-26

---

## 1. Goal

Make every parcel on a **scan-required job** scan-verified at collect and at deliver, so TOM
knows — with audit-grade certainty — that the right items went on board and the right item
reached the right drop on a multi-drop run. This is the proof layer that protects against
mis-delivery disputes and underpins the customer's "your parcel is on its way" promise.

### 1a. Scanning is per-job, not universal (`requires_scan`)

Not every booking needs barcode verification (ad-hoc document runs, envelope work). Each job
carries an explicit **`requires_scan`** flag (`jobs.requires_scan`, default **1**), declared in
TOM per booking/account rules — never inferred:

| `requires_scan` | Collect (POB) | Deliver (POD) |
|---|---|---|
| **true** | POB blocked (`409 parcels_outstanding`) until every expected parcel is collect-scanned | POD for a drop blocked (`409 parcels_outstanding`) until that drop's parcels are deliver-scanned |
| **false** | Driver confirms "parcels on board" directly — no scan step shown | Drop goes straight to signature/photo POD |

Both gates are enforced **server-side** (`store.advance_lifecycle` / `store.capture_pod`), with
the UI mirroring them (scan screens and the "📷 Scan" pill only appear on scan-required jobs).
Scanning stays *available* on non-required jobs — optional scans are accepted and audited the
same way.

## 2. Where scanning happens (the two phases)

| Phase | Trigger | What's verified |
|-------|---------|-----------------|
| **Collect (POB)** | At pickup, after **Arrived** | Each scanned barcode is matched against the job's *expected parcel set*. All present → status **POB**. Missing/extra → flagged. |
| **Deliver (POD)** | At each drop, before signature | The scanned barcode must belong to **this drop** (multi-drop guard). Right parcel → proceed to POD. Wrong drop → blocked with warning. |

## 3. Parcel model

Where do "expected" barcodes come from? Three supported sources, in priority order:

1. **From the booking** — if the customer/account supplies parcel barcodes at booking time (via
   portal/EDI), they arrive on the job's `drops[].parcels[]` and are the expected set.
2. **From a label printed by Xtra Mile** — TOM generates a barcode per parcel at booking
   (recommended; format below) and that is the expected set.
3. **Scan-to-create (fallback)** — for ad-hoc jobs with no pre-known barcodes, the *first* scan at
   collect *defines* the parcel; deliver-scan then must match it. Flagged `scan_defined` for ops.

### Recommended Xtra Mile label format
- Symbology: **Code 128** (dense, alphanumeric, ubiquitous) or **QR** for richer payloads.
- Payload: `XM<docket-suffix><drop-seq><parcel-seq><check>` e.g. `XM00420103` → human-readable +
  parseable, ties the parcel to docket + drop without a backend round-trip when offline.

## 4. Scanning UX

- Full-screen camera with a framed reticle; large, glanceable; works in sunlight and gloves.
- **Continuous multi-scan** at collect: scan parcel after parcel, each adds a green tick to a
  running checklist (`3 / 4 scanned`), haptic + sound on each accept.
- **Torch toggle**, tap-to-focus, and a **manual-entry** fallback (keypad) when a label is damaged —
  manual entries are flagged `entry: "manual"` and visually distinguished for ops.
- Clear, non-punitive error states (see §6) — the driver is never hard-blocked from doing their job,
  but every anomaly is recorded.

## 5. Technology

- **`react-native-vision-camera`** + **`vision-camera-code-scanner`** (Google **MLKit** barcode
  under the hood) — fast, on-device, offline, supports Code 128 / Code 39 / EAN / UPC / QR /
  DataMatrix / PDF417 / Aztec. No network needed to decode.
- Decoding is **entirely on-device**; only the resulting event is sent to TOM. This means scanning
  works in a basement loading bay with zero signal.

## 6. Match logic & guards (server-validated, client-previewed)

Endpoint `POST /api/driver/v1/jobs/{docket}/scan` (Doc 03 §5). The client computes a provisional
result from the cached expected set for instant feedback; the server is authoritative and records it.

| Result | Meaning | App behaviour | Recorded |
|--------|---------|---------------|----------|
| `expected` | Barcode in this job/drop's set, not yet scanned | ✅ tick, advance | yes |
| `duplicate` | Already scanned this barcode | ⚠️ "already scanned", no double count | yes |
| `unexpected` | Valid scan, not in expected set | ⚠️ "extra parcel" — driver confirms add or rejects | yes (always) |
| `wrong_drop` | Parcel belongs to a different drop | 🛑 "this parcel is for Drop 3, not Drop 2" — block POD | yes (always) |
| `manual` | Keyed, not scanned | accepted, flagged for ops | yes |

**Key principle:** anomalies return HTTP 200 with a warning *state*, not a 4xx — the physical event
happened and must be logged for ops even when it's irregular. Hard 4xx is reserved for auth/validation.

## 7. Offline behaviour

- Scans are **business events** → they go to the durable **outbox** (Doc 08), never the lossy
  location buffer.
- The expected parcel set is part of the cached run, so match-preview works offline.
- On reconnect, scan events replay **in capture order** with their device timestamps and idempotency
  keys; the server reconciles and returns any corrections (e.g. ops cancelled a parcel meanwhile).

## 8. Storage — new `parcel_events` table

Both `database/schema.py` + `database/pg_schema.py`.

| Column | Type | Notes |
|--------|------|-------|
| `event_id` | TEXT PK | client UUID = idempotency key |
| `docket_number` | TEXT | FK jobs |
| `drop_seq` | INTEGER NULL | which drop |
| `phase` | TEXT | collect / deliver |
| `barcode` | TEXT | scanned value |
| `symbology` | TEXT | code_128, qr, … |
| `entry` | TEXT | scan / manual |
| `match_result` | TEXT | expected/duplicate/unexpected/wrong_drop |
| `driver_id` | TEXT | actor |
| `recorded_at` / `received_at` | TEXT | device / server time |
| `lat`, `lng` | REAL NULL | where scanned |
| `request_id` | TEXT | correlation |

Every row also writes a `driver_actions` audit entry (the audit invariant). New job-level markers
(`last_scan_event_id`, `parcels_on_board_count`) are persisted via the **three-whitelist** discipline.

## 9. Integration with the rest of TOM

- **Status machine:** "all expected parcels scanned at collect" is the precondition the app uses to
  enable the **POB** transition; "this drop's parcel scanned" enables **POD**.
- **Multidrop:** the wrong-drop guard uses the job's existing `drops[]` ordering and
  `sequence_position` — scanning enforces the optimised sequence physically.
- **Financial Truth Layer:** unexpected/extra parcels can feed `extras_charge`; failed/returned
  parcels feed `failed_job_cost` / `redelivery_cost`.
- **Disputes:** the `parcel_events` trail + POD media + location breadcrumb form a complete,
  timestamped, geo-located evidence chain per parcel.

## 10. Edge cases
- **Damaged/missing label:** manual entry (flagged) or photo-of-parcel POD with a note.
- **One barcode, many physical items:** parcel `description`/quantity carried in the expected set;
  scan confirms the consignment, count shown to driver.
- **Re-used barcodes across jobs:** scope matching to the active job/drop, never global, so a
  recycled customer barcode can't cross-match.
- **Driver scans at wrong time** (deliver-scan before collect): server rejects the transition with a
  clear, recoverable message.
