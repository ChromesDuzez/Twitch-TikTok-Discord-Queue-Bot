"""/resync _queue_unpushed: sweep + target local-only items, respecting punch legacy."""
from cogs.timetracking.cog import TimeTracking
from _helpers import asynctest, db_ctx, add_punch, add_worktime, outbox

# _queue_unpushed never touches self, so an unbound call with a dummy self is fine.
queue = TimeTracking._queue_unpushed


async def _seed(db):
    await add_punch(db, 100, odoo=5000)             # synced parent
    await add_punch(db, 101, odoo=None)             # unsynced non-legacy standalone + parent of 205
    await add_punch(db, 102, odoo=None, legacy=1)   # legacy parent
    await add_worktime(db, 200, 100, mins=120)                   # eligible (parent synced)
    await add_worktime(db, 201, 100, mins=0)                     # zero hours -> skip
    await add_worktime(db, 202, 100, mins=120, project=None)     # no project -> skip
    await add_worktime(db, 203, 102, mins=120)                   # parent legacy -> skip
    await add_worktime(db, 204, 100, mins=120, odoo=9000)        # already synced -> skip
    await add_worktime(db, 205, 101, mins=120)                   # eligible; parent unsynced


@asynctest
async def test_full_sweep_queues_eligible_and_skips_rest():
    async with db_ctx() as db:
        await _seed(db)
        assert await queue(object(), db) == (1, 2)
        assert await outbox(db) == {
            ("punch", 101, "edit"), ("worktime", 200, "create"), ("worktime", 205, "create")}


@asynctest
async def test_sweep_is_idempotent():
    async with db_ctx() as db:
        await _seed(db)
        await queue(object(), db)
        assert await queue(object(), db) == (0, 0)


@asynctest
async def test_target_worktime_also_queues_unsynced_parent_punch():
    async with db_ctx() as db:
        await _seed(db)
        assert await queue(object(), db, only_worktime=205) == (1, 1)
        assert await outbox(db) == {("punch", 101, "edit"), ("worktime", 205, "create")}


@asynctest
async def test_target_legacy_punch_worktime_queues_nothing():
    async with db_ctx() as db:
        await _seed(db)
        assert await queue(object(), db, only_worktime=203) == (0, 0)
        assert await outbox(db) == set()


@asynctest
async def test_target_punch_queues_just_it_and_refuses_legacy():
    async with db_ctx() as db:
        await _seed(db)
        assert await queue(object(), db, only_punch=101) == (1, 0)
        assert await outbox(db) == {("punch", 101, "edit")}
        assert await queue(object(), db, only_punch=102) == (0, 0)
