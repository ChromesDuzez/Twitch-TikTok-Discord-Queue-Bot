"""/editpunch can toggle a punch's ignoreLunchBreak (admin pays the lunch back)."""
import types

from cogs.timetracking.cog import TimeTracking
from _helpers import asynctest, db_ctx

EDIT = TimeTracking.editpunch.callback


class _Ctx:
    def __init__(s): s.responses = []; s.author = types.SimpleNamespace(id=1)
    async def defer(s, **k): pass
    async def respond(s, msg, **k): s.responses.append(msg)


def _self(db):
    s = types.SimpleNamespace()
    async def _ensure_db(): return db
    async def _refresh(e): pass
    s._ensure_db = _ensure_db
    s._eph = lambda ctx: False
    s._refresh_clock = _refresh
    return s


async def _seed_punch(db, lunch=0):
    await db.execute(
        "INSERT INTO punch_clock (id,employeeID,punchInTime,punchOutTime,punchInApproval,punchOutApproval,ignoreLunchBreak) "
        "VALUES (900,1,'2026-11-02 08:00:00','2026-11-02 16:30:00',1,1,?)", (lunch,))


async def _call(db, **kw):
    ctx = _Ctx()
    opts = dict(punch="900", clock_in=None, clock_out=None, approved=None, ignore_lunch=None)
    opts.update(kw)
    await EDIT(_self(db), ctx, **opts)
    return ctx


async def _lunch(db):
    return (await db.fetchone("SELECT ignoreLunchBreak FROM punch_clock WHERE id=900"))["ignoreLunchBreak"]


@asynctest
async def test_ignore_lunch_true_pays_it_back():
    async with db_ctx() as db:
        await _seed_punch(db, lunch=0)
        await _call(db, ignore_lunch=True)
        assert await _lunch(db) == 1


@asynctest
async def test_ignore_lunch_false_deducts():
    async with db_ctx() as db:
        await _seed_punch(db, lunch=1)
        await _call(db, ignore_lunch=False)
        assert await _lunch(db) == 0


@asynctest
async def test_blank_leaves_lunch_untouched_and_reports_nothing_to_change():
    async with db_ctx() as db:
        await _seed_punch(db, lunch=1)
        ctx = await _call(db)   # no fields at all
        assert await _lunch(db) == 1
        assert any("Nothing to change" in m for m in ctx.responses)


@asynctest
async def test_lunch_change_is_independent_of_times():
    async with db_ctx() as db:
        await _seed_punch(db, lunch=0)
        await _call(db, ignore_lunch=True)
        row = await db.fetchone("SELECT punchInTime, punchOutTime FROM punch_clock WHERE id=900")
        assert row["punchInTime"] == "2026-11-02 08:00:00"    # times untouched
        assert row["punchOutTime"] == "2026-11-02 16:30:00"
