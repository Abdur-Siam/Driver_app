# Driver App — Legal Pack (go-live gate)

**Drafted 13 July 2026. Everything here is a DRAFT until the directors
approve it; a solicitor's review before commercial go-live is recommended.**

| Document | What it is | Who acts | Status |
|---|---|---|---|
| [`../11_DPIA_Location_Tracking.md`](../11_DPIA_Location_Tracking.md) | Data Protection Impact Assessment — the mandatory one | Directors review + sign §8 | DRAFT |
| [`Driver_Privacy_Notice.md`](Driver_Privacy_Notice.md) | Hand to every driver before first shift; acknowledgement slip at the bottom | Fill contact details; issue + file acknowledgements | DRAFT |
| [`Contract_Clauses_Tracking.md`](Contract_Clauses_Tracking.md) | §1 staff-handbook monitoring clause; §2 subcontractor-agreement tracking clause | Insert into handbook + subcontract template (solicitor check) | DRAFT |
| [`Data_Retention_Schedule.md`](Data_Retention_Schedule.md) | Written retention schedule (matches the DPIA) | Approve with the DPIA | DRAFT |
| [`Public_Privacy_Policy_DriverApp.md`](Public_Privacy_Policy_DriverApp.md) | Public policy for the app-store listing URL | Publish on xtramilecouriers.co.uk before store submission | DRAFT |
| [`Store_Privacy_Declarations_Prep.md`](Store_Privacy_Declarations_Prep.md) | Pre-filled Apple nutrition labels, Play Data Safety, background-location video script | Use when Jack's store accounts exist | READY |

## Not documents, but on the same gate

- **ICO data-protection fee — checked on the public register, 13 July 2026.**
  Two live Tier 1 registrations, both at 1110 Elliott Court, Coventry CV5 6UB:
  - **Xtra Mile Couriers London Ltd** — ref **ZB739114**, registered 20 Aug 2024
  - **Xtra Mile Medical Couriers Ltd** — ref **ZB739157**, registered 20 Aug 2024

  ⚠ **Both expire 19 August 2026 — about five weeks away.** Renew before or
  at go-live; an expired registration while running live driver tracking is
  exactly the wrong look. (Direct debit renewal also drops the fee slightly.)

  ⚠ **Entity name:** every draft in this pack says "Xtra Mile Couriers Ltd",
  but the ICO register shows the legal entities above. Directors to confirm
  which entity (or both) operates the Driver App as controller, then the
  drafts' headers and controller statements must be corrected to match —
  the DPIA and privacy notices must name the actual legal entity.
- **Retention deletion job** — the schedule commits to deleting raw GPS at
  90 days, messages/push-log at 12 months, shifts at 2 years. A small
  daily cron must ship with the deploy (see schedule, implementation note).
- **Blanks to fill** before issuing anything: privacy contact email,
  phone, registered address (marked `[...]` in the notice and policy).
