"""Costing engine: the reusable data layer behind every cost/pay report.

The recurring shape across all the owner's reports is: take an employee's hours
and pay over a period and slice them by work category. This module holds the pure,
testable primitives for that — hours per punch (net of lunch), category hours with
Shop as the remainder, weekly (>40) overtime, and the proportional
pay-distribution that both the Hipp department breakdown and (later) the salaried
cost split use.

Hours conventions mirror the existing weekly report (reports.py::_report_timecard_data):
a shift is (out - in) rounded to the nearest quarter-hour; a 0.5h lunch is deducted
when the net shift is >= 6h and the punch didn't skip lunch. Work categories are the
three stored punchTypes (Construction/Service/Office); **Shop is the remainder**
(net shift minus the categorized hours), never stored.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .db import Database
from .reports import convert_minutes_to_hours, round_to_quarter_hour
from . import pay

# The three stored work categories + the derived remainder.
WORK_CATEGORIES = ("Construction", "Service", "Office")
SHOP = "Shop"
_FMT = "%Y-%m-%d %H:%M:%S"


def punch_span(punch_in: str, punch_out: str | None, ignore_lunch) -> tuple[float, float, float]:
    """(gross_hrs, lunch_hrs, net_hrs) for one punch, matching the weekly report.

    Gross = (out - in) rounded to the nearest quarter hour, in hours. An open punch
    (no clock-out) is measured to now. Lunch = 0.5h when gross >= 6 and the punch
    didn't skip lunch, else 0. Net = gross - lunch."""
    start = datetime.strptime(punch_in, _FMT)
    end = datetime.strptime(punch_out, _FMT) if punch_out else datetime.now()
    gross = convert_minutes_to_hours(round_to_quarter_hour((end - start).total_seconds() / 60))
    lunch = 0.5 if not ignore_lunch and gross >= 6.0 else 0.0
    return gross, lunch, gross - lunch


async def _punches(db: Database, employee_id: int, start, end):
    """Punches for an employee whose clock-in falls in [start, end)."""
    return await db.fetchall(
        "SELECT id, punchInTime, punchOutTime, ignoreLunchBreak FROM punch_clock "
        "WHERE employeeID = ? AND punchInTime >= ? AND punchInTime < ? "
        "ORDER BY punchInTime",
        (employee_id, _as_str(start), _as_str(end)),
    )


def _as_str(d) -> str:
    return d if isinstance(d, str) else d.strftime(_FMT)


async def category_hours(db: Database, employee_id: int, start, end) -> dict[str, float]:
    """Hours by work category over [start, end): the three stored categories plus
    Shop as the per-punch remainder (net - Construction - Service - Office)."""
    out = {c: 0.0 for c in WORK_CATEGORIES}
    out[SHOP] = 0.0
    for p in await _punches(db, employee_id, start, end):
        _, _, net = punch_span(p["punchInTime"], p["punchOutTime"], p["ignoreLunchBreak"])
        rows = await db.fetchall(
            "SELECT punchType, SUM(timeSpent) AS mins FROM work_time "
            "WHERE punchID = ? AND detached = 0 GROUP BY punchType",
            (p["id"],),
        )
        categorized = 0.0
        for r in rows:
            hrs = (r["mins"] or 0) / 60
            if r["punchType"] in out:
                out[r["punchType"]] += hrs
                categorized += hrs
        out[SHOP] += max(0.0, net - categorized)  # remainder is never negative
    return out


def _ending_saturday(d: datetime) -> str:
    """Snap a datetime to its week-ending Saturday (YYYY-MM-DD)."""
    return (d + timedelta(days=(5 - d.weekday()) % 7)).strftime("%Y-%m-%d")


