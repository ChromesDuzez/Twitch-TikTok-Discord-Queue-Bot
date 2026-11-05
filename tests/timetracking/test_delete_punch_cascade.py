"""delete_punch_cascade: cascades worktime deletes to Odoo (no orphans), honors
remove_message, and keeps detached worktime's Odoo line."""
import types

from cogs.timetracking.views import delete_punch_cascade
from cogs.timetracking.odoo import sync
from _helpers import asynctest, db_ctx, add_punch, add_worktime, outbox


class _Msg:
    def __init__(s): s.deleted = False; s.content = "approval prompt"
    async def delete(s): s.deleted = True
    async def edit(s, **k): s.content = k.get("content", s.content)


class _Cog:
    def __init__(s, db): s.db = db; s.obtained = None; s.msg = _Msg()
    async def obtain_message(s, cid, mid): s.obtained = (cid, mid); return s.msg


async def _seed(db):
    # punch synced to Odoo, with a live approval message
    await db.execute(
        "INSERT INTO punch_clock (id,employeeID,punchInTime,punchOutTime,punchInApproval,punchOutApproval,"
        "odooId,checkChannelId,checkMessageId) VALUES (900,1,'2026-11-02 08:00:00','2026-11-02 16:00:00',0,0,5000,111,222)")
    await add_worktime(db, 200, 900, odoo=7001, mins=120)                 # synced -> Odoo line deleted
    await add_worktime(db, 201, 900, odoo=None, mins=60)                  # local-only -> nothing in Odoo
    await add_worktime(db, 202, 900, odoo=7002, mins=90)
    await db.execute("UPDATE work_time SET detached=1 WHERE id=202")      # detached -> Odoo line KEPT
    # pre-existing pending non-delete pushes that should be cancelled
    await sync.enqueue(db, "punch", 900, "edit")
    await sync.enqueue(db, "worktime", 200, "create")


async def _status(db, etype, eid, op):
    row = await db.fetchone(
        "SELECT status FROM odoo_outbox WHERE entity_type=? AND entity_id=? AND op=?", (etype, eid, op))
    return row["status"] if row else None


@asynctest
async def test_cascade_deletes_odoo_lines_then_attendance_no_orphans():
    async with db_ctx() as db:
        await _seed(db)
        cog = _Cog(db)
        emp = await delete_punch_cascade(cog, 900, to_odoo=True, annotate="\n🗑️ deleted.")
        assert emp == 1
        # local rows gone
        assert await db.fetchone("SELECT 1 FROM punch_clock WHERE id=900") is None
        assert await db.fetchall("SELECT 1 FROM work_time WHERE punchID=900") == []
        ob = await outbox(db)
        # synced worktime + the attendance get Odoo deletes (lines before attendance)
        assert ("worktime", 200, "delete") in ob
        assert ("punch", 900, "delete") in ob
        # local-only and DETACHED worktimes are NOT deleted in Odoo
        assert ("worktime", 201, "delete") not in ob
        assert ("worktime", 202, "delete") not in ob
        # the earlier pending non-delete pushes were cancelled
        assert await _status(db, "punch", 900, "edit") == "skipped"
        assert await _status(db, "worktime", 200, "create") == "skipped"


@asynctest
async def test_annotate_keeps_and_edits_the_approval_message():
    async with db_ctx() as db:
        await _seed(db)
        cog = _Cog(db)
        await delete_punch_cascade(cog, 900, to_odoo=True, annotate="\n🗑️ gone.")
        # message kept + edited (not deleted)
        assert cog.obtained == (111, 222)
        assert cog.msg.deleted is False
        assert "🗑️ gone." in cog.msg.content


@asynctest
async def test_no_annotate_deletes_the_approval_message():
    async with db_ctx() as db:
        await _seed(db)
        cog = _Cog(db)
        await delete_punch_cascade(cog, 900, to_odoo=True)   # annotate=None -> delete message
        assert cog.obtained == (111, 222) and cog.msg.deleted is True


@asynctest
async def test_offline_delete_enqueues_nothing_but_removes_local():
    async with db_ctx() as db:
        await add_punch(db, 901, odoo=None)
        await add_worktime(db, 300, 901, odoo=None, mins=30)
        cog = _Cog(db)
        emp = await delete_punch_cascade(cog, 901, to_odoo=False, annotate="\n🗑️ gone.")
        assert emp == 1
        assert await db.fetchone("SELECT 1 FROM punch_clock WHERE id=901") is None
        assert await outbox(db) == set()
