"""/editworktime: push gate (local-only non-legacy), customer block, task/project rules."""
import types

from cogs.timetracking.cog import TimeTracking
from _helpers import asynctest, db_ctx, add_punch, add_worktime, outbox

EDIT = TimeTracking.editworktime.callback  # raw coroutine behind the slash command


class _Client:
    def __init__(self, loaded=True):
        self.loaded = loaded

    async def read_record(self, model, odoo_id, fields):
        if model == "project.project":
            return {"id": odoo_id, "partner_id": [555, "TestCust"]} if odoo_id in (29, 40) else None
        if model == "project.task":
            proj = {900: 29, 901: 40}.get(odoo_id)   # task 900 in project 29, task 901 in project 40
            return None if proj is None else {"id": odoo_id, "project_id": [proj, "P"], "partner_id": [555, "TestCust"]}
        return None


class _Ctx:
    def __init__(self):
        self.responses = []
        self.author = types.SimpleNamespace(id=1)

    async def defer(self, **k):
        pass

    async def respond(self, msg, **k):
        self.responses.append(msg)


def _self(db, loaded=True):
    s = types.SimpleNamespace()
    s.client = _Client(loaded)

    async def _ensure_db():
        return db

    async def _refresh_clock(e):
        pass

    s._ensure_db = _ensure_db
    s._eph = lambda ctx: False
    s._refresh_clock = _refresh_clock
    s._resolve_worktime_link = TimeTracking._resolve_worktime_link.__get__(s)
    return s


async def _call(db, *, loaded=True, **kw):
    ctx = _Ctx()
    opts = dict(worktime=None, worktype=None, hours=None, customer=None, task=None, project=None, punch=None)
    opts.update(kw)
    await EDIT(_self(db, loaded), ctx, **opts)
    return ctx


async def _row(db, wid):
    return await db.fetchone(
        "SELECT timeSpent,customerID,odooProjectId,odooTaskId FROM work_time WHERE id=?", (wid,))


@asynctest
async def test_local_only_nonlegacy_worktime_pushes():
    async with db_ctx() as db:
        await add_punch(db, 100, odoo=5000)
        await add_worktime(db, 200, 100, mins=0, project=29)
        await _call(db, worktime="200", hours=2.0)
        assert (await _row(db, 200))["timeSpent"] == 120
        assert ("worktime", 200, "edit") in await outbox(db)


@asynctest
async def test_legacy_punch_worktime_edits_locally_but_never_pushes():
    async with db_ctx() as db:
        await add_punch(db, 102, odoo=None, legacy=1)
        await add_worktime(db, 203, 102, mins=0, project=29)
        await _call(db, worktime="203", hours=2.0)
        assert (await _row(db, 203))["timeSpent"] == 120   # local edit still applies
        assert await outbox(db, "worktime") == set()        # but nothing is pushed


@asynctest
async def test_customer_change_blocked_when_odoo_connected():
    async with db_ctx() as db:
        await add_punch(db, 100, odoo=5000)
        await add_worktime(db, 200, 100, mins=60, customer=0)
        ctx = await _call(db, worktime="200", customer="7")
        assert any("follows its Odoo task/project" in m for m in ctx.responses), ctx.responses
        assert (await _row(db, 200))["customerID"] == 0
        assert await outbox(db, "worktime") == set()


@asynctest
async def test_project_only_sets_project_clears_task_and_follows_customer():
    async with db_ctx() as db:
        await add_punch(db, 100, odoo=5000)
        await add_worktime(db, 200, 100, mins=60, project=29, task=900)
        await _call(db, worktime="200", project="40")
        r = await _row(db, 200)
        assert r["odooProjectId"] == 40 and r["odooTaskId"] is None and r["customerID"] == 7


@asynctest
async def test_project_plus_task_mismatch_is_rejected():
    async with db_ctx() as db:
        await add_punch(db, 100, odoo=5000)
        await add_worktime(db, 200, 100, mins=60, project=29)
        ctx = await _call(db, worktime="200", project="29", task="901")   # 901 lives in project 40
        assert any("isn't in the selected project" in m for m in ctx.responses), ctx.responses
        assert (await _row(db, 200))["odooTaskId"] is None


@asynctest
async def test_project_plus_matching_task_sets_both():
    async with db_ctx() as db:
        await add_punch(db, 100, odoo=5000)
        await add_worktime(db, 200, 100, mins=60, project=29)
        await _call(db, worktime="200", project="29", task="900")        # 900 lives in project 29
        r = await _row(db, 200)
        assert r["odooProjectId"] == 29 and r["odooTaskId"] == 900


@asynctest
async def test_task_only_validated_against_current_project():
    async with db_ctx() as db:
        await add_punch(db, 100, odoo=5000)
        await add_worktime(db, 200, 100, mins=60, project=29)
        await _call(db, worktime="200", task="900")                      # 900 in current project 29
        assert (await _row(db, 200))["odooTaskId"] == 900


@asynctest
async def test_task_only_mismatch_is_rejected():
    async with db_ctx() as db:
        await add_punch(db, 100, odoo=5000)
        await add_worktime(db, 200, 100, mins=60, project=29)
        ctx = await _call(db, worktime="200", task="901")                # 901 not in project 29
        assert any("isn't in this worktime's project" in m for m in ctx.responses), ctx.responses
