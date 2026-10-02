"""build_timecard_embed: per-punch durations, weekly hour header, Odoo deep-links."""
import types
from datetime import datetime

from cogs.timetracking.views import build_timecard_embed
from cogs.timetracking.odoo.client import OdooClient
from _helpers import asynctest, db_ctx, add_punch, add_worktime

WEEK_END = datetime(2026, 9, 12)          # window 09-06 .. 09-13
IN, OUT = "2026-09-11 08:00:00", "2026-09-11 16:30:00"   # 8.5h gross -> 8.0h net (0.5 lunch)


def _cog(db, *, loaded=True):
    url = "https://x.odoo.com/json/2" if loaded else None
    return types.SimpleNamespace(db=db, client=OdooClient(url, "db", "user", "key"))


async def _seed(db):
    await add_punch(db, 100, odoo=5000, pin=IN, pout=OUT)
    # Service worktime -> Field Service task link; customer 7 (odooId 555) -> contact link
    await add_worktime(db, 200, 100, odoo=7001, mins=240, ptype="Service", project=2, task=6950, customer=7)
    # Construction worktime -> project-timesheets link; customer 7 too
    await add_worktime(db, 201, 100, odoo=7002, mins=270, ptype="Construction", project=29, task=None, customer=7)


@asynctest
async def test_durations_header_and_links():
    async with db_ctx() as db:
        await _seed(db)
        embed = await build_timecard_embed(_cog(db), 1, "Tester", WEEK_END)
        d = embed.description
        assert "(8.5h)" in d, d                                  # per-punch shift duration
        assert "Σ 8.5h clocked · 8h after lunch" in d, d         # weekly totals header
        assert "](https://x.odoo.com/odoo/field-service/6950)" in d, d   # Service task link
        assert "](https://x.odoo.com/odoo/action-578/29/project-timesheets)" in d, d  # project link
        assert "](https://x.odoo.com/odoo/contacts/555)" in d, d         # customer contact link


@asynctest
async def test_plain_text_when_odoo_offline():
    async with db_ctx() as db:
        await _seed(db)
        embed = await build_timecard_embed(_cog(db, loaded=False), 1, "Tester", WEEK_END)
        d = embed.description
        assert "(8.5h)" in d                 # durations still render (no Odoo needed)
        assert "Σ 8.5h clocked" in d         # header still renders
        assert "](" not in d                 # but no hyperlinks when Odoo is off


@asynctest
async def test_office_worktime_has_no_customer_link():
    async with db_ctx() as db:
        await add_punch(db, 100, odoo=5000, pin=IN, pout=OUT)
        # Office: no customer (customerID 0) -> project link but no contact link
        await add_worktime(db, 200, 100, odoo=7003, mins=120, ptype="Office", project=3, task=None, customer=0)
        embed = await build_timecard_embed(_cog(db), 1, "Tester", WEEK_END)
        d = embed.description
        assert "/odoo/action-578/3/project-timesheets" in d
        assert "/odoo/contacts/" not in d    # Office has no customer, so no contact link
