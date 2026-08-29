"""Hipp staffing invoice workbook.

Two sheets:
* **Hipp Invoice** — one row per employee: current Pay Rate, Std/OT hours and billed
  rates (payrate x employee_type markup, OT x1.5) and the line total + grand total.
* **Payroll by Department** — each employee's total pay distributed across the work
  categories by hours (Pool = the bot's Construction, plus Service / Office), with
  the employee's **catch-all category** absorbing the rounding remainder.

Styling/layout comes from ``xlsxstyle`` (B2 offset, zebra rows, gray zeros, borders,
number formats, print setup, modern theme). Built programmatically so no real
accounting data lives in the repo. Blocking; call via asyncio.to_thread. Rows come
from costing.hipp_billing.
"""

from __future__ import annotations

from datetime import date

from openpyxl import Workbook

from . import costing
from .xlsxstyle import (ALT, BODY, CENTER, CUR, HDR, HFILL, HRS, HRS_TOTAL, MID, RIGHT,
                        ROW_SHIFT, TITLE, TOTAL_FILL, WHITE, addr, apply_modern_theme, cell,
                        finalize, put, shift_col)

# Canonical catch-all category -> the department sheet's column key (Pool=Construction,
# Store/Shop=Shop). Defaults to Shop.
_CANON_TO_DEPT = {"Shop": "Shop", "Construction": "Pool", "Service": "Service", "Office": "Office"}


def _build_invoice_sheet(ws, rows, period_label, invoice_number):
    ws.title = "Hipp Invoice"
    cell(ws, "A", 1, "HIPP Temporary Skills, Inc.", TITLE)
    cell(ws, "A", 2, "Payroll billing — SwimShack Inc.", BODY)
    cell(ws, "A", 3, f"Invoice #: {invoice_number or ''}", BODY)
    cell(ws, "E", 3, f"Date: {date.today().isoformat()}", BODY)
    cell(ws, "A", 4, f"Period Covered: {period_label}", BODY)

    first, last = "A", "H"
    top = 6
    headers = [("A", "Emp. #"), ("B", "Employee"), ("C", "Pay Rate"), ("D", "Std. Hrs."),
               ("E", "Std. Rate"), ("F", "OT Hrs."), ("G", "OT Rate"), ("H", "Total")]
    widths = [8, 26, 11, 10, 12, 10, 12, 14]
    for (col, text), w in zip(headers, widths):
        put(ws, col, top, text, "General", CENTER, HFILL, {"left", "right", "top", "bottom"}, HDR)
        ws.column_dimensions[shift_col(col)].width = w

    r = top
    grand = 0.0
    for n, row in enumerate(rows, start=1):
        r += 1
        fill = WHITE if n % 2 == 1 else ALT
        cells = [("A", n, "General", MID), ("B", row["name"], "General", None),
                 ("C", row.get("base_rate", 0.0), CUR, MID), ("D", row["std_hrs"], HRS, MID),
                 ("E", row["std_rate"], CUR, MID), ("F", row["ot_hrs"], HRS, MID),
                 ("G", row["ot_rate"], CUR, MID), ("H", row["total"], CUR, MID)]
        for col, val, fmt, align in cells:
            put(ws, col, r, val, fmt, align, fill, {"left", "right"}, zero_gray=True)
        grand += row["total"]
    r += 1
    total_cells = [("A", None, "General", None), ("B", "Total", "General", RIGHT),
                   ("C", None, CUR, None), ("D", None, HRS, None), ("E", None, CUR, None),
                   ("F", None, HRS, None), ("G", None, CUR, None), ("H", round(grand, 2), CUR, MID)]
    for col, val, fmt, align in total_cells:
        sides = {"top", "bottom"} | ({"left"} if col == first else set()) | ({"right"} if col == last else set())
        put(ws, col, r, val, fmt, align, TOTAL_FILL, sides, HDR)
    finalize(ws, header_last_row=top, last_col="H", last_row=r + ROW_SHIFT, landscape=False)


