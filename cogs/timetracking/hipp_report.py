"""Hipp staffing invoice workbook.

Two sheets, mirroring the owner's template:

* **Hipp Invoice** — one row per employee: current Pay Rate, Std/OT hours and
  billed rates (payrate x employee_type markup, OT x1.5) and the line total, with
  a grand total.
* **Payroll by Department** — each employee's total pay distributed across the work
  categories by hours (Pool = the bot's Construction, plus Service / Office), with
  **Shop** absorbing the rounding remainder.

Styling: zebra data rows (white / "White, Background 2, Darker 5%" = #DCDADA), a
"Darker 15%" (#C5C3C3) total row, and zero-valued data cells shown in that same
#C5C3C3 so they recede. Borders: the Invoice table is enclosed with a vertical
line on every column and a boxed header + boxed total band; the Payroll table keeps
vertical lines only on the key columns (Emp #, Employee, Total Hrs, Total Pay) with
the whole table, the header row, and the total row each outer-bordered. The gray
hexes are the modern Office theme's Background-2 tints (openpyxl bundles the older
theme, so the values are hardcoded rather than referenced as theme colors).

Built programmatically (openpyxl) so no real accounting data lives in the repo.
Blocking; call via asyncio.to_thread. Rows come from costing.hipp_billing.
"""

from __future__ import annotations

from datetime import date

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from . import costing

_HDR = Font(name="Arial", size=10, bold=True)
_BODY = Font(name="Arial", size=10)
_TITLE = Font(name="Arial", size=14, bold=True)
_ZERO = Font(name="Arial", size=10, color="C5C3C3")       # muted zeros
_CUR = '$#,##0.00'
_HRS = '0.00'
_HRS_TOTAL = '0.00 "hrs";[Red]-0.00 "hrs";"-"'             # Total-Hrs column format
_thin = Side(style="thin")
_HFILL = PatternFill("solid", fgColor="D9E1F2")            # header
_WHITE = PatternFill("solid", fgColor="FFFFFF")            # zebra: white row
_ALT = PatternFill("solid", fgColor="DCDADA")             # White, Background 2, Darker 5%
_TOTAL_FILL = PatternFill("solid", fgColor="C5C3C3")       # White, Background 2, Darker 15%
_CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
_RIGHT = Alignment(horizontal="right")


def _border(sides) -> Border:
    """Border with only the named sides ('left'/'right'/'top'/'bottom') drawn thin."""
    return Border(**{s: _thin for s in sides})


def _cell(ws, coord, value, font=_BODY, fmt="General", align=None):
    """Plain cell (letterhead) — no fill, no border."""
    c = ws[coord]
    c.value = value
    c.font = font
    c.number_format = fmt
    if align:
        c.alignment = align
    return c


def _put(ws, col, r, value, fmt, align, fill, sides, base_font=_BODY, zero_gray=False):
    c = ws[f"{col}{r}"]
    c.value = value
    c.font = _ZERO if (zero_gray and isinstance(value, (int, float)) and value == 0) else base_font
    c.number_format = fmt
    c.fill = fill
    c.border = _border(sides)
    if align:
        c.alignment = align
    return c


def _build_invoice_sheet(ws, rows, period_label, invoice_number):
    ws.title = "Hipp Invoice"
    _cell(ws, "A1", "HIPP Temporary Skills, Inc.", _TITLE)
    _cell(ws, "A2", "Payroll billing — SwimShack Inc.", _BODY)
    _cell(ws, "A3", f"Invoice #: {invoice_number or ''}", _BODY)
    _cell(ws, "E3", f"Date: {date.today().isoformat()}", _BODY)
    _cell(ws, "A4", f"Period Covered: {period_label}", _BODY)

    first, last = "A", "H"
    top = 6
    headers = [("A", "Emp. #"), ("B", "Employee"), ("C", "Pay Rate"), ("D", "Std. Hrs."),
               ("E", "Std. Rate"), ("F", "OT Hrs."), ("G", "OT Rate"), ("H", "Total")]
    widths = [8, 26, 11, 10, 12, 10, 12, 14]
    # Header: full box on every cell (top of table + a line under the header).
    for (col, text), w in zip(headers, widths):
        _put(ws, col, top, text, "General", _CENTER, _HFILL, {"left", "right", "top", "bottom"}, _HDR)
        ws.column_dimensions[col].width = w

    r = top
    grand = 0.0
    for n, row in enumerate(rows, start=1):
        r += 1
        fill = _WHITE if n % 2 == 1 else _ALT
        cells = [("A", n, "General", _CENTER), ("B", row["name"], "General", None),
                 ("C", row.get("base_rate", 0.0), _CUR, _RIGHT), ("D", row["std_hrs"], _HRS, _RIGHT),
                 ("E", row["std_rate"], _CUR, _RIGHT), ("F", row["ot_hrs"], _HRS, _RIGHT),
                 ("G", row["ot_rate"], _CUR, _RIGHT), ("H", row["total"], _CUR, _RIGHT)]
        for col, val, fmt, align in cells:      # vertical line on every column, no horizontals
            _put(ws, col, r, val, fmt, align, fill, {"left", "right"}, zero_gray=True)
        grand += row["total"]
    r += 1
    # Total row: an outer-bordered band with no inner vertical lines.
    total_cells = [("A", None, "General", None), ("B", "Total", "General", _RIGHT),
                   ("C", None, _CUR, None), ("D", None, _HRS, None), ("E", None, _CUR, None),
                   ("F", None, _HRS, None), ("G", None, _CUR, None), ("H", round(grand, 2), _CUR, _RIGHT)]
    for col, val, fmt, align in total_cells:
        sides = {"top", "bottom"} | ({"left"} if col == first else set()) | ({"right"} if col == last else set())
        _put(ws, col, r, val, fmt, align, _TOTAL_FILL, sides, _HDR)


