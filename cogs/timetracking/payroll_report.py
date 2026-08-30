"""Payroll-distribution workbooks (insurance-audit Weekly / Monthly reports).

Both reports are the same grid — one row per employee, columns = the pay categories
(Officer / Pool / Service / Shop / Office / Bonus) + Total Pay — differing only in
the title/subtitle and which pay runs feed them. The caller (cog.py) computes each
employee's category dollars via ``costing.run_distribution`` and passes plain row
dicts; this module only lays them out (on ``xlsxstyle``). Blocking; call via
asyncio.to_thread.

Row dict keys: ``name`` plus the six canonical categories
(officer/pool/service/shop/office/bonus). Total is summed here.
"""

from __future__ import annotations

from openpyxl import Workbook

from .xlsxstyle import (ALT, BODY, CENTER, CUR, HDR, HFILL, MID, RIGHT, ROW_SHIFT, TITLE,
                        TOTAL_FILL, WHITE, addr, apply_modern_theme, cell, finalize, put, shift_col)

# report column -> (header text with GL-code subtitle, row-dict key)
_COLS = [
    ("A", "Employee", "name"),
    ("B", "Officer\n0-655-0", "officer"),
    ("C", "Pool\n1-508-0", "pool"),
    ("D", "Service\n3-508-0", "service"),
    ("E", "Shop\n2-508-0", "shop"),
    ("F", "Office\n0-658-0", "office"),
    ("G", "Bonus\nHol/Vac/Sick", "bonus"),
    ("H", "Total\nPay", "total"),
]
_MONEY_COLS = ("B", "C", "D", "E", "F", "G", "H")
_WIDTHS = {"A": 26, "B": 11, "C": 11, "D": 11, "E": 11, "F": 11, "G": 13, "H": 13}
_VCOLS = {"A", "H"}   # vertical borders framing Employee + Total Pay


def _build(ws, sheet_title, title, subtitle, rows):
    ws.title = sheet_title
    cell(ws, "A", 1, title, TITLE)
    cell(ws, "A", 2, subtitle, BODY)

    top = 4
    for col, text, _ in _COLS:
        sides = {"top", "bottom"} | ({"left", "right"} if col in _VCOLS else set())
        put(ws, col, top, text, "General", CENTER, HFILL, sides, HDR)
        ws.column_dimensions[shift_col(col)].width = _WIDTHS[col]

    tot = {key: 0.0 for _, _, key in _COLS if key != "name"}
    r = top
    for n, row in enumerate(rows, start=1):
        r += 1
        fill = WHITE if n % 2 == 1 else ALT
        total = round(sum(float(row.get(k, 0.0)) for _, _, k in _COLS if k not in ("name", "total")), 2)
        for col, _, key in _COLS:
            sides = {"left", "right"} if col in _VCOLS else set()
            if key == "name":
                put(ws, col, r, row["name"], "General", None, fill, sides)
            else:
                val = total if key == "total" else float(row.get(key, 0.0))
                put(ws, col, r, val, CUR, MID, fill, sides, zero_gray=True)
                tot[key] += val

    r += 1
    for col, _, key in _COLS:
        sides = {"top", "bottom"} | ({"left", "right"} if col in _VCOLS else set())
        if key == "name":
            put(ws, col, r, "Totals", "General", RIGHT, TOTAL_FILL, sides, HDR)
        else:
            put(ws, col, r, round(tot[key], 2), CUR, MID, TOTAL_FILL, sides, HDR)
    finalize(ws, header_last_row=top, last_col="H", last_row=r + ROW_SHIFT, landscape=False)


def generate_payroll_distribution(file_path, sheet_title, title, subtitle, rows):
    """Write a single-sheet payroll-distribution workbook. ``rows`` is a list of
    {name, officer, pool, service, shop, office, bonus} dicts. Blocking."""
    wb = Workbook()
    apply_modern_theme(wb)
    _build(wb.active, sheet_title, title, subtitle, rows)
    wb.save(file_path)
