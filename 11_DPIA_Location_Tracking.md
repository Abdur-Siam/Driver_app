# 11 — DPIA: Driver App Location Tracking (DRAFT for director sign-off)

**Xtra Mile Couriers Ltd · TOM Driver App v1.1.0 · Drafted 13 July 2026**
**Status: DRAFT — requires review and sign-off by the data controller
(directors) before commercial go-live. Doc 09 §10 makes this a go-live gate.**

---

## 1. What this assessment covers

The Driver App continuously records the GPS position of couriers while they
are on duty, streams it to the company server, and uses it for live job
tracking, dispatch decisions, route optimisation and customer ETAs.
Location of an identifiable worker is personal data under UK GDPR / DPA
2018, and systematic monitoring of workers is the kind of processing the
ICO expects a DPIA for. This document is that assessment.

Also in scope, because the same app collects them: proof-of-delivery
signatures and photos, barcode scan events (time + place), and driver
profile data (including bank details for self-billing subcontractors).

## 2. The processing, described honestly

- **What:** GPS fixes (lat/lng, speed, heading, accuracy, battery),
  captured at an adaptive rate while a shift is active, tagged to the
  current job where one is in progress.
- **When:** only between *start shift* and *end shift* / *going home*.
  The app takes no fixes off-shift; the server rejects location submissions
  from any driver who has not granted consent (HTTP 403, enforced in code).
- **Who sees it:** operations staff on the TOM board (live positions,
  job progress); customers see only job-level ETAs, never a raw feed.
- **Where:** company-controlled server (UK/EU hosting), SQLite/Postgres;
  POD media in access-controlled storage served via short-lived signed URLs.
- **Automated decisions:** route order suggestions and dispatch advisories.
  No solely-automated decision with legal or similarly significant effect
  is taken about a driver; assignment stays with a human dispatcher
  (the North-Star human-approval invariant).

## 3. Lawful basis

- **Employed drivers:** legitimate interests (Art. 6(1)(f)) — proof of
  service, fleet safety, accurate customer ETAs — with the in-app consent
  flow retained as a transparency and control measure. Consent alone is a
  weak basis in an employment relationship (imbalance of power), so the
  legitimate-interests assessment below carries the weight.
- **Subcontractors:** contract (Art. 6(1)(b)) where tracking is a term of
  the subcontract; scope may be narrowed per contract (Doc 09 §5).
- **LIA balance:** tracking is limited to working time, is what a courier
  reasonably expects of the job in 2026, is proportionate to the business
  need (same-day and medical delivery requires live visibility), and the
  intrusion is reduced by the minimisation measures in §5. Balance: passes,
  provided off-shift capture stays impossible and retention stays short.

## 4. Necessity and proportionality

Could the purpose be met with less data? Alternatives considered:
- *Manual status calls* — the legacy ECHO pattern; slower, error-prone, and
  still discloses location, just verbally. Rejected as no less intrusive.
- *Per-stop check-ins only* — insufficient for live ETAs and for the
  medical work's SLA evidence.
- *Continuous tracking including off-shift* — rejected outright; the design
  makes it technically impossible (no shift, no fixes).

The chosen design (on-shift adaptive sampling, job-tagged, down-sampled
history) is the minimum that meets the operational need.

## 5. Minimisation, retention and security (proposed values)

| Data | Retention | Rationale |
|---|---|---|
| Raw GPS pings | **90 days**, then deleted (down-sampled per-job breadcrumb kept with the job record) | Recent-enough for disputes/incidents; short enough to be proportionate |
| Per-job breadcrumb + POD (signature, photos, scan events) | **6 years** with the job record | Limitation period for contract claims; proof of service |
| Driver profile / pay records | Duration of engagement + statutory periods (HMRC 6 years) | Legal obligation |
| Push tokens, session tokens | Pruned when stale/revoked | Housekeeping |

Security measures already implemented in v1.1.0: TLS-only deployment,
token auth with device binding and revocation, per-account lockout and
rate limiting (durable, multi-worker safe), driver-scoped signed media
URLs, strict security headers, audited mutations, server-enforced consent
gate on location ingest, production boot guardrails (no demo credentials,
explicit server secret). Bank-detail changes route to ops review rather
than applying live.

## 6. Rights and transparency

- **Privacy notice:** in-app Privacy & data screen + driver handbook page;
  must state what is tracked, when, why, retention, and who to contact.
- **Access / erasure:** in-app data-request buttons create `data_requests`
  rows routed to ops (implemented); erasure honours the retention table
  above (job-record data is kept under the legal-obligation/contract
  bases and this is explained in the notice).
- **Withdrawal:** location consent is withdrawable in-app; the server then
  refuses further pings. Operational consequence (no live-tracking work)
  is explained to the driver — that is an employment conversation, not a
  data-protection penalty.

## 7. Risks and mitigations

| Risk | L×S | Mitigation | Residual |
|---|---|---|---|
| Function creep — location used for performance surveillance beyond the stated purposes | M×H | Purposes fixed in this DPIA + privacy notice; ops access is to the live board, not ad-hoc history queries; any new purpose requires a DPIA revision | Low |
| Off-shift tracking (bug or misuse) | L×H | No-shift-no-fix design; server consent gate; audit rows on consent changes; test coverage | Low |
| Breach of location history | L×H | Access-controlled DB on a hardened single host, TLS, no world-readable media, 90-day raw retention caps blast radius; backup encryption at rest in Azure Storage | Low |
| Stolen phone → account takeover | M×M | Device-bound single session, biometric app-lock, ops remote sign-out, password reset revokes sessions | Low |
| Subcontractor scope mismatch | M×M | Per-contract tracking scope flag (declared in TOM, never inferred from ECHO) | Low |

## 8. Sign-off

| Role | Name | Decision | Date |
|---|---|---|---|
| Data controller (director) | — | ☐ approve ☐ approve with changes ☐ reject | — |
| Processing owner (ops) | — | | — |
| Author | Jeet Sarkar (AI developer) | drafted | 2026-07-13 |

Review annually, on any new tracking purpose, or at the TOM merge
(which changes the storage location of this data).
