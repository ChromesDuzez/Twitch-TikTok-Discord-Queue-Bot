"""Hipp staffing invoice workbook.

Two sheets, mirroring the owner's template:

* **Hipp Invoice** — one row per employee: current Pay Rate, Std/OT hours and
  billed rates (payrate x employee_type markup, OT x1.5) and the line total, with
  a grand total.
* **Payroll by Department** — each employee's total pay distributed across the work
  categories by hours (Pool = the bot's Construction, plus Service / Office), with
  **Shop** absorbing the rounding remainder.

Styling: zebra rows (white / "White, Background 2, Darker 5%"), a "Darker 15%"
total row, and vertical-only borders framing the key columns (Emp #, Employee, the
total-hours column, and the total-pay column). The gray hexes are the modern Office
theme's Background-2 tints (openpyxl's bundled theme is the old beige one, so the
values are hardcoded rather than referenced as theme colors).

Built programmatically (openpyxl) rather than from a bundled binary template so no
real accounting data ever lives in the repo. Blocking; call via asyncio.to_thread.
The rows come from costing.hipp_billing (one dict per employee).
"""

from __future__ import annotations

from datetime import date

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from . import costing

_HDR = Font(name="Arial", size=10, bold=True)
_BODY = Font(name="Arial", size=10)
_TITLE = Font(name="Arial", size=14, bold=True)
_CUR = '$#,##0.00'
_HRS = '0.00'
_thin = Side(style="thin")
_HFILL = PatternFill("solid", fgColor="D9E1F2")            # header
_WHITE = PatternFill("solid", fgColor="FFFFFF")            # zebra: white row
_ALT = PatternFill("solid", fgColor="DCDADA")             # White, Background 2, Darker 5%
_TOTAL_FILL = PatternFill("solid", fgColor="C5C3C3")       # White, Background 2, Darker 15%
_CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
_RIGHT = Alignment(horizontal="right")


def _vborder(framed: set, col: str) -> Border:
    """Vertical-only (left+right) border for a framed column, else no border."""
    return Border(left=_thin, right=_thin) if col in framed else Border()


def _cell(ws, coord, value, font=_BODY, fmt="General", border=None, fill=None, align=None):
    c = ws[coord]
    c.value = value
    c.font = font
    c.number_format = fmt
    if border is not None:
        c.border = border
    if fill is not None:
        c.fill = fill
    if align is not None:
        c.alignment = align
    return c


def _header(ws, row, headers, framed, widths):
    for (col, text), width in zip(headers, widths):
        _cell(ws, f"{col}{row}", text, _HDR, "General", _vborder(framed, col), _HFILL, _CENTER)
        ws.column_dimensions[col].width = width


def _row(ws, r, cells, framed, fill, font=_BODY):
    """Write a full table row so the fill spans every column. `cells` is a list of
    (col, value, number_format, alignment)."""
    for col, value, fmt, align in cells:
        _cell(ws, f"{col}{r}", value, font, fmt, _vborder(framed, col), fill, align)


def _build_invoice_sheet(ws, rows, period_label, invoice_number):
    ws.title = "Hipp Invoice"
    _cell(ws, "A1", "HIPP Temporary Skills, Inc.", _TITLE)
    _cell(ws, "A2", "Payroll billing — SwimShack Inc.", _BODY)
    _cell(ws, "A3", f"Invoice #: {invoice_number or ''}", _BODY)
    _cell(ws, "E3", f"Date: {date.today().isoformat()}", _BODY)
    _cell(ws, "A4", f"Period Covered: {period_label}", _BODY)

    framed = {"A", "B", "H"}   # Emp #, Employee, Total (pay)
    top = 6
    # "Pay Rate" is the current standard hourly rate; "Std. Rate" is the billed
    # (marked-up) rate.
    headers = [("A", "Emp. #"), ("B", "Employee"), ("C", "Pay Rate"), ("D", "Std. Hrs."),
               ("E", "Std. Rate"), ("F", "OT Hrs."), ("G", "OT Rate"), ("H", "Total")]
    _header(ws, top, headers, framed, [8, 26, 11, 10, 12, 10, 12, 14])

    r = top
    grand = 0.0
    for n, row in enumerate(rows, start=1):
        r += 1
        fill = _WHITE if n % 2 == 1 else _ALT
        _row(ws, r, [
            ("A", n, "General", _CENTER),
            ("B", row["name"], "General", None),
            ("C", row.get("base_rate", 0.0), _CUR, _RIGHT),
            ("D", row["std_hrs"], _HRS, _RIGHT),
            ("E", row["std_rate"], _CUR, _RIGHT),
            ("F", row["ot_hrs"], _HRS, _RIGHT),
            ("G", row["ot_rate"], _CUR, _RIGHT),
            ("H", row["total"], _CUR, _RIGHT),
        ], framed, fill)
        grand += row["total"]
    r += 1
    _row(ws, r, [
        ("A", None, "General", None), ("B", "Total", "General", _RIGHT),
        ("C", None, _CUR, None), ("D", None, _HRS, None), ("E", None, _CUR, None),
        ("F", None, _HRS, None), ("G", None, _CUR, None), ("H", round(grand, 2), _CUR, _RIGHT),
    ], framed, _TOTAL_FILL, font=_HDR)