def _build_department_sheet(ws, rows):
    ws.title = "Payroll by Department"
    first, last = "A", "L"
    vcols = {"A", "B", "G", "L"}   # Emp #, Employee, Total Hrs, Total Pay
    top = 1
    headers = [("A", "Emp. #"), ("B", "Employee"),
               ("C", "Shop\nHrs"), ("D", "Pool\nHrs"), ("E", "Service\nHrs"), ("F", "Office\nHrs"), ("G", "Total\nHrs"),
               ("H", "Shop $"), ("I", "Pool $"), ("J", "Service $"), ("K", "Office $"), ("L", "Total Pay")]
    widths = [8, 24, 8, 8, 9, 8, 9, 12, 12, 12, 12, 14]
    # Header: outer border of the band (top/bottom on all) + the framed verticals.
    for (col, text), w in zip(headers, widths):
        sides = {"top", "bottom"} | ({"left", "right"} if col in vcols else set())
        _put(ws, col, top, text, "General", _CENTER, _HFILL, sides, _HDR)
        ws.column_dimensions[col].width = w

    r = top
    tot = {k: 0.0 for k in ("shop_h", "pool_h", "svc_h", "off_h", "th",
                            "shop_d", "pool_d", "svc_d", "off_d", "tp")}
    for n, row in enumerate(rows, start=1):
        ch = row["category_hours"]
        shop_h, pool_h = ch[costing.SHOP], ch["Construction"]
        svc_h, off_h = ch["Service"], ch["Office"]
        total_h = shop_h + pool_h + svc_h + off_h
        dist = costing.distribute_pay(
            row["total"], {"Pool": pool_h, "Service": svc_h, "Office": off_h, "Shop": shop_h}, "Shop")
        r += 1
        fill = _WHITE if n % 2 == 1 else _ALT
        cells = [("A", n, "General", _CENTER), ("B", row["name"], "General", None),
                 ("C", shop_h, _HRS, _RIGHT), ("D", pool_h, _HRS, _RIGHT), ("E", svc_h, _HRS, _RIGHT),
                 ("F", off_h, _HRS, _RIGHT), ("G", round(total_h, 2), _HRS_TOTAL, _RIGHT),
                 ("H", dist["Shop"], _CUR, _RIGHT), ("I", dist["Pool"], _CUR, _RIGHT),
                 ("J", dist["Service"], _CUR, _RIGHT), ("K", dist["Office"], _CUR, _RIGHT),
                 ("L", row["total"], _CUR, _RIGHT)]
        for col, val, fmt, align in cells:
            sides = {"left", "right"} if col in vcols else set()
            _put(ws, col, r, val, fmt, align, fill, sides, zero_gray=True)
        tot["shop_h"] += shop_h; tot["pool_h"] += pool_h; tot["svc_h"] += svc_h
        tot["off_h"] += off_h; tot["th"] += total_h
        tot["shop_d"] += dist["Shop"]; tot["pool_d"] += dist["Pool"]
        tot["svc_d"] += dist["Service"]; tot["off_d"] += dist["Office"]; tot["tp"] += row["total"]
    r += 1
    # Total row: outer border of the band + framed verticals.
    total_cells = [("A", None, "General", None), ("B", "Totals", "General", _RIGHT),
                   ("C", round(tot["shop_h"], 2), _HRS, _RIGHT), ("D", round(tot["pool_h"], 2), _HRS, _RIGHT),
                   ("E", round(tot["svc_h"], 2), _HRS, _RIGHT), ("F", round(tot["off_h"], 2), _HRS, _RIGHT),
                   ("G", round(tot["th"], 2), _HRS_TOTAL, _RIGHT), ("H", round(tot["shop_d"], 2), _CUR, _RIGHT),
                   ("I", round(tot["pool_d"], 2), _CUR, _RIGHT), ("J", round(tot["svc_d"], 2), _CUR, _RIGHT),
                   ("K", round(tot["off_d"], 2), _CUR, _RIGHT), ("L", round(tot["tp"], 2), _CUR, _RIGHT)]
    for col, val, fmt, align in total_cells:
        sides = {"top", "bottom"} | ({"left", "right"} if col in vcols else set())
        _put(ws, col, r, val, fmt, align, _TOTAL_FILL, sides, _HDR)


def generate_hipp_invoice(file_path: str, rows: list[dict], period_label: str,
                          invoice_number: str | None = None) -> None:
    """Write the two-sheet Hipp invoice workbook to ``file_path``. ``rows`` is a list
    of costing.hipp_billing dicts. Blocking — run in a worker thread."""
    wb = Workbook()
    _build_invoice_sheet(wb.active, rows, period_label, invoice_number)
    _build_department_sheet(wb.create_sheet("Payroll by Department"), rows)
    wb.save(file_path)
