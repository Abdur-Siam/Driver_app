"""Earnings, statements, expenses, tax and instant-pay logic.

All money is handled as decimal strings end-to-end (TOM convention);
sums use float here for brevity — swap to Decimal at TOM-merge for
penny-exact parity with TIA. Ownership (driver_id) is enforced on every
read/write.
"""
from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .db import get_connection


def today() -> date:
    """Operational 'today'. The real current date, evaluated per call (a
    long-running worker rolls over at midnight — never an import-time
    constant). DRIVER_APP_TODAY (ISO date) overrides it so demo environments
    and tests can pin the date to the seed anchor deterministically."""
    override = os.environ.get("DRIVER_APP_TODAY", "").strip()
    if override:
        try:
            return date.fromisoformat(override)
        except ValueError:
            pass
    return date.today()


INSTANT_PAY_FEE_PCT = 1.5          # % fee on early payout (typical gig model)
TAX_SET_ASIDE_PCT = 25             # suggested set-aside for self-employed
HMRC_MILEAGE_RATE = 0.45           # £/mile, first 10k miles


def _f(v) -> float:
    try:
        return float(v) if v not in (None, "") else 0.0
    except Exception:
        return 0.0


def _money(x) -> str:
    return "%.2f" % round(x + 1e-9, 2)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Statements ───────────────────────────────────────────────────────

def _stmt_dict(r) -> Dict[str, Any]:
    d = dict(r)
    return {
        "statement_id": d["statement_id"], "period_label": d["period_label"],
        "period_start": d["period_start"], "period_end": d["period_end"],
        "frequency": d["frequency"], "gross": d["gross"], "deductions": d["deductions"],
        "net": d["net"], "vat": d["vat"], "status": d["status"], "reference": d["reference"],
        "issued_at": d["issued_at"], "paid_at": d["paid_at"],
    }


def list_statements(driver_id) -> List[Dict[str, Any]]:
    rows = get_connection().execute(
        "SELECT * FROM statements WHERE driver_id = ? ORDER BY period_end DESC", (driver_id,),
    ).fetchall()
    return [_stmt_dict(r) for r in rows]


def get_statement(driver_id, sid) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    r = conn.execute(
        "SELECT * FROM statements WHERE statement_id = ? AND driver_id = ?", (sid, driver_id),
    ).fetchone()
    if not r:
        return None
    lines = conn.execute(
        "SELECT line_date, type, docket_number, description, amount FROM statement_lines "
        "WHERE statement_id = ? ORDER BY line_date, id", (sid,),
    ).fetchall()
    out = _stmt_dict(r)
    out["lines"] = [dict(l) for l in lines]
    return out


# ── Earnings summary ─────────────────────────────────────────────────

def earnings_summary(driver_id) -> Dict[str, Any]:
    conn = get_connection()
    stmts = list_statements(driver_id)
    processing = next((s for s in stmts if s["status"] == "processing"), None)
    paid = [s for s in stmts if s["status"] == "paid"]

    # Today's jobs (earned vs on-run)
    jobs = [dict(r) for r in conn.execute(
        "SELECT docket_number, account, status, driver_pay_final, base_pay, waiting_pay, toll_pay, "
        "extras_pay, bonus_pay, deduction, miles FROM jobs WHERE driver_id = ? AND operational_date = ?",
        (driver_id, today().isoformat()),
    ).fetchall()]
    today_earned = sum(_f(j["driver_pay_final"]) for j in jobs if j["status"] == "COMPLETED")
    today_onrun = sum(_f(j["driver_pay_final"]) for j in jobs if j["status"] not in ("COMPLETED", "CANCELLED"))

    # Weekly chart from the processing period's lines (job + bonus per day)
    week = []
    if processing:
        start = date.fromisoformat(processing["period_start"])
        buckets = {}
        for l in conn.execute(
            "SELECT line_date, type, amount FROM statement_lines WHERE statement_id = ?",
            (processing["statement_id"],),
        ).fetchall():
            if l["type"] in ("job", "bonus") and l["line_date"]:
                buckets[l["line_date"]] = buckets.get(l["line_date"], 0.0) + _f(l["amount"])
        for i in range(7):
            day = (start + timedelta(days=i)).isoformat()
            week.append({"date": day, "dow": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][i],
                         "amount": _money(buckets.get(day, 0.0))})

    # Component breakdown for the processing period (by line type)
    comp = {"jobs": 0.0, "bonus": 0.0, "expenses": 0.0, "deductions": 0.0}
    if processing:
        for l in conn.execute("SELECT type, amount FROM statement_lines WHERE statement_id = ?",
                              (processing["statement_id"],)).fetchall():
            a = _f(l["amount"])
            if l["type"] == "job":
                comp["jobs"] += a
            elif l["type"] == "bonus":
                comp["bonus"] += a
            elif l["type"] == "expense":
                comp["expenses"] += a
            elif l["type"] == "deduction":
                comp["deductions"] += -a

    ytd_gross = sum(_f(s["gross"]) for s in stmts)
    paid_ytd = sum(_f(s["net"]) for s in paid)
    available = _f(processing["net"]) if processing else 0.0
    # Subtract any outstanding payout requests from available.
    pend = conn.execute(
        "SELECT COALESCE(SUM(CAST(amount AS REAL)),0) AS s FROM payout_requests "
        "WHERE driver_id = ? AND status = 'requested'", (driver_id,),
    ).fetchone()["s"]
    available = max(0.0, available - _f(pend))

    projected_date = None
    if processing:
        projected_date = (date.fromisoformat(processing["period_end"]) + timedelta(days=3)).isoformat()

    return {
        "currency": "GBP",
        "today": {"earned": _money(today_earned), "on_run": _money(today_onrun),
                  "jobs_done": sum(1 for j in jobs if j["status"] == "COMPLETED"),
                  "jobs_total": len(jobs)},
        "period": processing and {
            "label": processing["period_label"], "reference": processing["reference"],
            "gross": processing["gross"], "deductions": processing["deductions"], "net": processing["net"],
            "status": processing["status"], "projected_pay_date": projected_date,
        },
        "week_chart": week,
        "breakdown": {k: _money(v) for k, v in comp.items()},
        "totals": {"ytd_gross": _money(ytd_gross), "paid_ytd": _money(paid_ytd),
                   "available_balance": _money(available)},
        "today_jobs": [
            {"docket_number": j["docket_number"], "account": j["account"], "status": j["status"],
             "total": j["driver_pay_final"],
             "components": {"base": j["base_pay"], "waiting": j["waiting_pay"], "toll": j["toll_pay"],
                            "extras": j["extras_pay"], "bonus": j["bonus_pay"], "deduction": j["deduction"]}}
            for j in jobs
        ],
    }


