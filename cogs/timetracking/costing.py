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
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

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
    total = round_cents(float(total))
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
        out[cat] = round_cents(total * hrs / total_hours)
    out[catchall] = round_cents(total - sum(v for c, v in out.items() if c != catchall))
    return out


def round_cents(x: float) -> float:
    """Round money to 2 decimals, half away from zero — matching Excel's ROUND, and
    float-safe. Python's round() both uses banker's rounding AND is fooled by binary
    float noise (13.75*54.82 is stored as 753.77499999…, so round() gives 753.77);
    trimming to 6 decimals first, then Decimal-rounding half-up, yields 753.78 like
    the spreadsheet. Use this for every money figure so pennies match the reports."""
    return float(Decimal(str(round(x, 6))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _round_rate(rate: float, do_round) -> float:
    """Reduce a billed rate to 2 decimals. ``do_round`` truthy → normal rounding
    (half away from zero, like Excel ROUND); falsy → truncate (drop the extra
    digits). Uses Decimal on a noise-trimmed value so binary float artifacts (e.g.
    18*1.7 == 30.599999999999998) don't mis-truncate 30.60 to 30.59."""
    d = Decimal(str(round(rate, 6)))  # str(round(...)) strips float noise
    mode = ROUND_HALF_UP if do_round else ROUND_DOWN
    return float(d.quantize(Decimal("0.01"), rounding=mode))


async def hipp_billing(db: Database, employee_id: int, start, end) -> dict:
    """Everything the Hipp invoice needs for one employee over [start, end).

    Std/OT hours come from weekly (>40) net hours. The billed rate marks up the
    employee's CURRENT payrate by their employee_type.rate (Clerical 1.5,
    Construction 1.7); OT bills at that x1.5. Each billed rate is truncated to 2
    decimals unless that employee's per-rate rounding flag is set (std_rate_round
    for the standard rate, ot_rate_round for OT), in which case it rounds normally.
    The line total rounds each component to the cent (round(std_hrs*std_rate) +
    round(ot_hrs*ot_rate)). Also returns category hours for the department sheet.
    """
    emp = await db.fetchone(
        "SELECT e.name, e.std_rate_round, e.ot_rate_round, et.rate AS type_rate "
        "FROM employee e JOIN employee_type et ON e.employeeTypeID = et.id WHERE e.id = ?",
        (employee_id,),
    )
    if emp is None:
        return {}
    # Bill at the employee's CURRENT standard rate (what's set for them now), not
    # the rate that happened to be in effect during the work period -- an invoice
    # goes out at today's contracted rate. Hybrid stores its rate in hourly_rate
    # too, so this covers Hourly and Hybrid; a Salaried record has no hourly rate.
    pay_rec = await pay.current_pay(db, employee_id)
    base = float(pay_rec["hourly_rate"]) if pay_rec and pay_rec["hourly_rate"] is not None else 0.0
    type_rate = float(emp["type_rate"] or 1.0)

    std_hrs, ot_hrs = std_ot_split(await weekly_net_hours(db, employee_id, start, end))
    std_rate = _round_rate(base * type_rate, emp["std_rate_round"])
    ot_rate = _round_rate(base * type_rate * 1.5, emp["ot_rate_round"])
    # Mirror the invoice: SUM(ROUND(std_hrs*std_rate,2), ROUND(ot_hrs*ot_rate,2)).
    total = round_cents(round_cents(std_hrs * std_rate) + round_cents(ot_hrs * ot_rate))
    return {
        "employee_id": employee_id,
        "name": emp["name"],
        "base_rate": round(base, 2),   # the employee's current standard hourly rate
        "std_hrs": round(std_hrs, 2),
        "ot_hrs": round(ot_hrs, 2),
        "std_rate": std_rate,
        "ot_rate": ot_rate,
        "total": total,
        "category_hours": await category_hours(db, employee_id, start, end),
    }
