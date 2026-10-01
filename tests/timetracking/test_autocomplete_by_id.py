"""worktime/punch autocomplete can find an id older than the recent window.

Regression for: a /viewtimecard id (e.g. an abandoned 9/11 worktime) wouldn't appear
in /editworktime because the picker only loaded the newest 300 rows.
"""
import types

from cogs.timetracking.cog import TimeTracking
from _helpers import asynctest, db_ctx, add_punch, add_worktime

WT_AC = TimeTracking.worktime_autocomplete
PUNCH_AC = TimeTracking.punch_autocomplete


def _ctx(value):
    return types.SimpleNamespace(value=value, options={})


def _self(db):
    s = types.SimpleNamespace()
    s.db = db
    return s


async def _bury_old_worktime(db):
    """One old worktime (#7) plus 300 newer ones, so #7 is outside the newest-300."""
    await add_punch(db, 7, odoo=5000)
    await add_worktime(db, 7, 7, mins=0)                      # the abandoned old entry
    for i in range(1000, 1300):                               # 300 newer, none start with "7"
        await add_punch(db, i, odoo=None)
        await add_worktime(db, i, i, mins=60)


@asynctest
async def test_worktime_by_id_found_outside_recent_window():
    async with db_ctx() as db:
        await _bury_old_worktime(db)
        vals = [c.value for c in await WT_AC(_self(db), _ctx("7"))]
        assert vals == ["7"], vals  # only id 7 starts with "7"


@asynctest
async def test_worktime_name_search_still_works():
    async with db_ctx() as db:
        await add_punch(db, 7, odoo=5000)
        await add_worktime(db, 7, 7, mins=60)
        vals = [c.value for c in await WT_AC(_self(db), _ctx("tester"))]
        assert "7" in vals, vals


@asynctest
async def test_punch_by_id_found_outside_recent_window():
    async with db_ctx() as db:
        await add_punch(db, 7, odoo=5000)                    # old punch
        for i in range(1000, 1300):
            await add_punch(db, i, odoo=None)
        vals = [c.value for c in await PUNCH_AC(_self(db), _ctx("7"))]
        assert vals == ["7"], vals
