"""Manual push of an abandoned (0h) worktime to Odoo as a flagged placeholder.

Covers the description flag, the per-id enqueue (allow_zero payload + parent punch),
the eligibility rejections, and the sync-layer gating (0h posts ONLY with allow_zero
on a finished shift — never incidentally).
"""
import json

from cogs.timetracking.odoo.sync import SyncWorker, worktime_description
from cogs.timetracking.cog import TimeTracking
from _helpers import asynctest, db_ctx, add_punch, add_worktime, outbox

push_one = TimeTracking._push_abandoned_worktime   # unbound; never touches self
CLOSED = "2026-09-11 17:00:00"


class _Client:
    """Records add_timesheet calls; loaded so SyncWorker proceeds."""
    def __init__(self):
        self.calls = []
        self.loaded = True

    async def add_timesheet(self, **kw):
        self.calls.append(kw)
        return 7777


def test_description_flags_zero_hours_only():
    zero = worktime_description("Service", 0)
    assert zero.startswith("Service work (Discord timecard)")
    assert "abandoned" in zero.lower()
    assert "abandoned" not in worktime_description("Service", 120).lower()


@asynctest
async def test_push_enqueues_create_with_allow_zero():
    async with db_ctx() as db:
        await add_punch(db, 100, odoo=5000, pout=CLOSED)
        await add_worktime(db, 200, 100, mins=0, project=29)
        assert await push_one(object(), db, 200) is None
        row = await db.fetchone(
            "SELECT op, payload FROM odoo_outbox WHERE entity_type='worktime' AND entity_id=200")
        assert row["op"] == "create"
        assert json.loads(row["payload"]).get("allow_zero") is True


@asynctest
async def test_push_queues_unsynced_parent_punch_too():
    async with db_ctx() as db:
        await add_punch(db, 101, odoo=None, pout=CLOSED)   # parent not yet in Odoo
        await add_worktime(db, 201, 101, mins=0, project=29)
        assert await push_one(object(), db, 201) is None
        ob = await outbox(db)
        assert ("punch", 101, "edit") in ob and ("worktime", 201, "create") in ob


@asynctest
async def test_push_rejects_ineligible():
    async with db_ctx() as db:
        await add_punch(db, 100, odoo=5000, pout=CLOSED)
        await add_punch(db, 103, odoo=200, pout=None)              # open shift
        await add_punch(db, 102, odoo=None, legacy=1, pout=CLOSED)  # legacy
        await add_worktime(db, 300, 100, mins=0, project=29, odoo=9000)   # already synced
        await add_worktime(db, 301, 103, mins=0, project=29)              # open shift
        await add_worktime(db, 302, 102, mins=0, project=29)              # legacy punch
        await add_worktime(db, 303, 100, mins=120, project=29)            # has hours
        await add_worktime(db, 304, 100, mins=0, project=None)            # no project
        assert "already in Odoo" in await push_one(object(), db, 300)
        assert "still open" in await push_one(object(), db, 301)
        assert "legacy" in await push_one(object(), db, 302)
        assert "has hours" in await push_one(object(), db, 303)
        assert "no Odoo project" in await push_one(object(), db, 304)
        assert "doesn't exist" in await push_one(object(), db, 999)
        assert await outbox(db) == set()   # nothing enqueued for any rejection


@asynctest
async def test_sync_posts_zero_only_with_allow_zero_on_finished_shift():
    async with db_ctx() as db:
        await add_punch(db, 100, odoo=5000, pout=CLOSED)
        await add_worktime(db, 200, 100, mins=0, project=29, task=6950)
        client = _Client()
        w = SyncWorker(db, client)
        # normal create path never posts a 0h line
        assert await w._sync_worktime(200) is True
        assert client.calls == []
        # explicit allow_zero on a finished shift posts a 0h "abandoned" line
        assert await w._sync_worktime(200, allow_zero=True) is True
        assert len(client.calls) == 1 and client.calls[0]["hours"] == 0.0
        assert "abandoned" in client.calls[0]["description"].lower()
        assert (await db.fetchone("SELECT odooId FROM work_time WHERE id=200"))["odooId"] == 7777


@asynctest
async def test_sync_never_posts_zero_on_open_shift_even_with_allow_zero():
    async with db_ctx() as db:
        await add_punch(db, 103, odoo=200, pout=None)   # still open
        await add_worktime(db, 201, 103, mins=0, project=29)
        client = _Client()
        assert await SyncWorker(db, client)._sync_worktime(201, allow_zero=True) is True
        assert client.calls == []
