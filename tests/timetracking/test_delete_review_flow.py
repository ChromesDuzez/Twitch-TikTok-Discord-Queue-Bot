"""DeletePunchFlow confirm: reassign chosen worktime, delete the rest, annotate + on_complete."""
import types

from cogs.timetracking.views import DeletePunchFlow
from _helpers import asynctest, db_ctx, add_punch, add_worktime, outbox


class _Msg:
    def __init__(s): s.content = "approval prompt"; s.deleted = False
    async def edit(s, **k): s.content = k.get("content", s.content)
    async def delete(s): s.deleted = True


class _User:
    id = 1
    def __str__(s): return "Admin"


class _Resp:
    async def defer(s, **k): pass


class _Inter:
    def __init__(s): s.user = _User(); s.response = _Resp(); s.edited = None
    async def edit_original_response(s, **k): s.edited = k


class _Cog:
    def __init__(s, db, msg): s.db = db; s._msg = msg
    async def obtain_message(s, cid, mid): return s._msg


def _confirm_button(flow):
    return next(c for c in flow.children if getattr(c, "label", None) == "Confirm")


@asynctest
async def test_confirm_reassigns_one_deletes_rest_and_annotates():
    async with db_ctx() as db:
        await add_punch(db, 100, odoo=4000)                                  # surviving shift
        await add_punch(db, 900, odoo=5000)                                  # being deleted
        await db.execute("UPDATE punch_clock SET checkChannelId=111, checkMessageId=222 WHERE id=900")
        await add_worktime(db, 200, 900, odoo=None, mins=120, ptype="Service")   # -> reassign to 100
        await add_worktime(db, 201, 900, odoo=None, mins=60, ptype="Construction")  # -> delete

        msg = _Msg()
        cog = _Cog(db, msg)
        wts = [{"id": 200, "punchType": "Service", "timeSpent": 120, "cname": None},
               {"id": 201, "punchType": "Construction", "timeSpent": 60, "cname": None}]
        cands = [{"id": 100, "punchInTime": "2026-11-02 08:00:00", "punchOutTime": "2026-11-02 16:00:00"}]
        seen = {}

        async def _on_complete(inter, reassigned):
            seen["reassigned"] = reassigned

        flow = DeletePunchFlow(cog, 900, wts, cands, 1,
                               cascade_to_odoo=True, annotate="\n🗑️ Admin deleted this shift",
                               on_complete=_on_complete)
        flow.decisions = {200: "punch:100", 201: "delete"}   # as if the admin picked them
        await _confirm_button(flow).callback(_Inter())

        # 200 moved to the surviving shift; 201 and the punch are gone
        row = await db.fetchone("SELECT punchID FROM work_time WHERE id=200")
        assert row is not None and row["punchID"] == 100
        assert await db.fetchone("SELECT 1 FROM work_time WHERE id=201") is None
        assert await db.fetchone("SELECT 1 FROM punch_clock WHERE id=900") is None
        # Odoo: attendance delete queued; the reassigned (unsynced) line queued to create
        ob = await outbox(db)
        assert ("punch", 900, "delete") in ob
        assert ("worktime", 200, "create") in ob
        assert ("worktime", 201, "delete") not in ob   # local-only, nothing in Odoo
        # approval message kept + annotated with the reassigned count; on_complete ran
        assert msg.deleted is False
        assert "deleted this shift" in msg.content and "1 worktime reassigned" in msg.content
        assert seen.get("reassigned") == 1
        flow.stop()
