"""Minimal pure-Python PDF writer for downloadable pay statements.

No third-party dependency — emits a valid PDF 1.4 with the built-in
Helvetica font. Good enough for a clean, downloadable A4 statement; swap
for the TIA/Puppeteer branded template at TOM-merge if richer styling is
wanted (the API shape stays the same).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List

PAGE_W, PAGE_H = 595, 842
MARGIN = 48


def _esc(s: Any) -> str:
    """Escape a string for inclusion inside PDF text parentheses.

    - Converts None to empty string.
    - Escapes backslash, parentheses and newlines/carriage returns.
    """
    t = "" if s is None else str(s)
    # Escape backslash first
    t = t.replace("\\", "\\\\")
    t = t.replace("(", "\\(").replace(")", "\\)")
    t = t.replace("\r", "\\r").replace("\n", "\\n")
    return t


class _Canvas:
    def __init__(self):
        # each page is a list of PDF text/graphics operator strings
        self.pages: List[List[str]] = [[]]
        self.y = PAGE_H - MARGIN

    @property
    def ops(self) -> List[str]:
        return self.pages[-1]

    def newpage(self) -> None:
        self.pages.append([])
        self.y = PAGE_H - MARGIN

    def space(self, dy: float) -> None:
        self.y -= dy
        if self.y < MARGIN + 40:
            self.newpage()

    def text(self, x: float, size: float, s: Any, bold_gray: bool = False) -> None:
        g = 0.25 if bold_gray else 0.1
        self.ops.append(f"BT /F1 {size} Tf {g} {g} {g} rg {x} {self.y} Td ({_esc(s)}) Tj ET")

    def text_right(self, xr: float, size: float, s: Any) -> None:
        # crude right-align using Helvetica avg char width ~0.52*size
        s_str = "" if s is None else str(s)
        w = len(s_str) * size * 0.52
        self.text(xr - w, size, s_str)

    def line(self, x1: float, x2: float) -> None:
        self.ops.append(f"0.6 w 0.8 0.8 0.8 RG {x1} {self.y} m {x2} {self.y} l S")


def _fmt_amount(v: Any) -> str:
    """Format numeric-like values consistently as strings with 2 decimals.

    Accepts Decimal, float, int or numeric strings. Falls back to empty string
    for None or unparseable inputs.
    """
    if v is None:
        return ""
    if isinstance(v, str) and v == "":
        return ""
    try:
        dec = v if isinstance(v, Decimal) else Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return str(v)
    # Normalize to two decimal places
    try:
        dec = dec.quantize(Decimal('0.01'))
    except Exception:
        pass
    return f"{dec}"


def render_statement_pdf(statement: Dict[str, Any], driver: Dict[str, Any]) -> bytes:
    c = _Canvas()
    right = PAGE_W - MARGIN

    # Header
    c.text(MARGIN, 18, "Xtra Mile Couriers")
    c.text_right(right, 11, "PAY STATEMENT")
    c.space(16)
    c.text(MARGIN, 10, "Courier pay statement")
    c.text_right(right, 10, f"Ref {statement.get('reference', '')}")
    c.space(10)
    c.text_right(right, 10, f"Status: {str(statement.get('status', '')).upper()}")
    c.space(18)
    c.line(MARGIN, right)
    c.space(20)

    # Driver + period
    c.text(MARGIN, 11, f"Driver: {driver.get('name', '')}  ({driver.get('callsign', '')})")
    c.text_right(right, 11, f"Period: {statement.get('period_label', '')}")
    c.space(14)
    tax_ref = driver.get("utr_or_company_ref") or ""
    c.text(MARGIN, 9, f"Tax ref: {tax_ref}")
    c.text_right(right, 9, f"{statement.get('period_start','')} to {statement.get('period_end','')} ({statement.get('frequency','')})")
    c.space(12)
    if statement.get("paid_at"):
        c.text_right(right, 9, f"Paid: {statement.get('paid_at')}")
        c.space(12)
    c.space(8)

    # Table header
    cx_date, cx_desc, cx_type, cx_amt = MARGIN, MARGIN + 90, right - 150, right
    c.text(cx_date, 9, "DATE")
    c.text(cx_desc, 9, "DESCRIPTION")
    c.text(cx_type, 9, "TYPE")
    c.text_right(cx_amt, 9, "AMOUNT (GBP)")
    c.space(6)
    c.line(MARGIN, right)
    c.space(16)

    for ln in statement.get("lines", []) or []:
        c.text(cx_date, 9, (ln.get("line_date") or "")[:10])
        desc = (ln.get("description") or "")
        c.text(cx_desc, 9, desc[:42])
        c.text(cx_type, 9, str(ln.get("type", "")).title())
        c.text_right(cx_amt, 9, _fmt_amount(ln.get('amount', '')))
        c.space(15)

    c.space(4)
    c.line(MARGIN, right)
    c.space(20)

    def total_row(label: str, value: Any, size: int = 10) -> None:
        c.text(cx_type - 60, size, label)
        c.text_right(cx_amt, size, _fmt_amount(value))
        c.space(15)

    total_row("Gross", statement.get("gross", ""))
    # Show deductions with a leading minus if positive numeric provided
    deductions = statement.get("deductions", "0.00")
    try:
        # If numeric, ensure sign shown as negative
        ded_dec = Decimal(str(deductions))
        deductions_display = f"-{ded_dec.quantize(Decimal('0.01'))}" if ded_dec >= 0 else f"{ded_dec.quantize(Decimal('0.01'))}"
    except Exception:
        deductions_display = str(deductions)
    total_row("Deductions", deductions_display)

    if statement.get("vat"):
        try:
            if Decimal(str(statement.get("vat"))) != 0:
                total_row("VAT", statement.get("vat"))
        except Exception:
            # If vat cannot be parsed, show as-is
            total_row("VAT", statement.get("vat"))

    c.space(2)
    c.line(cx_type - 70, right)
    c.space(16)
    total_row("NET PAY", statement.get("net", ""), size=12)

    # Footer
    c.y = MARGIN + 24
    c.line(MARGIN, right)
    c.space(14)
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    c.text(MARGIN, 8, f"Generated {gen} · Xtra Mile Couriers · TOM Driver")
    c.text_right(right, 8, "Keep for your records / Self Assessment")

    return _build_pdf(["\n".join(p) for p in c.pages])


def _build_pdf(page_streams: List[str]) -> bytes:
    n = len(page_streams)
    page_ids: List[int] = []
    content_ids: List[int] = []
    nid = 4
    for _ in range(n):
        page_ids.append(nid)
        nid += 1
        content_ids.append(nid)
        nid += 1

    parts: Dict[int, bytes] = {}
    parts[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    parts[2] = f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode()
    parts[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    for i in range(n):
        pid, cid = page_ids[i], content_ids[i]
        parts[pid] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                      f"/Resources << /Font << /F1 3 0 R >> >> /Contents {cid} 0 R >>").encode()
        stream = page_streams[i].encode("latin-1", "replace")
        parts[cid] = b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"

    out = bytearray(b"%PDF-1.4\n")
    offsets: Dict[int, int] = {}
    for oid in sorted(parts):
        offsets[oid] = len(out)
        out += f"{oid} 0 obj\n".encode() + parts[oid] + b"\nendobj\n"
    xref_pos = len(out)
    total = max(parts) + 1
    out += f"xref\n0 {total}\n".encode()
    out += b"0000000000 65535 f \n"
    for oid in range(1, total):
        off = offsets.get(oid, 0)
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {total} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF".encode()
    return bytes(out)
