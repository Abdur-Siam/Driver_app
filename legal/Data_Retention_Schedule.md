# Data Retention Schedule — Driver App

**Xtra Mile Couriers Ltd · Version 1.0 DRAFT · 13 July 2026**
**Status: DRAFT — becomes the written retention schedule once the DPIA
(`Driver/11_DPIA_Location_Tracking.md`) is signed off. Values below match
the DPIA. Where a period differs on sign-off, update both documents.**

---

| # | Data | System location | Retention | Trigger / rationale | Disposal |
|---|---|---|---|---|---|
| 1 | Raw GPS pings (lat/lng, speed, heading, accuracy, battery) | `locations` table | **90 days** | Recent enough for incident/dispute review; proportionality (DPIA §5) | Scheduled deletion job; per-job summarised breadcrumb retained under #2 |
| 2 | Per-job route summary, job records, scan events | `jobs`, `drops`, `parcels`, `parcel_events` | **6 years** from job completion | Contract limitation period; proof of service | Deletion at year-end sweep |
| 3 | Proof of delivery media (signatures, photos) | `data/media` + refs in `drops` | **6 years** from job completion | As #2 — the POD *is* the service evidence | Delete files + refs together |
| 4 | Pay statements, expenses, payout records | `statements`, `statement_lines`, `expenses`, `payout_requests` | **6 years** from end of tax year | HMRC record-keeping | As #2 |
| 5 | Driver profile (incl. subcontractor bank/tax details) | `drivers` | Engagement + **6 years** | Employment/HMRC claims windows | Anonymise or delete row; bank details deleted at engagement end + final settlement |
| 6 | Ops ↔ driver messages | `messages` | **12 months** | Operational record only | Scheduled deletion |
| 7 | Audit log (app actions) | `audit` | **6 years** | Security/dispute evidence; mirrors TOM invariant | As #2 |
| 8 | Login sessions / device tokens | `tokens`, `device_tokens` | Until revoked/stale, pruned automatically | Housekeeping | Automatic |
| 9 | Push delivery log | `push_outbox` | **12 months** | Delivery audit | Scheduled deletion |
| 10 | Abuse-control events (login attempts) | `rate_events` | Sliding window (minutes), pruned automatically | Security only | Automatic |
| 11 | Data-subject requests | `data_requests` | **6 years** | Evidence of rights compliance | As #2 |
| 12 | Shift records | `shifts` | **2 years** | Working-time queries | Scheduled deletion |
| 13 | Server backups (nightly) | Azure Storage | **35 days** | Matches TOM PITR window | Automatic lifecycle rule |

**Implementation note (engineering):** items 1, 6, 9, 12 need a scheduled
deletion job — a small daily cron on the VM (`DELETE ... WHERE ts < cutoff`
+ media file sweep for #3's expiries). Not yet built; it is the one code
follow-up this schedule creates, and belongs with deploy, not after.

**Erasure requests:** delete/anonymise everything not held under a legal
obligation (#2–#5, #7, #11 are retained and the requester is told why —
see Privacy Notice).
