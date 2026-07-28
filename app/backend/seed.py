"""Demo seed for local running / commercial testing.

Two drivers with full profiles, today's London multidrop jobs (with pay
components), plus pay history: two PAID weekly statements and one current
PROCESSING period, expenses, and performance. Deterministic dates anchored
to 2026-06-26 so earnings/statements line up. Idempotent (drivers empty).

Replace at TOM-merge time with feeds from the spine board + TIA pay/statements.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from .auth import hash_password
from .db import get_connection

TODAY = date(2026, 6, 26)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mins_ago(m):
    return (datetime.now(timezone.utc) - timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")


# Demo ops/dispatch accounts (dev only — never seeded in production; provision
# real console users with tools/provision_ops.py). Mirrors the driver demo.
OPS_USERS = [
    ("ops", "Dispatch Desk", "ops1234", "admin"),
]

# A short recent GPS trail per demo driver so the tracking map is populated on
# first run (ends near their live job). Direct inserts — the consent gate lives
# on the API, not the table; this is demo data only.
DEMO_TRAILS = {
    "DRV001": [(51.5305, -0.1055), (51.5322, -0.1048), (51.5340, -0.1038), (51.5350, -0.1030)],
    "CX014":  [(51.5205, -0.0625), (51.5198, -0.0610), (51.5192, -0.0598), (51.5190, -0.0590)],
}


def _d(offset_days):
    return (TODAY + timedelta(days=offset_days)).isoformat()


DRIVERS = [
    ("DRV001", "Eduardo Henrique", "DRV001", "test1234", "Small Van", 0, "N7 8AA", "07700 900001"),
    ("CX014", "Priya Shah", "CX014", "test1234", "Bike", 1, "E2 7DG", "07700 900014"),
]

# Rich profile fields per driver (applied via UPDATE after base insert).
PROFILES = {
    "DRV001": dict(
        email="eduardo.h@example.com", address="22 Hornsey Road, London", dob="1991-03-14",
        emergency_name="Maria Henrique", emergency_phone="07700 900221",
        vehicle_reg="LV68 KXР".replace("Р", "P"), vehicle_make="Ford", vehicle_model="Transit Connect",
        utr_or_company_ref="UTR 4821 99123", vat_registered=0, vat_number=None, pay_cycle="weekly",
        bank_sort_code="20-45-77", bank_account_number="****6612", bank_account_name="E Henrique",
        bank_status="verified", rating=4.9, acceptance_pct=96, completion_pct=99, on_time_pct=94,
    ),
    "CX014": dict(
        email="priya.shah@example.com", address="5 Pritchards Road, London", dob="1989-11-02",
        emergency_name="Anil Shah", emergency_phone="07700 900514",
        vehicle_reg=None, vehicle_make="Cube", vehicle_model="Hybrid e-bike",
        utr_or_company_ref="Shah Couriers Ltd 09912233", vat_registered=1, vat_number="GB 412 5567 01",
        pay_cycle="weekly", bank_sort_code="04-00-04", bank_account_number="****8890",
        bank_account_name="Shah Couriers Ltd", bank_status="pending",
        rating=4.7, acceptance_pct=88, completion_pct=97, on_time_pct=90,
    ),
}

# Today's jobs (with pay components). pay = base+waiting+toll+extras+bonus-deduction.
JOBS = [
    {
        "docket": "XM-20260626-0042", "driver": "DRV001", "account": "ACME01", "vehicle": "Small Van",
        "pickup": ("Unit 4, Clerkenwell Workshops", "EC1R 0AT", 51.5237, -0.1075, "Sam (reception)", "Use the rear loading door"),
        "deadline": "14:30", "special": "Fragile — electronics", "requires_scan": 1,
        "pay": dict(base="22.50", waiting="0.00", toll="0.00", extras="4.80", bonus="0.00", deduction="0.00", miles=6.2),
        "drops": [
            ("12 Duncan Terrace", "N1 8BZ", 51.5350, -0.1030, "Mr Adeyemi", "Leave with concierge"),
            ("48 Rosebery Avenue", "EC1R 4RP", 51.5260, -0.1090, "Front desk", "Signature required"),
        ],
        "parcels": [(1, "XM00420101", "1 x A4 box"), (2, "XM00420201", "1 x padded envelope"), (2, "XM00420202", "1 x tube")],
    },
    {
        "docket": "XM-20260626-0051", "driver": "DRV001", "account": "BRIGHT-LTD", "vehicle": "Small Van",
        "pickup": ("St John Street Depot", "EC1V 4PY", 51.5290, -0.1025, "Loading bay 2", ""),
        "deadline": "16:00", "special": "", "requires_scan": 0,
        "pay": dict(base="16.30", waiting="3.50", toll="0.00", extras="0.00", bonus="0.00", deduction="0.00", miles=3.1),
        "drops": [("200 Pentonville Road", "N1 9JP", 51.5320, -0.1190, "Mailroom", "")],
        "parcels": [(1, "XM00510101", "2 x document wallets")],
    },
    {
        "docket": "XM-20260626-0067", "driver": "CX014", "account": "MEDLAB", "vehicle": "Bike",
        "pickup": ("The Pathology Lab, Whitechapel", "E1 1BB", 51.5190, -0.0600, "Sample reception", "Red box only — medical"),
        "deadline": "13:15", "special": "Medical — temperature controlled", "requires_scan": 1,
        "pay": dict(base="12.00", waiting="0.00", toll="0.00", extras="2.50", bonus="0.00", deduction="0.00", miles=2.4),
        "drops": [("Royal London Hospital", "E1 1FR", 51.5190, -0.0590, "Ward 12 desk", "Hand to nurse")],
        "parcels": [(1, "XM00670101", "1 x sample pouch")],
    },
]


def _job_total(pay):
    return "%.2f" % (sum(float(pay[k]) for k in ("base", "waiting", "toll", "extras", "bonus")) - float(pay["deduction"]))


def _ins_statement(conn, driver_id, label, start_off, end_off, freq, status, ref, lines, deductions="0.00",
                   issued_off=None, paid_off=None, vat="0.00"):
    sid = "ST-" + uuid.uuid4().hex[:10].upper()
    gross = sum(float(a) for (_dt, _t, _dk, _desc, a) in lines if float(a) > 0)
    ded = sum(-float(a) for (_dt, _t, _dk, _desc, a) in lines if float(a) < 0) + float(deductions)
    net = gross - ded
    conn.execute(
        "INSERT INTO statements (statement_id, driver_id, period_label, period_start, period_end, frequency, "
        "gross, deductions, net, vat, status, reference, issued_at, paid_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, driver_id, label, _d(start_off), _d(end_off), freq, "%.2f" % gross, "%.2f" % ded, "%.2f" % net,
         vat, status, ref, _d(issued_off) if issued_off is not None else None,
         _d(paid_off) if paid_off is not None else None),
    )
    for (line_date, ltype, docket, desc, amount) in lines:
        conn.execute(
            "INSERT INTO statement_lines (statement_id, line_date, type, docket_number, description, amount) "
            "VALUES (?,?,?,?,?,?)",
            (sid, line_date, ltype, docket, desc, amount),
        )
    return sid


def seed_if_empty() -> bool:
    conn = get_connection()
    if conn.execute("SELECT COUNT(*) AS n FROM drivers").fetchone()["n"] > 0:
        return False

    # ── drivers + profiles ──
    for d in DRIVERS:
        conn.execute(
            "INSERT INTO drivers (driver_id, name, callsign, password_hash, vehicle, is_subcontracted, "
            "active, home_postcode, phone) VALUES (?,?,?,?,?,?,1,?,?)",
            (d[0], d[1], d[2], hash_password(d[3]), d[4], d[5], d[6], d[7]),
        )
        p = PROFILES[d[0]]
        conn.execute(
            "UPDATE drivers SET email=?, address=?, dob=?, emergency_name=?, emergency_phone=?, vehicle_reg=?, "
            "vehicle_make=?, vehicle_model=?, utr_or_company_ref=?, vat_registered=?, vat_number=?, pay_cycle=?, "
            "bank_sort_code=?, bank_account_number=?, bank_account_name=?, bank_status=?, rating=?, acceptance_pct=?, "
            "completion_pct=?, on_time_pct=? WHERE driver_id=?",
            (p["email"], p["address"], p["dob"], p["emergency_name"], p["emergency_phone"], p["vehicle_reg"],
             p["vehicle_make"], p["vehicle_model"], p["utr_or_company_ref"], p["vat_registered"], p["vat_number"],
             p["pay_cycle"], p["bank_sort_code"], p["bank_account_number"], p["bank_account_name"], p["bank_status"],
             p["rating"], p["acceptance_pct"], p["completion_pct"], p["on_time_pct"], d[0]),
        )

    # ── today's jobs ──
    for j in JOBS:
        pu = j["pickup"]; pay = j["pay"]; total = _job_total(pay)
        conn.execute(
            "INSERT INTO jobs (docket_number, driver_id, status, lifecycle_status, account, vehicle, pickup_address, "
            "pickup_postcode, pickup_lat, pickup_lng, pickup_contact, pickup_notes, deadline, operational_date, "
            "special_instructions, requires_scan, sequence_position, route_version, driver_pay_final, base_pay, "
            "waiting_pay, toll_pay, extras_pay, bonus_pay, deduction, miles, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (j["docket"], j["driver"], "IN PROGRESS", "assigned", j["account"], j["vehicle"], pu[0], pu[1], pu[2],
             pu[3], pu[4], pu[5], j["deadline"], TODAY.isoformat(), j["special"], j.get("requires_scan", 1), 1, 1,
             total, pay["base"], pay["waiting"], pay["toll"], pay["extras"], pay["bonus"], pay["deduction"],
             pay["miles"], _now(), _now()),
        )
        for i, dr in enumerate(j["drops"], start=1):
            conn.execute(
                "INSERT INTO drops (docket_number, seq, address, postcode, lat, lng, contact, instructions, status) "
                "VALUES (?,?,?,?,?,?,?,?,'pending')",
                (j["docket"], i, dr[0], dr[1], dr[2], dr[3], dr[4], dr[5]),
            )
        for pc in j["parcels"]:
            conn.execute(
                "INSERT INTO parcels (docket_number, drop_seq, barcode, description, state) VALUES (?,?,?,?,'expected')",
                (j["docket"], pc[0], pc[1], pc[2]),
            )

    # ── DRV001 pay history: two PAID weekly statements + one PROCESSING ──
    # Week -2 (paid)
    _ins_statement(
        conn, "DRV001", "Week 24 · 8–14 Jun", -18, -12, "weekly", "paid", "XM-PAY-2026-W24",
        lines=[
            (_d(-18), "job", "XM-20260608-0021", "Multidrop · EC1 → N1 (3 drops)", "31.40"),
            (_d(-17), "job", "XM-20260609-0044", "Same-day · SE1 → E14", "24.10"),
            (_d(-16), "job", "XM-20260610-0088", "Multidrop · W1 → NW1 (4 drops)", "42.75"),
            (_d(-14), "job", "XM-20260612-0102", "Direct · EC2 → SW1", "18.90"),
            (_d(-14), "bonus", None, "On-time bonus (week)", "15.00"),
            (_d(-13), "expense", None, "Parking reimbursement", "6.50"),
            (_d(-13), "deduction", None, "Kit levy", "-2.50"),
        ], paid_off=-10),
    # Week -1 (paid)
    _ins_statement(
        conn, "DRV001", "Week 25 · 15–21 Jun", -11, -5, "weekly", "paid", "XM-PAY-2026-W25",
        lines=[
            (_d(-11), "job", "XM-20260615-0019", "Multidrop · N7 → EC1 (2 drops)", "27.30"),
            (_d(-10), "job", "XM-20260616-0051", "Same-day · E1 → SE1", "22.80"),
            (_d(-9), "job", "XM-20260617-0066", "Direct · EC1 → N1", "16.40"),
            (_d(-8), "job", "XM-20260618-0073", "Multidrop · W1 → E14 (5 drops)", "51.20"),
            (_d(-7), "job", "XM-20260619-0090", "Same-day · SW1 → NW3", "29.60"),
            (_d(-7), "bonus", None, "Peak incentive", "12.00"),
            (_d(-5), "deduction", None, "Kit levy", "-2.50"),
        ], paid_off=-3),
    # Current week (processing — not yet paid)
    _ins_statement(
        conn, "DRV001", "Week 26 · 22–28 Jun", -4, 2, "weekly", "processing", "XM-PAY-2026-W26",
        lines=[
            (_d(-4), "job", "XM-20260622-0012", "Multidrop · EC1 → N1 (3 drops)", "33.10"),
            (_d(-3), "job", "XM-20260623-0034", "Same-day · E1 → SE1", "21.50"),
            (_d(-2), "job", "XM-20260624-0059", "Direct · EC2 → W1", "17.80"),
            (_d(-1), "job", "XM-20260625-0071", "Multidrop · N7 → EC1 (2 drops)", "26.40"),
        ], issued_off=2),

    # ── expenses ──
    for (etype, amount, off, note, status) in [
        ("fuel", "38.40", -3, "Diesel — Shell EC1", "reimbursed"),
        ("parking", "4.50", -1, "Loading bay · N1", "approved"),
        ("toll", "15.00", 0, "Congestion charge", "submitted"),
    ]:
        conn.execute(
            "INSERT INTO expenses (driver_id, type, amount, exp_date, note, status, created_at) VALUES (?,?,?,?,?,?,?)",
            ("DRV001", etype, amount, _d(off), note, status, _now()),
        )

    conn.execute(
        "INSERT INTO messages (driver_id, ts, direction, text, read) VALUES "
        "('DRV001', ?, 'ops', 'Morning Eduardo — your run is ready. 2 jobs today. Week 25 statement is paid.', 0)",
        (_now(),),
    )

    # ── ops/dispatch console users (dev demo) ──
    for (username, name, pw, role) in OPS_USERS:
        conn.execute(
            "INSERT INTO ops_users (username, name, password_hash, role, active, created_at) "
            "VALUES (?,?,?,?,1,?)",
            (username, name, hash_password(pw), role, _now()),
        )

    # ── recent GPS trails so the tracking map has live markers on boot ──
    for did, pts in DEMO_TRAILS.items():
        for i, (lat, lng) in enumerate(pts):
            age = (len(pts) - 1 - i) * 3   # last point ~now, spaced 3 min apart
            conn.execute(
                "INSERT INTO locations (driver_id, ping_id, recorded_at, received_at, lat, lng, "
                "speed, heading, accuracy, battery) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (did, f"seed-{did}-{i}", _mins_ago(age), _mins_ago(age), lat, lng,
                 18.0, 90.0, 8.0, 82),
            )

    conn.commit()
    return True
