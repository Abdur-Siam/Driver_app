# 09 — Security, Privacy & Compliance

**TOM Driver App** · v0.1 · 2026-06-26

---

## 1. Threat model (what we're protecting)
- **Driver PII** (name, contact, bank/NI for subcontractors, location history).
- **Customer data** (addresses, contacts, parcel contents on the run).
- **Commercial data** (pricing, pay, account identities).
- **The Google Maps key** (billing-fraud target).
- **Account takeover** (a stolen phone = access to a driver's run and ops chat).

## 2. Authentication & session
- Login via `drivers_core/driver_auth_store.py` — **hashed** passwords, existing lockout/failed-count;
  the app never sees or stores the password beyond the login call.
- **Opaque bearer token**, short TTL + refresh; bound to a **device id** (one active device/driver).
  Tokens in **Keychain (iOS) / Keystore (Android)** via SecureStore — never in SQLite/MMKV/plain.
- **Biometric** unlock to re-enter after backgrounding; biometric failure → password.
- **Remote sign-out** + "signed out elsewhere" push on re-bind. Forced reset sets `password_set_at`.
- **No secrets in the binary** — no API keys, no admin tokens; the app only holds its own session token.

## 3. Transport & API security
- **TLS 1.2+ everywhere**; consider certificate pinning for the API host.
- All `/api/driver/v1/*` calls authenticated + device-bound; mutations idempotency-keyed.
- Driver tokens are **not** admin sessions — the driver API is its own `web/dept_policy.py` section;
  a driver token can never reach admin/sales/accounts routes.
- Rate-limiting on login (lockout) and location batch (size + frequency caps).
- Input validation server-side; status transitions validated against the allowed set; media type/size
  checks on upload.

## 4. The Google Maps key (critical)
- **Server-side only.** The device never holds a routing/optimisation key (Doc 06 §3). The Maps **SDK**
  display key (which must ship in the app) is **separately restricted** to the app's bundle id / SHA-1
  and to Maps-SDK APIs only — it cannot call billable routing.
- Server key: **IP-restricted** + **API-restricted** (only the routing/matrix/geocoding APIs it needs).
- **Usage caps + budget alerts** in Google Cloud; daily per-driver optimisation budget; anomaly alerts.
- Keys in **Azure Key Vault**, rotated; never in source or client config.

## 5. Location privacy & law (UK GDPR / DPA 2018)
Location of an identifiable worker is **personal data**; continuous worker tracking is sensitive.
- **Lawful basis + transparency:** documented basis (legitimate interest/contract), a clear privacy
  notice, and a worker-facing explanation of what's tracked, when, and why.
- **Explicit, revocable consent** captured in-app, timestamped to `driver_actions`.
- **On-duty / on-job only** — never off-shift; the off toggle is honoured and logged (Doc 04 §5).
- **Data minimisation** — adaptive sampling, short raw retention, down-sample to per-job breadcrumb
  (Doc 04 §3.4 / Doc 08 §8).
- **Subject rights** — driver can view and request erasure of their own location history.
- **Subcontractor nuance** — tracking scope may be contract-specific; per-driver policy, not hardcoded.
- **DPIA** — a Data Protection Impact Assessment for continuous tracking should be on file before go-live.

## 6. Data protection in depth
- **PII at rest:** sensitive driver fields (bank/NI for subbies) follow TIA's precedent —
  **AES-256-GCM** encryption + an access log for any privileged read (mirrors TIA's `PiiAccessLog`).
- **Least exposure to the app:** the app receives only what the run needs; no full customer ledger,
  no other drivers' data, no pricing internals beyond the driver's own pay.
- **Audit:** every driver mutation → `driver_actions` (actor, action, before/after, `request_id`) —
  the existing TOM invariant, extended to scan/POD/location-consent events.
- **Media:** POD signatures/photos in access-controlled object storage with signed, expiring URLs.

## 7. Device & app hardening
- Block on rooted/jailbroken devices (warn + optionally restrict) for fleet-grade trust.
- No sensitive data in logs/crash reports (PII redaction — TOM already has `modules/pii/redaction.py`
  as a server precedent; mirror client-side).
- Obfuscation/minification of the release build; disable debug bridges in prod.
- Secure local DB (the outbox/run cache holds customer addresses) — encrypt the SQLite file or store
  only what's necessary, purge completed runs.

## 8. App-store compliance
- **Apple:** location "Always" usage strings + a clear justification (background delivery tracking);
  privacy nutrition labels; no hidden tracking; sign-in security.
- **Google Play:** Background Location declaration + demo video justifying necessity; Data Safety form;
  Prominent Disclosure + consent before first background fix.
- Both: account-deletion path, privacy policy URL, data-handling disclosure.

## 9. Operational security
- Staging vs prod key separation; secrets in Key Vault; CI never prints secrets.
- SLO + alerting on auth failures, location anomalies (spoofing/improbable jumps), error spikes.
- Incident response: remote sign-out, token revocation, key rotation runbooks.
- Ties into TOM's outstanding hardening items (secrets→Key Vault, TLS, WAF, MFA, log rotation) — the
  driver API should not ship ahead of those backend controls.

## 10. Compliance checklist (go-live gate)
- [ ] DPIA for driver location tracking completed and signed off.
- [ ] Privacy notice + in-app consent flow live and logged.
- [ ] Maps keys restricted (app-bundle for SDK, IP+API for server) with budget caps.
- [ ] PII encryption + privileged-read access log for sensitive driver fields.
- [ ] Tokens in Keychain/Keystore; device binding + remote sign-out working.
- [ ] Retention + erasure jobs implemented and scheduled.
- [ ] Store privacy declarations (Apple + Google) prepared.
- [ ] Pen test / security review of the new driver API surface.
- [ ] All new tables/queries green on SQLite **and** Postgres; PgBouncer-safe.