async def weekly_net_hours(db: Database, employee_id: int, start, end) -> dict[str, float]:
    """Net worked hours bucketed by ending-Saturday over [start, end)."""
    weeks: dict[str, float] = {}
    for p in await _punches(db, employee_id, start, end):
        _, _, net = punch_span(p["punchInTime"], p["punchOutTime"], p["ignoreLunchBreak"])
        wk = _ending_saturday(datetime.strptime(p["punchInTime"], _FMT))
        weeks[wk] = weeks.get(wk, 0.0) + net
    return weeks


def std_ot_split(weekly: dict[str, float], threshold: float = 40.0) -> tuple[float, float]:
    """Standard vs overtime hours: per week, hours up to `threshold` are standard,
    the rest are OT; summed across weeks."""
    std = sum(min(h, threshold) for h in weekly.values())
    ot = sum(max(h - threshold, 0.0) for h in weekly.values())
    return std, ot


def distribute_pay(total: float, hours_by_cat: dict[str, float], catchall: str) -> dict[str, float]:
    """Split `total` pay across categories proportionally by hours, rounding each to
    the cent, with `catchall` absorbing the remainder so the parts sum EXACTLY to
    `total`. This is the shared primitive behind the Hipp department breakdown and
    (later) the salaried cost distribution. A zero-hours input yields all-zero
    except the catch-all, which gets the full total."""
    total = round(float(total), 2)
    total_hours = sum(hours_by_cat.values())
    out = {c: 0.0 for c in hours_by_cat}
    if catchall not in out:
        out[catchall] = 0.0
    if total_hours <= 0:
        out[catchall] = total
        return out
    for cat, hrs in hours_by_cat.items():
        if cat == catchall:
            continue
        out[cat] = round(total * hrs / total_hours, 2)
    out[catchall] = round(total - sum(v for c, v in out.items() if c != catchall), 2)
    return out


def _round_rate(rate: float, round_up) -> float:
    """Truncate the billed rate to 2 decimals by default; round to nearest cent when
    the per-employee round-up flag is set (matches the Hipp template)."""
    if round_up:
        return round(rate, 2)
    return int(rate * 100) / 100.0  # truncate toward zero (rates are non-negative)


async def hipp_billing(db: Database, employee_id: int, start, end) -> dict:
    """Everything the Hipp invoice needs for one employee over [start, end).

    Std/OT hours come from weekly (>40) net hours. The billed rate marks up the
    employee's effective payrate by their employee_type.rate (Clerical 1.5,
    Construction 1.7); OT bills at that x1.5. Rates are truncated to 2 decimals
    unless the employee's rate_round_up flag is set. The line total rounds each
    component to the cent (round(std_hrs*std_rate) + round(ot_hrs*ot_rate)). Also
    returns category hours for the department-distribution sheet.
    """
    emp = await db.fetchone(
        "SELECT e.name, e.rate_round_up, et.rate AS type_rate "
        "FROM employee e JOIN employee_type et ON e.employeeTypeID = et.id WHERE e.id = ?",
        (employee_id,),
    )
    if emp is None:
        return {}
    pay_rec = await pay.effective_pay(db, employee_id, _as_str(start)[:10])
    base = float(pay_rec["hourly_rate"]) if pay_rec and pay_rec["hourly_rate"] is not None else 0.0
    type_rate = float(emp["type_rate"] or 1.0)

    std_hrs, ot_hrs = std_ot_split(await weekly_net_hours(db, employee_id, start, end))
    std_rate = _round_rate(base * type_rate, emp["rate_round_up"])
    ot_rate = _round_rate(base * type_rate * 1.5, emp["rate_round_up"])
    total = round(round(std_hrs * std_rate, 2) + round(ot_hrs * ot_rate, 2), 2)
    return {
        "employee_id": employee_id,
        "name": emp["name"],
        "std_hrs": round(std_hrs, 2),
        "ot_hrs": round(ot_hrs, 2),
        "std_rate": std_rate,
        "ot_rate": ot_rate,
        "total": total,
        "category_hours": await category_hours(db, employee_id, start, end),
    }
