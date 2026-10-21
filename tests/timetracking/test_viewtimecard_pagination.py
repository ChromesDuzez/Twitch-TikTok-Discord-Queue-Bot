"""In-week pagination for /viewtimecard: a busy week spans pages, never cut mid-link."""
import types
from datetime import datetime

from cogs.timetracking.views import build_timecard_embed, _paginate_lines, TimecardWeekView
from cogs.timetracking.odoo.client import OdooClient
from _helpers import asynctest, db_ctx, add_punch, add_worktime

WEEK_END = datetime(2026, 9, 12)
IN, OUT = "2026-09-11 08:00:00", "2026-09-11 16:30:00"


def _cog(db):
    return types.SimpleNamespace(db=db, client=OdooClient("https://x.odoo.com/json/2", "db", "u", "k"))


async def _seed_busy(db, n=45):
    """One punch with many hyperlinked worktimes — enough to overflow one embed."""
    await add_punch(db, 100, odoo=5000, pin=IN, pout=OUT)
    ids = list(range(200, 200 + n))
    for i in ids:
        await add_worktime(db, i, 100, odoo=7000 + i, mins=15, ptype="Service",
                           project=2, task=6900 + i, customer=7)
    return ids


# ---- the pure paginator ----------------------------------------------------

def test_paginate_never_splits_a_line_and_loses_nothing():
    lines = [f"line-{i}-" + "x" * 100 for i in range(100)]
    pages = _paginate_lines(lines, "HEADER")
    assert len(pages) > 1
    rejoined = []
    for p in pages:
        rejoined.extend(p.split("\n"))
    assert rejoined == lines                       # whole lines, in order, nothing dropped


def test_paginate_single_page_when_small():
    assert _paginate_lines(["a", "b", "c"], "H") == ["a\nb\nc"]
    assert _paginate_lines([], "H") == [""]


# ---- end-to-end through the embed builder ----------------------------------

@asynctest
async def test_busy_week_paginates_keeps_all_entries_under_limit():
    async with db_ctx() as db:
        ids = await _seed_busy(db, 45)
        _, total = await build_timecard_embed(_cog(db), 1, "Tester", WEEK_END, page=0)
        assert total > 1, total
        seen = ""
        for pg in range(total):
            embed, t = await build_timecard_embed(_cog(db), 1, "Tester", WEEK_END, page=pg)
            assert t == total
            assert len(embed.description) <= 4096          # Discord's hard cap
            assert "Σ" in embed.description                 # weekly header on every page
            # no line was cut mid-link: any line with a link closes it
            for line in embed.description.split("\n"):
                if "](" in line:
                    assert line.rstrip().endswith(")"), line
            seen += embed.description
        for i in ids:
            assert f"#{i} " in seen, f"worktime #{i} fell off every page"


@asynctest
async def test_page_out_of_range_clamps():
    async with db_ctx() as db:
        await _seed_busy(db, 45)
        _, total = await build_timecard_embed(_cog(db), 1, "Tester", WEEK_END, page=0)
        embed, _t = await build_timecard_embed(_cog(db), 1, "Tester", WEEK_END, page=999)
        assert f"page {total}/{total}" in embed.title       # clamped to the last page


# ---- the view wiring (page buttons) ----------------------------------------

@asynctest
async def test_view_tracks_pages_and_button_states():
    async with db_ctx() as db:
        await _seed_busy(db, 45)
        view = TimecardWeekView(_cog(db), 1, "Tester", WEEK_END, 999)
        await view.render_embed()
        assert view.total_pages > 1
        assert view._page_btns["pageprev"].disabled is True     # on first page
        assert view._page_btns["pagenext"].disabled is False
        view.page = view.total_pages - 1
        await view.render_embed()
        assert view._page_btns["pageprev"].disabled is False    # on last page
        assert view._page_btns["pagenext"].disabled is True
        view.stop()


@asynctest
async def test_single_page_week_disables_both_page_buttons():
    async with db_ctx() as db:
        await add_punch(db, 100, odoo=5000, pin=IN, pout=OUT)
        await add_worktime(db, 200, 100, odoo=7001, mins=60, ptype="Service", project=2, task=6950, customer=7)
        view = TimecardWeekView(_cog(db), 1, "Tester", WEEK_END, 999)
        await view.render_embed()
        assert view.total_pages == 1
        assert view._page_btns["pageprev"].disabled and view._page_btns["pagenext"].disabled
        view.stop()
