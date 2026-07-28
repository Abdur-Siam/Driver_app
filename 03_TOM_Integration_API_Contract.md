# 03 — TOM Integration & API Contract

**TOM Driver App** · v0.1 · 2026-06-26
Surface: **`/api/driver/v1/*`** (new Flask blueprint `driver_api_v1`)

---

## 1. Design rules

- **JSON in/out**, UTF-8, ISO-8601 UTC timestamps, money as **decimal strings** (`"15.50"`).
- **Auth:** bearer token (opaque), issued at login, bound to a device id. Header
  `Authorization: Bearer <token>` + `X-Device-Id: <uuid>`.
- **Idempotency:** all mutating calls accept `X-Idempotency-Key: <uuid>` (the offline outbox UUID);
  server stores the key + result, replays return the stored result, never double-apply.
- **Correlation:** server stamps/propagates `X-Request-ID` (reuses `web/request_id.py`).
- **Versioning:** `/v1`; breaking changes → `/v2`. The app sends `X-App-Version` for soft gating.
- **Errors:** `{ "error": { "code": "string", "message": "human", "retryable": bool } }` + HTTP status.
- **Audit:** every mutation writes a `driver_actions` row (actor = driver id, action, before/after,
  request_id) — TOM's existing invariant.

## 2. Auth & device

### `POST /api/driver/v1/auth/login`
Backed by `drivers_core/driver_auth_store.py` (existing lockout/failed-count logic).
```jsonc
// request
{ "driver_id": "DRV001", "password": "••••••", "device": {
    "id": "uuid", "platform": "ios|android", "model": "Pixel 7", "app_version": "1.0.0" } }
// 200
{ "token": "opaque", "expires_at": "2026-06-26T20:00:00Z",
  "driver": { "driver_id": "DRV001", "name": "...", "callsign": "B05 - N11",
              "is_subcontracted": 0, "can_receive_jobs": 1, "vehicle": "Small Van" },
  "must_set_password": false, "feature_flags": { "in_app_nav": false } }
// 401 locked → { "error": { "code": "account_locked", "retryable": false, ... } }
```
Other auth: `POST /auth/refresh`, `POST /auth/logout` (revokes device), `POST /auth/set-password`
(first-login / forced reset → sets `password_set_at`), `POST /auth/biometric-rebind`.

**Device binding:** one active device per driver; a login from a new device invalidates the old
token and emits a `driver_actions` event + push to the old device ("signed out elsewhere").

## 3. The run (read)

### `GET /api/driver/v1/run?date=today`
Returns the driver's assigned jobs for `operational_date`, **in optimised sequence** (Doc 06),
read from the spine board (`_merged_job_list` scoped to this driver), preserving `drops`,
`drop_coords`, `sequence_position`, `multidrop_status`.
```jsonc
{
  "date": "2026-06-26",
  "route": { "optimised": true, "version": 7, "total_distance_km": 41.2,
             "total_duration_min": 96, "computed_at": "2026-06-26T07:55:00Z" },
  "jobs": [
    {
      "docket_number": "XM-20260626-0042",
      "status": "ACCEPTED",
      "sequence_position": 1,
      "account": "ACME01",
      "vehicle": "Small Van",
      "deadline_minutes": 870,                 // 14:30 as minute-of-day
      "pickup": { "address": "...", "postcode": "EC1A 1BB",
                  "lat": 51.5, "lng": -0.1, "contact": "...", "notes": "Use rear door" },
      "drops": [
        { "seq": 1, "address": "...", "postcode": "N1 9GU", "lat": 51.5, "lng": -0.09,
          "contact": "...", "instructions": "Leave with concierge",
          "parcels": [ { "barcode": "XM7791234567", "description": "1 x box" } ] }
      ],
      "parcel_count": 1,
      "special_instructions": "Fragile",
      "requires_scan": true,                   // per-job: scanning enforced at collect + deliver.
                                               // false → straight to POB/POD (scanning optional).
                                               // Declared in TOM per booking/account — never inferred.
      "is_subcontracted": 0
    }
  ]
}
```
Supporting reads: `GET /run/{docket}` (single job detail), `GET /run/history?from=&to=`,
`GET /run/offers` (if offer-mode).

## 4. Job lifecycle (write)

### `POST /api/driver/v1/jobs/{docket}/status`
Drives the live TOM status machine. Allowed transitions validated server-side against
`_JOBS_ACTIVE_STATUSES` / completed sets in `app/web/app.py`.
```jsonc
// request  (X-Idempotency-Key required)
{ "status": "ARRIVED", "at": "2026-06-26T08:12:33Z",
  "location": { "lat": 51.5, "lng": -0.1, "accuracy_m": 8 },
  "drop_seq": null }                          // set when a status applies to one drop
// 200 → echoes canonical job state after transition
```
Status vocabulary (subset, already live in TOM):
`ACCEPTED · ON ROUTE TO PU · ARRIVED · POB · ON ROUTE - POB · POD · COMPLETED · COA · CANCELLED ON ARRIVAL`.

Accept/decline (offer mode): `POST /jobs/{docket}/accept`, `POST /jobs/{docket}/decline`
(`{ "reason_code": "too_far|vehicle|capacity|other", "note": "" }`).

Self-flag: `POST /jobs/{docket}/issue` → appends to the job's `driver_issues` (JSON) list.

