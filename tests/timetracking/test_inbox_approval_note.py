"""Odoo updating a punch time annotates its live approval message in the admin channel."""
import types

from cogs.timetracking.odoo.inbox import InboxWorker
from cogs.timetracking.odoo import sync
from _helpers import asynctest, new_db

CHECK_IN_UTC, CHECK_OUT_UTC = "2026-10-01 14:00:00", "2026-10-01 22:00:00"
NEW_IN = sync.utc_str_to_local_str(CHECK_IN_UTC)
NEW_OUT = sync.utc_str_to_local_str(CHECK_OUT_UTC)
REC = {"employee_id": [50, "Tester"], "check_in": CHECK_IN_UTC, "check_out": CHECK_OUT_UTC}


class _Msg:
    def __init__(s): s.content = "⚠️ non-standard clock-in — approve?"; s.edits = 0
    async def edit(s, **k): s.content = k.get("content", s.content); s.edits += 1


class _Client:
    async def read_record(s, model, odoo_id, fields): return dict(REC, id=odoo_id)


def _worker(db, msg):
    cog = types.SimpleNamespace(db=db, client=_Client())

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
async def test_annotates_approval_message_on_odoo_time_change():
    db = await new_db(employee_odoo=50)
    try:
        await _punch(db)                       # existing punch, different times, has a live approval msg
        msg = _Msg()
        w, cog = _worker(db, msg)
        await w._reconcile_attendance(900)
        assert msg.edits == 1, "approval message should be annotated"
        assert "☁️ Odoo updated" in msg.content
        assert "clock-in" in msg.content and NEW_IN in msg.content
        assert "clock-out" in msg.content and NEW_OUT in msg.content
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
