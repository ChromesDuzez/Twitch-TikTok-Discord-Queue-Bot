"""Odoo check-out ends a locally-open jobsite worktime (no stranded 0h line).

Exercises InboxWorker._reconcile_attendance / _close_open_worktimes.
"""
from datetime import datetime, timedelta

from cogs.timetracking.odoo.inbox import InboxWorker
from cogs.timetracking.odoo import sync
from _helpers import asynctest, db_ctx, add_punch, add_worktime, outbox

CHECK_IN_UTC = "2026-09-14 14:00:00"
CHECK_OUT_UTC = "2026-09-14 22:13:00"
END_LOCAL = sync.utc_str_to_local_str(CHECK_OUT_UTC)   # what we compare against
REC = {"employee_id": [50, "Tester"], "check_in": CHECK_IN_UTC, "check_out": CHECK_OUT_UTC}


def _dt(s):
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")


class _Client:
    def __init__(self, rec, loaded=True):
        self._rec, self.loaded = rec, loaded

    async def read_record(self, model, odoo_id, fields):
        return dict(self._rec, id=odoo_id)


class _Cog:
    def __init__(self, db, client):
        self.db, self.client = db, client


def _worker(db, rec=REC):
    w = InboxWorker(_Cog(db, _Client(rec)))

    async def _noop(*a, **k):
        pass

    w._refresh_employee_clock = _noop   # skip the Discord render
    return w


async def _seed_open(db, att=900, *, started, ptype="Construction", pout=None):
    """An open punch (odooId=att) carrying one open worktime."""
    await add_punch(db, att, odoo=att, pin=sync.utc_str_to_local_str(CHECK_IN_UTC), pout=pout)
    await add_worktime(db, att + 1, att, mins=0, ptype=ptype, started=started)
    return att, att + 1


async def _mins(db, wid):
    return (await db.fetchone("SELECT timeSpent FROM work_time WHERE id=?", (wid,)))["timeSpent"]


async def _punch_out(db, pid):
    return (await db.fetchone("SELECT punchOutTime FROM punch_clock WHERE id=?", (pid,)))["punchOutTime"]


@asynctest
async def test_open_worktime_closed_at_checkout_time():
    async with db_ctx() as db:
        started = (_dt(END_LOCAL) - timedelta(hours=3, minutes=13)).strftime("%Y-%m-%d %H:%M:%S")
        pid, wid = await _seed_open(db, started=started)
        await _worker(db)._reconcile_attendance(900)
        assert await _mins(db, wid) == 195            # 3h13m -> 3.25h
        assert ("worktime", wid, "create") in await outbox(db)
        assert await _punch_out(db, pid)


@asynctest
async def test_subquarter_span_floored_to_15():
    async with db_ctx() as db:
        started = (_dt(END_LOCAL) - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        _, wid = await _seed_open(db, started=started)
        await _worker(db)._reconcile_attendance(900)
        assert await _mins(db, wid) == 15


@asynctest
async def test_negative_span_clamped_to_15():
    async with db_ctx() as db:
        started = (_dt(END_LOCAL) + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        _, wid = await _seed_open(db, started=started)
        await _worker(db)._reconcile_attendance(900)
        assert await _mins(db, wid) == 15             # clamped, never negative / CHECK-violating


@asynctest
async def test_echoed_checkout_is_noop():
    async with db_ctx() as db:
        started = (_dt(END_LOCAL) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
        pid, wid = await _seed_open(db, started=started, pout=END_LOCAL)
        await db.execute("UPDATE work_time SET timeSpent=180 WHERE id=?", (wid,))  # already finished locally
        changed = await _worker(db)._reconcile_attendance(900)
        assert changed is False
        assert await _mins(db, wid) == 180
        assert ("worktime", wid, "create") not in await outbox(db)


@asynctest
async def test_checkout_without_open_worktime_just_closes_punch():
    async with db_ctx() as db:
        await add_punch(db, 901, odoo=901, pin=sync.utc_str_to_local_str(CHECK_IN_UTC))
        await _worker(db)._reconcile_attendance(901)
        assert await _punch_out(db, 901)
        assert await outbox(db, "worktime") == set()