def _build_department_sheet(ws, rows):
    ws.title = "Payroll by Department"
    framed = {"A", "B", "G", "L"}   # Emp #, Employee, Total Hrs, Total Pay
    top = 1
    # hours block, then dollars block. Pool = the bot's Construction category.
    headers = [("A", "Emp. #"), ("B", "Employee"),
               ("C", "Shop\nHrs"), ("D", "Pool\nHrs"), ("E", "Service\nHrs"), ("F", "Office\nHrs"), ("G", "Total\nHrs"),
               ("H", "Shop $"), ("I", "Pool $"), ("J", "Service $"), ("K", "Office $"), ("L", "Total Pay")]
    _header(ws, top, headers, framed, [8, 24, 8, 8, 9, 8, 9, 12, 12, 12, 12, 14])

    r = top
    tot = {k: 0.0 for k in ("shop_h", "pool_h", "svc_h", "off_h", "th",
                            "shop_d", "pool_d", "svc_d", "off_d", "tp")}
    for n, row in enumerate(rows, start=1):
        ch = row["category_hours"]
        shop_h, pool_h = ch[costing.SHOP], ch["Construction"]
        svc_h, off_h = ch["Service"], ch["Office"]
        total_h = shop_h + pool_h + svc_h + off_h
        dist = costing.distribute_pay(
            row["total"],
            {"Pool": pool_h, "Service": svc_h, "Office": off_h, "Shop": shop_h},
            "Shop",
        )
        r += 1
        fill = _WHITE if n % 2 == 1 else _ALT
        _row(ws, r, [
            ("A", n, "General", _CENTER), ("B", row["name"], "General", None),
            ("C", shop_h, _HRS, _RIGHT), ("D", pool_h, _HRS, _RIGHT), ("E", svc_h, _HRS, _RIGHT),
            ("F", off_h, _HRS, _RIGHT), ("G", round(total_h, 2), _HRS, _RIGHT),
            ("H", dist["Shop"], _CUR, _RIGHT), ("I", dist["Pool"], _CUR, _RIGHT),
            ("J", dist["Service"], _CUR, _RIGHT), ("K", dist["Office"], _CUR, _RIGHT),
            ("L", row["total"], _CUR, _RIGHT),
        ], framed, fill)
        tot["shop_h"] += shop_h; tot["pool_h"] += pool_h; tot["svc_h"] += svc_h
        tot["off_h"] += off_h; tot["th"] += total_h
        tot["shop_d"] += dist["Shop"]; tot["pool_d"] += dist["Pool"]
        tot["svc_d"] += dist["Service"]; tot["off_d"] += dist["Office"]; tot["tp"] += row["total"]
    r += 1
    _row(ws, r, [
        ("A", None, "General", None), ("B", "Totals", "General", _RIGHT),
        ("C", round(tot["shop_h"], 2), _HRS, _RIGHT), ("D", round(tot["pool_h"], 2), _HRS, _RIGHT),
        ("E", round(tot["svc_h"], 2), _HRS, _RIGHT), ("F", round(tot["off_h"], 2), _HRS, _RIGHT),
        ("G", round(tot["th"], 2), _HRS, _RIGHT), ("H", round(tot["shop_d"], 2), _CUR, _RIGHT),
        ("I", round(tot["pool_d"], 2), _CUR, _RIGHT), ("J", round(tot["svc_d"], 2), _CUR, _RIGHT),
        ("K", round(tot["off_d"], 2), _CUR, _RIGHT), ("L", round(tot["tp"], 2), _CUR, _RIGHT),
    ], framed, _TOTAL_FILL, font=_HDR)


def generate_hipp_invoice(file_path: str, rows: list[dict], period_label: str,
                          invoice_number: str | None = None) -> None:
    """Write the two-sheet Hipp invoice workbook to ``file_path``. ``rows`` is a list
    of costing.hipp_billing dicts. Blocking — run in a worker thread."""
    wb = Workbook()
    _build_invoice_sheet(wb.active, rows, period_label, invoice_number)
    _build_department_sheet(wb.create_sheet("Payroll by Department"), rows)
    wb.save(file_path)
