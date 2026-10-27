"""Per-employee auto-clock-out: trigger math, the due-sweep, and the clock-out action."""
import os
import types
from datetime import datetime

import pytest

import cogs.timetracking.cog as cogmod
from cogs.timetracking.cog import (
    TimeTracking, _auto_clockout_trigger, _parse_time_of_day, _fmt_minutes,
)
from _helpers import asynctest, db_ctx, add_worktime, outbox

EMP = "phoneNumber,addressLine1,addressCity,addressState,addressZip"


def _dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


# ---- pure helpers -----------------------------------------------------------

def test_trigger_first_occurrence_after_clockin():
    assert _auto_clockout_trigger(_dt("2026-10-26 08:00:00"), 0) == _dt("2026-10-27 00:00:00")      # day shift, midnight
    assert _auto_clockout_trigger(_dt("2026-10-26 08:00:00"), 18 * 60) == _dt("2026-10-26 18:00:00")  # same evening
    assert _auto_clockout_trigger(_dt("2026-10-26 18:00:00"), 18 * 60) == _dt("2026-10-27 18:00:00")  # exactly at T -> next day
    assert _auto_clockout_trigger(_dt("2026-10-26 19:00:00"), 18 * 60) == _dt("2026-10-27 18:00:00")  # already past -> next day


def test_parse_time_of_day():
    assert _parse_time_of_day("00:00") == 0
    assert _parse_time_of_day("17:30") == 1050
    for off in ("", "off", "none", "disable", None):
        assert _parse_time_of_day(off) is None
    for bad in ("08:07", "8:07"):          # not on a quarter hour
        with pytest.raises(ValueError):
            _parse_time_of_day(bad)
    for junk in ("25:00", "abc", "17"):
        with pytest.raises(ValueError):
            _parse_time_of_day(junk)


def test_fmt_minutes():
    assert _fmt_minutes(0) == "00:00"
    assert _fmt_minutes(1050) == "17:30"
    assert _fmt_minutes(None) == "off"


# ---- the due-sweep (_run_auto_clockouts) -----------------------------------

async def _emp(db, eid, minutes):
    await db.execute(
        f"INSERT INTO employee (id,name,employeeTypeID,autoClockOutMinutes,{EMP}) "
        "VALUES (?,?,2,?,'5','1','T','TX','0')", (eid, f"Emp{eid}", minutes))


async def _open_punch(db, pid, eid, pin, legacy=0):
    await db.execute(
        "INSERT INTO punch_clock (id,employeeID,punchInTime,punchInApproval,legacy) VALUES (?,?,?,1,?)",
        (pid, eid, pin, legacy))


@asynctest
async def test_run_clocks_out_only_the_due_nonlegacy_configured():
    async with db_ctx(with_customer=False) as db:
        await _emp(db, 11, 0)       # midnight, open since long ago -> DUE
        await _emp(db, 12, None)    # disabled -> excluded by the query
        await _emp(db, 13, 0)       # midnight, but clocked in in the far future -> NOT due yet
        await _emp(db, 14, 0)       # legacy punch -> excluded
        await _open_punch(db, 100, 11, "2020-01-01 08:00:00")
        await _open_punch(db, 101, 12, "2020-01-01 08:00:00")
        await _open_punch(db, 102, 13, "2099-01-01 08:00:00")
        await _open_punch(db, 103, 14, "2020-01-01 08:00:00", legacy=1)

        calls = []

        async def _perform(punch_id, employee_id, end_str):
            calls.append((punch_id, employee_id, end_str))

        fake = types.SimpleNamespace(db=db, _perform_auto_clockout=_perform)
        await TimeTracking._run_auto_clockouts(fake)
        assert calls == [(100, 11, "2020-01-02 00:00:00")]   # only emp11's punch, at its trigger


# ---- the clock-out action (_perform_auto_clockout) -------------------------

class _Msg:
    def __init__(s, cid, mid): s.channel = types.SimpleNamespace(id=cid); s.id = mid; s.content = "x"
    async def edit(s, **k): s.content = k.get("content", s.content)


class _Channel:
    def __init__(s): s.sent = []
    async def send(s, content):
        s.sent.append(content); return _Msg(555, 999)


class _User:
    def __init__(s): s.dms = []
    async def send(s, content): s.dms.append(content)


def _fake_cog(db, channel, user):
    s = types.SimpleNamespace()
    s.db = db
    s.refreshed = []

    async def _refresh(eid): s.refreshed.append(eid)
    async def _obtain(cid, mid): return _Msg(cid, mid)
    s._refresh_clock = _refresh
    s.obtain_message = _obtain
    s.bot = types.SimpleNamespace(
        get_channel=lambda cid: channel,
        get_user=lambda uid: user,
        fetch_user=None,
    )
    return s


@asynctest
async def test_perform_clocks_out_pending_closes_worktime_dms_and_enqueues(monkeypatch):
    async with db_ctx() as db:
        await db.execute(
            "INSERT INTO punch_clock (id,employeeID,punchInTime,punchInApproval) "
            "VALUES (900,1,'2020-01-01 08:00:00',1)")
        await add_worktime(db, 200, 900, mins=0, ptype="Service", project=2, task=6950, customer=7)

        os.environ["TIMECARD_ADMIN_CHANNEL_ID"] = "123"

        class _FakeApprove:
            @staticmethod
            async def create(cog, punch_id, msg): return object()
        monkeypatch.setattr(cogmod, "ApprovePunch", _FakeApprove)

        ch, usr = _Channel(), _User()
        cog = _fake_cog(db, ch, usr)
        await TimeTracking._perform_auto_clockout(cog, 900, 1, "2020-01-02 00:00:00")

        punch = await db.fetchone("SELECT punchOutTime, punchOutApproval, checkMessageId FROM punch_clock WHERE id=900")
        assert punch["punchOutTime"] == "2020-01-02 00:00:00"
        assert punch["punchOutApproval"] == 0            # pending — the time is a guess
        assert punch["checkMessageId"] == 999            # linked to the approval message
        wt = await db.fetchone("SELECT timeSpent FROM work_time WHERE id=200")
        assert wt["timeSpent"] > 0                        # open worktime got closed
        ob = await outbox(db)
        assert ("punch", 900, "out") in ob and ("worktime", 200, "create") in ob
        assert len(ch.sent) == 1                          # approval message posted
        assert len(usr.dms) == 1 and "forgot to clock out" in usr.dms[0].lower()   # employee DM'd
        assert cog.refreshed == [1]                       # clock re-rendered
