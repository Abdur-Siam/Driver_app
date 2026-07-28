# 07 — Screen & UX Specification

**TOM Driver App** · v0.1 · 2026-06-26

---

## 1. UX principles (designed for the cab, not the desk)

1. **One-handed, glanceable, big.** Primary action is always a single large bottom button. Minimum
   48dp tap targets; critical text ≥ 18sp.
2. **Driving-safe.** No reading required to know "what next" — colour + icon + one word. Navigation is
   handed to a dedicated nav app; the driver app never asks them to read prose while moving.
3. **Forgiving offline.** Every screen renders from cache; a subtle banner shows sync state. Nothing
   blocks on the network.
4. **Sunlight & gloves.** High-contrast default, optional dark, no thin hairlines, generous spacing.
5. **Trust through clarity.** Status, ETA, and what TOM expects next are always visible; surprises are
   the enemy.

## 2. Navigation map

```
Splash / biometric
  └─ Login (first run / re-auth)
        └─ MAIN (bottom tab bar)
             ├─ TODAY (default)        ── the run
             │     ├─ Job detail
             │     │     ├─ Scan (collect)         (full-screen camera)
             │     │     ├─ Scan (deliver)         (full-screen camera)
             │     │     ├─ POD capture            (signature + photo + name)
             │     │     └─ Failure capture        (reason + photo)
             │     └─ Route overview (map, optimised order)
             ├─ MESSAGES               ── two-way ops chat
             ├─ EARNINGS               ── per-job/day, statements (P1)
             └─ ME                     ── availability, documents, vehicle check, profile, SOS
```

## 3. Screen-by-screen

### 3.1 Login
Fields: driver id, password. States: normal · invalid · **locked** (from auth store lockout) ·
first-login (force set password) · offline (explain login needs signal once). Biometric opt-in after
first success. Footer: app version, "Contact ops" link.

### 3.2 Today (the run) — **home**
- Header: date · on/off-duty toggle · sync chip · battery/tracking indicator.
- **Route summary card:** total stops, distance, duration, "View route" → map.
- **Ordered job list** (by `sequence_position`): each row = stop #, account, postcode, deadline pill
  (green/amber/red vs now), status chip, parcel count. Current/next stop highlighted.
- Primary button adapts to context: **Start run** → **Navigate to pickup** → **Scan parcels** → …
- Empty states: "No jobs yet — you're on duty, ops will send your run" / "You're off duty".

### 3.3 Job detail
- Pickup + ordered drops, each with address, contact, instructions, parcels.
- Map mini-view of this job's stops.
- **Status lifecycle bar** showing where the job is (`ACCEPTED → ON ROUTE TO PU → ARRIVED → POB →
  ON ROUTE - POB → POD → COMPLETED`).
- Context actions: **Navigate** (hand-off), **Call contact**, **Report issue** (`driver_issues`),
  **Can't complete** (failure flow), accept/decline (offer mode).
- Bottom primary button = the single next legal transition.

### 3.4 Scan — collect (POB)
Full-screen camera, reticle, torch, **running checklist** (`3/4 scanned`), haptic/sound per accept,
manual-entry fallback. Warning sheets for unexpected/duplicate. "All scanned → Confirm on board"
enables **POB**. (Doc 05.)

### 3.5 Scan — deliver
Same camera; **single-parcel match for this drop**; wrong-drop → blocking warning sheet
("This parcel is for Drop 3"). Match → continue to POD.

### 3.6 POD capture
Recipient name (text) · **on-glass signature** · **photo(s)** · optional note. Big "Confirm
delivery" → **POD** for the drop; advances to next drop or **COMPLETED** on last. Fully offline-capable
(queued).

### 3.7 Failure capture
Reason chips (`no-access · refused · damaged · wrong-address · other`) · photo · note · who to notify.
Posts in real time to ops; routes per ops decision; feeds cost fields.

### 3.8 Route overview
Google Map with all stops numbered in optimised order, the live driver pin (Doc 04), per-leg ETAs,
"Re-optimise" (requests `POST /route/optimise`), and a list mirror of the order.

### 3.9 Messages
Threaded ops chat (reuses `driver_messages`), unread badge, push deep-link, attachment (photo).
Offline compose → outbox.

### 3.10 Earnings (P1)
Per-job and per-day totals from durable `driver_pay_final`; subcontractor self-billing statement view
(aligns with TIA driverpay); decimal-exact display, never recomputed on device.

### 3.11 Me
- **Availability / shift** toggle + working area (feeds dispatch eligibility).
- **Document wallet:** insurance · goods-in-transit · MOT · licence — status (valid/expiring/expired),
  upload, expiry dates (mirrors `driver_documents`). Expired mandatory doc → ops-blocked offers.
- **Vehicle / daily walk-around check** (P1; medical: red box, spill kit).
- **Profile** (read-mostly; callsign, vehicle), **consent & privacy** (tracking consent, data rights),
  **SOS / help** (one-tap call ops + incident report), **send logs to ops**, sign out.

## 4. Global components
- **Sync banner** (synced / syncing / offline — N queued).
- **Tracking indicator** (active / degraded / off) — always honest.
- **Bottom primary action button** (the single most important pattern — context-driven).
- **Status chip** + **deadline pill** (shared colour language across screens).
- **Warning sheet** (non-punitive anomaly dialogs for scan/route).

## 5. Design tokens (starting point — align with Xtra Mile brand)
- **Palette:** map TIA's "Courier Blue" `#2A6BCC` accent for brand continuity; status colours
  green `#1E9E5A` (on-time/ok), amber `#E0A200` (warning), red `#D23B3B` (late/fail), neutral slate
  for chrome. Dark theme mandatory for night driving.
- **Type:** a legible sans (e.g. Inter/Sora) — Sora already used in TIA; large dynamic-type support.
- **Spacing:** 8pt grid; generous touch padding.
- **Motion:** minimal, purposeful (status transitions, scan accept); respect reduce-motion.

## 6. Accessibility
WCAG-minded: contrast ≥ AA, dynamic type, screen-reader labels on every action, haptic + audio
confirmation for scans (eyes-up), no colour-only meaning (icon + text always), one-handed reach.

## 7. Localisation
i18n scaffold from day one (string catalogue, no hardcoded copy); English first; date/number/£
formatting via locale; right-to-left ready.
