# App-Store Privacy Declarations — Prep Pack

**Xtra Mile Couriers Ltd · TOM Driver App · 13 July 2026 DRAFT**
**Use this when filling in App Store Connect and the Play Console — the
answers below match what the app actually does (v1.1.0), so the forms can
be completed in one sitting. Blocked only on Jack's store accounts.**

---

## 1. Apple — App Privacy ("nutrition labels")

Declare **Data Linked to You** (all of it is linked — drivers sign in):

| Apple category | Our data | Purpose to select |
|---|---|---|
| Location → Precise Location | On-shift GPS | App Functionality |
| Contact Info → Name, Phone | Driver profile | App Functionality |
| User Content → Photos | POD photos, signatures, receipts | App Functionality |
| Financial Info → Payment Info | Subcontractor bank details, pay data | App Functionality |
| Identifiers → User ID, Device ID | Driver ID, device binding | App Functionality |
| Usage Data → Product Interaction | Audit log of app actions | App Functionality |

Declare **none** for: tracking (ATT does not apply — no cross-app tracking,
no advertising), Data Used to Track You = No, third-party advertising = No.

Background location justification (App Review notes): *"Couriers on an
active shift are tracked so dispatch can allocate same-day and medical
deliveries and give customers arrival times. Tracking runs only between
the driver's explicit shift start/end in the app; the server refuses
location submissions outside a shift. See privacy policy [URL]."*

`Info.plist` strings are already injected (native project): location
always/when-in-use, camera, Face ID. Ensure the wording matches the above.

## 2. Google Play — Data Safety form

| Question | Answer |
|---|---|
| Collects data? | Yes |
| Location (precise) | Collected, not shared*, required, App functionality |
| Photos | Collected, not shared, required (POD), App functionality |
| Financial info (user payment info) | Collected, not shared, required for subbies, App functionality |
| Personal info (name, phone) | Collected, not shared, required, App functionality |
| App activity | Collected, not shared, required, App functionality (audit) |
| Device IDs | Collected, not shared, required, Security |
| Data encrypted in transit? | Yes (TLS only) |
| Deletion mechanism? | Yes — in-app erasure request + contact route |

\* "Shared" in Play's sense means to third parties for their purposes.
Firebase (push) and Google Maps act as our service providers — that is
"collected", not "shared", under Play's definitions.

## 3. Google Play — Background Location declaration + video

Play requires: (a) prominent in-app disclosure **before** the runtime
permission, (b) a short screen-recording demonstrating it, (c) written
justification.

**Prominent disclosure (already the consent sheet — confirm wording):**
*"Xtra Mile Couriers collects location data to enable live job tracking,
dispatch and customer arrival estimates while you are on shift, even when
the app is closed or not in use. Location is never collected off shift."*

**Video script (30–45 s, record on a real device):**
1. Open app → log in as a demo driver.
2. Tap Start shift → the prominent-disclosure/consent sheet appears →
   read it on screen for ~3 s → Accept → OS location permission prompt
   ("Allow all the time") appears → grant.
3. Show the run screen with live tracking active; background the app;
   show the notification/tracking continuing.
4. Tap End shift → show tracking stopped.

**Written justification:** same text as the Apple note in §1.

## 4. Account deletion (both stores now require it)

Path to declare: in-app **Me → Settings → Privacy & data → Request
erasure**, plus the contact email on the privacy policy. Deletion honours
the Data Retention Schedule (legally-required records are retained and
the policy says so).

## 5. Store-listing checklist

- [ ] Privacy policy live at a public URL (use `Public_Privacy_Policy_DriverApp.md`)
- [ ] Apple nutrition labels per §1
- [ ] Apple background-location review note per §1
- [ ] Play Data Safety per §2
- [ ] Play background-location disclosure text verified in-app + video per §3
- [ ] Account-deletion path declared per §4
- [ ] Support email + phone on both listings
