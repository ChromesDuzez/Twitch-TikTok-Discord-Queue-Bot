"""Pay helpers: effective-dated pay records and the hybrid hour bank.

Pay is stored as effective-dated rows in ``pay_rate`` (a raise/cut/type-change is
just a new row), and the hybrid arrangement's banked hours live in ``hour_bank``
as a signed ledger. These are small, pure DB reads that the cost-of-work reports
will build on later; today they back the ``/payhistory`` / ``/bankbalance``
commands and are the single source of truth for "what was this person paid on
date X".

Amount columns are type-specific: ``hourly_rate`` for Hourly/Hybrid ($/hr),
``cycle_gross`` for Salaried (the bi-weekly gross). Exactly one is populated per
row, matched to that row's ``pay_type``.
"""

from __future__ import annotations

from datetime import datetime

from .db import Database


def _date_only(on_date) -> str:
    """Normalize a date or datetime (str or object) to a 'YYYY-MM-DD' string so it
    compares correctly against the DATE column."""
    if on_date is None:
        return datetime.now().strftime("%Y-%m-%d")
    if isinstance(on_date, str):
        return on_date[:10]
    return on_date.strftime("%Y-%m-%d")


async def effective_pay(db: Database, employee_id: int, on_date=None):
    """The pay record in effect for ``employee_id`` on ``on_date`` (defaults to
    today). Returns the row with the greatest ``effective_date <= on_date``; if
    ``on_date`` precedes the earliest record, falls back to that earliest record
    so no work is ever left uncosted. Returns None only when the employee has no
    pay records at all."""
    day = _date_only(on_date)
    row = await db.fetchone(
        "SELECT * FROM pay_rate WHERE employeeID = ? AND effective_date <= ? "
        "ORDER BY effective_date DESC, id DESC LIMIT 1",
        (employee_id, day),
    )
    if row is not None:
        return row
    # on_date is before the first record -> use the earliest one we have.
    return await db.fetchone(
        "SELECT * FROM pay_rate WHERE employeeID = ? "
        "ORDER BY effective_date ASC, id ASC LIMIT 1",
        (employee_id,),
    )


async def current_pay(db: Database, employee_id: int):
    """The pay record in effect today."""
    return await effective_pay(db, employee_id, None)


async def bank_balance(db: Database, employee_id: int) -> float:
    """Net banked hours for ``employee_id`` (sum of the signed ledger)."""
    row = await db.fetchone(
        "SELECT COALESCE(SUM(hours), 0) AS bal FROM hour_bank WHERE employeeID = ?",
        (employee_id,),
    )
    return float(row["bal"]) if row and row["bal"] is not None else 0.0
