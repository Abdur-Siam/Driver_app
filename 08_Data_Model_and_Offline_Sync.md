# 08 — Data Model & Offline Sync

**TOM Driver App** · v0.1 · 2026-06-26

---

## 1. Principle

**Server is authoritative for assignment; device is authoritative for field events.** TOM decides
*which* jobs are yours; the device records *what physically happened* (arrive, scan, POD, fail) with
on-device timestamps. The two reconcile through an idempotent, ordered sync — never losing a business
event, tolerating lost location pings.

## 2. On-device schema (SQLite via `op-sqlite`/`expo-sqlite`)

| Table | Purpose | Notes |
|-------|---------|-------|
| `run_jobs` | cached today's run (jobs + drops + parcels + sequence) | refreshed from `GET /run`; read-side source of truth offline |
| `parcels_expected` | expected parcel set per drop | powers offline scan match-preview |
| `outbox` | durable queue of mutations | the heart of offline-first (below) |
| `location_buffer` | ring buffer of pings | bounded, lossy, flushed in batches |
| `messages` | cached ops chat | |
| `documents` | cached doc status | |
| `kv` (MMKV) | tokens (SecureStore), flags, last-sync cursors | secrets in Keychain/Keystore, not SQLite |

### Outbox row
```
{ id: uuid,                 // = idempotency key sent as X-Idempotency-Key
  type: "status|scan|pod|fail|message|availability|document",
  docket_number, drop_seq,
  payload: json,            // the request body
  media_refs: [localUri],   // signature/photo files staged on disk
  created_at,               // device time (authoritative for the event)
  attempts, last_error,
  state: "pending|sending|done|conflict" }
```

## 3. Sync engine

### 3.1 Outbound (writes) — durable, ordered, idempotent
1. A user action writes an `outbox` row **first** (and stages any media on disk), then updates the
   local UI optimistically.
2. A drainer sends rows **in `created_at` order**, one logical event at a time, with the row `id` as
   `X-Idempotency-Key`.
3. Server applies idempotently (Doc 03 §1): first apply does the work + stores the result keyed by
   the UUID; any retry returns the stored result. So a flaky network that retries never double-posts a
   POD or a status change.
4. On `2xx` → mark `done`, delete staged media. On retryable error → backoff + retry. On
   `conflict` (server state moved) → mark `conflict`, surface a corrective state pulled from server.

### 3.2 Inbound (reads) — cache + revalidate
- `react-query` caches `GET /run`, messages, docs, earnings; revalidates on focus/online/push.
- Push messages are **hints** — the app refetches authoritative state, never trusts the payload as
  truth (avoids the TIA-style "client state drifts" problem).
- A `route.version` / job `updated_at` cursor lets the client detect and reconcile server changes.

### 3.3 Location flush
- `location_buffer` is a **bounded ring** (e.g. last N pings); oldest dropped under pressure.
- Flushed via `POST /location/batch` on timer/threshold; `202` clears the flushed slice.
- **Lossy on purpose** — location is telemetry, not proof; business events live in the durable outbox.

## 4. Conflict resolution

| Scenario | Resolution |
|----------|-----------|
| Driver completes a job ops cancelled meanwhile | Server returns `conflict`; app shows "ops cancelled this job", moves event to an audit-only note; no silent overwrite. |
| Ops reassigns a job after driver cached it | Next `GET /run` drops it from the run; any queued events for it are reconciled/rejected with reason. |
| Two devices for one driver | Device binding (one active device) prevents split-brain; old device is signed out. |
| Re-optimise changes order mid-run | `route.version` bump; in-progress (POB) job stays pinned; remaining re-sequenced. |
| Duplicate POD from retry | Idempotency key collapses to one. |

**Timestamps:** device `created_at` is authoritative for *when the event happened*; server
`received_at` records arrival. ETAs/billing use server time to avoid device-clock abuse.

## 5. New server-side tables (both `schema.py` + `pg_schema.py`)

| Table | Role | Cross-ref |
|-------|------|-----------|
| `driver_locations` | raw pings | Doc 04 §3.2 |
| `driver_latest_location` | one-row-per-driver hot read | Doc 04 §3.3 |
| `parcel_events` | scan events | Doc 05 §8 |
| `driver_devices` | device binding (driver↔device, active flag, last seen) | Doc 03 §2 |
| `driver_push_tokens` | FCM/APNs tokens | Doc 03 §9 |
| `idempotency_keys` | (key, route, result_json, created_at) | Doc 03 §1 |
| `driver_pod_media` | references to signature/photo objects (object store keys) | Doc 03 §6 |

New **persisted job fields** (added to all **three** whitelists + round-trip test, per ADR-003
trap): `pod_captured_at`, `last_scan_event_id`, `parcels_on_board_count`, `route_version`. Money-like
fields (none new here) would follow the decimal-string rule.

## 6. Media handling
- Signatures (PNG) and photos (JPEG, compressed) staged on device, uploaded with the POD/fail event
  (multipart), then deleted locally on confirmation.
- Server stores media in object storage (e.g. Azure Blob) and keeps only references in
  `driver_pod_media`; PgBouncer/Postgres never holds blobs.
- Media is access-controlled (signed, time-boxed URLs); part of the dispute evidence chain.

## 7. Dual-backend + PgBouncer compliance
- All new tables/queries go through `database/stores/*` and the `?`→`%s` translation; tested on
  **both** SQLite and Postgres (the ~10k-test gate).
- Hot inserts (location, events) are single-statement, immediate-commit; no transaction is held across
  network/object-store I/O — **PgBouncer transaction-pooling safe**.
- No long `FOR UPDATE` locks on the driver hot path; job mutations reuse `jobs_core.mutations`'
  existing short-lock pattern.

## 8. Data lifecycle & retention (ties to Doc 09)
- Location: raw retained 30–90 days → down-sampled per-job breadcrumb → purge.
- Outbox: deleted on confirmed sync; failed rows retained for support with a cap.
- POD media: retained per contractual/dispute window, then purged on policy.
- Driver right-to-erasure honoured via documented purge jobs.