Scan enforcement (server-side, only when the job carries `requires_scan: true`):
`collected`/POB is rejected `409 parcels_outstanding` until every expected parcel is
collect-scanned, and POD for a drop is rejected `409 parcels_outstanding` until that
drop's parcels are deliver-scanned. Jobs with `requires_scan: false` skip both gates.

## 5. Scanning & parcels (write) — detail in Doc 05

### `POST /api/driver/v1/jobs/{docket}/scan`
```jsonc
{ "phase": "collect|deliver",
  "drop_seq": 1,
  "barcode": "XM7791234567",
  "symbology": "code_128",
  "entry": "scan|manual",                     // manual = keyed fallback, flagged
  "at": "2026-06-26T08:13:10Z",
  "location": { "lat": 51.5, "lng": -0.1 } }
// 200
{ "match": "expected|unexpected|duplicate|wrong_drop",
  "parcel_state": "on_board|delivered",
  "remaining_expected": 0 }
```
Server writes a `parcel_events` row and a `driver_actions` audit row. `wrong_drop` and `unexpected`
return 200 with a warning state (the app blocks/queries the driver), never a hard 4xx — the event is
still recorded for ops visibility.

## 6. Proof of delivery (write)

### `POST /api/driver/v1/jobs/{docket}/pod`  (multipart)
```
fields:  drop_seq, recipient_name, at, location, note, X-Idempotency-Key
files:   signature (png), photo[] (jpeg, ≤ N)
```
On success: sets job/drop POD fields, writes the durable `pod_captured_at` (new persisted field,
three-whitelist registered), uploads media to object storage, returns media references. Failure
capture uses `POST /jobs/{docket}/fail` with `reason_code` + photo; this can populate
`failed_job_cost` / `redelivery_cost` in TOM's Financial Truth Layer downstream.

## 7. Live location (write) — detail in Doc 04

### `POST /api/driver/v1/location/batch`
Hot path. Batched, compact, fire-and-forget (best-effort, lossy).
```jsonc
{ "device_id": "uuid",
  "pings": [
    { "t": "2026-06-26T08:12:00Z", "lat": 51.5007, "lng": -0.1246,
      "spd": 9.4, "hdg": 270, "acc": 6, "job": "XM-20260626-0042" }
  ] }
// 202 Accepted  (no body)
```
Writes to `driver_locations`; the spine reads the latest row per driver to enrich the board and
ETAs. Single-statement inserts, immediate commit — PgBouncer-safe.

## 8. Routing

### `POST /api/driver/v1/route/optimise`
The app requests (re)optimisation; **TOM calls Google Maps server-side** and returns the ordered
sequence. The device never holds the Maps key. See Doc 06 for the engine.
```jsonc
// request
{ "date": "2026-06-26",
  "from": { "lat": 51.5, "lng": -0.1 },       // current position
  "constraints": { "respect_deadlines": true, "locked_first": "XM-20260626-0042" } }
// 200
{ "version": 8, "ordered_dockets": ["XM-...0042","XM-...0051"],
  "legs": [ { "from_docket": null, "to_docket": "XM-...0042",
              "distance_km": 3.1, "duration_min": 9, "eta": "2026-06-26T08:10:00Z" } ],
  "total_distance_km": 41.2, "total_duration_min": 96 }
```
Optimisation result is persisted to each job's `sequence_position` so the board and app agree.

## 9. Earnings, docs, availability, messages (reuse existing stores)

| Capability | New v1 endpoint | Backed by existing TOM |
|------------|-----------------|------------------------|
| Earnings (per job/day) | `GET /earnings?from=&to=` | durable `driver_pay_final` (Step 3) |
| Self-billing (subbie) | `GET /earnings/statement/{period}` | aligns w/ TIA driverpay model |
| Documents wallet | `GET /documents`, `POST /documents` (multipart) | `driver_documents` table, `driver_documents_routes.py` |
| Availability/shift | `GET /availability`, `POST /availability` | `driver_availability_routes.py`, `drivers.active` |
| Messages | `GET /messages`, `POST /messages`, `POST /messages/read` | `driver_messages_routes.py` / `driver_messages` table |
| Notifications register | `POST /push/register` (FCM/APNs token), `DELETE /push/register` | new `driver_push_tokens` table |
| Expenses | `POST /expenses` (P2) | `driver_expenses_routes.py` |

## 10. Push notification events (server → device)
`job.assigned` · `job.updated` · `route.reoptimised` · `message.new` · `document.expiring`
· `shift.reminder` · `auth.signed_out_elsewhere`. Payloads carry the `docket_number` / resource id
for deep-linking; the app refetches authoritative state (push is a hint, never the source of truth).

## 11. Rate limits & abuse
- Login: existing lockout (failed-count) + IP throttle.
- Location batch: bounded payload size + per-device rate cap (drop excess client-side).
- All mutating routes idempotency-keyed to neutralise retry storms from flaky networks.

## 12. Mapping summary — new vs reused

**New backend work:** `driver_api_v1` blueprint · `driver_locations`, `parcel_events`,
`driver_push_tokens`, `driver_devices`, `idempotency_keys` tables · route-optimise proxy ·
push dispatcher · a handful of new persisted job fields (POD/scan markers).

**Reused unchanged:** driver auth store · drivers table · `driver_documents` · `driver_messages` ·
availability · spine board / multidrop / `sequence_position` · durable pricing/pay fields ·
`driver_actions` audit · `request_id` correlation · dual-backend stores layer.
