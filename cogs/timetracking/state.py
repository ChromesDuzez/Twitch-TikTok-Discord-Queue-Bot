"""Clock-state readers.

These functions are the single source of truth for "is this employee clocked
in / what worktime is open / what can they do". Everything is read from
SQLite, never reconstructed from Discord embed text (the old approach in
``on_ready`` that caused views to desync).
"""

from __future__ import annotations

from dataclasses import dataclass

from .db import Database


@dataclass
class ClockState:
    employee_id: int
    clocked_in: bool
    current_punch: int | None
    open_worktime: int | None
    open_worktime_type: str | None
    ignore_lunch: bool
    allow_construction: bool
    allow_service: bool
    allow_office: bool
    lunch_skipable: bool


def _as_bool(value) -> bool:
    """Legacy rows may store booleans as 'TRUE'/'FALSE' strings or 0/1."""
    if isinstance(value, str):
        return value.strip().upper() in ("TRUE", "1")
    return bool(value)


async def get_current_punch(db: Database, employee_id: int) -> int | None:
    """The id of the employee's open punch (clocked in), or None."""
    row = await db.fetchone(
        "SELECT id FROM punch_clock WHERE employeeID = ? AND punchOutTime IS NULL "
        "ORDER BY id DESC LIMIT 1",
        (employee_id,),
    )
    return row["id"] if row else None


async def load_state(db: Database, employee_id: int) -> ClockState:
    """Build the full ClockState for an employee straight from the database."""
    current_punch = await get_current_punch(db, employee_id)

    open_wt = open_wt_type = None
    ignore_lunch = False
    if current_punch is not None:
        wt = await db.fetchone(
            "SELECT id, punchType FROM work_time WHERE punchID = ? AND timeSpent = 0 "
            "ORDER BY id DESC LIMIT 1",
            (current_punch,),
        )
        if wt:
            open_wt, open_wt_type = wt["id"], wt["punchType"]

        lunch_row = await db.fetchone(
            "SELECT ignoreLunchBreak FROM punch_clock WHERE id = ?", (current_punch,)
        )
        if lunch_row:
            ignore_lunch = _as_bool(lunch_row["ignoreLunchBreak"])

    emp = await db.fetchone(
        """
        SELECT et.construction, et.service, et.office, e.lunchSkipable
        FROM employee e
        JOIN employee_type et ON e.employeeTypeID = et.id
        WHERE e.id = ?
        """,
        (employee_id,),
    )
    if emp is None:
        # Employee not in the table yet; grant nothing but don't crash.
        allow_c = allow_s = allow_o = lunch_skipable = False
    else:
        allow_c = _as_bool(emp["construction"])
        allow_s = _as_bool(emp["service"])
        allow_o = _as_bool(emp["office"])
        lunch_skipable = _as_bool(emp["lunchSkipable"])

    return ClockState(
        employee_id=employee_id,
        clocked_in=current_punch is not None,
        current_punch=current_punch,
        open_worktime=open_wt,
        open_worktime_type=open_wt_type,
        ignore_lunch=ignore_lunch,
        allow_construction=allow_c,
        allow_service=allow_s,
        allow_office=allow_o,
        lunch_skipable=lunch_skipable,
    )
