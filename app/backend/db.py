"""SQLite engine + schema for the standalone Driver App.

WAL mode, per-thread connection cache, dict rows — the same shape as
TOM's database.engine, so the merge maps cleanly. Money is stored as
TEXT decimal strings (TOM convention). Every table here corresponds to a
table in the Driver design package (Docs 03/04/05/08).
"""
from __future__ import annotations

import os
import sqlite3
import threading
from typing import Optional

from . import config

_local = threading.local()


SCHEMA = [
    # ── Drivers (in TOM: the `drivers` table + driver_auth) ──────────
    """
    CREATE TABLE IF NOT EXISTS drivers (
        driver_id       TEXT PRIMARY KEY,
        name            TEXT NOT NULL,
        callsign        TEXT NOT NULL,
        password_hash   TEXT,
        vehicle         TEXT,
        is_subcontracted INTEGER NOT NULL DEFAULT 0,
        active          INTEGER NOT NULL DEFAULT 1,
        home_postcode   TEXT,
        phone           TEXT,
        -- personal
        email           TEXT,
        address         TEXT,
        dob             TEXT,
        emergency_name  TEXT,
        emergency_phone TEXT,
        -- vehicle detail
        vehicle_reg     TEXT,
        vehicle_make    TEXT,
        vehicle_model   TEXT,
        -- tax / commercial
        utr_or_company_ref TEXT,
        vat_registered  INTEGER DEFAULT 0,
        vat_number      TEXT,
        pay_cycle       TEXT DEFAULT 'weekly',
        -- bank (sensitive; changes go via profile_change_requests review)
        bank_sort_code     TEXT,
        bank_account_number TEXT,
        bank_account_name  TEXT,
        bank_status        TEXT DEFAULT 'unverified',
        -- performance (seeded / computed)
        rating          REAL DEFAULT 5.0,
        acceptance_pct  INTEGER DEFAULT 100,
        completion_pct  INTEGER DEFAULT 100,
        on_time_pct     INTEGER DEFAULT 100,
        -- preferences
        notify_jobs     INTEGER DEFAULT 1,
        notify_pay      INTEGER DEFAULT 1,
        notify_msgs     INTEGER DEFAULT 1,
        nav_app         TEXT DEFAULT 'google',
        -- shift / availability (from the legacy ECHO app model)
        duty_status     TEXT DEFAULT 'off',        -- off | available | going_home
        theme           TEXT DEFAULT 'night',      -- day | night | auto
        sound_alert_conn INTEGER DEFAULT 0,
        biometric       INTEGER DEFAULT 0
    )
    """,
    # ── Jobs (in TOM: the spine board / jobs table) ──────────────────
    """
    CREATE TABLE IF NOT EXISTS jobs (
        docket_number     TEXT PRIMARY KEY,
        driver_id         TEXT,
        status            TEXT NOT NULL DEFAULT 'IN PROGRESS',
        lifecycle_status  TEXT,
        account           TEXT,
        vehicle           TEXT,
        pickup_address    TEXT,
        pickup_postcode   TEXT,
        pickup_lat        REAL,
        pickup_lng        REAL,
        pickup_contact    TEXT,
        pickup_notes      TEXT,
        deadline          TEXT,
        operational_date  TEXT,
        special_instructions TEXT,
        -- Barcode scanning is per-job, not universal: 1 = scans enforced at
        -- collect + deliver; 0 = straight to POB/POD (scanning still allowed).
        requires_scan     INTEGER NOT NULL DEFAULT 1,
        sequence_position INTEGER,
        route_version     INTEGER NOT NULL DEFAULT 1,
        driver_pay_final  TEXT,
        -- pay components (decimal strings) for the earnings breakdown
        base_pay          TEXT,
        waiting_pay       TEXT,
        toll_pay          TEXT,
        extras_pay        TEXT,
        bonus_pay         TEXT,
        deduction         TEXT,
        miles             REAL,
        completed_at      TEXT,
        statement_id      TEXT,
        created_at        TEXT,
        updated_at        TEXT
    )
    """,
    # ── Drops (per-job delivery stops; multidrop) ────────────────────
    """
    CREATE TABLE IF NOT EXISTS drops (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        docket_number TEXT NOT NULL,
        seq           INTEGER NOT NULL,
        address       TEXT,
        postcode      TEXT,
        lat           REAL,
        lng           REAL,
        contact       TEXT,
        instructions  TEXT,
        status        TEXT NOT NULL DEFAULT 'pending',
        pod_recipient TEXT,
        pod_signature TEXT,
        pod_photo     TEXT,
        pod_note      TEXT,
        pod_at        TEXT,
        fail_reason   TEXT,
        fail_note     TEXT
    )
    """,
    # ── Parcels (expected set per drop; barcode verification) ────────
    """
    CREATE TABLE IF NOT EXISTS parcels (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        docket_number TEXT NOT NULL,
        drop_seq      INTEGER NOT NULL,
        barcode       TEXT NOT NULL,
        description   TEXT,
        state         TEXT NOT NULL DEFAULT 'expected'
    )
    """,
    # ── Tokens (bearer; SHA-256 hash at rest; one device per driver) ─
    """
    CREATE TABLE IF NOT EXISTS tokens (
        token_hash   TEXT PRIMARY KEY,
        driver_id    TEXT NOT NULL,
        device_id    TEXT,
        device_label TEXT,
        created_at   TEXT NOT NULL,
        expires_at   TEXT NOT NULL,
        last_used_at TEXT,
        revoked      INTEGER NOT NULL DEFAULT 0
    )
    """,
    # ── Idempotency ledger (offline-outbox replay) ───────────────────
    """
    CREATE TABLE IF NOT EXISTS idempotency (
        idempotency_key TEXT PRIMARY KEY,
        driver_id       TEXT,
        route           TEXT,
        status_code     INTEGER,
        result_json     TEXT,
        created_at      TEXT NOT NULL
    )
    """,
    # ── Location pings ───────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS locations (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        driver_id     TEXT NOT NULL,
        ping_id       TEXT,
        docket_number TEXT,
        recorded_at   TEXT,
        received_at   TEXT,
        lat           REAL,
        lng           REAL,
        speed         REAL,
        heading       REAL,
        accuracy      REAL,
        battery       INTEGER
    )
    """,
    # ── Parcel scan events (audit trail) ─────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS parcel_events (
        event_id      TEXT PRIMARY KEY,
        docket_number TEXT NOT NULL,
        drop_seq      INTEGER,
        phase         TEXT,
        barcode       TEXT,
        entry         TEXT,
        match_result  TEXT,
        driver_id     TEXT,
        recorded_at   TEXT,
        lat           REAL,
        lng           REAL
    )
    """,
    # ── Audit log (every driver mutation; mirrors driver_actions) ────
    """
    CREATE TABLE IF NOT EXISTS audit (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        ts            TEXT NOT NULL,
        driver_id     TEXT,
        action        TEXT NOT NULL,
        docket_number TEXT,
        request_id    TEXT,
        detail_json   TEXT
    )
    """,
    # ── Ops <-> driver messages ──────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS messages (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        driver_id  TEXT NOT NULL,
        ts         TEXT NOT NULL,
        direction  TEXT NOT NULL,
        text       TEXT NOT NULL,
        category   TEXT,
        read       INTEGER NOT NULL DEFAULT 0
    )
    """,
    # ── Shifts (driver enters end time so dispatch can pre-allocate) ──
    """
    CREATE TABLE IF NOT EXISTS shifts (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        driver_id    TEXT NOT NULL,
        start_at     TEXT NOT NULL,
        planned_end  TEXT,
        status       TEXT NOT NULL DEFAULT 'active',   -- active | ended
        ended_at     TEXT
    )
    """,
    # ── Pay statements (issued after the company processes payment) ──
    """
    CREATE TABLE IF NOT EXISTS statements (
        statement_id TEXT PRIMARY KEY,
        driver_id    TEXT NOT NULL,
        period_label TEXT,
        period_start TEXT,
        period_end   TEXT,
        frequency    TEXT,
        gross        TEXT,
        deductions   TEXT,
        net          TEXT,
        vat          TEXT,
        status       TEXT NOT NULL DEFAULT 'processing',
        reference    TEXT,
        issued_at    TEXT,
        paid_at      TEXT
    )
    """,
    # ── Statement line items (jobs / expenses / bonuses / adjustments) ─
    """
    CREATE TABLE IF NOT EXISTS statement_lines (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        statement_id  TEXT NOT NULL,
        line_date     TEXT,
        type          TEXT NOT NULL DEFAULT 'job',
        docket_number TEXT,
        description   TEXT,
        amount        TEXT
    )
    """,
    # ── Expenses (driver-logged; feed statements + tax) ──────────────
    """
    CREATE TABLE IF NOT EXISTS expenses (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        driver_id   TEXT NOT NULL,
        type        TEXT NOT NULL,
        amount      TEXT NOT NULL,
        exp_date    TEXT,
        note        TEXT,
        receipt     TEXT,
        status      TEXT NOT NULL DEFAULT 'submitted',
        created_at  TEXT
    )
    """,
    # ── Profile change requests (sensitive edits → ops review) ───────
    """
    CREATE TABLE IF NOT EXISTS profile_change_requests (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        driver_id     TEXT NOT NULL,
        field         TEXT NOT NULL,
        old_value     TEXT,
        new_value     TEXT,
        status        TEXT NOT NULL DEFAULT 'pending',
        requested_at  TEXT,
        reviewed_at   TEXT
    )
    """,
    # ── Early-payout (instant pay) requests ──────────────────────────
    """
    CREATE TABLE IF NOT EXISTS payout_requests (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        driver_id    TEXT NOT NULL,
        amount       TEXT NOT NULL,
        fee          TEXT,
        status       TEXT NOT NULL DEFAULT 'requested',
        requested_at TEXT
    )
    """,
    # ── Push notification device tokens (FCM/APNs; one row per device) ─
    """
    CREATE TABLE IF NOT EXISTS device_tokens (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        driver_id  TEXT NOT NULL,
        token      TEXT NOT NULL UNIQUE,
        platform   TEXT,
        created_at TEXT,
        last_seen  TEXT
    )
    """,
    # ── Push outbox (durable per-token delivery log; pending until FCM
    #    credentials are configured, then flushed — see push.py) ────────
    """
    CREATE TABLE IF NOT EXISTS push_outbox (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        driver_id  TEXT NOT NULL,
        token      TEXT NOT NULL,
        platform   TEXT,
        kind       TEXT,
        title      TEXT,
        body       TEXT,
        data_json  TEXT,
        status     TEXT NOT NULL DEFAULT 'pending',   -- pending | sent | failed
        detail     TEXT,
        created_at TEXT,
        sent_at    TEXT
    )
    """,
    # ── Abuse-control events (login limiter + account lockout) ────────
    #    DB-backed so every gunicorn worker sees the same counts and a
    #    restart doesn't reset lockout state (in-process dicts do both).
    """
    CREATE TABLE IF NOT EXISTS rate_events (
        id     INTEGER PRIMARY KEY AUTOINCREMENT,
        bucket TEXT NOT NULL,
        ts     REAL NOT NULL
    )
    """,
    # ── GDPR data-subject requests (access / erasure) → ops actions ───
    """
    CREATE TABLE IF NOT EXISTS data_requests (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        driver_id    TEXT NOT NULL,
        kind         TEXT NOT NULL,            -- access | erasure
        status       TEXT NOT NULL DEFAULT 'received',
        requested_at TEXT
    )
    """,
    # ── Ops / dispatch users (the console side; separate from drivers) ─
    #    In TOM these map to operator accounts. Passwords hashed at rest
    #    (werkzeug scrypt), roles gate who can create/assign/cancel work.
    """
    CREATE TABLE IF NOT EXISTS ops_users (
        username      TEXT PRIMARY KEY,
        name          TEXT,
        password_hash TEXT,
        role          TEXT NOT NULL DEFAULT 'dispatcher',   -- admin | dispatcher
        active        INTEGER NOT NULL DEFAULT 1,
        created_at    TEXT,
        last_login_at TEXT
    )
    """,
    # ── Ops bearer tokens (SHA-256 hash at rest; kept apart from driver
    #    tokens so a leaked driver token can never reach the console) ────
    """
    CREATE TABLE IF NOT EXISTS ops_tokens (
        token_hash   TEXT PRIMARY KEY,
        username     TEXT NOT NULL,
        label        TEXT,
        created_at   TEXT NOT NULL,
        expires_at   TEXT NOT NULL,
        last_used_at TEXT,
        revoked      INTEGER NOT NULL DEFAULT 0
    )
    """,
    # ── Ops audit (every console mutation — mirrors the driver audit
    #    invariant: no dispatch action without an audit row) ─────────────
    """
    CREATE TABLE IF NOT EXISTS ops_audit (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        ts         TEXT NOT NULL,
        actor      TEXT NOT NULL,
        action     TEXT NOT NULL,
        target     TEXT,
        detail_json TEXT
    )
    """,
]

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_jobs_driver ON jobs(driver_id)",
    "CREATE INDEX IF NOT EXISTS idx_drops_docket ON drops(docket_number)",
    "CREATE INDEX IF NOT EXISTS idx_parcels_docket ON parcels(docket_number)",
    "CREATE INDEX IF NOT EXISTS idx_tokens_driver ON tokens(driver_id)",
    "CREATE INDEX IF NOT EXISTS idx_locations_driver ON locations(driver_id, recorded_at)",
    # Offline-replay dedup: a client ping id is unique per driver. Runs AFTER
    # the ping_id migration (init_db order: tables → migrations → indexes).
    # NULL ping_ids (legacy / no-id pings) are distinct in SQLite, so they are
    # never deduped — dedup applies only when the client sends an id.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_locations_ping ON locations(driver_id, ping_id)",
    "CREATE INDEX IF NOT EXISTS idx_statements_driver ON statements(driver_id, period_end)",
    "CREATE INDEX IF NOT EXISTS idx_statement_lines_sid ON statement_lines(statement_id)",
    "CREATE INDEX IF NOT EXISTS idx_expenses_driver ON expenses(driver_id, exp_date)",
    "CREATE INDEX IF NOT EXISTS idx_device_tokens_driver ON device_tokens(driver_id)",
    "CREATE INDEX IF NOT EXISTS idx_data_requests_driver ON data_requests(driver_id)",
    "CREATE INDEX IF NOT EXISTS idx_push_outbox_status ON push_outbox(status, id)",
    "CREATE INDEX IF NOT EXISTS idx_rate_events_bucket ON rate_events(bucket, ts)",
    "CREATE INDEX IF NOT EXISTS idx_ops_tokens_user ON ops_tokens(username)",
    "CREATE INDEX IF NOT EXISTS idx_messages_docket ON messages(docket_number)",
]

# Additive migrations for an already-created DB (column -> def). New tables are
# handled by CREATE TABLE IF NOT EXISTS above; this only backfills columns.
_MIGRATIONS = [
    ("drivers", "email", "TEXT"), ("drivers", "address", "TEXT"), ("drivers", "dob", "TEXT"),
    ("drivers", "emergency_name", "TEXT"), ("drivers", "emergency_phone", "TEXT"),
    ("drivers", "vehicle_reg", "TEXT"), ("drivers", "vehicle_make", "TEXT"), ("drivers", "vehicle_model", "TEXT"),
    ("drivers", "utr_or_company_ref", "TEXT"), ("drivers", "vat_registered", "INTEGER DEFAULT 0"),
    ("drivers", "vat_number", "TEXT"), ("drivers", "pay_cycle", "TEXT DEFAULT 'weekly'"),
    ("drivers", "bank_sort_code", "TEXT"), ("drivers", "bank_account_number", "TEXT"),
    ("drivers", "bank_account_name", "TEXT"), ("drivers", "bank_status", "TEXT DEFAULT 'unverified'"),
    ("drivers", "rating", "REAL DEFAULT 5.0"), ("drivers", "acceptance_pct", "INTEGER DEFAULT 100"),
    ("drivers", "completion_pct", "INTEGER DEFAULT 100"), ("drivers", "on_time_pct", "INTEGER DEFAULT 100"),
    ("drivers", "notify_jobs", "INTEGER DEFAULT 1"), ("drivers", "notify_pay", "INTEGER DEFAULT 1"),
    ("drivers", "notify_msgs", "INTEGER DEFAULT 1"), ("drivers", "nav_app", "TEXT DEFAULT 'google'"),
    ("jobs", "base_pay", "TEXT"), ("jobs", "waiting_pay", "TEXT"), ("jobs", "toll_pay", "TEXT"),
    ("jobs", "extras_pay", "TEXT"), ("jobs", "bonus_pay", "TEXT"), ("jobs", "deduction", "TEXT"),
    ("jobs", "miles", "REAL"), ("jobs", "completed_at", "TEXT"), ("jobs", "statement_id", "TEXT"),
    ("jobs", "requires_scan", "INTEGER NOT NULL DEFAULT 1"),
    ("drivers", "duty_status", "TEXT DEFAULT 'off'"), ("drivers", "theme", "TEXT DEFAULT 'night'"),
    ("drivers", "sound_alert_conn", "INTEGER DEFAULT 0"), ("drivers", "biometric", "INTEGER DEFAULT 0"),
    ("drivers", "location_consent_at", "TEXT"),
    ("drivers", "avatar_url", "TEXT"), ("drivers", "vehicle_photo_url", "TEXT"),
    ("drops", "pod_photos", "TEXT"),     # JSON list of media refs (multi-photo POD)
    ("messages", "category", "TEXT"),
    ("messages", "docket_number", "TEXT"),   # optional per-job chat tag (ops console)
    ("push_outbox", "claimed_at", "TEXT"),   # worker claim stamp (multi-worker flush)
    ("locations", "ping_id", "TEXT"),        # client-generated id → offline-replay dedup
]


def get_connection() -> sqlite3.Connection:
    """Per-thread WAL connection (created on first use)."""
    conn: Optional[sqlite3.Connection] = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
        conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        _local.conn = conn
    return conn


def close_connection() -> None:
    conn: Optional[sqlite3.Connection] = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        finally:
            _local.conn = None


def init_db() -> None:
    """Create tables + indexes + additive migrations (idempotent)."""
    conn = get_connection()
    for stmt in SCHEMA:
        conn.execute(stmt)
    for table, col, col_def in _MIGRATIONS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
        except sqlite3.OperationalError:
            pass  # column already exists
    for idx in _INDEXES:
        conn.execute(idx)
    conn.commit()