# ── Tax centre ───────────────────────────────────────────────────────

def tax_summary(driver_id) -> Dict[str, Any]:
    conn = get_connection()
    stmts = list_statements(driver_id)
    ytd_gross = sum(_f(s["gross"]) for s in stmts)
    miles = conn.execute(
        "SELECT COALESCE(SUM(miles),0) AS m FROM jobs WHERE driver_id = ?", (driver_id,),
    ).fetchone()["m"] or 0.0
    # Demo: scale today's miles into a plausible YTD figure for the allowance illustration.
    ytd_miles = round(miles * 180, 0)
    mileage_allowance = ytd_miles * HMRC_MILEAGE_RATE
    expenses_total = sum(_f(r["amount"]) for r in conn.execute(
        "SELECT amount FROM expenses WHERE driver_id = ? AND status IN ('approved','reimbursed')", (driver_id,),
    ).fetchall())
    taxable_est = max(0.0, ytd_gross - mileage_allowance - expenses_total)
    set_aside = taxable_est * (TAX_SET_ASIDE_PCT / 100.0)
    drv = conn.execute("SELECT vat_registered FROM drivers WHERE driver_id = ?", (driver_id,)).fetchone()
    return {
        "ytd_gross": _money(ytd_gross), "ytd_miles": int(ytd_miles),
        "mileage_rate": HMRC_MILEAGE_RATE, "mileage_allowance": _money(mileage_allowance),
        "allowable_expenses": _money(expenses_total), "estimated_taxable": _money(taxable_est),
        "suggested_set_aside_pct": TAX_SET_ASIDE_PCT, "suggested_set_aside": _money(set_aside),
        "vat_registered": bool(drv and drv["vat_registered"]),
        "note": "Indicative only — not tax advice. Figures for your accountant / Self Assessment.",
    }


# ── Expenses ─────────────────────────────────────────────────────────

def list_expenses(driver_id) -> List[Dict[str, Any]]:
    rows = get_connection().execute(
        "SELECT id, type, amount, exp_date, note, receipt, status, created_at FROM expenses "
        "WHERE driver_id = ? ORDER BY exp_date DESC, id DESC", (driver_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def add_expense(driver_id, etype, amount, exp_date, note, receipt) -> Dict[str, Any]:
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO expenses (driver_id, type, amount, exp_date, note, receipt, status, created_at) "
        "VALUES (?,?,?,?,?,?,'submitted',?)",
        (driver_id, etype, _money(_f(amount)), exp_date or today().isoformat(), note, receipt, _now()),
    )
    conn.commit()
    return {"id": cur.lastrowid, "status": "submitted"}


# ── Instant pay / early payout ───────────────────────────────────────

def request_payout(driver_id, amount) -> Dict[str, Any]:
    summ = earnings_summary(driver_id)
    available = _f(summ["totals"]["available_balance"])
    amt = _f(amount)
    if amt <= 0:
        return {"ok": False, "reason": "invalid_amount"}
    if amt > available:
        return {"ok": False, "reason": "exceeds_available", "available": _money(available)}
    fee = amt * (INSTANT_PAY_FEE_PCT / 100.0)
    conn = get_connection()
    conn.execute(
        "INSERT INTO payout_requests (driver_id, amount, fee, status, requested_at) VALUES (?,?,?,'requested',?)",
        (driver_id, _money(amt), _money(fee), _now()),
    )
    conn.commit()
    return {"ok": True, "amount": _money(amt), "fee": _money(fee), "net": _money(amt - fee),
            "fee_pct": INSTANT_PAY_FEE_PCT, "eta": "within 30 minutes"}


def performance(driver_id) -> Dict[str, Any]:
    conn = get_connection()
    d = conn.execute(
        "SELECT rating, acceptance_pct, completion_pct, on_time_pct FROM drivers WHERE driver_id = ?",
        (driver_id,),
    ).fetchone()
    jobs_done = conn.execute(
        "SELECT COUNT(*) AS n FROM statement_lines sl JOIN statements s ON s.statement_id = sl.statement_id "
        "WHERE s.driver_id = ? AND sl.type = 'job'", (driver_id,),
    ).fetchone()["n"]
    return {
        "rating": d["rating"], "acceptance_pct": d["acceptance_pct"], "completion_pct": d["completion_pct"],
        "on_time_pct": d["on_time_pct"], "jobs_completed": jobs_done,
    }