def _build_department_sheet(ws, rows):
    ws.title = "Payroll by Department"
    vcols = {"A", "B", "G", "L"}   # Emp #, Employee, Total Hrs, Total Pay
    top = 1
    headers = [("A", "Emp. #"), ("B", "Employee"),
               ("C", "Shop\nHrs"), ("D", "Pool\nHrs"), ("E", "Service\nHrs"), ("F", "Office\nHrs"), ("G", "Total\nHrs"),
               ("H", "Shop $"), ("I", "Pool $"), ("J", "Service $"), ("K", "Office $"), ("L", "Total Pay")]
    widths = [8, 24, 8, 8, 9, 8, 9, 12, 12, 12, 12, 14]
    for (col, text), w in zip(headers, widths):
        sides = {"top", "bottom"} | ({"left", "right"} if col in vcols else set())
        put(ws, col, top, text, "General", CENTER, HFILL, sides, HDR)
        ws.column_dimensions[shift_col(col)].width = w

    r = top
    tot = {k: 0.0 for k in ("shop_h", "pool_h", "svc_h", "off_h", "th",
                            "shop_d", "pool_d", "svc_d", "off_d", "tp")}
    for n, row in enumerate(rows, start=1):
        ch = row["category_hours"]
        shop_h, pool_h = ch[costing.SHOP], ch["Construction"]
        svc_h, off_h = ch["Service"], ch["Office"]
        total_h = shop_h + pool_h + svc_h + off_h
        catchall = _CANON_TO_DEPT.get(row.get("catchall", "Shop"), "Shop")
        dist = costing.distribute_pay(
            row["total"], {"Pool": pool_h, "Service": svc_h, "Office": off_h, "Shop": shop_h}, catchall)
        r += 1
        fill = WHITE if n % 2 == 1 else ALT
        cells = [("A", n, "General", MID), ("B", row["name"], "General", None),
                 ("C", shop_h, HRS, MID), ("D", pool_h, HRS, MID), ("E", svc_h, HRS, MID),
                 ("F", off_h, HRS, MID), ("G", round(total_h, 2), HRS_TOTAL, MID),
                 ("H", dist["Shop"], CUR, MID), ("I", dist["Pool"], CUR, MID),
                 ("J", dist["Service"], CUR, MID), ("K", dist["Office"], CUR, MID),
                 ("L", row["total"], CUR, MID)]
        for col, val, fmt, align in cells:
            sides = {"left", "right"} if col in vcols else set()
            put(ws, col, r, val, fmt, align, fill, sides, zero_gray=True)
        tot["shop_h"] += shop_h; tot["pool_h"] += pool_h; tot["svc_h"] += svc_h
        tot["off_h"] += off_h; tot["th"] += total_h
        tot["shop_d"] += dist["Shop"]; tot["pool_d"] += dist["Pool"]
        tot["svc_d"] += dist["Service"]; tot["off_d"] += dist["Office"]; tot["tp"] += row["total"]
    r += 1
    total_cells = [("A", None, "General", None), ("B", "Totals", "General", RIGHT),
                   ("C", round(tot["shop_h"], 2), HRS, MID), ("D", round(tot["pool_h"], 2), HRS, MID),
                   ("E", round(tot["svc_h"], 2), HRS, MID), ("F", round(tot["off_h"], 2), HRS, MID),
                   ("G", round(tot["th"], 2), HRS_TOTAL, MID), ("H", round(tot["shop_d"], 2), CUR, MID),
                   ("I", round(tot["pool_d"], 2), CUR, MID), ("J", round(tot["svc_d"], 2), CUR, MID),
                   ("K", round(tot["off_d"], 2), CUR, MID), ("L", round(tot["tp"], 2), CUR, MID)]
    for col, val, fmt, align in total_cells:
        sides = {"top", "bottom"} | ({"left", "right"} if col in vcols else set())
        put(ws, col, r, val, fmt, align, TOTAL_FILL, sides, HDR)
    finalize(ws, header_last_row=top, last_col="L", last_row=r + ROW_SHIFT, landscape=True)


def generate_hipp_invoice(file_path: str, rows: list[dict], period_label: str,
                          invoice_number: str | None = None) -> None:
    """Write the two-sheet Hipp invoice workbook to ``file_path``. ``rows`` is a list
    of costing.hipp_billing dicts. Blocking — run in a worker thread."""
    wb = Workbook()
    apply_modern_theme(wb)
    _build_invoice_sheet(wb.active, rows, period_label, invoice_number)
    _build_department_sheet(wb.create_sheet("Payroll by Department"), rows)
    wb.save(file_path)
