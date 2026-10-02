"""/abandonedworktime: list 0h worktimes stranded on a finished, non-legacy shift."""
from cogs.timetracking.cog import TimeTracking
from _helpers import asynctest, db_ctx, add_punch, add_worktime

# _abandoned_worktimes never touches self; call unbound with a dummy self.
find = TimeTracking._abandoned_worktimes

CLOSED = "2026-09-11 17:00:00"   # punchOutTime for a finished shift


async def _seed(db):
    # finished shifts
    await add_punch(db, 100, odoo=5000, pout=CLOSED)
    await add_punch(db, 102, odoo=None, legacy=1, pout=CLOSED)   # legacy
    await add_punch(db, 103, odoo=200, pout=None)                # still open
    # worktimes
    await add_worktime(db, 200, 100, mins=0, ptype="Service", started="2026-09-11 16:36:27")   # abandoned
    await add_worktime(db, 201, 100, mins=0, ptype="Construction", started="2026-08-01 09:00:00")  # abandoned (older)
    await add_worktime(db, 202, 100, mins=120, started="2026-09-11 13:00:00")                   # has hours -> not abandoned
    await add_worktime(db, 203, 102, mins=0, started="2026-09-11 10:00:00")                     # legacy punch -> excluded
    await add_worktime(db, 204, 103, mins=0, started="2026-09-11 16:00:00")                     # open shift -> excluded


@asynctest
async def test_lists_only_zero_hour_closed_nonlegacy():
    async with db_ctx() as db:
        await _seed(db)
        ids = [r["id"] for r in await find(object(), db)]
        assert ids == [200, 201], ids   # newest-first, excludes hours/legacy/open


@asynctest
async def test_since_filter():
    async with db_ctx() as db:
        await _seed(db)
        ids = [r["id"] for r in await find(object(), db, since="2026-09-01")]
        assert ids == [200], ids        # 201 is from August


@asynctest
async def test_employee_filter_and_empty_result():
    async with db_ctx() as db:
        await _seed(db)
        # employee 1 owns all; a different employee id yields nothing
        assert [r["id"] for r in await find(object(), db, employee_id=1)] == [200, 201]
        assert await find(object(), db, employee_id=999) == []


@asynctest
async def test_limit():
    async with db_ctx() as db:
        await _seed(db)
        assert len(await find(object(), db, limit=1)) == 1
