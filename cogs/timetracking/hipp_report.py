"""Hipp staffing invoice workbook.

Two sheets, mirroring the owner's template:

* **Hipp Invoice** — one row per employee: Std/OT hours and billed rates (payrate x
  employee_type markup, OT x1.5) and the line total, with a grand total.
* **Payroll by Department** — each employee's total pay distributed across the work
  categories by hours (Pool = the bot's Construction, plus Service / Office), with
  **Shop** absorbing the rounding remainder.

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
_BOX = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)
_HFILL = PatternFill("solid", fgColor="D9E1F2")
_CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
_RIGHT = Alignment(horizontal="right")


def _cell(ws, coord, value, font=_BODY, fmt="General", border=None, align=None):
    c = ws[coord]
    c.value = value
    c.font = font
    c.number_format = fmt
    if border:
        c.border = border
    if align:
        c.alignment = align
    return c


def _header_row(ws, row, headers, widths=None):
    for i, (col, text) in enumerate(headers):
        c = _cell(ws, f"{col}{row}", text, _HDR, "General", _BOX, _CENTER)
        c.fill = _HFILL
        if widths:
            ws.column_dimensions[col].width = widths[i]


def _build_invoice_sheet(ws, rows, period_label, invoice_number):
    ws.title = "Hipp Invoice"
    _cell(ws, "A1", "HIPP Temporary Skills, Inc.", _TITLE)
    _cell(ws, "A2", "Payroll billing — SwimShack Inc.", _BODY)
    _cell(ws, "A3", f"Invoice #: {invoice_number or ''}", _BODY)
    _cell(ws, "E3", f"Date: {date.today().isoformat()}", _BODY)
    _cell(ws, "A4", f"Period Covered: {period_label}", _BODY)

    top = 6
    headers = [("A", "Emp. #"), ("B", "Employee"), ("C", "Std. Hrs."), ("D", "Std. Rate"),
               ("E", "OT Hrs."), ("F", "OT Rate"), ("G", "Total")]
    _header_row(ws, top, headers, widths=[8, 26, 10, 12, 10, 12, 14])

    r = top
    grand = 0.0
    for n, row in enumerate(rows, start=1):
        r += 1
        _cell(ws, f"A{r}", n, _BODY, "General", _BOX, _CENTER)
        _cell(ws, f"B{r}", row["name"], _BODY, "General", _BOX)
        _cell(ws, f"C{r}", row["std_hrs"], _BODY, _HRS, _BOX, _RIGHT)
        _cell(ws, f"D{r}", row["std_rate"], _BODY, _CUR, _BOX, _RIGHT)
        _cell(ws, f"E{r}", row["ot_hrs"], _BODY, _HRS, _BOX, _RIGHT)
        _cell(ws, f"F{r}", row["ot_rate"], _BODY, _CUR, _BOX, _RIGHT)
        _cell(ws, f"G{r}", row["total"], _BODY, _CUR, _BOX, _RIGHT)
        grand += row["total"]
    r += 1
    _cell(ws, f"B{r}", "Total", _HDR, "General", _BOX, _RIGHT)
    _cell(ws, f"G{r}", round(grand, 2), _HDR, _CUR, _BOX, _RIGHT)


def _build_department_sheet(ws, rows):
    ws.title = "Payroll by Department"
    top = 1
    # hours block, then dollars block. Pool = the bot's Construction category.
    headers = [("A", "Emp. #"), ("B", "Employee"),
               ("C", "Shop\nHrs"), ("D", "Pool\nHrs"), ("E", "Service\nHrs"), ("F", "Office\nHrs"), ("G", "Total\nHrs"),
               ("H", "Shop $"), ("I", "Pool $"), ("J", "Service $"), ("K", "Office $"), ("L", "Total Pay")]
    _header_row(ws, top, headers, widths=[8, 24, 8, 8, 9, 8, 9, 12, 12, 12, 12, 14])

    r = top
    tot = {k: 0.0 for k in ("shop_h", "pool_h", "svc_h", "off_h", "th",
                            "shop_d", "pool_d", "svc_d", "off_d", "tp")}
    for n, row in enumerate(rows, start=1):
        ch = row["category_hours"]
        shop_h, pool_h = ch[costing.SHOP], ch["Construction"]
        svc_h, off_h = ch["Service"], ch["Office"]
        total_h = shop_h + pool_h + svc_h + off_h
        # distribute the employee's total pay by hours, Shop = catch-all remainder
        dist = costing.distribute_pay(
            row["total"],
            {"Pool": pool_h, "Service": svc_h, "Office": off_h, "Shop": shop_h},
            "Shop",
        )
        r += 1
        vals = [(("A", n, "General", _CENTER)), ("B", row["name"], "General", None),
                ("C", shop_h, _HRS, _RIGHT), ("D", pool_h, _HRS, _RIGHT), ("E", svc_h, _HRS, _RIGHT),
                ("F", off_h, _HRS, _RIGHT), ("G", round(total_h, 2), _HRS, _RIGHT),
                ("H", dist["Shop"], _CUR, _RIGHT), ("I", dist["Pool"], _CUR, _RIGHT),
                ("J", dist["Service"], _CUR, _RIGHT), ("K", dist["Office"], _CUR, _RIGHT),
                ("L", row["total"], _CUR, _RIGHT)]
        for col, v, fmt, align in vals:
            _cell(ws, f"{col}{r}", v, _BODY, fmt, _BOX, align)
        tot["shop_h"] += shop_h; tot["pool_h"] += pool_h; tot["svc_h"] += svc_h
        tot["off_h"] += off_h; tot["th"] += total_h
        tot["shop_d"] += dist["Shop"]; tot["pool_d"] += dist["Pool"]
        tot["svc_d"] += dist["Service"]; tot["off_d"] += dist["Office"]; tot["tp"] += row["total"]
    r += 1
    _cell(ws, f"B{r}", "Totals", _HDR, "General", _BOX, _RIGHT)
    for col, key, fmt in (("C", "shop_h", _HRS), ("D", "pool_h", _HRS), ("E", "svc_h", _HRS),
                          ("F", "off_h", _HRS), ("G", "th", _HRS), ("H", "shop_d", _CUR),
                          ("I", "pool_d", _CUR), ("J", "svc_d", _CUR), ("K", "off_d", _CUR), ("L", "tp", _CUR)):
        _cell(ws, f"{col}{r}", round(tot[key], 2), _HDR, fmt, _BOX, _RIGHT)


def generate_hipp_invoice(file_path: str, rows: list[dict], period_label: str,
                          invoice_number: str | None = None) -> None:
    """Write the two-sheet Hipp invoice workbook to ``file_path``. ``rows`` is a list
    of costing.hipp_billing dicts. Blocking — run in a worker thread."""
    wb = Workbook()
    _build_invoice_sheet(wb.active, rows, period_label, invoice_number)
    _build_department_sheet(wb.create_sheet("Payroll by Department"), rows)
    wb.save(file_path)
