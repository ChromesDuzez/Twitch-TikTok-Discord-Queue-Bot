"""Odoo updating a punch time annotates its live approval message in the admin channel."""
import types

from cogs.timetracking.odoo.inbox import InboxWorker
from cogs.timetracking.odoo import sync
from _helpers import asynctest, new_db

CHECK_IN_UTC, CHECK_OUT_UTC = "2026-10-01 14:00:00", "2026-10-01 22:00:00"
NEW_IN = sync.utc_str_to_local_str(CHECK_IN_UTC)
NEW_OUT = sync.utc_str_to_local_str(CHECK_OUT_UTC)
# write_uid [res.users id, name]; hr_emp = the hr.employee id user_id maps to (or None).
REC = {"employee_id": [50, "Tester"], "check_in": CHECK_IN_UTC, "check_out": CHECK_OUT_UTC,
       "write_uid": [7, "Admin User"]}


class _Msg:
    def __init__(s): s.content = "⚠️ non-standard clock-in — approve?"; s.edits = 0
    async def edit(s, **k): s.content = k.get("content", s.content); s.edits += 1


class _Client:
    def __init__(s, rec=REC, hr_emp=None): s._rec = rec; s._hr_emp = hr_emp
    async def read_record(s, model, odoo_id, fields): return dict(s._rec, id=odoo_id)
    async def call(s, endpoint, data):
        # Only /hr.employee/search_read (editor mapping) is exercised here.
        return [{"id": s._hr_emp}] if s._hr_emp is not None else []


def _worker(db, msg, client=None):
    cog = types.SimpleNamespace(db=db, client=client or _Client())

    async def _obtain(cid, mid): cog.obtained = (cid, mid); return msg
    cog.obtain_message = _obtain
    cog.obtained = None
    w = InboxWorker(cog)

    async def _noop(*a, **k): pass
    w._refresh_employee_clock = _noop
    return w, cog


async def _punch(db, *, check_ids=True, pin="2026-01-01 00:00:00", pout="2026-01-01 08:00:00"):
    cc, cm = (555, 999) if check_ids else (None, None)
    await db.execute(
        "INSERT INTO punch_clock (id,employeeID,punchInTime,punchOutTime,punchInApproval,punchOutApproval,"
        "odooId,checkChannelId,checkMessageId) VALUES (900,1,?,?,0,0,900,?,?)",
        (pin, pout, cc, cm))


@asynctest
async def test_annotates_with_odoo_name_when_editor_not_linked():
    db = await new_db(employee_odoo=50)
    try:
        await _punch(db)                       # existing punch, different times, has a live approval msg
        msg = _Msg()
        w, cog = _worker(db, msg)              # default client: editor maps to no local employee
        await w._reconcile_attendance(900)
        assert msg.edits == 1, "approval message should be annotated"
        assert "Admin User updated this in Odoo" in msg.content   # falls back to the Odoo write_uid name
        assert "clock-in" in msg.content and NEW_IN in msg.content
        assert "clock-out" in msg.content and NEW_OUT in msg.content
    finally:
        await db.close()


@asynctest
async def test_annotates_with_discord_mention_when_editor_linked():
    db = await new_db(employee_odoo=50)        # local employee 1 <-> hr.employee 50
    try:
        await _punch(db)
        msg = _Msg()
        # Odoo editor's res.users -> hr.employee id 50 -> local employee 1.
        w, cog = _worker(db, msg, client=_Client(hr_emp=50))
        await w._reconcile_attendance(900)
        assert "<@1> updated this in Odoo" in msg.content          # relates to the linked Discord account
    finally:
        await db.close()


@asynctest
async def test_no_approval_message_means_no_annotation():
    db = await new_db(employee_odoo=50)
    try:
        await _punch(db, check_ids=False)      # no live approval message
        msg = _Msg()
        w, cog = _worker(db, msg)
        await w._reconcile_attendance(900)
        assert msg.edits == 0 and cog.obtained is None
    finally:
        await db.close()


@asynctest
async def test_echo_no_change_no_annotation():
    db = await new_db(employee_odoo=50)
    try:
        await _punch(db, pin=NEW_IN, pout=NEW_OUT)   # already matches Odoo -> echo, nothing changes
        msg = _Msg()
        w, cog = _worker(db, msg)
        changed = await w._reconcile_attendance(900)
        assert changed is False and msg.edits == 0
    finally:
        await db.close()
